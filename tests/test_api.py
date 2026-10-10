import asyncio
from dataclasses import replace
import os
import threading
import unittest

from fastapi.testclient import TestClient
import httpx

from hub_service.app import create_app
from hub_service.errors import DeviceError
from hub_service.protocol import Frame, decode_result
from hub_service.settings import Settings


DATA = {
    0x20: (-1234).to_bytes(4, "big", signed=True) + bytes.fromhex("03 00 7B 00"),
    0x21: (100).to_bytes(4, "big") + bytes.fromhex("03 00 00 00"),
    0x22: b"\x01" + bytes(7),
    0x23: (100653).to_bytes(4, "big") + bytes.fromhex("01 00 7B 00"),
    0x24: (5550).to_bytes(4, "big") + bytes.fromhex("03 00 00 00"),
    0x25: bytes.fromhex("02 00 09 C6 03 00 00 00"),
    0x30: b"\xe6" + bytes(7),
    0x31: (70000).to_bytes(4, "big") + bytes(4),
    0x43: b"\x00" + bytes(7),
    0x52: b"\x00" + bytes(7),
}


class FakeTransport:
    def __init__(self, settings):
        self.calls = []
        self.error = None
        self.closed = False
        self.gate = threading.Event(); self.gate.set()

    def quiet(self):
        pass

    def close(self):
        self.closed = True

    def exchange(self, command, idx):
        self.calls.append((command.node, command.code, idx))
        if not self.gate.wait(2):
            raise OSError("test gate")
        if self.error:
            error, self.error = self.error, None
            raise error
        probe = command.layout == "probe"
        frame = Frame(1, command.node, command.code, idx, 1,
                      bytes(8) if probe else DATA[command.code], kind=2, status=1 if probe else 0)
        return decode_result(command, frame)


class ApiTests(unittest.TestCase):
    def test_read_endpoints_and_units(self):
        app = create_app(Settings("test"), FakeTransport)
        expected = (("temperature/0", "value", -12.34), ("pressure/0", "value", 100653),
                    ("voc/1", "value", 100), ("humidity/2", "value", 55.5),
                    ("dust/0", "voltage_mv", 2502), ("doors/1/state", "closed", True),
                    ("fans/0/pwm", "value", 230), ("fans/5/rpm", "value", 70000),
                    ("locks/0/state", "lock_output_enabled", False), ("locks/1/state", "lock_output_enabled", False),
                    ("vacuum/state", "enabled", False))
        with TestClient(app) as client:
            self.assertIsNone(client.get("/health").json()["last_response_at"])
            for path, key, value in expected:
                response = client.get("/api/v1/" + path)
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json()[key], value)
                self.assertIn("received_at", response.json())
            self.assertFalse(client.get("/api/v1/pressure/0").json()["fresh"])
            self.assertNotIn("idx", client.get("/api/v1/vacuum/state").json())
            self.assertTrue(client.get("/api/v1/hub/probe").json()["responding"])
            self.assertEqual(client.get("/api/v1/capabilities").json()["channels"]["door"]["node"], 15)
            self.assertIn((15, 0x22, 1), app.state.service.transport.calls)
            self.assertIn((16, 0x43, 0), app.state.service.transport.calls)
            self.assertIn((16, 0x43, 1), app.state.service.transport.calls)
            self.assertIn((16, 0x52, 0), app.state.service.transport.calls)
            self.assertEqual(client.get("/docs").status_code, 200)
        self.assertTrue(app.state.service.transport.closed)

    def test_invalid_indices_never_reach_port_and_no_write_methods(self):
        app = create_app(Settings("test"), FakeTransport)
        with TestClient(app) as client:
            for path in ("temperature/-1", "pressure/3", "humidity/3", "voc/2", "dust/2",
                         "doors/2/state", "locks/2/state", "fans/6/pwm", "fans/0.1/rpm", "pressure/x"):
                self.assertEqual(client.get("/api/v1/" + path).status_code, 422, path)
            self.assertEqual(app.state.service.transport.calls, [])
            self.assertEqual(client.post("/api/v1/fans/0/pwm", json={"pwm": 100}).status_code, 405)
            self.assertEqual(client.post("/api/v1/mode", json={"mode": 1}).status_code, 404)
            self.assertEqual(client.get("/api/v1/relay-fan/level").status_code, 404)
            for path, operations in client.get("/openapi.json").json()["paths"].items():
                self.assertEqual(set(operations), {"get"}, path)

    def test_device_error_is_structured_http_504(self):
        app = create_app(Settings("test"), FakeTransport)
        with TestClient(app) as client:
            app.state.service.transport.error = DeviceError("DEVICE_TIMEOUT", "test", 504, device_status=7)
            result = client.get("/api/v1/pressure/0")
            self.assertEqual(result.status_code, 504)
            self.assertEqual(result.json()["error"]["code"], "DEVICE_TIMEOUT")
            self.assertEqual(result.json()["error"]["device_status"], 7)
            self.assertEqual(len(app.state.service.transport.calls), 1)

    def test_optional_token(self):
        app = create_app(Settings("test", api_token="test-token"), FakeTransport)
        with TestClient(app) as client:
            for headers in ({}, {"Authorization": "Bearer wrong"}):
                self.assertEqual(client.get("/api/v1/pressure/0", headers=headers).status_code, 401)
            self.assertEqual(app.state.service.transport.calls, [])
            self.assertEqual(client.get("/api/v1/pressure/0", headers={"Authorization": "Bearer test-token"}).status_code, 200)

    def test_open_failure_stops_lifespan(self):
        def factory(settings):
            raise OSError("busy port")
        with self.assertRaisesRegex(OSError, "busy port"), TestClient(create_app(Settings("test"), factory)):
            self.fail("startup should fail")


class AsyncApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_waiting_for_hardware_does_not_block_health(self):
        app = create_app(Settings("test"), FakeTransport)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                port = app.state.service.transport
                port.gate.clear()
                reading = asyncio.create_task(client.get("/api/v1/pressure/0"))
                try:
                    for _ in range(100):
                        if port.calls:
                            break
                        await asyncio.sleep(.005)
                    response = await asyncio.wait_for(client.get("/health"), .3)
                    self.assertTrue(response.json()["active"])
                    self.assertFalse(reading.done())
                finally:
                    port.gate.set()
                    self.assertEqual((await reading).status_code, 200)


if __name__ == "__main__":
    unittest.main()

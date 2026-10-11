import importlib.util
import os
from pathlib import Path
from dataclasses import replace
import select
import subprocess
import threading
import time
import unittest

from fastapi.testclient import TestClient
from hub_service.app import create_app
from hub_service.protocol import Frame, decode_result, envelope
from hub_service.settings import Settings

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("chem_console", ROOT / "scripts/api_console.py")
console = importlib.util.module_from_spec(spec)
spec.loader.exec_module(console)


class ChainPort:
    """Standalone wire mock, or opt-in production C++ bridge from tjc-sketch."""
    binary = os.environ.get("CHEM_FIRMWARE_CHAIN")

    def __init__(self, settings):
        self.process = (subprocess.Popen([str(self.binary), "--bridge"], stdin=subprocess.PIPE,
                                        stdout=subprocess.PIPE, text=True) if self.binary else None)
        self.requests = []
        self.pwm = [0, 0]
        self.locked, self.level = False, 0

    def quiet(self):
        pass

    def packet(self, packet):
        if self.process is None:
            request = Frame.decode(packet)
            cmd, idx = request.command, request.idx
            data, status = bytes(8), 0
            if cmd == 0x20 and idx == 2:
                status = 1  # Expected local hub probe.
            elif cmd in (0x20, 0x23, 0x24, 0x26):
                value = {0x20: -1234 + idx, 0x23: 100653 + idx,
                         0x24: 5550 + idx, 0x26: (512 << 16) | 2502}[cmd]
                data = (value & 0xffffffff).to_bytes(4, "big") + bytes.fromhex("03 00 7b 00")
            elif cmd == 0x22:
                data = bytes([idx == 0]) + bytes(7)
            elif cmd == 0x31:
                data = (1200 + idx).to_bytes(4, "big") + bytes(4)
            elif cmd in (0x30, 0x32):
                if cmd == 0x32:
                    self.pwm[idx] = request.data[0]
                data = bytes([self.pwm[idx]]) + bytes(7)
            elif cmd in (0x42, 0x43):
                if cmd == 0x42:
                    self.locked = bool(request.data[0])
                else:
                    data = bytes([self.locked]) + bytes(7)
            elif cmd in (0x50, 0x51):
                if cmd == 0x51:
                    self.level = request.data[0]
                data = bytes([self.level]) + bytes(7)
            else:
                status = 3
            return Frame.decode(replace(request, kind=2, status=status, data=data).encode())
        self.process.stdin.write(packet.hex() + "\n")
        self.process.stdin.flush()
        result = self.process.stdout.readline()
        if not result:
            raise OSError("C++ firmware bridge stopped")
        return Frame.decode(bytes.fromhex(result))

    def exchange(self, command, idx, data=bytes(8)):
        request = Frame(len(self.requests) + 1, command.node, command.code, idx, 123, data=data)
        self.requests.append(request)
        reply = self.packet(request.encode())
        assert reply.matches(request)
        return decode_result(command, reply)

    def close(self):
        if self.process is None:
            return
        if self.process.stdin:
            self.process.stdin.close()
        self.process.wait(timeout=2)
        self.process.stdout.close()


class ApiTests(unittest.TestCase):
    def test_all_gets_through_wire_protocol(self):
        with TestClient(create_app(Settings("fake"), ChainPort)) as client:
            paths = console.selftest_paths(client.get("/openapi.json").json())
            self.assertEqual(len(paths), 19)
            for path in paths:
                reply = client.get(path)
                self.assertEqual(reply.status_code, 200, (path, reply.text))
            self.assertEqual(client.get("/api/v1/temperature/0").json()["value"], -12.34)
            self.assertEqual(client.get("/api/v1/pressure/1").json()["unit"], "Pa")
            self.assertEqual(client.get("/api/v1/humidity/0").json()["value"], 55.5)
            gas = client.get("/api/v1/gas/0").json()
            self.assertEqual((gas["adc"], gas["voltage_mv"], gas["voltage_v"]), (512, 2502, 2.502))
            self.assertEqual(gas["age_ms"], 123)
            self.assertEqual(client.get("/api/v1/capabilities").json()["greenhouse"], "chemreactor")

    def test_writes_direct_pwm_common_lock_and_all_relay_levels(self):
        with TestClient(create_app(Settings("fake"), ChainPort)) as client:
            for idx in range(2):
                r = client.post(f"/api/v1/fans/{idx}/pwm", json={"pwm": 100})
                self.assertEqual(r.status_code, 200, r.text)
                self.assertEqual(r.json()["value"], 100)
            for state in (True, False):
                r = client.post("/api/v1/admin/lock", json={"lock_state": state})
                self.assertTrue(r.json()["acknowledged"])
                self.assertEqual(client.get("/api/v1/lock/state").json()["lock_output_enabled"], state)
            for level in range(11):
                r = client.post("/api/v1/relay-fan/level", json={"level": level})
                self.assertEqual(r.status_code, 200, r.text)
                self.assertEqual(r.json()["level"], level)
                self.assertEqual(client.get("/api/v1/relay-fan/level").json()["level"], level)
            self.assertEqual(client.get("/api/v1/fans/1/pwm").json()["value"], 100)

    def test_bad_arguments_and_fablab_routes_never_reach_hardware(self):
        app = create_app(Settings("fake"), ChainPort)
        with TestClient(app) as client:
            paths = console.selftest_paths(client.get("/openapi.json").json())
            for path in paths:
                if "/0" in path:
                    self.assertEqual(client.get(path.replace("/0", "/2")).status_code, 422)
            cases = [("/api/v1/fans/0/pwm", {"pwm": v}) for v in (True, "100", 100.5, -1, 256)]
            cases += [("/api/v1/relay-fan/level", {"level": v}) for v in (True, "1", -1, 11)]
            cases += [("/api/v1/admin/lock", {"lock_state": v}) for v in (0, 1, "true")]
            cases += [("/api/v1/fans/0/pwm", {"pwm": 100, "extra": 1})]
            for path, body in cases:
                self.assertEqual(client.post(path, json=body).status_code, 422, (path, body))
            for path in ("/api/v1/voc/0", "/api/v1/dust/0", "/api/v1/vacuum/state", "/api/v1/locks/0/state", "/api/v1/admin/mode"):
                self.assertEqual(client.get(path).status_code, 404)
            self.assertEqual(client.post("/api/v1/admin/mode", json={"mode": 2, "idx": 0}).status_code, 404)
            self.assertEqual(app.state.service.transport.requests, [])
            self.assertNotIn("securitySchemes", client.get("/openapi.json").json()["components"])

    @unittest.skipUnless(os.name == "posix", "PTY requires POSIX")
    def test_http_pyserial_simplebus_chain_and_back(self):
        master, slave = os.openpty()
        port = ChainPort(Settings("fake"))
        stop = threading.Event()
        failures = []

        def device():
            try:
                window, frame = bytearray(), bytearray()
                while not stop.is_set():
                    if not select.select([master], [], [], .05)[0]:
                        continue
                    for byte in os.read(master, 1024):
                        window.append(byte)
                        if len(window) < 5:
                            continue
                        if window[0] != 13 or window[4] != 10:
                            del window[0]
                            continue
                        self.assertEqual(window[1:3], b"0C")
                        frame.append(window[3]); window.clear()
                        if len(frame) == 28:
                            reply = port.packet(bytes(frame)); frame.clear()
                            wire = b"".join(envelope(b, 99) for b in reply.encode())
                            os.write(master, wire[:7]); time.sleep(.005); os.write(master, wire[7:])
            except BaseException as error:
                failures.append(error)

        thread = threading.Thread(target=device, daemon=True)
        thread.start()
        try:
            with TestClient(create_app(Settings(os.ttyname(slave)))) as client:
                for path in console.selftest_paths(client.get("/openapi.json").json()):
                    reply = client.get(path)
                    self.assertEqual(reply.status_code, 200, (path, reply.text))
                for path, body in (("/api/v1/fans/1/pwm", {"pwm": 100}),
                                   ("/api/v1/admin/lock", {"lock_state": True}),
                                   ("/api/v1/relay-fan/level", {"level": 10})):
                    reply = client.post(path, json=body)
                    self.assertEqual(reply.status_code, 200, reply.text)
        finally:
            stop.set(); thread.join(2); port.close(); os.close(master); os.close(slave)
        self.assertFalse(failures, failures)


class ConsoleTests(unittest.TestCase):
    def test_commands_and_no_indices_for_lock_and_relay(self):
        class FakeClient:
            calls = []
            def get(self, path):
                self.calls.append((path, None))
                return True, 200, {}, 0
            def request(self, path, body):
                self.calls.append((path, body))
                return True, 200, {}, 0
        client = FakeClient()
        for command in ("gas 1", "lock", "relay", "set_door_lock 1", "set_relay_level 10", "set_pwm 1 100"):
            self.assertTrue(console.execute(client, command))
        self.assertIn(("/api/v1/admin/lock", {"lock_state": True}), client.calls)
        self.assertIn(("/api/v1/relay-fan/level", {"level": 10}), client.calls)
        count = len(client.calls)
        for command in ("lock 0", "set_door_lock 0 1", "set_pwm 2 100", "set_relay_level 11", "vacuum", "voc 0", "get_mode", "set_mode 2 0"):
            with self.assertRaises(ValueError):
                console.execute(client, command)
        self.assertEqual(len(client.calls), count)


if __name__ == "__main__":
    unittest.main()

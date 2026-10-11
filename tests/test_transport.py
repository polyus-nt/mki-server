from dataclasses import replace
import os
import select
import threading
import time
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from hub_service.app import create_app
from hub_service.errors import HubError
from hub_service.protocol import COMMANDS, Frame, envelope
from hub_service.settings import Settings
from hub_service.transport import SerialTransport


class SerialFake:
    def __init__(self, **kwargs):
        self.options = kwargs
        self.wire = bytearray()
        self.requests = []
        self.closed = False

    def write(self, data):
        # Проверяем работу при частичной записи.
        self.wire.extend(data[:2])
        return min(2, len(data))

    @property
    def out_waiting(self):
        return 0

    def read(self, size):
        request = Frame.decode(bytes(self.wire[3::5])); self.requests.append(request)
        self.wire.clear()
        data = (100653).to_bytes(4, "big") + bytes.fromhex("03 00 00 00")
        response = replace(request, kind=2, data=data)
        stale = replace(response, session_id=request.session_id ^ 1)
        invalid = replace(response, data=bytes(8))
        bad = bytearray(response.encode()); bad[-1] ^= 1
        return b"".join(envelope(b, 99) for b in stale.encode() + invalid.encode() + bytes(bad) + response.encode())

    def close(self):
        self.closed = True


class TransportTests(unittest.TestCase):
    def test_serial_config_pacing_partial_writes_and_session_rollover(self):
        with patch("hub_service.transport.serial.Serial", SerialFake):
            port = SerialTransport(Settings("COM15"))
        self.assertEqual(port.port.options["baudrate"], 9600)
        self.assertFalse(port.port.options["rtscts"])
        self.assertFalse(port.port.options["xonxoff"])
        port.quiet = unittest.mock.Mock()
        port.tx_id, port.session = 65535, (1 << 64) - 1
        first = port.exchange(COMMANDS["pressure"], 0)
        second = port.exchange(COMMANDS["pressure"], 1)
        self.assertEqual(first["value"], 100653)
        self.assertEqual(second["idx"], 1)
        self.assertEqual([(r.tx_id, r.session_id) for r in port.port.requests], [(65535, (1 << 64)-1), (1, 1)])
        self.assertEqual(port.quiet.call_count, 2)
        port.close(); self.assertTrue(port.port.closed)

    def test_quiet_has_bounded_noise_recovery(self):
        with patch("hub_service.transport.serial.Serial", SerialFake):
            port = SerialTransport(Settings("test"))
        now = [0.0]
        def noisy(timeout):
            now[0] += .1
            return b"noise"
        port.read = noisy
        with patch("hub_service.transport.time.monotonic", side_effect=lambda: now[0]):
            with self.assertRaisesRegex(OSError, "3 с"):
                port.quiet()

    def test_quiet_can_finish_between_autonomous_polls(self):
        with patch("hub_service.transport.serial.Serial", SerialFake):
            port = SerialTransport(Settings("test"))
        now = [0.0]
        def polling(timeout):
            now[0] += timeout
            return b"background traffic" if now[0] <= .4 else b""
        port.read = polling
        with patch("hub_service.transport.time.monotonic", side_effect=lambda: now[0]):
            port.quiet()
        # Последний байт ~0.4, тишина 1 с закончилась до следующего опроса.
        self.assertGreaterEqual(now[0], 1.35)
        self.assertLess(now[0], 1.9)

    def test_raw_silence_is_host_timeout(self):
        with patch("hub_service.transport.serial.Serial", SerialFake):
            port = SerialTransport(Settings("test", exchange_timeout=.01))
        port.quiet = lambda: None
        port.send = lambda packet, deadline: None
        port.read = lambda timeout: b""
        with self.assertRaises(HubError) as error:
            port.exchange(COMMANDS["pressure"], 0)
        self.assertEqual(error.exception.code, "HOST_TIMEOUT")
        self.assertEqual(error.exception.details["rx_bytes"], 0)

    @unittest.skipUnless(os.name == "posix", "PTY requires POSIX")
    def test_http_to_real_pyserial_pty_and_back(self):
        master, slave = os.openpty()
        stop = threading.Event()
        errors, requests = [], []

        def hardware():
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
                            del window[0]; continue
                        self.assertEqual(window[1:3], b"0C")
                        frame.append(window[3]); window.clear()
                        if len(frame) != 28:
                            continue
                        request = Frame.decode(bytes(frame)); frame.clear(); requests.append(request)
                        if request.command == 0x20 and request.idx == 3:
                            response = replace(request, kind=2, status=1)
                        elif request.command == 0x22:
                            self.assertEqual(request.node, 15)
                            response = replace(request, kind=2, data=b"\x01" + bytes(7))
                        elif request.command in (0x30, 0x31, 0x32, 0x40, 0x41):
                            self.assertEqual(request.node, 14)  # node17 не виден компьютеру.
                            if request.command == 0x40:
                                self.assertEqual(request.idx, 255)
                                data = b"\x00\xff" + bytes(6)
                            elif request.command == 0x41:
                                self.assertEqual(request.data, b"\x02" + bytes(7))
                                data = bytes([request.data[0], request.idx]) + bytes(6)
                            elif request.command == 0x31:
                                data = (1200 + request.idx).to_bytes(4, "big") + bytes(4)
                            elif request.command == 0x32:
                                self.assertEqual(request.idx, 5)
                                self.assertEqual(request.data, b"\x64" + bytes(7))
                                data = request.data
                            else:
                                data = bytes([100 + request.idx]) + bytes(7)
                            response = replace(request, kind=2, data=data)
                        elif request.command in (0x42, 0x53):
                            self.assertEqual(request.node, 16)
                            self.assertEqual(request.data, b"\x01" + bytes(7))
                            response = replace(request, kind=2, data=bytes(8))
                        else:
                            response = replace(request, kind=2, data=(100653).to_bytes(4, "big") + bytes.fromhex("03 00 00 00"))
                        wire = b"".join(envelope(b, 99) for b in response.encode())
                        # Разбиваем пакет между чтениями, как USB может сделать сам.
                        os.write(master, wire[:7]); time.sleep(.005); os.write(master, wire[7:])
            except BaseException as error:
                errors.append(error)

        thread = threading.Thread(target=hardware, daemon=True); thread.start()
        try:
            with TestClient(create_app(Settings(os.ttyname(slave)))) as client:
                self.assertTrue(client.get("/api/v1/hub/probe").json()["responding"])
                response = client.get("/api/v1/pressure/0")
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json()["value"], 100653)
                self.assertTrue(client.get("/api/v1/doors/1/state").json()["closed"])
                for idx in range(6):
                    for command, value in (("pwm", 100 + idx), ("rpm", 1200 + idx)):
                        result = client.get(f"/api/v1/fans/{idx}/{command}")
                        self.assertEqual(result.status_code, 200, result.text)
                        self.assertEqual(result.json()["idx"], idx)
                        self.assertEqual(result.json()["value"], value)
                result = client.post("/api/v1/fans/5/pwm", json={"pwm": 100})
                self.assertEqual(result.status_code, 200, result.text)
                self.assertEqual(result.json()["value"], 100)
                self.assertIsNone(client.get("/api/v1/admin/mode").json()["idx"])
                result = client.post("/api/v1/admin/mode", json={"mode": 2, "idx": 4})
                self.assertEqual(result.status_code, 200, result.text)
                self.assertEqual(result.json()["idx"], 4)
                for path, body in (("vacuum", {"mode": 1}), ("locks/1", {"lock_state": True})):
                    result = client.post("/api/v1/admin/" + path, json=body)
                    self.assertEqual(result.status_code, 200, result.text)
                    self.assertTrue(result.json()["acknowledged"])
            self.assertEqual(len(requests), 20)
            self.assertEqual(len({r.session_id for r in requests}), 1)
            self.assertEqual([r.tx_id for r in requests], list(range(1, 21)))
        finally:
            stop.set(); thread.join(1); os.close(master); os.close(slave)
        self.assertFalse(errors, errors)


if __name__ == "__main__":
    unittest.main()

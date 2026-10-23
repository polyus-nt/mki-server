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
        self.incoming = bytearray()
        self.requests, self.claims = [], []
        self.closed = False
        self.counter = 0
        self.resets = 0
        self.next_ready = time.monotonic() + .02

    def write(self, data):
        written = min(2,len(data))
        self.wire.extend(data[:written])
        if len(self.wire) == 140:
            frame = Frame.decode(bytes(self.wire[3::5]))
            self.wire.clear()
            if frame.command == 0xf1:
                self.claims.append(frame)
                self.incoming.extend(self.encode(replace(frame, kind=2, command=0xf2,
                                        session_id=frame.session_id ^ int.from_bytes(frame.data,"big"), data=b"\x00\x64" + bytes(6))))
            else:
                self.requests.append(frame)
                payload = (100653).to_bytes(4, "big") + bytes.fromhex("03 00 00 00")
                response = replace(frame, kind=2, data=payload)
                stale = replace(response, session_id=frame.session_id ^ 1)
                invalid = replace(response, data=bytes(8))
                bad = bytearray(response.encode()); bad[-1] ^= 1
                self.incoming.extend(self.encode(stale) + self.encode(invalid) +
                    b"".join(envelope(b,99) for b in bad) + self.encode(response))
        return written

    @staticmethod
    def encode(frame):
        return b"".join(envelope(b,99) for b in frame.encode())

    @property
    def in_waiting(self):
        return len(self.incoming)

    def reset_input_buffer(self):
        self.incoming.clear(); self.resets += 1

    def read(self, size):
        if not size:
            return b""
        if not self.incoming:
            if time.monotonic() < self.next_ready:
                time.sleep(.001)
                return b""
            self.next_ready = time.monotonic() + .02
            self.counter += 1
            frame = Frame(self.counter & 65535, 12, 0xf0, 0,
                          123 ^ (self.counter >> 16), kind=2,
                          data=b"\x00\x64" + bytes(6))
            self.incoming.extend(self.encode(frame))
        result = bytes(self.incoming[:size]); del self.incoming[:size]
        return result

    def close(self):
        self.closed = True


class TransportTests(unittest.TestCase):
    def test_fresh_grant_after_old_ready_lease_still_sends_once(self):
        with patch("hub_service.transport.serial.Serial",SerialFake):
            port=SerialTransport(Settings("test"))
        port.receiver.close()
        self.addCleanup(port.close)
        now=[1.0]; sent=[]
        ready=Frame(42,12,0xf0,0,123,kind=2,data=b"\x00\x64"+bytes(6))
        class Receiver:
            received_at=0.0
            dropped_bytes=0
            def begin(self): pass
            def end(self): pass
            def close(self): pass
            def read(self,timeout):
                if not sent: frame=ready
                elif sent[-1].command==0xf1:
                    now[0]+=.03
                    frame=replace(ready,command=0xf2,session_id=123^port.session)
                else:
                    frame=replace(sent[-1],kind=2,data=(100653).to_bytes(4,"big")+bytes.fromhex("03 00 00 00"))
                self.received_at=now[0]
                return SerialFake.encode(frame)
        port.receiver=Receiver()
        def send(frame,deadline):
            sent.append(frame)
            if frame.command==0xf1: now[0]+=.08
        port.send_frame=send
        with patch("hub_service.transport.time.monotonic",side_effect=lambda:now[0]):
            self.assertEqual(port.exchange(COMMANDS["pressure"],0)["value"],100653)
        self.assertEqual([f.command for f in sent],[0xf1,0x23])

    def test_ready_backlog_produces_one_claim_for_newest_ticket(self):
        with patch("hub_service.transport.serial.Serial", SerialFake):
            port = SerialTransport(Settings("test",exchange_timeout=.5))
        self.addCleanup(port.close)
        begin = port.receiver.begin
        def inject():
            result = begin()
            ready = Frame(0,12,0xf0,0,123,kind=2,data=b"\x00\x64"+bytes(6))
            # Как в журнале Raspberry: новые окна, затем обратный ход старых.
            frames = [replace(ready,tx_id=i) for i in list(range(100,121))+list(range(90,100))]
            port.port.incoming.extend(b"".join(port.port.encode(f) for f in frames))
            return result
        port.receiver.begin = inject
        self.assertEqual(port.exchange(COMMANDS["pressure"],0)["value"],100653)
        self.assertEqual([f.tx_id for f in port.port.claims],[120])
        self.assertEqual(len(port.port.requests),1)

    def test_recovery_after_no_grant_without_reopening_port(self):
        with patch("hub_service.transport.serial.Serial", SerialFake):
            port = SerialTransport(Settings("test",exchange_timeout=.08))
        self.addCleanup(port.close)
        original = port.send_frame
        port.send_frame = lambda frame,deadline: None
        with self.assertRaises(HubError) as error:
            port.exchange(COMMANDS["pwm"],0)
        self.assertFalse(error.exception.details["command_sent"])
        port.send_frame = original
        self.assertEqual(port.exchange(COMMANDS["pressure"],0)["value"],100653)
        self.assertFalse(port.port.closed)

    def test_serial_config_handshake_partial_writes_and_ticket_rollover(self):
        with patch("hub_service.transport.serial.Serial", SerialFake):
            port = SerialTransport(Settings("COM15"))
        self.addCleanup(port.close)
        self.assertEqual(port.port.options["baudrate"], 115200)
        self.assertFalse(port.port.options["rtscts"])
        self.assertFalse(port.port.options["xonxoff"])
        port.port.counter = 65534
        port.session = 456
        start = time.monotonic()
        first = port.exchange(COMMANDS["pressure"], 0)
        second = port.exchange(COMMANDS["pressure"], 1)
        self.assertLess(time.monotonic()-start, .2)
        self.assertEqual(first["value"], 100653)
        self.assertEqual(second["idx"], 1)
        self.assertEqual([(r.tx_id, r.session_id) for r in port.port.requests],
                         [(65535, 123 ^ 456), (0, 122 ^ 456)])
        self.assertEqual(len(port.port.claims), 2)
        self.assertEqual(port.port.resets, 1)
        port.close(); self.assertTrue(port.port.closed)

    def test_buffer_reset_is_immediate(self):
        with patch("hub_service.transport.serial.Serial", SerialFake):
            port = SerialTransport(Settings("test"))
        self.addCleanup(port.close)
        resets = port.port.resets
        port.quiet()
        self.assertEqual(port.port.resets, resets) # Не flush параллельно с reader.
        self.assertFalse(port.receiver._chunks)

    def test_raw_silence_never_sends_a_command(self):
        with patch("hub_service.transport.serial.Serial", SerialFake):
            port = SerialTransport(Settings("test", exchange_timeout=.05))
        self.addCleanup(port.close)
        port.read = lambda timeout: b""
        with self.assertRaises(HubError) as error:
            port.exchange(COMMANDS["pressure"], 0)
        self.assertEqual(error.exception.code, "HOST_TIMEOUT")
        self.assertFalse(error.exception.details["command_sent"])
        self.assertFalse(port.port.requests)

    def test_missing_grant_never_sends_set(self):
        with patch("hub_service.transport.serial.Serial", SerialFake):
            port = SerialTransport(Settings("test", exchange_timeout=.05))
        self.addCleanup(port.close)
        original = port.send_frame
        def lose_claim(frame, deadline):
            if frame.command != 0xf1:
                original(frame, deadline)
        port.send_frame = lose_claim
        from hub_service.protocol import WRITE_COMMANDS
        with self.assertRaises(HubError) as error:
            port.exchange(WRITE_COMMANDS["pwm"], 0, b"\x64"+bytes(7))
        self.assertFalse(error.exception.details["command_sent"])
        self.assertFalse(port.port.requests)

    @unittest.skipUnless(os.name == "posix", "PTY requires POSIX")
    def test_lost_set_reply_never_resends_action(self):
        with patch("hub_service.transport.serial.Serial", SerialFake):
            port=SerialTransport(Settings("test",exchange_timeout=.15))
        self.addCleanup(port.close)
        write=port.port.write
        def lose_reply(data):
            count=write(data)
            if port.port.requests:
                port.port.incoming.clear()
            return count
        port.port.write=lose_reply
        from hub_service.protocol import WRITE_COMMANDS
        with self.assertRaises(HubError) as error:
            port.exchange(WRITE_COMMANDS["pwm"],0,b"\x64"+bytes(7))
        self.assertTrue(error.exception.details["command_sent"])
        self.assertEqual(len(port.port.requests),1)
        self.assertEqual(port.port.requests[0].command,0x32)

    @unittest.skipUnless(os.name == "posix", "PTY requires POSIX")
    def test_http_to_real_pyserial_pty_and_back(self):
        master, slave = os.openpty()
        import tty
        tty.setraw(slave)  # Не эхоить ранний READY до открытия pyserial.
        stop = threading.Event()
        errors, requests, claims = [], [], []

        def hardware():
            try:
                window, frame = bytearray(), bytearray()
                last_ready = 0.0; counter = 0
                while not stop.is_set():
                    if time.monotonic()-last_ready > .03:
                        counter += 1
                        ready = Frame(counter & 65535, 12, 0xf0, 0, 123 ^ (counter >> 16),
                                      kind=2, data=b"\x00\x64"+bytes(6))
                        os.write(master,b"".join(envelope(b,99) for b in ready.encode()))
                        last_ready = time.monotonic()
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
                        request = Frame.decode(bytes(frame)); frame.clear()
                        if request.command == 0xf1:
                            claims.append(request)
                            grant = replace(request,kind=2,command=0xf2,
                                            session_id=request.session_id ^ int.from_bytes(request.data,"big"), data=b"\x00\x64"+bytes(6))
                            os.write(master,b"".join(envelope(b,99) for b in grant.encode()))
                            last_ready=time.monotonic()
                            continue
                        requests.append(request)
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
                # Пока HTTP-запросов нет, RX работает, но клиент не занимает шину CLAIM.
                before = len(claims)
                time.sleep(.35)
                self.assertEqual(len(claims),before)
                self.assertFalse(client.app.state.service.transport.receiver._chunks)
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
            self.assertEqual(len({(r.tx_id,r.session_id) for r in requests}),20)
        finally:
            stop.set(); thread.join(1); os.close(master); os.close(slave)
        self.assertFalse(errors, errors)


if __name__ == "__main__":
    unittest.main()

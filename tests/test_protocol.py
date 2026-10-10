import binascii
from dataclasses import replace
import json
from pathlib import Path
import unittest

from hub_service.errors import DeviceError
from hub_service.protocol import COMMANDS, PROBE, WRITE_COMMANDS, Frame, ReplyParser, decode_result, envelope


def reply(command, data=bytes(8), status=0, idx=0):
    return Frame(1, command.node, command.code, idx, 1, data, kind=2, status=status)


class ProtocolTests(unittest.TestCase):
    def test_global_mode_and_control_acknowledgements(self):
        for mode, idx in ((0, 255), (1, 4), (2, 5)):
            for command in (COMMANDS["mode"], WRITE_COMMANDS["mode"]):
                result = decode_result(command, reply(command, bytes([mode, idx]) + bytes(6)))
                self.assertEqual(result, {"mode": mode, "idx": None if mode == 0 else idx})
        for data in (bytes([0, 0]) + bytes(6), bytes([1, 255]) + bytes(6),
                     bytes([2, 6]) + bytes(6), bytes([3, 0]) + bytes(6),
                     bytes([0, 255]) + bytes(5) + b"\x01"):
            with self.assertRaises(ValueError):
                decode_result(COMMANDS["mode"], reply(COMMANDS["mode"], data))
        for name in ("vacuum", "lock"):
            self.assertEqual(decode_result(WRITE_COMMANDS[name], reply(WRITE_COMMANDS[name])), {"acknowledged": True})
            with self.assertRaises(ValueError):
                decode_result(WRITE_COMMANDS[name], reply(WRITE_COMMANDS[name], b"\x01" + bytes(7)))

    def test_44_existing_golden_vectors(self):
        fixtures = json.loads((Path(__file__).parent / "fixtures/hardware-v2-vectors.json").read_text(encoding="utf-8"))
        self.assertEqual(binascii.crc_hqx(b"123456789", 0xffff), 0x29b1)
        self.assertEqual(len(fixtures["round_trips"]), 44)
        for vector in fixtures["round_trips"]:
            for direction in ("request", "response"):
                fields = {**vector[direction], "data": bytes(vector[direction]["data"])}
                frame = Frame(**fields)
                wire = bytes.fromhex(vector[direction + "_hex"])
                self.assertEqual(frame.encode(), wire, vector["name"])
                if frame.version == 2:
                    self.assertEqual(Frame.decode(wire), frame)
                else:
                    with self.assertRaises(ValueError):
                        Frame.decode(wire)

    def test_new_board_routing_and_scalar_layouts(self):
        self.assertEqual(COMMANDS["door"].node, 15)
        self.assertEqual(COMMANDS["lock"].node, 16)
        self.assertEqual(COMMANDS["vacuum"].code, 0x52)
        self.assertNotIn("relay_fan", COMMANDS)
        examples = (("door", b"\x01" + bytes(7), "closed", True),
                    ("lock", b"\x00" + bytes(7), "lock_output_enabled", False),
                    ("vacuum", b"\x01" + bytes(7), "enabled", True),
                    ("pwm", b"\xff" + bytes(7), "value", 255),
                    ("rpm", (70000).to_bytes(4, "big") + bytes(4), "value", 70000))
        for name, data, key, value in examples:
            self.assertEqual(decode_result(COMMANDS[name], reply(COMMANDS[name], data))[key], value)
        for name, data in (("door", b"\x02" + bytes(7)), ("vacuum", b"\x02" + bytes(7)),
                           ("pwm", bytes(7) + b"\x01")):
            with self.assertRaises(ValueError):
                decode_result(COMMANDS[name], reply(COMMANDS[name], data))

    def test_measurement_units_quality_and_errors(self):
        examples = (("temperature", (-1234).to_bytes(4, "big", signed=True), "value", -12.34),
                    ("pressure", (100653).to_bytes(4, "big"), "value", 100653),
                    ("humidity", (5550).to_bytes(4, "big"), "value", 55.5),
                    ("voc", (100).to_bytes(4, "big"), "value", 100),
                    ("dust", bytes.fromhex("02 00 09 C6"), "voltage_v", 2.502))
        for name, number, key, value in examples:
            command = COMMANDS[name]
            data = number + bytes.fromhex("03 00 7B 00")
            result = decode_result(command, reply(command, data))
            self.assertEqual(result[key], value)
            self.assertTrue(result["valid"] and result["fresh"])
            self.assertEqual(result["age_ms"], 123)
            for flags in (0, 2, 4):
                bad = bytearray(data); bad[4] = flags
                with self.assertRaises(ValueError):
                    decode_result(command, reply(command, bytes(bad)))
        for name, value in (("voc", 0), ("voc", 501), ("humidity", 10001), ("dust", 1024 << 16)):
            with self.assertRaises(ValueError):
                decode_result(COMMANDS[name], reply(COMMANDS[name], value.to_bytes(4, "big") + b"\x03\x00\x00\x00"))
        for status in range(1, 13):
            with self.assertRaises(DeviceError) as error:
                decode_result(COMMANDS["pressure"], reply(COMMANDS["pressure"], status=status))
            self.assertEqual(error.exception.details["device_status"], status)
        with self.assertRaises(ValueError):
            decode_result(COMMANDS["pressure"], reply(COMMANDS["pressure"], b"\x01" + bytes(7), 7))
        self.assertTrue(decode_result(PROBE, reply(PROBE, status=1, idx=3))["responding"])

    def test_noise_echo_crc_resync_binary_delimiters_and_bounded_memory(self):
        request = Frame(10, 13, 0x23, 0, 0xa55a0d0a12345678)
        response = replace(request, kind=2, data=bytes.fromhex("00 01 89 2D 03 00 00 00"))
        corrupt = bytearray(response.encode()); corrupt[-1] ^= 1
        wire = b"noise" + b"".join(envelope(b) for b in request.encode())
        wire += b"".join(envelope(b, 99) for b in corrupt + response.encode())
        parser, found = ReplyParser(), []
        for byte in wire:
            found.extend(parser.feed(bytes((byte,)), 10))
            self.assertLessEqual(len(parser.bus), 4)
            self.assertLessEqual(len(parser.frame), 27)
        self.assertEqual(found, [response])
        self.assertTrue(response.matches(request))
        self.assertFalse(replace(response, session_id=1).matches(request))
        parser = ReplyParser()
        list(parser.feed(envelope(0xa5, 99), 10))
        self.assertEqual(list(parser.feed(b"".join(envelope(b, 99) for b in response.encode()), 10.3)), [response])


if __name__ == "__main__":
    unittest.main()

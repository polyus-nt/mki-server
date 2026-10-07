#!/usr/bin/env python3
"""Validate the proposed contract and golden bytes, not production firmware."""

import binascii
import json
from pathlib import Path
import struct


ROOT = Path(__file__).resolve().parents[1]
SPEC = json.loads((ROOT / "docs/protocol/hardware-v1.json").read_text())
VECTORS = json.loads((ROOT / "docs/protocol/hardware-v1-vectors.json").read_text())
COMMANDS = {row["code"]: row for row in SPEC["commands"]}
STATUS_NAMES = {value: name for name, value in SPEC["status_codes"].items()}


def check(condition, message):
    if not condition:
        raise AssertionError(message)


def parse_frame(hex_text, *, check_crc=True):
    raw = bytes.fromhex(hex_text)
    check(len(raw) == 20, "frame must contain exactly 20 bytes")
    check(raw[:2] == bytes([0xA5, 0x5A]), "invalid magic")
    if check_crc:
        check(
            binascii.crc_hqx(raw[2:18], 0xFFFF) == int.from_bytes(raw[18:], "big"),
            "CRC does not match",
        )
    return {
        "version": raw[2],
        "kind": raw[3],
        "tx_id": int.from_bytes(raw[4:6], "big"),
        "node": raw[6],
        "command": raw[7],
        "status": raw[8],
        "idx": raw[9],
        "data": list(raw[10:18]),
    }


def correlation(fields):
    return tuple(fields[name] for name in ("tx_id", "node", "command", "idx"))


def request_error(request):
    if request["version"] != 1:
        return 8
    if request["status"] != 0:
        return 2
    command = COMMANDS.get(request["command"])
    if command is None:
        return 3
    if request["node"] != command["node"]:
        return 11
    if request["idx"] not in command["idx"]:
        return 1
    data = request["data"]
    if command["request_data"] == "zero":
        return 2 if any(data) else 0
    if any(data[1:]):
        return 2
    if request["command"] == 0x41 and data[0] not in (0, 1, 2):
        return 2
    if request["command"] == 0x42 and data[0] not in (0, 1):
        return 2
    return 0


def api_result(case):
    response = case["response"]
    status = response["status"]
    if status:
        result = {"ok": False, "error": {"code": STATUS_NAMES[status]}}
        if status in (4, 5):
            result["value"] = None
        if status in (7, 12) and COMMANDS[response["command"]].get("mutating"):
            result["error"]["outcome"] = "unknown"
        return result
    data = bytes(response["data"])
    command = response["command"]
    if command in (0x20, 0x23):
        flags = data[4]
        check(flags in (1, 3), "successful measurement must be valid")
        check(data[7] == 0, "measurement padding must be zero")
        age = int.from_bytes(data[5:7], "big")
        check(age <= 65534, "measurement age exceeds saturation limit")
        value = struct.unpack(">i" if command == 0x20 else ">I", data[:4])[0]
        return {
            "ok": True,
            "value": value / 100 if command == 0x20 else value,
            "unit": "degC" if command == 0x20 else "Pa",
            "valid": bool(flags & 1),
            "fresh": bool(flags & 2),
            "age_ms": age,
        }
    if command == 0x31:
        check(not any(data[4:]), "RPM padding must be zero")
        return {"ok": True, "value": int.from_bytes(data[:4], "big"), "unit": "rpm"}
    if command in (0x40, 0x41):
        check(data[0] in (0, 1, 2), "invalid response mode")
        check(data[1] == 255 if data[0] == 0 else data[1] < 6, "invalid active idx")
        check(not any(data[2:]), "mode padding must be zero")
        return {"ok": True, "mode": data[0], "idx": None if data[0] == 0 else data[1]}
    check(not any(data[1:]), "scalar padding must be zero")
    if command == 0x22:
        check(data[0] in (0, 1), "door state must be bool")
        return {"ok": True, "value": bool(data[0])}
    if command == 0x42:
        check(data[0] in (0, 1), "lock state must be bool")
        return {"ok": True, "applied_lock_state": bool(data[0])}
    if command == 0x32:
        return {"ok": True, "applied_pwm": data[0]}
    check(command == 0x30, "successful result has no defined decoder")
    return {"ok": True, "value": data[0]}


def valid_stream_frames(hex_text):
    """Small offline oracle for golden resynchronization examples."""
    raw = bytes.fromhex(hex_text)
    found = []
    offset = 0
    while offset + 20 <= len(raw):
        candidate = raw[offset:offset + 20]
        try:
            parsed = parse_frame(candidate.hex())
            check(parsed["kind"] in (1, 2), "unknown kind")
        except AssertionError:
            offset += 1
            continue
        found.append(candidate)
        offset += 20
    return found


def main():
    check(SPEC["protocol_version"] == VECTORS["protocol_version"] == 1, "version mismatch")
    covered = []
    for field in SPEC["fields"]:
        covered.extend(range(field["offset"], field["offset"] + field["size"]))
    check(covered == list(range(20)), "field layout has gaps or overlaps")
    check(len(COMMANDS) == 10, "ten distinct commands are required")
    check(sum(c["access"] == "user" for c in COMMANDS.values()) == 7, "seven User commands")
    check(all(COMMANDS[c]["access"] == "admin" for c in (0x40, 0x41, 0x42)), "Admin boundary")
    check(binascii.crc_hqx(b"123456789", 0xFFFF) == SPEC["crc"]["check"] == 0x29B1, "CRC check")
    check(bytes.fromhex(VECTORS["crc_check"]["hex"]) == bytes([0x29, 0xB1]), "vector CRC check")
    tested = set()
    names = set()
    for case in VECTORS["round_trips"]:
        check(case["name"] not in names, "duplicate vector name")
        names.add(case["name"])
        request = parse_frame(case["request_hex"])
        response = parse_frame(case["response_hex"])
        check(request == case["request"], f'{case["name"]}: request bytes/fields differ')
        check(response == case["response"], f'{case["name"]}: response bytes/fields differ')
        check(request["kind"] == 1 and response["kind"] == 2, "wrong request/response kinds")
        check(response["version"] == 1, "wrong response version")
        check(correlation(request) == correlation(response), "response correlation mismatch")
        validation_error = request_error(request)
        if validation_error:
            check(response["status"] == validation_error, "wrong validation error or precedence")
        if response["status"]:
            check(not any(response["data"]), "error result data must be zero")
        check(api_result(case) == case["api_result"], f'{case["name"]}: decoded API result differs')
        if request["command"] in COMMANDS:
            tested.add(request["command"])
    check(tested == set(COMMANDS), "missing command vectors")
    for case in VECTORS["negative_frames"]:
        expected = case["expected"]
        raw = bytes.fromhex(case["input_hex"])
        if case["name"] == "corrupted_request_crc":
            check(binascii.crc_hqx(raw[2:18], 0xFFFF) != int.from_bytes(raw[18:], "big"), "bad CRC case")
        elif expected == "discard_partial_candidate":
            check(len(raw) < 20 and case["advance_ms"] > SPEC["timeouts_ms"]["inter_byte"], "partial frame")
        elif case["name"] == "unsupported_kind":
            check(parse_frame(raw.hex())["kind"] not in (1, 2), "bad kind case")
        elif expected == "ignore_without_finishing_pending_request":
            check(correlation(parse_frame(raw.hex())) != correlation(case["pending_request"]), "late response")
        elif expected == "reject_semantically_invalid_response":
            check(parse_frame(raw.hex())["data"][4] == 2, "fresh without valid negative case")
        elif expected == "recover_next_complete_crc_valid_frame":
            check(valid_stream_frames(raw.hex()) == [bytes.fromhex(x) for x in case["expected_valid_frames"]], "resync")
        else:
            raise AssertionError(f'Unverified negative example {case["name"]}')
    for case in VECTORS["serial_envelopes"]:
        expected = b"\r" + f'{case["destination"]:02X}'.encode("ascii") + bytes([case["data"]]) + b"\n"
        check(bytes.fromhex(case["hex"]) == expected, "serial envelope mismatch")
    print(
        f'Validated {len(VECTORS["round_trips"])} round trips, '
        f'{len(VECTORS["negative_frames"])} negative cases, '
        f'{len(VECTORS["serial_envelopes"])} serial envelopes; all ten commands covered. '
        'Mode sequence is a golden scenario for future firmware tests, not executed here.'
    )


if __name__ == "__main__":
    main()

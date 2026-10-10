"""Hardware API v2: 28 байтов, CRC16, Big Endian; никакого текстового UART."""
import binascii
from dataclasses import dataclass

from .errors import DeviceError


@dataclass(frozen=True)
class Command:
    name: str
    code: int
    node: int
    count: int
    layout: str


COMMANDS = {
    "temperature": Command("get_temp", 0x20, 13, 3, "temperature"),
    "voc": Command("get_voc", 0x21, 13, 2, "voc"),
    "pressure": Command("get_pressure", 0x23, 13, 3, "pressure"),
    "humidity": Command("get_humidity", 0x24, 13, 3, "humidity"),
    "dust": Command("get_dust", 0x25, 13, 2, "dust"),
    "pwm": Command("get_pwm", 0x30, 14, 6, "pwm"),
    "rpm": Command("get_rpm", 0x31, 14, 6, "rpm"),
    "door": Command("get_door_state", 0x22, 15, 2, "door"),
    "lock": Command("get_door_lock", 0x43, 16, 2, "lock"),
    "vacuum": Command("get_vacuum", 0x52, 16, 1, "vacuum"),
}
PROBE = Command("hub_probe", 0x20, 13, 0, "probe")
STATUS_NAMES = ("OK", "INVALID_INDEX", "INVALID_ARGUMENT", "UNKNOWN_COMMAND",
                "NOT_IMPLEMENTED", "SENSOR_UNAVAILABLE", "BUSY", "DEVICE_TIMEOUT",
                "UNSUPPORTED_VERSION", "INTERNAL_ERROR", "REQUEST_ID_CONFLICT",
                "INVALID_NODE", "TRANSPORT_OVERFLOW")
STATUS_HTTP = (200, 422, 422, 501, 501, 503, 503, 504, 502, 502, 502, 502, 502)


@dataclass(frozen=True)
class Frame:
    tx_id: int
    node: int
    command: int
    idx: int
    session_id: int
    data: bytes = bytes(8)
    kind: int = 1
    status: int = 0
    version: int = 2

    def encode(self) -> bytes:
        if len(self.data) != 8:
            raise ValueError("data must contain 8 bytes")
        packet = b"\xa5\x5a" + bytes((self.version, self.kind))
        packet += self.tx_id.to_bytes(2, "big")
        packet += bytes((self.node, self.command, self.status, self.idx))
        packet += self.data + self.session_id.to_bytes(8, "big")
        return packet + binascii.crc_hqx(packet[2:], 0xffff).to_bytes(2, "big")

    @classmethod
    def decode(cls, packet: bytes):
        if (len(packet) != 28 or packet[:2] != b"\xa5\x5a" or packet[2] != 2
                or packet[3] not in (1, 2)
                or binascii.crc_hqx(packet[2:26], 0xffff) != int.from_bytes(packet[26:], "big")):
            raise ValueError("Invalid Hardware API v2 frame")
        return cls(tx_id=int.from_bytes(packet[4:6], "big"), node=packet[6],
                   command=packet[7], status=packet[8], idx=packet[9],
                   data=packet[10:18], session_id=int.from_bytes(packet[18:26], "big"),
                   kind=packet[3], version=packet[2])

    def matches(self, request):
        return (self.kind == 2 and self.version == request.version
                and (self.tx_id, self.node, self.command, self.idx, self.session_id)
                == (request.tx_id, request.node, request.command, request.idx, request.session_id))


def envelope(value: int, address: int = 12) -> bytes:
    return b"\r" + f"{address:02X}".encode("ascii") + bytes((value,)) + b"\n"


class ReplyParser:
    """Ограниченные окна 5/28 байтов, восстановление после шума и потери байта."""
    def __init__(self):
        self.bus = bytearray()
        self.frame = bytearray()
        self.first = self.last = 0.0

    def feed(self, chunk: bytes, now: float):
        for byte in chunk:
            self.bus.append(byte)
            if len(self.bus) < 5:
                continue
            if (self.bus[0] != 13 or self.bus[4] != 10
                    or any(c not in b"0123456789ABCDEF" for c in self.bus[1:3])):
                del self.bus[0]
                continue
            address, payload = int(self.bus[1:3], 16), self.bus[3]
            self.bus.clear()
            if address != 99:  # Эхо и трафик шлюзов не являются ответом компьютеру.
                continue
            if now - self.last >= .250 or now - self.first >= 1.0:
                self.frame.clear()
            self.last = now
            if not self.frame:
                if payload != 0xa5:
                    continue
                self.first = now
            self.frame.append(payload)
            if len(self.frame) == 2 and self.frame[1] != 0x5a:
                self.frame[:] = b"\xa5" if payload == 0xa5 else b""
                self.first = now
            if len(self.frame) == 28:
                packet = bytes(self.frame)
                try:
                    result = Frame.decode(packet)
                except ValueError:
                    start = packet.find(b"\xa5\x5a", 1)
                    self.frame[:] = packet[start:] if start >= 0 else (b"\xa5" if payload == 0xa5 else b"")
                    self.first = now
                else:
                    self.frame.clear()
                    yield result


def decode_result(command: Command, reply: Frame) -> dict:
    data = reply.data
    if not 0 <= reply.status < len(STATUS_NAMES):
        raise ValueError("Unknown device status")
    if reply.status and any(data):
        raise ValueError("Nonzero error data")
    if command.layout == "probe" and reply.status == 1:
        return {"responding": True, "expected_status": "INVALID_INDEX"}
    if reply.status:
        raise DeviceError(STATUS_NAMES[reply.status], "Оборудование вернуло ошибку",
                          STATUS_HTTP[reply.status], device_status=reply.status,
                          node=reply.node, command=command.name, idx=reply.idx)
    result = {"idx": reply.idx}
    if command.layout in ("pwm", "rpm", "door", "lock", "vacuum"):
        size = 4 if command.layout == "rpm" else 1
        if any(data[size:]) or (command.layout in ("door", "lock", "vacuum") and data[0] > 1):
            raise ValueError("Invalid scalar/reserved bytes")
        if command.layout == "door":
            return {**result, "closed": bool(data[0])}
        if command.layout == "lock":
            return {**result, "lock_output_enabled": bool(data[0])}
        if command.layout == "vacuum":
            return {"enabled": bool(data[0])}
        return {**result, "value": int.from_bytes(data[:size], "big"),
                "unit": "rpm" if size == 4 else "PWM 0..255"}
    if command.layout == "probe":
        raise ValueError("Probe must return INVALID_INDEX")
    age = int.from_bytes(data[5:7], "big")
    if data[4] not in (1, 3) or age == 65535 or data[7]:
        raise ValueError("Invalid measurement quality/reserved bytes")
    result.update(valid=True, fresh=data[4] == 3, age_ms=age)
    value = int.from_bytes(data[:4], "big")
    if command.layout == "temperature":
        value, unit = int.from_bytes(data[:4], "big", signed=True) / 100, "degC"
    elif command.layout == "pressure":
        unit = "Pa"
    elif command.layout == "voc" and 1 <= value <= 500:
        unit = "VOC Index"
    elif command.layout == "humidity" and value <= 10000:
        value, unit = value / 100, "%RH"
    elif command.layout == "dust" and int.from_bytes(data[:2], "big") <= 1023:
        mv = int.from_bytes(data[2:4], "big")
        return {**result, "adc": int.from_bytes(data[:2], "big"), "voltage_mv": mv, "voltage_v": mv / 1000}
    else:
        raise ValueError("Invalid measurement value/layout")
    return {**result, "value": value, "unit": unit}

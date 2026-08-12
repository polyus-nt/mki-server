"""SimpleBus protocol helpers extracted to a separate library.

Provides packet encode/decode, port listing, and a small DataCollector
for assembling payloads wrapped by a marker byte.
"""
from __future__ import annotations

CR = b"\r"
LF = b"\n"
COMPUTER_ADDR = 99
MARKER = 12  # start/end of transmission to computer

try:
    from serial.tools import list_ports
except Exception:  # pragma: no cover
    list_ports = None


def list_serial_ports() -> list[str]:
    if list_ports is None:
        return []
    return [p.device for p in list_ports.comports()]


def encode_packet(address: int, data: bytes | str) -> bytes:
    if not 0 <= address <= 0xFF:
        raise ValueError("Address must be a byte value between 0 and 255.")
    if isinstance(data, str):
        if len(data) != 1:
            raise ValueError("Data payload must be a single character.")
        data_bytes = data.encode("ascii")
    else:
        if len(data) != 1:
            raise ValueError("Data payload must be exactly one byte.")
        data_bytes = data
    addr_hex = f"{address:02X}".encode("ascii")
    return CR + addr_hex + data_bytes + LF


def decode_packet(packet: bytes) -> tuple[int, bytes]:
    if len(packet) != 5 or not packet.startswith(CR) or not packet.endswith(LF):
        raise ValueError(f"Invalid packet length or framing: {packet!r}")
    addr_hex = packet[1:3].decode("ascii")
    address = int(addr_hex, 16)
    data = packet[3:4]
    return address, data


class DataCollector:
    """Collects bytes between marker START/END bytes (MARKER).

    Usage: call `process_byte(b)` for each incoming byte (int 0-255).
    When `process_byte` returns True a complete capture finished and can
    be retrieved with `get_buffer()`.
    """

    def __init__(self, marker: int = MARKER):
        self.marker = marker
        self.capturing = False
        self._buf: list[int] = []

    def process_byte(self, b: int) -> bool:
        if b == self.marker:
            if not self.capturing:
                self.capturing = True
                self._buf = []
                return False
            else:
                # end of capture
                self.capturing = False
                return True
        if self.capturing:
            self._buf.append(b)
        return False

    def get_buffer(self) -> bytes:
        return bytes(self._buf)

    def reset(self) -> None:
        self._buf = []
        self.capturing = False

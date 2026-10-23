"""Поиск пользовательского канала CH344 без открытия остальных портов."""
from pathlib import Path
import logging
import re

from serial.tools import list_ports

log = logging.getLogger(__name__)


def usb_identity(device):
    """sysfs работает и с cdc_acm, и с нестандартным ttyCH343USB WCH."""
    path = (Path("/sys/class/tty") / Path(device).name / "device").resolve()
    interface = None
    for parent in (path, *path.parents):
        try:
            if interface is None and (parent / "bInterfaceNumber").exists():
                interface = int((parent / "bInterfaceNumber").read_text().strip(), 16)
            if (parent / "idVendor").exists():
                return (int((parent / "idVendor").read_text().strip(), 16),
                        int((parent / "idProduct").read_text().strip(), 16), interface)
        except (OSError, ValueError):
            return None
    return None


def resolve_port(configured):
    if configured.lower() != "auto":
        return configured
    candidates = set()
    ports = {p.device: p for p in list_ports.comports()}
    # Некоторые версии pySerial не перечисляют нестандартное имя WCH.
    devices = set(ports) | {str(p) for p in Path("/dev").glob("ttyCH343USB*")}
    wch_ports = [device for device in devices
                 if re.fullmatch(r"/dev/ttyCH343USB[0-9]+", device)]
    if wch_ports:
        port = max(wch_ports, key=lambda device: int(device.removeprefix("/dev/ttyCH343USB")))
        log.info("Автопоиск МС-ТЮК: %s (максимальный номер ttyCH343USB)", port)
        return port
    for device in devices:
        identity = usb_identity(device)
        info = ports.get(device)
        if identity is None and info is not None:
            # Windows сообщает номер USB-интерфейса в hardware ID.
            match = re.search(r"MI_([0-9A-F]{2})", info.hwid or "", re.I)
            identity = (info.vid, info.pid, int(match[1], 16) if match else None)
        if identity == (0x1a86, 0x55d5, 6):
            candidates.add(str(Path(device).resolve()) if device.startswith("/") else device)
    if len(candidates) != 1:
        raise OSError("Автопоиск CH344 if06: " +
                      ("порт не найден" if not candidates else "несколько портов: " + ", ".join(sorted(candidates))) +
                      ". Укажите --serial-port явно или подключите один МС-ТЮК")
    port = candidates.pop()
    log.info("Автопоиск МС-ТЮК: %s (CH344 if06)", port)
    return port

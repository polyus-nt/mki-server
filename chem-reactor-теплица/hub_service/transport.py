"""Синхронный транспорт. Все методы вызывает один выделенный I/O-поток."""
import logging
import os
import secrets
import time

import serial

from .errors import HubError
from .protocol import Command, Frame, ReplyParser, decode_result, envelope
from .settings import Settings

log = logging.getLogger(__name__)


class SerialTransport:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.session = secrets.randbits(64) or 1
        self.tx_id = 1
        self.port = serial.Serial(port=settings.serial_port, baudrate=9600,
            bytesize=serial.EIGHTBITS, parity=serial.PARITY_NONE, stopbits=serial.STOPBITS_ONE,
            timeout=.05, write_timeout=1, xonxoff=False, rtscts=False, dsrdtr=False,
            exclusive=True if os.name == "posix" else None)

    def close(self):
        self.port.close()

    def read(self, timeout: float):
        self.port.timeout = max(0, timeout)
        return self.port.read(1024)

    def quiet(self):
        # Не reset_input_buffer(): поздние ответы дренируются до реальной тишины.
        start = last = time.monotonic()
        while time.monotonic() - last < 1:
            if time.monotonic() - start >= 3:
                raise OSError("Нет секунды тишины на шине за 3 с")
            if self.read(.05):
                last = time.monotonic()

    def send(self, packet: bytes, exchange_deadline: float):
        deadline = min(time.monotonic() + 1, exchange_deadline)
        earliest_end = time.monotonic() + len(packet) * 10 / 9600
        self.port.write_timeout = max(.001, deadline - time.monotonic())
        while packet:
            if time.monotonic() >= deadline:
                raise OSError("Тайм-аут записи COM-порта")
            count = self.port.write(packet)
            if not count:
                raise OSError("COM-порт не принимает данные")
            packet = packet[count:]
        # Windows pyserial.flush() опрашивает очередь с шагом 50 мс. Для 28
        # пакетов это может превысить 1 с на сборку кадра в хабе. Не используем его.
        while self.port.out_waiting:
            if time.monotonic() >= deadline:
                raise OSError("Тайм-аут передачи COM-порта")
            time.sleep(0)
        gap_end = max(earliest_end, time.monotonic()) + .002
        while time.monotonic() < gap_end:
            time.sleep(0)

    def exchange(self, command: Command, idx: int, data: bytes = bytes(8)) -> dict:
        request = Frame(self.tx_id, command.node, command.code, idx, self.session, data=data)
        self.tx_id += 1
        if self.tx_id > 65535:
            self.tx_id = 1
            self.session = (self.session + 1) % (1 << 64) or 1
        deadline = time.monotonic() + self.settings.exchange_timeout
        parser, received = ReplyParser(), 0
        log.debug("TX API: %s", request.encode().hex(" "))
        for byte in request.encode():
            self.send(envelope(byte), deadline)
        while time.monotonic() < deadline:
            chunk = self.read(min(.05, max(0, deadline - time.monotonic())))
            received += len(chunk)
            if chunk:
                log.debug("RX RAW: %s", chunk.hex(" "))
            for reply in parser.feed(chunk, time.monotonic()):
                if not reply.matches(request):
                    continue
                try:
                    return decode_result(command, reply)
                except ValueError as error:
                    log.warning("Отклонён ответ: %s", error)
        raise HubError("HOST_TIMEOUT", "Подходящий ответ хаба не пришёл вовремя", 504,
                       rx_bytes=received, node=command.node, command=command.name, idx=idx)

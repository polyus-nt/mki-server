"""115200 SimpleBus. Один I/O-поток, без фиксированного ожидания тишины."""
import logging
import os
import secrets
import time
from dataclasses import replace
import serial
from .errors import DeviceError, HubError
from .protocol import Command, Frame, ReplyParser, decode_result, envelope
from .settings import Settings

log = logging.getLogger(__name__)
BUS_BAUD = 115200
READY, CLAIM, GRANT = 0xf0, 0xf1, 0xf2

def control(frame, code):
    return (frame.kind == 2 and frame.node == 12 and frame.command == code
            and frame.status == 0 and frame.idx == 0
            and frame.data == b"\x00\x64" + bytes(6))

class SerialTransport:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.session = secrets.randbits(64) or 1
        self.last_activity = float("-inf")
        self.port = serial.Serial(port=settings.serial_port, baudrate=BUS_BAUD,
            bytesize=serial.EIGHTBITS, parity=serial.PARITY_NONE, stopbits=serial.STOPBITS_ONE,
            timeout=.005, write_timeout=1, xonxoff=False, rtscts=False, dsrdtr=False,
            exclusive=True if os.name == "posix" else None)

    def close(self):
        self.port.close()

    def read(self, timeout):
        # Не ждём заполнения 1024 байт: USB мог уже доставить целый короткий кадр.
        self.port.timeout = max(0, timeout)
        first = self.port.read(1)
        return first + self.port.read(self.port.in_waiting) if first else b""

    def quiet(self):
        # Совместимое имя для service/recovery. Только немедленный сброс старого
        # буфера вне активного обмена: старое READY не является разрешением.
        self.port.reset_input_buffer()

    def send(self, packet, deadline):
        self.port.write_timeout = max(.001, deadline - time.monotonic())
        while packet:
            if time.monotonic() >= deadline:
                raise OSError("Тайм-аут записи COM-порта")
            count = self.port.write(packet)
            if not count:
                raise OSError("COM-порт не принимает данные")
            packet = packet[count:]
        # Все 28 пакетов одной записью, без пауз между байтами API.
        # UART хаба переключает ReDe только после TC. Windows flush не нужен.

    def send_frame(self, frame, deadline):
        log.debug("TX API: %s", frame.encode().hex(" "))
        self.send(b"".join(envelope(byte) for byte in frame.encode()), deadline)

    def exchange(self, command: Command, idx: int, data: bytes = bytes(8)) -> dict:
        if time.monotonic()-self.last_activity > .05:
            self.quiet()  # После простоя отбросить устаревшие предложения окна.
        deadline = time.monotonic() + self.settings.exchange_timeout
        parser, received, ticket, request = ReplyParser(), 0, None, None
        while time.monotonic() < deadline:
            chunk = self.read(min(.005, max(0, deadline-time.monotonic())))
            received += len(chunk)
            if chunk:
                log.debug("RX RAW: %s", chunk.hex(" "))
            for reply in parser.feed(chunk, time.monotonic()):
                if request is None:
                    if control(reply, READY):
                        ticket = reply
                        self.send_frame(replace(reply, kind=1, command=CLAIM, data=self.session.to_bytes(8, "big")), deadline)
                    elif (ticket is not None and control(reply, GRANT)
                          and (reply.tx_id, reply.session_id) == (ticket.tx_id, ticket.session_id ^ self.session)):
                        request = Frame(reply.tx_id, command.node, command.code, idx,
                                        reply.session_id, data=data)
                        # Запись действия ровно один раз. Ошибка/потеря ответа не
                        # приводят к повторной отправке SET даже с новым READY.
                        self.send_frame(request, deadline)
                    continue
                if not reply.matches(request):
                    continue
                try:
                    result = decode_result(command, reply)
                    self.last_activity = time.monotonic()
                    return result
                except DeviceError:
                    self.last_activity = time.monotonic()
                    raise  # Валидная ошибка датчика тоже завершает пайплайн немедленно.
                except ValueError as error:
                    log.warning("Отклонён ответ: %s", error)
        raise HubError("HOST_TIMEOUT", "Разрешение/подходящий ответ хаба не пришёл вовремя", 504,
                       rx_bytes=received, node=command.node, command=command.name, idx=idx,
                       command_sent=request is not None)

from dataclasses import dataclass
import math
import os


@dataclass(frozen=True)
class Settings:
    serial_port: str
    queue_capacity: int = 64
    queue_timeout: float = 30.0
    exchange_timeout: float = 5.0
    api_token: str | None = None
    admin_token: str | None = None

    def __post_init__(self):
        if not self.serial_port.strip():
            raise ValueError("Укажите --serial-port или MSTJUK_SERIAL_PORT")
        if not 1 <= self.queue_capacity <= 1024:
            raise ValueError("queue_capacity должен быть 1..1024")
        for value in (self.queue_timeout, self.exchange_timeout):
            if not math.isfinite(value) or value <= 0:
                raise ValueError("Тайм-аут должен быть конечным положительным числом")
        if self.api_token and self.admin_token and self.api_token == self.admin_token:
            raise ValueError("User и Admin токены должны отличаться")

    @classmethod
    def from_env(cls):
        return cls(serial_port=os.getenv("MSTJUK_SERIAL_PORT", ""),
                   api_token=os.getenv("MSTJUK_API_TOKEN") or None,
                   admin_token=os.getenv("MSTJUK_ADMIN_TOKEN") or None)

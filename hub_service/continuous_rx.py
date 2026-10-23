"""Постоянный RX: один читатель порта, ограниченный буфер только для активного обмена."""
from collections import deque
import threading
import time


class ContinuousReader:
    """Поток не пишет в порт, не печатает и не вызывает flush/reset_input_buffer.

    Во время простоя байты вычитываются и отбрасываются. Во время транзакции
    сохраняются до 64 КиБ; переполнение не создаёт неограниченную очередь.
    Последовательный потребитель забирает сразу всю доступную пачку.
    """
    LIMIT = 65536

    def __init__(self, read_bytes):
        self._read_bytes = read_bytes
        self._condition = threading.Condition()
        self._stop = threading.Event()
        self._chunks = deque()
        self._size = 0
        self._active = False
        self._error = None
        self.rx_bytes = 0
        self.dropped_bytes = 0
        self.received_at = 0.0
        self._thread = threading.Thread(target=self._run, name="mstjuk-rx", daemon=True)
        self._thread.start()

    def _run(self):
        try:
            while not self._stop.is_set():
                chunk = self._read_bytes(.01)
                if not chunk:
                    self._stop.wait(.001)  # Также не крутим CPU при неблокирующем fake/драйвере.
                    continue
                now = time.monotonic()
                with self._condition:
                    self.rx_bytes += len(chunk)
                    if self._active:
                        # Читатель получает ограниченные куски от backend.
                        if len(chunk) > self.LIMIT:
                            self.dropped_bytes += len(chunk) - self.LIMIT
                            chunk = chunk[-self.LIMIT:]
                        self._chunks.append((chunk, now))
                        self._size += len(chunk)
                        while self._size > self.LIMIT:
                            old, _ = self._chunks.popleft()
                            self._size -= len(old)
                            self.dropped_bytes += len(old)
                        self._condition.notify_all()
        except Exception as error:
            with self._condition:
                self._error = error
                self._condition.notify_all()

    def clear(self):
        with self._condition:
            self._chunks.clear()
            self._size = 0

    def begin(self):
        with self._condition:
            if self._error is not None:
                raise OSError(f"Ошибка фонового RX: {self._error}") from self._error
            self._chunks.clear()
            self._size = 0
            self._active = True
            return self.rx_bytes

    def end(self):
        with self._condition:
            self._active = False
            self._chunks.clear()
            self._size = 0

    def read(self, timeout):
        deadline = time.monotonic() + max(0, timeout)
        with self._condition:
            while not self._chunks and self._error is None and not self._stop.is_set():
                left = deadline - time.monotonic()
                if left <= 0:
                    return b""
                self._condition.wait(left)
            if self._error is not None:
                raise OSError(f"Ошибка фонового RX: {self._error}") from self._error
            if not self._chunks:
                return b""
            self.received_at = self._chunks[-1][1]
            result = b"".join(chunk for chunk, _ in self._chunks)
            self._chunks.clear()
            self._size = 0
            return result

    def close(self):
        self._stop.set()
        with self._condition:
            self._condition.notify_all()
        self._thread.join(timeout=.5)
        if self._thread.is_alive():
            raise OSError("Поток RX не завершился за 0,5 с")

class PermitTracker:
    """Отбрасывает дубли/обратный ход READY в одной сессии и старые сессии.

    Время получения на ПК не доказывает возраст пакета в USB-адаптере.
    После секунды без продвижения разрешаем новую базу (например, сброс хаба).
    """
    def __init__(self):
        self.key = None
        self.at = 0.0
        self.retired = deque(maxlen=8)

    def accept(self, key, now):
        if self.key is not None:
            if key[0] == self.key[0]:
                delta = (key[1] - self.key[1]) & 0xffff
                if (not delta or delta >= 0x8000) and now - self.at < 1.0:
                    return False
            else:
                if key[0] in self.retired and now - self.at < 1.0:
                    return False
                self.retired.append(self.key[0])
        self.key, self.at = key, now
        return True

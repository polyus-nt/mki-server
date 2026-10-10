"""Ограниченная FIFO; один физический обмен, ожидание HTTP без блокировки loop."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
import logging
import time

from .errors import DeviceError, HubError
from .protocol import Command
from .settings import Settings
from .transport import SerialTransport

log = logging.getLogger(__name__)


@dataclass
class Job:
    command: Command
    idx: int
    queued_at: float
    result: asyncio.Future
    started: asyncio.Event


class HubService:
    def __init__(self, settings: Settings, transport_factory=SerialTransport):
        self.settings = settings
        self.factory = transport_factory
        self.queue = asyncio.Queue(maxsize=settings.queue_capacity)
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="mstjuk-serial")
        self.transport = None
        self.task = None
        self.connected = self.active = self.recovering = self.closing = False
        self.last_response_at = None

    async def io(self, function, *args):
        return await asyncio.get_running_loop().run_in_executor(self.executor, function, *args)

    async def start(self):
        try:
            self.transport = await self.io(self.factory, self.settings)
            await self.io(self.transport.quiet)
        except BaseException:
            await self.close()
            raise
        self.connected = True
        self.task = asyncio.create_task(self.work(), name="mstjuk-fifo")

    @staticmethod
    def fail(job, error):
        if not job.result.done():
            job.result.set_exception(error)
        job.started.set()

    def drain(self, error):
        while not self.queue.empty():
            job = self.queue.get_nowait()
            self.fail(job, error)
            self.queue.task_done()

    async def read(self, command: Command, idx: int):
        if not self.connected or self.closing:
            raise HubError("DISCONNECTED", "COM-порт недоступен")
        loop = asyncio.get_running_loop()
        job = Job(command, idx, time.monotonic(), loop.create_future(), asyncio.Event())
        try:
            self.queue.put_nowait(job)
        except asyncio.QueueFull:
            raise HubError("QUEUE_FULL", "Очередь аппаратных запросов заполнена") from None
        try:
            try:
                await asyncio.wait_for(job.started.wait(), self.settings.queue_timeout)
            except TimeoutError:
                if not job.started.is_set():
                    job.result.cancel()
                    raise HubError("QUEUE_TIMEOUT", "Истекло ожидание в очереди; запрос не отправлен", 504) from None
            return await job.result
        except asyncio.CancelledError:
            # Отменённое ожидание HTTP не прерывает уже начатый физический обмен.
            # Запрос, оставшийся в очереди, worker пропустит без передачи.
            job.result.cancel()
            raise

    async def work(self):
        job = None
        try:
            while not self.closing:
                job = await self.queue.get()
                try:
                    if job.result.cancelled():
                        continue
                    if time.monotonic() - job.queued_at >= self.settings.queue_timeout:
                        self.fail(job, HubError("QUEUE_TIMEOUT", "Запрос просрочен до отправки", 504))
                        continue
                    self.active = True
                    job.started.set()
                    recover = False
                    try:
                        data = await self.io(self.transport.exchange, job.command, job.idx)
                    except DeviceError as error:
                        self.last_response_at = datetime.now(timezone.utc).isoformat()
                        self.fail(job, error)
                        recover = error.details.get("device_status") in (7, 9, 12)
                    except HubError as error:
                        self.fail(job, error)
                        recover = error.code == "HOST_TIMEOUT"
                    except OSError as error:
                        self.connected = False
                        self.fail(job, HubError("DISCONNECTED", "Ошибка COM-порта"))
                        log.error("COM-порт отключён: %s", error)
                        return
                    else:
                        self.last_response_at = datetime.now(timezone.utc).isoformat()
                        if not job.result.done():
                            job.result.set_result({**data, "received_at": self.last_response_at})
                    if recover:
                        self.recovering = True
                        try:
                            await self.io(self.transport.quiet)
                        except OSError as error:
                            self.connected = False
                            log.error("Не удалось восстановить тишину: %s", error)
                            return
                        finally:
                            self.recovering = False
                finally:
                    if not job.result.done():
                        self.fail(job, HubError(
                            "SHUTTING_DOWN" if self.closing else "INTERNAL_ERROR",
                            "Аппаратный обмен прерван", 503 if self.closing else 500))
                    self.active = False
                    self.queue.task_done()
                    job = None
        except asyncio.CancelledError:
            # При shutdown I/O-поток завершит ограниченный по времени обмен;
            # close() закроет порт в том же потоке после него.
            if job:
                self.fail(job, HubError("SHUTTING_DOWN", "Сервис останавливается"))
            raise
        except Exception:
            log.exception("Аппаратный worker остановился")
            if job:
                self.fail(job, HubError("INTERNAL_ERROR", "Ошибка аппаратного worker", 500))
        finally:
            self.connected = False
            self.drain(HubError("DISCONNECTED", "Аппаратный worker недоступен"))
            if self.transport and not self.closing:
                await self.io(self.transport.close)

    async def close(self):
        if self.closing:
            return
        self.closing = True
        self.connected = False
        self.drain(HubError("SHUTTING_DOWN", "Сервис останавливается"))
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
        if self.transport:
            await self.io(self.transport.close)
        await asyncio.to_thread(self.executor.shutdown, wait=True)

    def health(self):
        return {"status": "ready" if self.connected else "disconnected",
                "serial_open": self.connected, "queue_depth": self.queue.qsize(),
                "queue_capacity": self.settings.queue_capacity, "active": self.active,
                "recovering": self.recovering, "last_response_at": self.last_response_at}

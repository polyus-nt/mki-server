import asyncio
import threading
import time
import unittest

from hub_service.errors import DeviceError, HubError
from hub_service.protocol import COMMANDS, WRITE_COMMANDS
from hub_service.service import HubService
from hub_service.settings import Settings


class FakeTransport:
    def __init__(self, settings):
        self.threads = [threading.get_ident()]
        self.calls = []
        self.quiet_calls = self.closed = 0
        self.gate = threading.Event(); self.gate.set()
        self.error = None
        self.payloads = []

    def quiet(self):
        self.threads.append(threading.get_ident())
        self.quiet_calls += 1

    def close(self):
        self.threads.append(threading.get_ident())
        self.closed += 1

    def exchange(self, command, idx, data=bytes(8)):
        self.threads.append(threading.get_ident())
        self.calls.append((command.name, idx))
        self.payloads.append(data)
        if not self.gate.wait(2):
            raise OSError("test gate timeout")
        if self.error:
            error, self.error = self.error, None
            raise error
        return {"idx": idx, "value": 100653, "unit": "Pa", "valid": True, "fresh": True, "age_ms": 0}


class ServiceTests(unittest.IsolatedAsyncioTestCase):
    async def start_service(self, **kwargs):
        self.service = HubService(Settings("fake", **kwargs), FakeTransport)
        await self.service.start()
        self.port = self.service.transport
        self.addAsyncCleanup(self.cleanup_service)
        return self.service

    async def cleanup_service(self):
        self.port.gate.set()
        await self.service.close()

    async def wait_active(self):
        for _ in range(100):
            if self.port.calls:
                return
            await asyncio.sleep(.005)
        self.fail("worker never started")

    async def test_fifo_and_single_io_thread(self):
        service = await self.start_service()
        tasks = [asyncio.create_task(service.read(COMMANDS["pressure"], i)) for i in (0, 1, 2)]
        results = await asyncio.gather(*tasks)
        self.assertEqual([r["idx"] for r in results], [0, 1, 2])
        self.assertEqual(self.port.calls, [("get_pressure", i) for i in (0, 1, 2)])
        await service.close()
        self.assertEqual(len(set(self.port.threads)), 1)
        self.assertNotEqual(self.port.threads[0], threading.get_ident())

    async def test_read_and_write_share_fifo_and_payload_is_preserved(self):
        service = await self.start_service()
        jobs = [(COMMANDS["rpm"], 1, bytes(8)),
                (WRITE_COMMANDS["pwm"], 1, b"\xff" + bytes(7)),
                (WRITE_COMMANDS["lock"], 0, b"\x01" + bytes(7)),
                (WRITE_COMMANDS["relay"], 0, b"\x01" + bytes(7)),
                (COMMANDS["pwm"], 0, bytes(8))]
        await asyncio.gather(*(service.read(*job) for job in jobs))
        self.assertEqual(self.port.calls, [(c.name, idx) for c, idx, _ in jobs])
        self.assertEqual(self.port.payloads, [data for _, _, data in jobs])
        self.assertEqual(len(set(self.port.threads)), 1)

    async def test_queue_full_and_cancellation_skips_unsent(self):
        service = await self.start_service(queue_capacity=1)
        self.port.gate.clear()
        active = asyncio.create_task(service.read(COMMANDS["pressure"], 0))
        await self.wait_active()
        queued = asyncio.create_task(service.read(COMMANDS["pressure"], 1))
        await asyncio.sleep(.01)
        with self.assertRaises(HubError) as error:
            await service.read(COMMANDS["pressure"], 2)
        self.assertEqual(error.exception.code, "QUEUE_FULL")
        queued.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await queued
        self.port.gate.set(); await active
        await asyncio.sleep(.01)
        self.assertEqual(self.port.calls, [("get_pressure", 0)])

    async def test_queue_timeout_does_not_cancel_active_exchange(self):
        service = await self.start_service(queue_timeout=.04)
        self.port.gate.clear()
        active = asyncio.create_task(service.read(COMMANDS["pressure"], 0))
        await self.wait_active()
        with self.assertRaises(HubError) as error:
            await service.read(COMMANDS["pressure"], 1)
        self.assertEqual(error.exception.code, "QUEUE_TIMEOUT")
        self.assertFalse(active.done())
        self.port.gate.set(); await active
        await asyncio.sleep(.01)
        self.assertEqual(len(self.port.calls), 1)

    async def test_timeout_recovery_without_retry(self):
        service = await self.start_service()
        self.port.error = HubError("HOST_TIMEOUT", "test", 504)
        with self.assertRaises(HubError):
            await service.read(COMMANDS["pressure"], 0)
        await service.read(COMMANDS["pressure"], 1)
        self.assertEqual(self.port.quiet_calls, 2)
        self.assertEqual(len(self.port.calls), 2)
        self.port.error = DeviceError("DEVICE_TIMEOUT", "test", 504, device_status=7)
        with self.assertRaises(DeviceError):
            await service.read(COMMANDS["pressure"], 0)
        await service.read(COMMANDS["pressure"], 2)
        self.assertEqual(self.port.quiet_calls, 3)

    async def test_write_timeout_never_resends_set(self):
        service = await self.start_service()
        self.port.error = HubError("HOST_TIMEOUT", "lost SET reply", 504)
        with self.assertRaises(HubError):
            await service.read(WRITE_COMMANDS["pwm"], 5, b"\x64" + bytes(7))
        await service.read(COMMANDS["pwm"], 5)
        self.assertEqual(self.port.calls, [("set_pwm", 5), ("get_pwm", 5)])
        self.assertEqual(self.port.payloads, [b"\x64" + bytes(7), bytes(8)])
        self.assertEqual(self.port.quiet_calls, 2)

    async def test_disconnect_finishes_all_waiters(self):
        service = await self.start_service()
        self.port.gate.clear(); self.port.error = OSError("unplugged")
        active = asyncio.create_task(service.read(COMMANDS["pressure"], 0))
        await self.wait_active()
        queued = asyncio.create_task(service.read(COMMANDS["pressure"], 1))
        await asyncio.sleep(.01); self.port.gate.set()
        results = await asyncio.gather(active, queued, return_exceptions=True)
        self.assertTrue(all(isinstance(error, HubError) and error.code == "DISCONNECTED" for error in results))
        self.assertFalse(service.connected)
        with self.assertRaises(HubError):
            await service.read(COMMANDS["pressure"], 2)

    async def test_shutdown_resolves_active_and_waiting_jobs(self):
        service = await self.start_service()
        self.port.gate.clear()
        active = asyncio.create_task(service.read(COMMANDS["pressure"], 0))
        await self.wait_active()
        queued = asyncio.create_task(service.read(COMMANDS["pressure"], 1))
        await asyncio.sleep(.01)
        close = asyncio.create_task(service.close())
        await asyncio.sleep(.01); self.port.gate.set(); await close
        results = await asyncio.gather(active, queued, return_exceptions=True)
        self.assertTrue(all(isinstance(error, HubError) and error.code == "SHUTTING_DOWN" for error in results))
        self.assertEqual(self.port.closed, 1)

    async def test_unexpected_worker_error_does_not_leave_http_hanging(self):
        service = await self.start_service()
        self.port.error = RuntimeError("unexpected")
        with self.assertRaises(HubError) as error:
            await asyncio.wait_for(service.read(COMMANDS["pressure"], 0), .5)
        self.assertEqual(error.exception.code, "INTERNAL_ERROR")
        self.assertFalse(service.connected)


if __name__ == "__main__":
    unittest.main()

"""Тесты общего RX-механизма без железа: очередь, простой, ошибка, завершение."""
import queue
import time
import unittest
from hub_service.continuous_rx import ContinuousReader, PermitTracker


class ReceiverTests(unittest.TestCase):
    def wait_until(self, predicate):
        deadline = time.monotonic() + 1
        while not predicate() and time.monotonic() < deadline:
            time.sleep(.001)
        self.assertTrue(predicate())

    def make_reader(self):
        incoming = queue.Queue()
        def read(timeout):
            try:
                return incoming.get(timeout=timeout)
            except queue.Empty:
                return b""
        reader = ContinuousReader(read)
        self.addCleanup(reader.close)
        return reader, incoming

    def test_idle_drains_without_retaining_data_or_transmitting(self):
        reader, incoming = self.make_reader()
        for _ in range(100):
            incoming.put(b"old READY"*20)
        self.wait_until(lambda: reader.rx_bytes == 18000)
        self.assertEqual(reader._size, 0)
        self.assertFalse(reader._chunks)
        reader.begin()
        incoming.put(b"new READY")
        self.assertEqual(reader.read(.5), b"new READY")
        reader.end()
        incoming.put(b"idle again")
        self.wait_until(lambda: reader.rx_bytes == 18019)
        self.assertFalse(reader._chunks)

    def test_bounded_active_backlog_and_batch_drain(self):
        reader, incoming = self.make_reader()
        reader.begin()
        for i in range(100):
            incoming.put(bytes([i])*4096)
        self.wait_until(lambda: reader.rx_bytes == 409600)
        self.assertLessEqual(reader._size, reader.LIMIT)
        self.assertGreater(reader.dropped_bytes, 0)
        chunk = reader.read(.1)
        self.assertEqual(len(chunk), reader.LIMIT)
        self.assertEqual(chunk[-4096:], bytes([99])*4096)
        self.assertFalse(reader._chunks)

    def test_close_and_error_propagation(self):
        reader, _ = self.make_reader()
        reader.close()
        self.assertFalse(reader._thread.is_alive())
        def broken(timeout):
            raise OSError("device disconnected")
        reader = ContinuousReader(broken)
        self.addCleanup(reader.close)
        self.wait_until(lambda: reader._error is not None)
        with self.assertRaisesRegex(OSError, "device disconnected"):
            reader.begin()

    def test_ticket_replay_wrap_and_hub_restart(self):
        p = PermitTracker()
        self.assertTrue(p.accept((123, 0x2b0c), 1))
        self.assertTrue(p.accept((123, 0x2b0f), 1))
        self.assertFalse(p.accept((123, 0x2adc), 1))
        self.assertFalse(p.accept((123, 0x2b0f), 1.1))
        p = PermitTracker()
        self.assertTrue(p.accept((123, 0xfffe), 1.1))
        self.assertTrue(p.accept((123, 0xffff), 1.2))
        self.assertTrue(p.accept((122, 0), 1.3))
        self.assertFalse(p.accept((123, 0xffff), 1.4))
        self.assertTrue(p.accept((124, 1), 1.5))
        self.assertTrue(p.accept((124, 0), 3))  # Сброс с той же меткой.


if __name__ == "__main__":
    unittest.main()

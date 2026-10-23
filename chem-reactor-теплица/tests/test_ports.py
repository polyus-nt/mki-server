import asyncio
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from hub_service.ports import resolve_port
from hub_service.__main__ import serve
from scripts.systemd_unit import render


class PortTests(unittest.TestCase):
    def test_explicit_does_not_scan(self):
        with patch('hub_service.ports.list_ports.comports') as scan:
            self.assertEqual(resolve_port('/dev/serial/by-id/fixed'), '/dev/serial/by-id/fixed')
            scan.assert_not_called()

    def test_only_user_interface_selected_after_renumber(self):
        for device in ('/dev/ttyACM6', '/dev/ttyCH343USB0'):
            ports = [SimpleNamespace(device=device), SimpleNamespace(device='/dev/ttyACM0')]
            with patch('hub_service.ports.list_ports.comports', return_value=ports), \
                 patch('hub_service.ports.Path.glob', return_value=[]), \
                 patch('hub_service.ports.usb_identity', side_effect=lambda d: (0x1a86, 0x55d5, 6 if d == device else 0)):
                self.assertEqual(resolve_port('auto'), device)

    def test_vendor_tty_found_even_when_pyserial_omits_it(self):
        with patch('hub_service.ports.list_ports.comports', return_value=[]), \
             patch('hub_service.ports.Path.glob', return_value=[Path('/dev/ttyCH343USB0')]), \
             patch('hub_service.ports.usb_identity', return_value=(0x1a86, 0x55d5, 6)):
            self.assertEqual(resolve_port('auto'), '/dev/ttyCH343USB0')

    def test_largest_vendor_suffix_wins_without_interface_filter(self):
        with patch('hub_service.ports.list_ports.comports', return_value=[]), \
             patch('hub_service.ports.Path.glob', return_value=[
                 Path('/dev/ttyCH343USB9'), Path('/dev/ttyCH343USB10'),
                 Path('/dev/ttyCH343USB2'), Path('/dev/ttyCH343USB99.invalid')]), \
             patch('hub_service.ports.usb_identity') as identity:
            self.assertEqual(resolve_port('auto'), '/dev/ttyCH343USB10')
            identity.assert_not_called()

    def test_missing_and_multiple_are_errors(self):
        for devices in ([], ['/dev/ttyACM0', '/dev/ttyACM4']):
            with patch('hub_service.ports.list_ports.comports', return_value=[SimpleNamespace(device=d) for d in devices]), \
                 patch('hub_service.ports.Path.glob', return_value=[]), \
                 patch('hub_service.ports.usb_identity', return_value=(0x1a86, 0x55d5, 6)):
                with self.assertRaises(OSError):
                    resolve_port('auto')

    def test_systemd_uses_actual_checkout_and_retries(self):
        unit = render(Path('/home/rvv/my project'), 'rvv')
        self.assertIn('User=rvv', unit)
        self.assertIn('ExecStart="/home/rvv/my project/.venv/bin/python" -m hub_service', unit)
        self.assertIn('Restart=always', unit)
        self.assertIn('StartLimitIntervalSec=0', unit)


class SupervisorTests(unittest.IsolatedAsyncioTestCase):
    async def test_dead_serial_worker_shuts_down_http_for_systemd_restart(self):
        worker = asyncio.create_task(asyncio.sleep(0))
        await worker
        app = SimpleNamespace(state=SimpleNamespace(service=SimpleNamespace(task=worker)))

        class Server:
            should_exit = False

            async def serve(self):
                while not self.should_exit:
                    await asyncio.sleep(.01)

        server = Server()
        with patch('hub_service.__main__.uvicorn.Config'), \
             patch('hub_service.__main__.uvicorn.Server', return_value=server):
            await asyncio.wait_for(serve(app, SimpleNamespace(host='127.0.0.1', http_port=8001, debug=False)), 2)
        self.assertTrue(server.should_exit)

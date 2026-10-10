import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from hub_service.app import create_app

spec = importlib.util.spec_from_file_location(
    "api_console", Path(__file__).resolve().parents[1] / "scripts" / "api_console.py")
console = importlib.util.module_from_spec(spec)
spec.loader.exec_module(console)


class ConsoleTests(unittest.TestCase):
    def setUp(self):
        self.schema = create_app().openapi()
        self.requests = []
        self.failed_path = None
        self.redirect = False
        self.posts = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                owner.posts.append((self.path, self.headers.get("Authorization"), payload))
                raw = b'{"acknowledged":true}'
                self.send_response(200)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):
                owner.requests.append((self.path, self.headers.get("Authorization")))
                if owner.redirect:
                    self.send_response(302)
                    self.send_header("Location", "/redirect-target")
                    self.end_headers()
                    return
                if self.path == "/openapi.json":
                    payload, status = owner.schema, 200
                elif self.path == owner.failed_path:
                    payload, status = {"error": {"code": "DEVICE_TIMEOUT"}}, 504
                else:
                    payload, status = {"value": 12, "valid": True}, 200
                raw = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.client = console.Client(f"http://127.0.0.1:{self.server.server_port}")

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def test_commands_json_without_auth_and_invalid_index(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertTrue(console.execute(self.client, "temp 2"))
            self.assertTrue(console.execute(self.client, "vacuum"))
        self.assertEqual(self.requests, [("/api/v1/temperature/2", None),
                                         ("/api/v1/vacuum/state", None)])
        self.assertIn('"value": 12', output.getvalue())
        for command in ("temp 3", "lock -1", "vacuum 0", "pwm", "bogus", "get https://evil.test/"):
            with self.assertRaises(ValueError):
                console.execute(self.client, command)
        self.assertEqual(len(self.requests), 2)

    def test_selftest_covers_every_get_and_every_index(self):
        paths = console.selftest_paths(self.schema)
        self.assertEqual(len(paths), 34)
        expected = []
        for path, count in console.COMMANDS.values():
            expected.extend([path] if count is None else [path.format(idx=i) for i in range(count)])
        self.assertEqual(set(paths), set(expected))
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(console.selftest(self.client))
        self.assertEqual([p for p, _ in self.requests], ["/openapi.json"] + paths)

    def test_selftest_continues_after_failure_and_reports(self):
        self.failed_path = "/api/v1/pressure/0"
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertFalse(console.selftest(self.client))
        self.assertEqual(len(self.requests), 35)
        self.assertIn("DEVICE_TIMEOUT", output.getvalue())
        self.assertIn("ошибок 1", output.getvalue())
        self.assertEqual(self.requests[-1][0], "/api/v1/admin/mode")
        self.assertEqual(self.posts, [])

    def test_control_commands_without_auth_and_validation(self):
        with contextlib.redirect_stdout(io.StringIO()):
            for command in ("set_pwm 5 123", "set_mode 2 4", "set_door_lock 1 1", "set_vacuum 0"):
                self.assertTrue(console.execute(self.client, command))
        self.assertEqual(self.posts, [
            ("/api/v1/fans/5/pwm", None, {"pwm": 123}),
            ("/api/v1/admin/mode", None, {"mode": 2, "idx": 4}),
            ("/api/v1/admin/locks/1", None, {"lock_state": True}),
            ("/api/v1/admin/vacuum", None, {"mode": 0}),
        ])
        for command in ("set_pwm 6 1", "set_pwm 0 256", "set_mode 3 0", "set_mode 1 6",
                        "set_door_lock 2 1", "set_vacuum 2", "set_mode 1"):
            with self.assertRaises(ValueError):
                console.execute(self.client, command)
        self.assertEqual(len(self.posts), 4)

    def test_redirect_is_failure_not_followed(self):
        self.redirect = True
        result = self.client.get("/health")
        self.assertFalse(result[0])
        self.assertEqual(result[1], 302)
        self.assertEqual(len(self.requests), 1)

    def test_one_shot_selftest_exit_codes(self):
        command = [sys.executable, str(Path(console.__file__)), "--url", self.client.base_url,
                   "--command", "selftest"]
        env = dict(os.environ, PYTHONIOENCODING="utf-8", MSTJUK_API_TOKEN="obsolete-user",
                   MSTJUK_ADMIN_TOKEN="obsolete-admin")
        result = subprocess.run(command, capture_output=True, encoding="utf-8", env=env, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("ошибок 0", result.stdout)
        self.assertTrue(all(auth is None for _, auth in self.requests))
        self.failed_path = "/api/v1/voc/1"
        result = subprocess.run(command, capture_output=True, encoding="utf-8", env=env, timeout=10)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("ошибок 1", result.stdout)
        result = subprocess.run(command[:-1] + ["temp 99"], capture_output=True,
                                encoding="utf-8", env=env, timeout=10)
        self.assertEqual(result.returncode, 2)

    def test_bad_openapi_and_settings(self):
        for schema in ({}, {"paths": {}}, {"paths": {"/x/{idx}": {"get": {}}}}):
            with self.assertRaises(ValueError):
                console.selftest_paths(schema)
        for url in ("ftp://localhost", "http://user:password@localhost", "http://localhost/prefix"):
            with self.assertRaises(ValueError):
                console.Client(url)
        for timeout in (0, -1, float("nan"), float("inf")):
            with self.assertRaises(ValueError):
                console.Client("http://localhost", timeout)

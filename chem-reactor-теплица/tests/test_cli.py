import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from hub_service.__main__ import config_path, parse_options, read_config
from scripts.build_windows import build_command


class CliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "config.json"
        env = patch.dict("os.environ", {}, clear=True)
        env.start()
        self.addCleanup(env.stop)
        default = patch("hub_service.__main__.config_path", return_value=self.path)
        default.start()
        self.addCleanup(default.stop)

    def write(self, data):
        self.path.write_text(json.dumps(data), encoding="utf-8-sig")

    def test_config_and_cli_precedence(self):
        self.write(dict(serial_port="COM15", http_port=8123, debug=True))
        args = parse_options([])
        self.assertEqual((args.serial_port, args.http_port, args.debug), ("COM15", 8123, True))
        with patch.dict("os.environ", {"MSTJUK_SERIAL_PORT": "COM16"}):
            self.assertEqual(parse_options([]).serial_port, "COM16")
            args = parse_options(["--serial-port", "COM17", "--no-debug"])
        self.assertEqual(args.serial_port, "COM17")
        self.assertFalse(args.debug)

    def test_no_config_keeps_old_cli(self):
        args = parse_options(["--serial-port", "COM13"])
        self.assertEqual(args.settings.serial_port, "COM13")
        self.assertEqual(args.http_port, 8001)

    def test_bad_config_types_and_ranges(self):
        for data in ([], {"serial_port": 15}, {"http_port": True}, {"debug": "false"},
                     {"queue_timeout": False}, {"unknown": 1},
                     {"http_port": 0}, {"exchange_timeout": float("nan")}):
            with self.subTest(data=data):
                self.write(data)
                with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                    parse_options(["--serial-port", "COM13"])
        self.path.write_text("{bad", encoding="utf-8")
        with self.assertRaises(ValueError):
            read_config(self.path)

    def test_explicit_missing_config_is_error(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            parse_options(["--config", str(self.path), "--serial-port", "COM13"])

    def test_list_ports_needs_no_config_or_com(self):
        self.path.write_text("bad", encoding="utf-8")
        self.assertTrue(parse_options(["--list-ports"]).list_ports)

    def test_frozen_path_uses_exe_not_extraction_directory(self):
        exe = str(Path(self.temp.name) / "ChemicalReactor.exe")
        with patch("sys.frozen", True, create=True), patch("sys.executable", exe):
            self.assertEqual(config_path(), self.path)

    def test_build_includes_assets_and_uvicorn_dynamic_imports(self):
        command = build_command(Path("python.exe"), Path("dist with spaces"))
        self.assertIn("--onefile", command)
        self.assertIn("--console", command)
        self.assertEqual(command[command.index("--collect-submodules") + 1], "uvicorn")
        self.assertTrue(command[command.index("--add-data") + 1].endswith(":hub_service/static"))
        self.assertEqual(command[command.index("--distpath") + 1], "dist with spaces")


if __name__ == "__main__":
    unittest.main()

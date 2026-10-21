import contextlib
import io
import unittest
from unittest.mock import patch

from scripts.windows_entry import run


class WindowsEntryTests(unittest.TestCase):
    def check_exit(self, error, expected, interactive, pauses):
        with patch("scripts.windows_entry.freeze_support"), \
             patch("hub_service.__main__.main", side_effect=error), \
             patch("scripts.windows_entry.sys.stdin") as stdin, \
             patch("builtins.input", return_value="") as wait, \
             contextlib.redirect_stderr(io.StringIO()) as stderr:
            stdin.isatty.return_value = interactive
            self.assertEqual(run(), expected)
            self.assertEqual(wait.call_count, pauses)
            return stderr.getvalue()

    def test_config_and_server_exit_codes_pause(self):
        for code in (1, 2, 3):
            with self.subTest(code=code):
                self.check_exit(SystemExit(code), code, True, 1)

    def test_exception_remains_visible(self):
        output = self.check_exit(RuntimeError("port unavailable"), 1, True, 1)
        self.assertIn("RuntimeError: port unavailable", output)

    def test_normal_exit_help_and_ctrl_c_do_not_pause(self):
        for error, code in ((None, 0), (SystemExit(0), 0), (SystemExit(), 0),
                            (KeyboardInterrupt(), 130), (SystemExit(130), 130)):
            with self.subTest(error=error):
                self.check_exit(error, code, True, 0)

    def test_redirected_input_never_waits(self):
        self.check_exit(SystemExit(2), 2, False, 0)

    def test_closed_console_does_not_hide_original_error(self):
        with patch("hub_service.__main__.main", side_effect=SystemExit(3)), \
             patch("scripts.windows_entry.sys.stdin") as stdin, \
             patch("builtins.input", side_effect=EOFError), \
             contextlib.redirect_stderr(io.StringIO()):
            stdin.isatty.return_value = True
            self.assertEqual(run(), 3)

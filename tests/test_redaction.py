"""The bot token must never reach logs or Telegram messages.

It's part of every Bot API URL, and requests puts the URL into exception
messages -- so every place that prints or sends an exception from Telegram
code is a potential leak. One test per real leak path, plus a guard that
fails if a new unredacted {exc} is ever added to those files.

    python -m unittest discover -s tests -v
"""
from __future__ import annotations

import contextlib
import io
import os
import re
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from radar import intake, notify  # noqa: E402
from radar.redact import redact  # noqa: E402

TOKEN = "8123456789:AAEfakeTOKENfakeTOKENfakeTOKEN12345"
ROOT = os.path.join(os.path.dirname(__file__), "..")


def leaking_request(*args, **kwargs):
    url = args[0] if args else kwargs.get("url", "")
    raise intake.requests.ConnectionError(f"Max retries exceeded with url: {url}")


class TokenNeverPrinted(unittest.TestCase):
    def run_quiet(self, fn):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            result = fn()
        return result, buf.getvalue()

    def test_api_connection_error(self):
        with mock.patch.object(intake.requests, "post", leaking_request):
            result, out = self.run_quiet(
                lambda: intake._api(TOKEN, "sendMessage", text="x"))
        self.assertIsNone(result)
        self.assertNotIn(TOKEN, out)
        self.assertIn("***", out)

    def test_api_http_error_logs_telegrams_reason(self):
        r = mock.Mock(status_code=400, ok=False)
        r.json.return_value = {"description": "Bad Request: can't parse entities"}
        with mock.patch.object(intake.requests, "post", return_value=r):
            _, out = self.run_quiet(lambda: intake._api(TOKEN, "sendMessage"))
        self.assertIn("can't parse entities", out)
        self.assertNotIn(TOKEN, out)

    def test_photo_download(self):
        with mock.patch.object(intake, "_api", return_value={"file_path": "p/1.jpg"}), \
                mock.patch.object(intake.requests, "get", leaking_request):
            result, out = self.run_quiet(lambda: intake.download_photo(TOKEN, "f"))
        self.assertIsNone(result)
        self.assertNotIn(TOKEN, out)

    def test_notifier_used_by_the_nightly_index(self):
        with mock.patch.dict(os.environ, {"TG_TOKEN": TOKEN, "TG_CHAT": "1"}):
            n = notify.Notifier()
            with mock.patch.object(notify.requests, "post", leaking_request):
                ok, out = self.run_quiet(lambda: n.send("hello"))
        self.assertFalse(ok)
        self.assertNotIn(TOKEN, out)


class RedactHelper(unittest.TestCase):
    def test_env_values_are_replaced(self):
        with mock.patch.dict(os.environ, {"TG_TOKEN": "short-but-real-token"}):
            self.assertEqual(redact("x short-but-real-token y"), "x *** y")

    def test_token_shaped_values_are_replaced_even_if_not_in_env(self):
        with mock.patch.dict(os.environ, {"TG_TOKEN": ""}):
            self.assertNotIn(TOKEN, redact(f".../bot{TOKEN}/getFile"))
            self.assertNotIn("AIzaSyFAKEfakeFAKEfakeFAKEfakeFAKE12",
                             redact("key=AIzaSyFAKEfakeFAKEfakeFAKEfakeFAKE12"))

    def test_ordinary_text_is_untouched(self):
        self.assertEqual(redact("CHF 4'900, 2012, 150000 km"),
                         "CHF 4'900, 2012, 150000 km")


class NoUnredactedExceptions(unittest.TestCase):
    """Guard: a new print(f"...{exc}") in Telegram code would reopen the leak
    silently. Every exception interpolated there must go through redact()."""

    ALLOWED = ("could not append to",)      # manual.yaml file errors: no secret

    def test_telegram_files_never_interpolate_a_raw_exception(self):
        for rel in ("radar/intake.py", "radar/notify.py"):
            for n, line in enumerate(open(os.path.join(ROOT, rel), encoding="utf-8"), 1):
                if re.search(r"\{exc\}", line) and not any(a in line for a in self.ALLOWED):
                    self.fail(f"{rel}:{n} interpolates a raw exception: {line.strip()}")


class ErrorRepliesAreValidHtml(unittest.TestCase):
    def test_settings_error_escapes_user_input(self):
        from radar import db
        import yaml
        cfg = yaml.safe_load(open(os.path.join(ROOT, "config.yaml"), encoding="utf-8"))
        out = intake.handle_command(db.connect(":memory:"), cfg, "/budget <5000&x>")
        self.assertNotIn("<5000", out)
        self.assertIn("&lt;5000&amp;x&gt;", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)

"""Tests for Gemini model auto-selection.

Identical in job-scout and SwissCarScout, like gemini_models.py itself.
REAL_MODELS is the actual list the API returned on 2026-09-23 -- the rule is
tested against what Google offers, not against a shape assumed in advance,
which is the mistake the grounding safeguard made.

    python -m unittest discover -s tests -v
"""
from __future__ import annotations

import contextlib
import io
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

try:
    from jobscout import ai as aimod, gemini_models as gm  # noqa: E402
except ImportError:
    from radar import ai as aimod, gemini_models as gm     # noqa: E402

REAL_MODELS = """gemini-2.5-flash gemini-2.5-pro gemini-2.5-flash-preview-tts
gemini-2.5-pro-preview-tts gemma-4-26b-a4b-it gemma-4-31b-it gemini-flash-latest
gemini-flash-lite-latest gemini-pro-latest gemini-2.5-flash-lite
gemini-2.5-flash-image gemini-3-flash-preview gemini-3.1-pro-preview
gemini-3.1-pro-preview-customtools gemini-3.1-flash-lite-preview
gemini-3.1-flash-lite gemini-3-pro-image-preview gemini-3-pro-image
nano-banana-pro-preview gemini-3.1-flash-image-preview gemini-3.1-flash-image
gemini-3.1-flash-lite-image gemini-3.5-flash gemini-3.5-flash-lite
gemini-omni-flash-preview gemini-omni-1.1-flash gemini-3.5-transcribe
gemini-3.6-flash gemini-3.7-flash gemini-3.8-flash lyria-3-clip-preview
lyria-3-pro-preview lyria-3.5 gemini-3.1-flash-tts-preview gemini-3.8-flash-tts
gemini-3.8-flash-lite-tts gemini-robotics-er-2-preview
gemini-2.5-computer-use-preview-10-2025 antigravity-preview-05-2026
antigravity-preview-09-2026 antigravity-preview-latest
deep-research-max-preview-04-2026 deep-research-preview-04-2026
deep-research-pro-preview-12-2025""".split()


class Pick(unittest.TestCase):
    def test_real_list_picks_newest_stable_flash(self):
        self.assertEqual(len(REAL_MODELS), 44)
        self.assertEqual(gm.pick(REAL_MODELS)[0], "gemini-3.8-flash")

    def test_versions_compare_as_numbers_not_text(self):
        # As text "3.10" < "3.8" -- the tool would cling to an old model.
        self.assertEqual(gm.pick(["gemini-3.8-flash", "gemini-3.10-flash"])[0],
                         "gemini-3.10-flash")

    def test_other_families_are_never_chosen(self):
        for name in ("gemini-3.8-flash-tts", "gemini-3.1-flash-image",
                     "gemini-3-flash-preview", "gemini-omni-1.1-flash",
                     "gemini-3.5-transcribe", "gemma-4-31b-it",
                     "gemini-2.5-pro", "lyria-3.5"):
            self.assertIsNone(gm.pick([name])[0], name)

    def test_pinned_model_is_respected_while_it_exists(self):
        self.assertEqual(gm.pick(REAL_MODELS, "gemini-2.5-pro")[0], "gemini-2.5-pro")
        self.assertEqual(gm.pick(REAL_MODELS, "models/gemini-2.5-pro")[0],
                         "gemini-2.5-pro")

    def test_retired_pin_falls_back_to_auto(self):
        # exactly the .env still sitting on the VPS
        self.assertEqual(gm.pick(REAL_MODELS, "gemini-2.0-flash")[0],
                         "gemini-3.8-flash")

    def test_fallback_order(self):
        no_flash = [n for n in REAL_MODELS if not gm._TIERS[0][0].match(n)]
        self.assertEqual(gm.pick(no_flash)[0], "gemini-3.5-flash-lite")
        self.assertEqual(gm.pick(["gemini-flash-latest", "gemini-2.5-pro"])[0],
                         "gemini-flash-latest")
        self.assertIsNone(gm.pick(["gemini-2.5-pro", "lyria-3.5"])[0])


def api_list(names, status=200, pages=1):
    """Fake GET /v1beta/models, optionally split across pages."""
    calls = []
    chunks = [names[i::pages] for i in range(pages)]

    def fake_get(url, headers=None, params=None, timeout=None):
        calls.append(dict(headers=headers, params=params))
        r = mock.Mock()
        r.status_code, r.ok = status, status == 200
        page = int((params or {}).get("pageToken") or 0)
        body = {"models": [{"name": f"models/{n}",
                            "supportedGenerationMethods": ["generateContent"]}
                           for n in chunks[page]]}
        if page + 1 < pages:
            body["nextPageToken"] = str(page + 1)
        r.json.return_value = body
        return r
    return fake_get, calls


class Resolve(unittest.TestCase):
    def setUp(self):
        fd, self.cache = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        os.remove(self.cache)
        self._orig = gm.CACHE_PATH
        gm.CACHE_PATH = self.cache
        gm._reset_for_tests()
        self.notices = []

    def tearDown(self):
        gm.CACHE_PATH = self._orig
        gm._reset_for_tests()
        if os.path.exists(self.cache):
            os.remove(self.cache)

    def resolve(self, names, **kw):
        fake, calls = api_list(names)
        with mock.patch.object(gm.requests, "get", fake):
            got = gm.resolve("KEY", notify=self.notices.append, **kw)
        return got, calls

    def test_first_run_picks_and_announces_once(self):
        got, calls = self.resolve(REAL_MODELS)
        self.assertEqual(got, "gemini-3.8-flash")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["headers"], {"x-goog-api-key": "KEY"})
        self.assertEqual(len(self.notices), 1)
        self.assertIn("gemini-3.8-flash", self.notices[0])

        got, calls = self.resolve(REAL_MODELS)       # same process
        self.assertEqual((got, len(calls), len(self.notices)),
                         ("gemini-3.8-flash", 0, 1))

    def test_new_process_reads_cache_without_listing(self):
        self.resolve(REAL_MODELS)
        gm._reset_for_tests()                         # simulate a restart
        got, calls = self.resolve(REAL_MODELS)
        self.assertEqual((got, len(calls), len(self.notices)),
                         ("gemini-3.8-flash", 0, 1))

    def test_retirement_switches_and_says_so(self):
        self.resolve(REAL_MODELS)
        gone = [n for n in REAL_MODELS if n != "gemini-3.8-flash"]
        got, _ = self.resolve(gone, force=True, retired="gemini-3.8-flash")
        self.assertEqual(got, "gemini-3.7-flash")
        self.assertIn("gemini-3.8-flash → gemini-3.7-flash", self.notices[-1])
        self.assertIn("retired", self.notices[-1])

    def test_retired_model_excluded_even_if_still_listed(self):
        # the API can list a model for a while after generateContent 404s it
        got, _ = self.resolve(REAL_MODELS, force=True, retired="gemini-3.8-flash")
        self.assertEqual(got, "gemini-3.7-flash")

    def test_retired_pin_is_announced_with_the_fix(self):
        got, _ = self.resolve(REAL_MODELS, preferred="gemini-2.0-flash")
        self.assertEqual(got, "gemini-3.8-flash")
        self.assertIn("gemini-2.0-flash", self.notices[0])
        self.assertIn("GEMINI_MODEL=auto", self.notices[0])

    def test_list_failure_keeps_last_known_model(self):
        self.resolve(REAL_MODELS)
        gm._reset_for_tests()

        def down(*a, **k):
            raise gm.requests.ConnectionError("down for url ...?key=KEY")
        with mock.patch.object(gm.requests, "get", down), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            got = gm.resolve("KEY", force=True, notify=self.notices.append)
        self.assertEqual(got, "gemini-3.8-flash")
        self.assertNotIn("KEY", out.getvalue())       # type only, no message
        self.assertIn("couldn't reach", gm.status()["problem"])

    def test_list_failure_without_cache_turns_ai_off(self):
        def down(*a, **k):
            raise gm.requests.ConnectionError("x")
        with mock.patch.object(gm.requests, "get", down), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertIsNone(gm.resolve("KEY", notify=self.notices.append))

    def test_rejected_key_is_reported_as_such(self):
        fake, _ = api_list([], status=403)
        with mock.patch.object(gm.requests, "get", fake), \
                contextlib.redirect_stdout(io.StringIO()):
            gm.resolve("KEY", notify=self.notices.append)
        self.assertIn("rejected the key", gm.status()["problem"])

    def test_pagination_is_followed(self):
        fake, calls = api_list(REAL_MODELS, pages=3)
        with mock.patch.object(gm.requests, "get", fake):
            got = gm.resolve("KEY", notify=self.notices.append)
        self.assertEqual((got, len(calls)), ("gemini-3.8-flash", 3))


class ClientRetriesOnRetirement(unittest.TestCase):
    """A generateContent 404 must re-resolve, switch, announce and retry --
    the real failure: gemini-2.0-flash 404'd and nothing noticed."""

    def setUp(self):
        fd, self.cache = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        os.remove(self.cache)
        self._orig = gm.CACHE_PATH
        gm.CACHE_PATH = self.cache
        gm._reset_for_tests()

    def tearDown(self):
        gm.CACHE_PATH = self._orig
        gm._reset_for_tests()
        if os.path.exists(self.cache):
            os.remove(self.cache)

    def test_404_switches_model_and_retries(self):
        listed = list(REAL_MODELS)
        posted = []

        def fake_get(url, headers=None, params=None, timeout=None):
            r = mock.Mock(status_code=200, ok=True)
            r.json.return_value = {"models": [
                {"name": f"models/{n}", "supportedGenerationMethods": ["generateContent"]}
                for n in listed]}
            return r

        def fake_post(url, headers=None, json=None, timeout=None):
            posted.append(url)
            r = mock.Mock()
            if "gemini-3.8-flash:" in url:
                listed.remove("gemini-3.8-flash")     # Google retires it
                r.status_code = 404
            else:
                r.status_code = 200
            return r

        g = aimod.Gemini()
        g.key, g.preferred = "KEY", "auto"
        with mock.patch.object(gm.requests, "get", fake_get), \
                mock.patch.object(aimod.requests, "post", fake_post), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            r = g._post({"contents": []}, 10)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(posted), 2)
        self.assertIn("gemini-3.7-flash:", posted[1])
        self.assertIn("gemini-3.8-flash → gemini-3.7-flash", out.getvalue())

    def test_client_has_no_hardcoded_model_or_key_in_url(self):
        src = open(aimod.__file__, encoding="utf-8").read()
        self.assertNotIn("gemini-2.0-flash\"", src)
        self.assertNotIn('params={"key"', src)


if __name__ == "__main__":
    unittest.main(verbosity=2)

"""Tests for the live /usage fetch and its fallback to the on-disk cache.
Stdlib unittest, no network - urlopen is mocked.

    python -m unittest discover -v
"""
import io
import json
import unittest
from unittest import mock

import usage_sources as us


_SAMPLE = {
    "five_hour": {"utilization": 92, "resets_at": "2026-09-07T18:09:59+00:00"},
    "seven_day": {"utilization": 21, "resets_at": "2026-09-11T18:59:59+00:00"},
    "seven_day_opus": None,
    "limits": [],
}


def _fake_urlopen(payload):
    def _open(req, timeout=0):
        body = json.dumps(payload).encode()
        return mock.MagicMock(
            __enter__=lambda s: io.BytesIO(body),
            __exit__=lambda *a: False)
    return _open


def _reset():
    us._live_usage.update(ts=0.0, util=None, tried=0.0)
    us._LIVE_USAGE_ENABLED = True


class WindowsFromUtilTests(unittest.TestCase):
    def test_extracts_labelled_windows_with_local_reset_ts(self):
        w = {x["key"]: x for x in us._windows_from_util(_SAMPLE)}
        self.assertEqual(w["five_hour"]["pct"], 92.0)
        self.assertEqual(w["seven_day"]["pct"], 21.0)
        self.assertGreater(w["five_hour"]["resets_ts"], 0)
        self.assertNotIn("seven_day_opus", w)          # null block skipped


class FetchLiveUsageTests(unittest.TestCase):
    def setUp(self):
        _reset()
        self._tok = mock.patch.object(us, "_oauth_access_token",
                                      return_value="tok-abc")
        self._tok.start()

    def tearDown(self):
        self._tok.stop()
        _reset()

    def test_parses_bare_utilization_body(self):
        with mock.patch.object(us.urllib.request, "urlopen",
                               _fake_urlopen(_SAMPLE)):
            util = us.fetch_live_usage(force=True)
        self.assertEqual(util["seven_day"]["utilization"], 21)

    def test_parses_wrapped_utilization_body(self):
        with mock.patch.object(us.urllib.request, "urlopen",
                               _fake_urlopen({"utilization": _SAMPLE})):
            util = us.fetch_live_usage(force=True)
        self.assertEqual(util["five_hour"]["utilization"], 92)

    def test_http_failure_returns_none(self):
        def boom(req, timeout=0):
            raise OSError("network down")
        with mock.patch.object(us.urllib.request, "urlopen", boom):
            self.assertIsNone(us.fetch_live_usage(force=True))

    def test_disabled_by_config_returns_none_without_calling(self):
        us._LIVE_USAGE_ENABLED = False
        called = []
        with mock.patch.object(us.urllib.request, "urlopen",
                               lambda *a, **k: called.append(1)):
            self.assertIsNone(us.fetch_live_usage(force=True))
        self.assertEqual(called, [])

    def test_no_token_returns_none(self):
        self._tok.stop()
        with mock.patch.object(us, "_oauth_access_token", return_value=None):
            with mock.patch.object(us.urllib.request, "urlopen",
                                   _fake_urlopen(_SAMPLE)):
                self.assertIsNone(us.fetch_live_usage(force=True))
        self._tok.start()

    def test_result_is_cached_between_calls(self):
        calls = []

        def once(req, timeout=0):
            calls.append(1)
            body = json.dumps(_SAMPLE).encode()
            return mock.MagicMock(__enter__=lambda s: io.BytesIO(body),
                                  __exit__=lambda *a: False)

        with mock.patch.object(us.urllib.request, "urlopen", once):
            us.fetch_live_usage(force=True)
            us.fetch_live_usage()           # within TTL -> no second call
            us.fetch_live_usage()
        self.assertEqual(len(calls), 1)


class DetectFallbackTests(unittest.TestCase):
    def setUp(self):
        _reset()

    def tearDown(self):
        _reset()

    def test_live_ok_is_marked_live(self):
        with mock.patch.object(us, "fetch_live_usage", return_value=_SAMPLE):
            d = us.detect_claude_usage()
        self.assertTrue(d["live"])
        self.assertEqual({w["label"] for w in d["windows"]},
                         {"5-hour", "weekly"})

    def test_falls_back_to_disk_cache_when_live_none(self):
        disk = {"cachedUsageUtilization": {"fetchedAtMs": 1_700_000_000_000,
                                           "utilization": _SAMPLE}}
        with mock.patch.object(us, "fetch_live_usage", return_value=None), \
             mock.patch("builtins.open",
                        mock.mock_open(read_data=json.dumps(disk))):
            d = us.detect_claude_usage()
        self.assertFalse(d["live"])
        self.assertEqual(len(d["windows"]), 2)


if __name__ == "__main__":
    unittest.main()

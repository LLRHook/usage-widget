import io
import json
import tempfile
import unittest
import urllib.error
from email.message import Message
from pathlib import Path
from unittest.mock import MagicMock, patch

import probe_claude


UTILIZATION = "anthropic-ratelimit-unified-5h-utilization"


class ProbeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.cache = Path(self.temp.name) / "cache.json"
        for target, value in (("CACHE", self.cache),):
            mock = patch.object(probe_claude, target, value)
            mock.start()
            self.addCleanup(mock.stop)
        for target, value in (("read_creds", {}), ("refresh_token_if_needed", "fixture-token")):
            mock = patch.object(probe_claude, target, return_value=value)
            mock.start()
            self.addCleanup(mock.stop)
        clock = patch.object(probe_claude.time, "time", return_value=1000)
        clock.start()
        self.addCleanup(clock.stop)

    def write_cache(self, **values):
        data = {UTILIZATION: "0.2", "_fetched_at_unix": 100, **values}
        self.cache.write_text(json.dumps(data), encoding="utf-8")
        return data

    def http_error(self, status, **headers):
        message = Message()
        for key, value in headers.items():
            message[key] = value
        return urllib.error.HTTPError("https://fixture.invalid", status, "fixture", message, io.BytesIO())

    def test_rate_limit_headers_are_kept_and_retry_after_is_honored(self):
        self.write_cache()
        error = self.http_error(429, **{"Retry-After": "600", UTILIZATION: "1.0"})
        with patch.object(probe_claude.urllib.request, "urlopen", side_effect=error) as request:
            result = probe_claude.probe()
            self.assertEqual(result[UTILIZATION], "1.0")
            self.assertEqual(result["_http_status"], 429)
            self.assertTrue(error.closed)
            with patch.object(probe_claude.time, "time", return_value=1400):
                self.assertEqual(probe_claude.probe()[UTILIZATION], "1.0")
            self.assertEqual(request.call_count, 1)

    def test_network_failure_preserves_usage_and_backs_off(self):
        self.write_cache()
        with patch.object(probe_claude.urllib.request, "urlopen", side_effect=urllib.error.URLError("offline")) as request:
            result = probe_claude.probe()
            self.assertEqual(result[UTILIZATION], "0.2")
            probe_claude.probe()
            self.assertEqual(request.call_count, 1)

    def test_cache_reader_ignores_non_object_json(self):
        for value in (None, [], "text", 1):
            with self.subTest(value=value):
                self.cache.write_text(json.dumps(value), encoding="utf-8")
                self.assertEqual(probe_claude._read_cache(), {})

    def test_cache_only_recovers_from_partial_cache_without_network(self):
        self.cache.write_text('{"partial":', encoding="utf-8")
        with patch("sys.argv", ["probe_claude.py", "--no-network"]), patch("sys.stdout", new_callable=io.StringIO) as output:
            self.assertEqual(probe_claude.main(), 0)
        self.assertEqual(json.loads(output.getvalue()), {})

    def test_fresh_cache_needs_no_credentials_or_network(self):
        expected = self.write_cache(_fetched_at_unix=950)
        with patch.object(probe_claude, "read_creds") as credentials, patch.object(
            probe_claude.urllib.request, "urlopen"
        ) as request:
            self.assertEqual(probe_claude.probe(), expected)
        credentials.assert_not_called()
        request.assert_not_called()

    def test_force_refresh_still_respects_retry_after(self):
        self.write_cache(_retry_at_unix=1600)
        with patch.object(probe_claude.urllib.request, "urlopen") as request:
            self.assertEqual(probe_claude.probe(force=True)[UTILIZATION], "0.2")
        request.assert_not_called()

    def test_retry_after_http_date_is_honored_without_usage_headers(self):
        self.write_cache()
        error = self.http_error(429, **{"Retry-After": "Thu, 01 Jan 1970 00:30:00 GMT"})
        with patch.object(probe_claude.urllib.request, "urlopen", side_effect=error) as request:
            result = probe_claude.probe()
            self.assertEqual(result[UTILIZATION], "0.2")
            self.assertEqual(result["_retry_at_unix"], 1800)
            with patch.object(probe_claude.time, "time", return_value=1700):
                probe_claude.probe()
        self.assertEqual(request.call_count, 1)
        self.assertTrue(error.closed)

    def test_success_after_cooldown_replaces_stale_metadata(self):
        self.write_cache()
        response = MagicMock()
        response.__enter__.return_value.headers = {UTILIZATION: "0.4"}
        response.__enter__.return_value.read.return_value = b"{}"
        with patch.object(probe_claude.urllib.request, "urlopen", side_effect=[
            urllib.error.URLError("offline"), response,
        ]) as request:
            probe_claude.probe()
            with patch.object(probe_claude.time, "time", return_value=1299):
                probe_claude.probe()
            self.assertEqual(request.call_count, 1)
            with patch.object(probe_claude.time, "time", return_value=1300):
                result = probe_claude.probe()
        self.assertEqual(request.call_count, 2)
        self.assertEqual(result[UTILIZATION], "0.4")
        self.assertNotIn("_stale_error", result)
        self.assertNotIn("_retry_at_unix", result)
        self.assertEqual(probe_claude._read_cache(), result)
        response.__exit__.assert_called_once()

    def test_unavailable_credentials_preserve_usage_and_back_off(self):
        self.write_cache()
        with patch.object(probe_claude, "read_creds", side_effect=FileNotFoundError("fixture")) as credentials:
            result = probe_claude.probe()
            self.assertEqual(result[UTILIZATION], "0.2")
            self.assertIn("_stale_error", result)
            probe_claude.probe()
        self.assertEqual(credentials.call_count, 1)

    def test_cold_network_failure_is_cached_for_backoff(self):
        with patch.object(probe_claude.urllib.request, "urlopen", side_effect=urllib.error.URLError("offline")) as request:
            self.assertIn("_error", probe_claude.probe())
            probe_claude.probe()
        self.assertEqual(request.call_count, 1)

    def test_failed_atomic_replace_keeps_previous_cache_and_cleans_temporary_file(self):
        expected = self.write_cache()
        with patch.object(probe_claude.os, "replace", side_effect=PermissionError("fixture")):
            probe_claude._write_cache({UTILIZATION: "0.8"})
        self.assertEqual(probe_claude._read_cache(), expected)
        self.assertEqual(list(self.cache.parent.iterdir()), [self.cache])


if __name__ == "__main__":
    unittest.main()

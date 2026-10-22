import io
import json
import multiprocessing
import tempfile
import unittest
import urllib.error
from contextlib import contextmanager
from email.message import Message
from pathlib import Path
from unittest.mock import MagicMock, patch

import probe_claude


UTILIZATION = "anthropic-ratelimit-unified-5h-utilization"


def delayed_probe(cache, ready, release, results, fails):
    """Run a real probe in a separate process, with only I/O seams mocked."""
    def request(*args, **kwargs):
        ready.set()
        if not release.wait(10):
            raise RuntimeError("fixture release timed out")
        if fails:
            raise urllib.error.URLError("fixture offline")
        response = MagicMock()
        response.__enter__.return_value.headers = {UTILIZATION: "0.8"}
        response.__enter__.return_value.read.return_value = b"{}"
        return response

    with patch.object(probe_claude, "CACHE", Path(cache)), patch.object(
        probe_claude, "read_creds", return_value={}
    ), patch.object(probe_claude, "refresh_token_if_needed", return_value="fixture-token"), patch.object(
        probe_claude.time, "time", return_value=1000
    ), patch.object(probe_claude.urllib.request, "urlopen", side_effect=request):
        results.put(probe_claude.probe())


def probe_while_locked(cache, attempting, done, results, fails):
    real_lock = probe_claude._cache_lock

    @contextmanager
    def notified_lock():
        attempting.set()
        with real_lock():
            yield

    with patch.object(probe_claude, "_cache_lock", notified_lock):
        delayed_probe(cache, MagicMock(), MagicMock(), results, fails)
    done.set()


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

    def run_overlapping_probes(self, older_fails):
        self.write_cache()
        context = multiprocessing.get_context("spawn")
        ready, release = context.Event(), context.Event()
        results = context.Queue()
        process = context.Process(target=delayed_probe, args=(
            str(self.cache), ready, release, results, older_fails,
        ))
        process.start()
        try:
            self.assertTrue(ready.wait(10), "older probe did not reach the request")
            response = MagicMock()
            response.__enter__.return_value.headers = {UTILIZATION: "0.8"}
            response.__enter__.return_value.read.return_value = b"{}"
            newer = response if older_fails else urllib.error.URLError("fixture offline")
            with patch.object(probe_claude.urllib.request, "urlopen", side_effect=[newer]):
                probe_claude.probe()
            release.set()
            result = results.get(timeout=10)
            process.join(10)
            self.assertEqual(process.exitcode, 0)
            self.assertEqual(result[UTILIZATION], "0.8")
            self.assertEqual(result["_fetched_at_unix"], 1000)
            self.assertNotIn("_retry_at_unix", result)
            self.assertNotIn("_stale_error", result)
            self.assertEqual(probe_claude._read_cache(), result)
            with patch.object(probe_claude.urllib.request, "urlopen") as request:
                self.assertEqual(probe_claude.probe(), result)
            request.assert_not_called()
        finally:
            release.set()
            if process.is_alive():
                process.terminate()
            process.join(10)
            results.close()
            results.join_thread()

    def test_older_failed_process_preserves_newer_success_without_cooldown(self):
        self.run_overlapping_probes(older_fails=True)

    def test_success_replaces_an_overlapping_failure_and_its_cooldown(self):
        self.run_overlapping_probes(older_fails=False)

    def test_success_and_failure_updates_wait_for_the_same_process_lock(self):
        context = multiprocessing.get_context("spawn")
        for fails in (False, True):
            with self.subTest(fails=fails):
                self.write_cache()
                attempting, done = context.Event(), context.Event()
                results = context.Queue()
                process = context.Process(target=probe_while_locked, args=(
                    str(self.cache), attempting, done, results, fails,
                ))
                try:
                    with probe_claude._cache_lock():
                        process.start()
                        self.assertTrue(attempting.wait(10))
                        self.assertFalse(done.wait(0.1), "cache writer bypassed the process lock")
                        probe_claude._write_cache({
                            UTILIZATION: "0.8", "_fetched_at_unix": 1000,
                            "_measurement_generation": 1,
                        })
                    result = results.get(timeout=10)
                    process.join(10)
                    self.assertEqual(process.exitcode, 0)
                    self.assertEqual(result[UTILIZATION], "0.8")
                    self.assertNotIn("_retry_at_unix", result)
                    self.assertEqual(probe_claude._read_cache(), result)
                finally:
                    if process.is_alive():
                        process.terminate()
                    if process.pid is not None:
                        process.join(10)
                    results.close()
                    results.join_thread()

    def test_forced_success_at_the_same_timestamp_supersedes_older_failure(self):
        response = MagicMock()
        response.__enter__.return_value.headers = {UTILIZATION: "0.2"}
        response.__enter__.return_value.read.return_value = b"{}"
        with patch.object(probe_claude.urllib.request, "urlopen", return_value=response):
            old = probe_claude.probe()
            response.__enter__.return_value.headers = {UTILIZATION: "0.8"}
            fresh = probe_claude.probe(force=True)
        result = probe_claude._failed_probe(old, "fixture offline", 1000)
        self.assertEqual(result, fresh)
        self.assertEqual(result[UTILIZATION], "0.8")
        self.assertNotIn("_retry_at_unix", result)

    def test_overlapping_failures_keep_the_longest_cooldown(self):
        cached = self.write_cache()
        probe_claude._failed_probe(cached, "long retry", 1000, {"Retry-After": "900"})
        result = probe_claude._failed_probe(cached, "short retry", 1000)
        self.assertEqual(result["_retry_at_unix"], 1900)
        self.assertEqual(result[UTILIZATION], "0.2")

    def test_lock_failure_keeps_cache_and_returns_offline_fallback(self):
        cached = self.write_cache()
        with patch.object(probe_claude, "_cache_lock", side_effect=PermissionError("fixture")):
            result = probe_claude._failed_probe(cached, "fixture offline", 1000)
            self.assertEqual(result[UTILIZATION], "0.2")
            self.assertEqual(result["_retry_at_unix"], 1300)
            response = MagicMock()
            response.__enter__.return_value.headers = {UTILIZATION: "0.8"}
            response.__enter__.return_value.read.return_value = b"{}"
            with patch.object(probe_claude.urllib.request, "urlopen", return_value=response):
                self.assertEqual(probe_claude.probe()[UTILIZATION], "0.8")
        self.assertEqual(probe_claude._read_cache(), cached)

    def test_windows_lock_releases_the_same_byte_after_an_exception(self):
        windows = MagicMock(LK_LOCK=1, LK_UNLCK=0)
        with patch.object(probe_claude.sys, "platform", "win32"), patch.dict(
            "sys.modules", {"msvcrt": windows}
        ):
            with self.assertRaisesRegex(RuntimeError, "fixture"):
                with probe_claude._cache_lock():
                    raise RuntimeError("fixture")
        calls = windows.locking.call_args_list
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0].args[1:], (windows.LK_LOCK, 1))
        self.assertEqual(calls[1].args, (calls[0].args[0], windows.LK_UNLCK, 1))


if __name__ == "__main__":
    unittest.main()

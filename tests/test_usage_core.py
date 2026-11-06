"""Actual ctypes/native parity and adapter tests. Every usage root is synthetic."""

import ctypes
import io
import json
import math
import os
import random
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import build_native
import usage_core
import widget
from tests import claude_reference as reference

FIXED = datetime(2026, 11, 2, 15, 30, tzinfo=timezone.utc)
REPO = Path(__file__).resolve().parents[1]


class FrozenDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        return FIXED.astimezone(tz) if tz is not None else FIXED.astimezone().replace(tzinfo=None)


class NativeParityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="usage-core-synthetic-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "claude"
        self.root.mkdir()
        self.codex = Path(self.temp.name) / "codex"
        self.codex.mkdir()
        self.cache = Path(self.temp.name) / "cache.json"
        self.cache.write_text("{}", encoding="utf-8")
        for module in (widget, reference):
            clock = patch.object(module, "datetime", FrozenDateTime)
            clock.start()
            self.addCleanup(clock.stop)
        roots = patch.object(reference, "CLAUDE_PROJECTS", self.root)
        roots.start()
        self.addCleanup(roots.stop)

    def write(self, records, project="C--demo-fixture", name="session.jsonl"):
        path = self.root / project / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(json.dumps(r) for r in records), encoding="utf-8")
        return path

    def message(self, ident="fixture", *, timestamp=None, usage=None, **changes):
        record = {
            "timestamp": timestamp if timestamp is not None else FIXED.isoformat(),
            "sessionId": "session-fixture",
            "message": {"id": ident, "model": "claude-sonnet", "usage": usage if usage is not None else {
                "input_tokens": 100, "output_tokens": 7, "cache_creation_input_tokens": 31, "cache_read_input_tokens": 59,
            }},
        }
        record["message"].update(changes)
        return record

    def compare(self, windows=(5, 168)):
        expected = reference.scan_claude()
        actual, counts = widget._scan_claude_batch(self.root, now=FIXED.astimezone(), windows=windows)
        self.assertEqual(actual, expected)
        self.assertEqual(list(actual["by_day"]), list(expected["by_day"]))
        self.assertEqual(list(actual["by_model"]), list(expected["by_model"]))
        self.assertEqual(counts, [reference.claude_window_tokens(h) for h in windows])
        for hours, count in zip(windows, counts):
            self.assertEqual(widget.claude_window_tokens(hours, self.root, now=FIXED.astimezone()), count)
        return actual, counts

    def test_all_buckets_prices_sessions_and_tools_match_legacy(self):
        for index, model in enumerate(("opus", "SONNET", "haiku", "future-model", None)):
            records = [self.message(f"m-{index}", model=model, content=[
                {"type": "tool_use", "name": "Read"}, {"type": "tool_use", "name": "Edit"},
                {"type": "tool_use", "name": 3}, {"type": "tool_use"}, "bad-block",
            ])]
            records[0]["sessionId"] = str(index)
            self.write(records, project=f"C--project-{index}")
        result, _ = self.compare()
        self.assertEqual(result["totals"]["messages"], 5)
        self.assertEqual(result["totals"]["sessions"], 5)
        self.assertEqual(result["tools"][0], {"name": "Read", "count": 5})

    def test_window_dedup_validates_timestamp_before_claiming_id(self):
        invalid = self.message("bad-first", timestamp="invalid")
        old = self.message("old-first", timestamp=(FIXED - timedelta(days=9)).isoformat())
        duplicate = self.message("twice", content=[{"type": "tool_use", "name": "Read"}])
        self.write([invalid, self.message("bad-first"), old, self.message("old-first"), duplicate, duplicate])
        result, windows = self.compare(windows=(1, 5, 168, -1, 0))
        self.assertEqual(result["totals"]["messages"], 2)
        self.assertEqual(result["tools"], [{"name": "Read", "count": 2}])
        self.assertEqual(windows[:3], [197 * 3] * 3)
        self.assertEqual(windows[3], 0)

    def test_window_and_burn_boundaries_are_inclusive_and_allow_future_records(self):
        cutoffs = (timedelta(hours=1), timedelta(hours=5), timedelta(hours=168))
        times = [FIXED - d + timedelta(microseconds=offset) for d in cutoffs for offset in (-1, 0, 1)]
        times += [FIXED + timedelta(days=1)]
        self.write([self.message(str(i), timestamp=ts.isoformat()) for i, ts in enumerate(times)])
        self.compare(windows=(0, 1, 5, 168, 0.5))

    def test_midnight_dst_offsets_naive_and_old_dates_use_host_local_interpretation(self):
        self.write([self.message(str(i), timestamp=ts) for i, ts in enumerate([
            "2026-11-01T01:30:00-04:00", "2026-11-01T01:30:00-05:00",
            "2026-11-02T00:01:00+14:00", "2026-11-01T23:59:00-12:00",
            "2026-11-02T13:00:00", "2026-10-01T01:00:00Z", "not-a-date", 123,
        ])])
        self.compare()

    def test_signed_large_and_coerced_tokens_preserve_python_integer_behavior(self):
        values = (2**100 + 17, -(2**95), " 123_456 ", True, None, 1.9, [], {}, "NaN", float("inf"))
        self.write([self.message(str(i), usage={
            "input_tokens": value, "output_tokens": -9, "cache_read_input_tokens": 2**80 + i,
        }) for i, value in enumerate(values)])
        result, _ = self.compare()
        self.assertGreater(result["totals"]["cache_r"], 2**64)

    def test_numeric_rounding_matches_python_at_binary64_boundaries(self):
        values = [2**e + delta for e in (53, 63, 80, 100, 300) for delta in (-3, -1, 0, 1, 3)]
        self.write([self.message(str(i), usage={
            "input_tokens": n, "output_tokens": n // 7, "cache_creation_input_tokens": n // 11,
            "cache_read_input_tokens": n // 17,
        }, model=("opus", "haiku", "sonnet")[i % 3]) for i, n in enumerate(values)])
        self.compare()

    def test_duplicate_extreme_timestamp_does_not_introduce_a_summary_error(self):
        # Offset conversion underflows year 1, but the summary has already seen
        # this ID and must skip it before interpreting the timestamp.
        self.write([self.message("same"), self.message("same", timestamp="0001-01-01T00:00:00+23:59")])
        expected = reference.scan_claude()
        self.assertEqual(widget.scan_claude(self.root), expected)
        with self.assertRaises((ValueError, OverflowError)):
            reference.claude_window_tokens(5)
        with self.assertRaises((ValueError, OverflowError)):
            widget.claude_window_tokens(5, self.root)

    def test_cost_overflow_raises_but_window_only_counts_remain_unbounded(self):
        self.write([self.message(usage={"input_tokens": 10**400})])
        with self.assertRaises(OverflowError):
            reference.scan_claude()
        with self.assertRaises(OverflowError):
            widget.scan_claude(self.root)
        self.assertEqual(widget.claude_window_tokens(5, self.root), 10**400)

    def test_nonfinite_cost_intermediates_are_not_changed_to_null(self):
        self.write([self.message(usage={"input_tokens": 10**308})])
        result, _ = self.compare()
        self.assertEqual(result["totals"]["cost"], math.inf)

    def test_stable_top_six_projects_and_top_eight_tools_keep_encounter_order(self):
        for i in range(10):
            self.write([self.message(str(i), content=[{"type": "tool_use", "name": f"tool-{i}"}])], project=f"project-{i}")
        result, _ = self.compare()
        self.assertEqual(len(result["projects"]), 6)
        self.assertEqual(len(result["tools"]), 8)

    def test_unicode_nul_and_lone_surrogates_remain_opaque(self):
        record = self.message("id\x00\ud800", content=[{"type": "tool_use", "name": "工具\udfff\x00"}])
        record["sessionId"] = "会话\ud800"
        self.write([record, record, self.message("id\ud801")], project="C--demo-é-工具")
        result, _ = self.compare()
        self.assertEqual(result["tools"][0]["name"], "工具\udfff\x00")

    def test_malformed_lines_nested_shapes_and_replacement_utf8_match(self):
        path = self.write([None, [], 3, {"message": ["bad"]}, {"message": {"content": [None, {}]}},
                           self.message("empty", usage={}), self.message("malformed", usage="bad"), self.message()])
        with path.open("ab") as stream:
            stream.write(b'\n{"message":{"content":[{"type":"tool_use","name":"bad-\xff"}]}}\n{"unfinished":')
        self.compare()

    def test_empty_missing_and_unreadable_files_preserve_found_shapes(self):
        self.compare()
        missing = self.root / "absent"
        with patch.object(reference, "CLAUDE_PROJECTS", missing):
            self.assertEqual(widget.scan_claude(missing), reference.scan_claude())
            self.assertEqual(widget.claude_window_tokens(5, missing), 0)
        # A directory ending .jsonl raises OSError on open on every host.
        (self.root / "unreadable.jsonl").mkdir()
        self.compare()

    def test_seeded_mixed_history_has_exact_oracle_parity(self):
        rng = random.Random(20261106)
        records = []
        for i in range(300):
            record = self.message(str(rng.randrange(250)), timestamp=(
                FIXED + timedelta(minutes=rng.randrange(-12000, 100))
            ).isoformat(), model=rng.choice(("opus", "sonnet", "haiku", "unknown")), usage={
                key: rng.choice((None, "bad", -3, True, rng.randrange(1000000)))
                for key in ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
            }, content=[{"type": "tool_use", "name": rng.choice(("Read", "Write", "?"))}])
            record["sessionId"] = f"s-{i % 11}"
            records.append(record)
        self.write(records)
        self.compare()

    def test_normal_cli_reads_claude_history_once_and_uses_real_native_batch(self):
        self.write([self.message()])
        with patch.object(widget, "iter_jsonl", wraps=widget.iter_jsonl) as read, patch.object(
            usage_core, "_request", wraps=usage_core._request
        ) as native, patch("sys.argv", [
            "widget.py", "--json", "--claude-projects", str(self.root),
            "--codex-sessions", str(self.codex), "--claude-cache", str(self.cache),
        ]), patch("sys.stdout", new_callable=io.StringIO) as output:
            self.assertEqual(widget.main(), 0)
        self.assertEqual(read.call_count, 1)
        self.assertEqual(native.call_count, 1)
        data = json.loads(output.getvalue())
        self.assertEqual(data["claude"]["totals"]["messages"], 1)
        self.assertEqual(data["windows"]["claude_5h_tokens"], 197)

    def test_concurrent_collectors_have_independent_native_state(self):
        self.write([self.message(str(i)) for i in range(20)])
        expected = reference.scan_claude()
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: widget.scan_claude(self.root), range(12)))
        self.assertEqual(results, [expected] * 12)

    def collector_command(self):
        return [sys.executable, str(REPO / "widget.py"), "--json", "--claude-projects", str(self.root),
                "--codex-sessions", str(self.codex), "--claude-cache", str(self.cache)]

    def test_subprocess_json_and_summary_use_packaged_library_from_another_cwd(self):
        self.write([self.message(timestamp=datetime.now(timezone.utc).isoformat())])
        run = subprocess.run(self.collector_command(), cwd=self.temp.name, capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(run.stdout)["windows"]["claude_week_tokens"], 197)
        command = [arg for arg in self.collector_command() if arg != "--json"] + ["--summary"]
        run = subprocess.run(command, cwd=self.temp.name, capture_output=True, text=True, check=True)
        self.assertIn("Claude today: 197 tokens", run.stdout)

    def test_dashboard_and_tray_adapters_reach_actual_synthetic_native_collector(self):
        # Reuse the existing GUI-free imports, while allowing the real collector
        # subprocess. Only the probe is mocked; it never reads credentials.
        from tests.test_widget_native import native
        from tests.test_tray import tray
        self.write([self.message(timestamp=datetime.now(timezone.utc).isoformat())])
        real_output = subprocess.check_output

        def output(command, **kwargs):
            if Path(command[1]).name == "probe_claude.py":
                return "{}"
            self.assertEqual(Path(command[1]), REPO / "widget.py")
            return real_output(self.collector_command(), cwd=self.temp.name, **kwargs)

        with patch.object(native, "find_python", return_value=Path(sys.executable)), patch.object(
            tray, "find_python", return_value=Path(sys.executable)
        ), patch.object(subprocess, "check_output", side_effect=output):
            data = native.query()
            self.assertEqual(data["claude"]["today"]["tokens"], 197)
            snapshot = tray.Stats().refresh()
            self.assertEqual(snapshot["claude_5h"]["tokens"], 197)
            self.assertTrue(snapshot["claude_5h"]["fallback"])


class NativeBoundaryTests(unittest.TestCase):
    def tearDown(self):
        usage_core._load_library.cache_clear()

    def test_invalid_batches_are_rejected_by_actual_ffi_and_next_request_succeeds(self):
        for payload in (b"{broken", b"{}", b"[]", b"null", b"\xff", b'{"version":2}'):
            with self.subTest(payload=payload), self.assertRaises(usage_core.NativeCoreError):
                usage_core._request(payload)
        result = usage_core.aggregate_claude([], widget.CLAUDE_PRICES, 0)
        self.assertEqual(result["summary"]["totals"]["messages"], 0)

    def test_missing_and_wrong_architecture_library_report_rebuild_without_fallback(self):
        with tempfile.TemporaryDirectory(prefix="usage-core-library-") as directory:
            path = Path(directory) / usage_core.library_name()
            for exists in (False, True):
                if exists:
                    path.write_text("synthetic invalid library", encoding="utf-8")
                usage_core._load_library.cache_clear()
                with patch.object(usage_core, "library_path", return_value=path), self.assertRaisesRegex(
                    usage_core.NativeCoreError, "run python build_native.py"
                ):
                    usage_core._load_library()

    def test_library_lookup_is_architecture_specific_and_app_relative(self):
        cases = [("AMD64", 8, "win32", "win32-x86_64-64", "ai_usage_core.dll"),
                 ("AMD64", 4, "win32", "win32-x86-32", "ai_usage_core.dll"),
                 ("arm64", 8, "darwin", "darwin-aarch64-64", "libai_usage_core.dylib"),
                 ("x86_64", 8, "linux", "linux-x86_64-64", "libai_usage_core.so")]
        for machine, size, system, key, name in cases:
            with self.subTest(key=key), patch.object(usage_core.platform, "machine", return_value=machine), patch.object(
                usage_core.ctypes, "sizeof", return_value=size
            ), patch.object(usage_core.sys, "platform", system):
                self.assertEqual(usage_core.library_path(), REPO / "native" / "lib" / key / name)

    def test_windows_emulation_lookup_uses_python_build_architecture(self):
        with patch.object(usage_core.sys, "platform", "win32"), patch.object(
            usage_core.platform, "machine", return_value="ARM64"
        ), patch.object(usage_core.sysconfig, "get_platform", return_value="win-amd64"), patch.object(
            usage_core.ctypes, "sizeof", return_value=8
        ):
            self.assertEqual(usage_core.platform_key(), "win32-x86_64-64")

    def test_abi_mismatch_and_missing_exports_are_recoverable(self):
        for version in (2, None):
            usage_core._load_library.cache_clear()
            lib = MagicMock()
            if version is None:
                del lib.ai_usage_core_version
            else:
                lib.ai_usage_core_version.return_value = version
            with patch.object(usage_core.ctypes, "CDLL", return_value=lib), self.assertRaises(
                usage_core.NativeCoreError
            ):
                usage_core._load_library()

    def test_actual_response_is_freed_even_if_host_decode_fails(self):
        lib = usage_core._load_library()
        for raw in (b"not-json", b"[]", b'{"version":99}', b'{"version":1}'):
            with self.subTest(raw=raw), patch.object(usage_core.ctypes, "string_at", return_value=raw), patch.object(
                lib, "ai_usage_core_free", wraps=lib.ai_usage_core_free
            ) as free, self.assertRaises(usage_core.NativeCoreError):
                usage_core._request(b"{}")
            free.assert_called_once()
        self.assertEqual(usage_core.aggregate_claude([], {}, 0)["summary"]["totals"]["input"], 0)

    def test_native_failure_keeps_cli_stdout_empty_and_returns_failure(self):
        with tempfile.TemporaryDirectory(prefix="usage-core-missing-") as directory:
            root = Path(directory)
            with patch.object(usage_core, "library_path", return_value=root / "missing"), patch(
                "sys.argv", ["widget.py", "--json", "--claude-projects", str(root),
                             "--codex-sessions", str(root), "--claude-cache", str(root / "cache")]
            ), patch("sys.stdout", new_callable=io.StringIO) as out, patch("sys.stderr", new_callable=io.StringIO) as err:
                usage_core._load_library.cache_clear()
                self.assertEqual(widget.main(), 1)
            self.assertEqual(out.getvalue(), "")
            self.assertIn("build_native.py", err.getvalue())

    def test_build_rejects_cross_target_override_before_cargo_runs(self):
        with patch.dict(os.environ, {"CARGO_BUILD_TARGET": "synthetic-target"}), patch.object(
            build_native.subprocess, "run"
        ) as run, self.assertRaisesRegex(SystemExit, "unset CARGO_BUILD_TARGET"):
            build_native.main()
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()

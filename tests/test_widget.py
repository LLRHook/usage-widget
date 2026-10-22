import io
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import widget


class CodexScanTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.sessions = patch.object(widget, "CODEX_SESSIONS", self.root)
        self.sessions.start()
        self.addCleanup(self.sessions.stop)

    def write_records(self, records):
        path = self.root / "rollout-fixture.jsonl"
        path.write_text("\n".join(json.dumps(record) for record in records), encoding="utf-8")
        return path

    def event(self, tokens, timestamp="2026-10-22T12:00:00Z", **payload):
        info = None if tokens is None else {
            "total_token_usage": {"input_tokens": tokens, "total_tokens": tokens}
        }
        return {
            "type": "event_msg", "timestamp": timestamp,
            "payload": {"type": "token_count", "info": info, **payload},
        }

    def test_rate_limit_only_events_do_not_reset_usage_counters(self):
        rate = {"primary": {"used_percent": 25, "resets_at": 1792700000}}
        self.write_records([
            self.event(100),
            self.event(None, rate_limits=rate),
            self.event(150),
        ])
        result = widget.scan_codex()
        self.assertEqual(result["totals"]["tokens"], 150)
        self.assertEqual(result["rate_limits"], rate)
        self.assertEqual(sum(row["tokens"] for row in result["by_day"].values()), 150)

    def test_rate_limit_only_session_does_not_create_usage(self):
        self.write_records([self.event(None, rate_limits={"primary": {"used_percent": 42}})])
        result = widget.scan_codex()
        self.assertEqual(result["totals"]["sessions"], 0)
        self.assertEqual(result["by_day"], {})
        self.assertEqual(result["by_model"], {})
        self.assertEqual(result["rate_limits"]["primary"]["used_percent"], 42)

    def test_jsonl_ignores_non_object_values_and_partial_lines(self):
        path = self.write_records([None, [], 2, "text", self.event(10)])
        with path.open("a", encoding="utf-8") as stream:
            stream.write('\n{"unfinished":')
        self.assertEqual(list(widget.iter_jsonl(path)), [self.event(10)])
        self.assertEqual(widget.scan_codex()["totals"]["tokens"], 10)

    def test_timestamp_parser_rejects_non_strings(self):
        for value in (5, [], {}, True):
            with self.subTest(value=value):
                self.assertIsNone(widget.parse_ts(value))

    def test_usage_deltas_are_attributed_to_the_day_they_occur(self):
        midnight = datetime(2026, 10, 22).astimezone()
        before = midnight - timedelta(minutes=1)
        after = midnight + timedelta(minutes=1)
        self.write_records([self.event(100, before.isoformat()), self.event(150, after.isoformat())])
        result = widget.scan_codex()
        self.assertEqual(result["by_day"][before.strftime("%Y-%m-%d")]["tokens"], 100)
        self.assertEqual(result["by_day"][after.strftime("%Y-%m-%d")]["tokens"], 50)
        self.assertEqual(result["totals"]["sessions"], 1)

    def test_real_counter_resets_still_count_the_new_usage(self):
        self.write_records([self.event(100), self.event(25), self.event(50)])
        self.assertEqual(widget.scan_codex()["totals"]["tokens"], 150)

    def test_empty_or_malformed_usage_does_not_reset_the_baseline(self):
        self.write_records([
            self.event(100),
            {"type": "event_msg", "payload": ["invalid"]},
            self.event(None, info=["invalid"]),
            self.event(None, info={"total_token_usage": []}),
            self.event(None, info={"total_token_usage": {}}),
            self.event(150),
        ])
        self.assertEqual(widget.scan_codex()["totals"]["tokens"], 150)

    def test_rate_limit_timestamps_with_and_without_offsets_can_be_merged(self):
        local_timestamp = datetime(2026, 10, 22, 13, tzinfo=timezone.utc).astimezone().replace(tzinfo=None).isoformat()
        self.write_records([
            self.event(None, "2026-10-22T12:00:00Z", rate_limits={"primary": {"used_percent": 20}}),
            self.event(None, local_timestamp, rate_limits={"primary": {"used_percent": 30}}),
        ])
        self.assertEqual(widget.scan_codex()["rate_limits"]["primary"]["used_percent"], 30)

    def test_json_cli_combines_only_temporary_fixture_sources(self):
        claude_root = self.root / "claude"
        claude_root.mkdir()
        timestamp = datetime.now().astimezone().isoformat()
        (claude_root / "fixture.jsonl").write_text(json.dumps({
            "timestamp": timestamp,
            "message": {"id": "fixture", "usage": {"input_tokens": 10, "output_tokens": 2}},
        }), encoding="utf-8")
        self.write_records([self.event(100, timestamp), self.event(None, timestamp), self.event(150, timestamp)])
        (self.root / "claude_ratelimits.json").write_text(json.dumps({
            "anthropic-ratelimit-unified-5h-utilization": "0.25",
        }), encoding="utf-8")
        with patch.object(widget, "CLAUDE_PROJECTS", claude_root), patch(
            "tempfile.gettempdir", return_value=str(self.root)
        ), patch("sys.argv", ["widget.py", "--json"]), patch("sys.stdout", new_callable=io.StringIO) as output:
            self.assertEqual(widget.main(), 0)
        view = json.loads(output.getvalue())
        self.assertEqual(view["claude"]["today"]["tokens"], 12)
        self.assertEqual(view["codex"]["today"]["tokens"], 150)
        self.assertEqual(view["windows"]["claude_5h_tokens"], 12)
        self.assertEqual(view["windows"]["claude_plan"]["5h-utilization"], "0.25")


class ClaudeScanTests(unittest.TestCase):
    def test_malformed_nested_records_do_not_hide_later_usage(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            records = [
                {"message": ["invalid"]},
                {"message": {"usage": "invalid"}, "timestamp": "2026-10-22T12:00:00Z"},
                {"message": {"usage": {"input_tokens": "invalid"}}, "timestamp": "2026-10-22T12:00:00Z"},
                {"message": {"id": "fixture", "usage": {"input_tokens": 10, "output_tokens": 2}},
                 "timestamp": datetime.now().astimezone().isoformat()},
            ]
            (root / "fixture.jsonl").write_text("\n".join(json.dumps(r) for r in records), encoding="utf-8")
            with patch.object(widget, "CLAUDE_PROJECTS", root):
                self.assertEqual(widget.scan_claude()["totals"]["input"], 10)
                self.assertEqual(widget.claude_window_tokens(5), 12)


class PercentageTests(unittest.TestCase):
    def test_invalid_percentages_remain_unknown_and_valid_values_are_bounded(self):
        for value in (None, "invalid", "NaN", float("inf"), []):
            with self.subTest(value=value):
                self.assertIsNone(widget.usage_percent(value))
        self.assertEqual(widget.usage_percent("0.25", 100), 25)
        self.assertEqual(widget.usage_percent(0), 0)
        self.assertEqual(widget.usage_percent(-2), 0)
        self.assertEqual(widget.usage_percent(130), 100)


class RateLimitMergeTests(unittest.TestCase):
    def test_samples_without_reset_times_do_not_carry_usage_across_unknown_windows(self):
        candidates = [
            (widget.parse_ts("2026-10-21T12:00:00Z"), {"primary": {"used_percent": 99}}),
            (widget.parse_ts("2026-10-22T12:00:00Z"), {"primary": {"used_percent": 20}}),
        ]
        merged, _ = widget.merge_codex_rate_limits(candidates)
        self.assertEqual(merged["primary"]["used_percent"], 20)

    def test_malformed_sample_does_not_hide_valid_same_window_high_water_mark(self):
        candidates = [
            (widget.parse_ts("2026-10-22T10:00:00Z"), {"primary": {"used_percent": "invalid", "resets_at": 2000}}),
            (widget.parse_ts("2026-10-22T11:00:00Z"), {"primary": {"used_percent": 80, "resets_at": 2000}}),
            (widget.parse_ts("2026-10-22T12:00:00Z"), {"primary": {"used_percent": 25, "resets_at": 2000}}),
        ]
        merged, _ = widget.merge_codex_rate_limits(candidates)
        self.assertEqual(merged["primary"]["used_percent"], 80)

    def test_new_reset_window_does_not_inherit_previous_window_usage(self):
        candidates = [
            (widget.parse_ts("2026-10-22T10:00:00Z"), {"primary": {"used_percent": 100, "resets_at": 1000}}),
            (widget.parse_ts("2026-10-22T12:00:00Z"), {"primary": {"used_percent": 5, "resets_at": 2000}}),
        ]
        merged, _ = widget.merge_codex_rate_limits(candidates)
        self.assertEqual(merged["primary"]["used_percent"], 5)

    def test_invalid_bucket_shapes_are_omitted(self):
        candidates = [(widget.parse_ts("2026-10-22T12:00:00Z"), {"primary": "invalid"})]
        merged, _ = widget.merge_codex_rate_limits(candidates)
        self.assertNotIn("primary", merged)


if __name__ == "__main__":
    unittest.main()

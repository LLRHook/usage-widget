"""Independent Python oracle retained from the pre-native collector.

Tests must inject a synthetic CLAUDE_PROJECTS and clock before scanning.
This module is never imported by the application.
"""

import json
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable

CLAUDE_PROJECTS = None  # Test-only: no default personal usage root.

CLAUDE_PRICES = {
    "opus":   {"in": 15.00, "cache_w": 18.75, "cache_r": 1.50, "out": 75.00},
    "sonnet": {"in":  3.00, "cache_w":  3.75, "cache_r": 0.30, "out": 15.00},
    "haiku":  {"in":  0.80, "cache_w":  1.00, "cache_r": 0.08, "out":  4.00},
}


def family(model: str) -> str:
    m = model.lower() if isinstance(model, str) else ""
    if "opus" in m:
        return "opus"
    if "sonnet" in m:
        return "sonnet"
    if "haiku" in m:
        return "haiku"
    return "sonnet"


def parse_ts(s: str) -> datetime | None:
    if not isinstance(s, str) or not s:
        return None
    try:
        parsed = datetime.fromisoformat(s.replace("Z", "+00:00"))
        # Preserve the local interpretation of unqualified timestamps, but
        # make them comparable with the offset-aware timestamps in other files.
        return parsed if parsed.tzinfo is not None else parsed.astimezone()
    except ValueError:
        return None


def iter_jsonl(path: Path) -> Iterable[dict]:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                    if isinstance(record, dict):
                        yield record
                except json.JSONDecodeError:
                    continue
    except OSError:
        return


def scan_claude() -> dict:
    """Walk ~/.claude/projects/**/*.jsonl, sum usage by day/model/project/hour."""
    by_day: dict[str, dict] = defaultdict(lambda: {
        "input": 0, "output": 0, "cache_w": 0, "cache_r": 0,
        "cost": 0.0, "messages": 0, "by_model": defaultdict(int),
    })
    by_model: dict[str, dict] = defaultdict(lambda: {
        "input": 0, "output": 0, "cache_w": 0, "cache_r": 0, "cost": 0.0, "messages": 0,
    })
    by_project: dict[str, dict] = defaultdict(lambda: {"tokens": 0, "cost": 0.0, "messages": 0})
    # 7d x 24h heatmap — keys are (day_offset, hour) in local time, value = total tokens
    heatmap: dict[tuple[int, int], int] = defaultdict(int)
    tool_use: dict[str, int] = defaultdict(int)

    totals = {"input": 0, "output": 0, "cache_w": 0, "cache_r": 0,
              "cost": 0.0, "messages": 0, "sessions": set()}
    seen_msg_ids: set[str] = set()

    burn_cut_min = datetime.now().astimezone() - timedelta(minutes=60)
    burn_tokens = 0
    burn_cost = 0.0

    if not CLAUDE_PROJECTS.exists():
        return {"by_day": {}, "totals": _finalise_totals(totals), "found": False}

    now_local = datetime.now().astimezone()
    today_date = now_local.date()

    for jsonl in CLAUDE_PROJECTS.rglob("*.jsonl"):
        # The project folder name encodes the cwd with dashes — undo for display.
        project_name = jsonl.parent.name.replace("C--", "C:/").replace("-", "/")
        # Trim to a friendly tail
        project_short = "/".join(project_name.split("/")[-2:]) if "/" in project_name else project_name

        for rec in iter_jsonl(jsonl):
            msg = rec.get("message") or {}
            if not isinstance(msg, dict):
                continue

            # Tool-use frequency (rough)
            content = msg.get("content")
            for block in content if isinstance(content, list) else []:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    name = block.get("name") or "?"
                    if isinstance(name, str):
                        tool_use[name] += 1

            usage = msg.get("usage")
            if not isinstance(usage, dict) or not usage:
                continue
            msg_id = msg.get("id")
            if not isinstance(msg_id, str):
                msg_id = None
            if msg_id and msg_id in seen_msg_ids:
                continue
            if msg_id:
                seen_msg_ids.add(msg_id)

            ts = parse_ts(rec.get("timestamp"))
            if not ts:
                continue
            local = ts.astimezone()
            day = local.strftime("%Y-%m-%d")

            inp = as_int(usage.get("input_tokens"))
            out = as_int(usage.get("output_tokens"))
            cw  = as_int(usage.get("cache_creation_input_tokens"))
            cr  = as_int(usage.get("cache_read_input_tokens"))
            tok = inp + out + cw + cr

            fam = family(msg.get("model", ""))
            p = CLAUDE_PRICES[fam]
            cost = (inp * p["in"] + cw * p["cache_w"] +
                    cr * p["cache_r"] + out * p["out"]) / 1_000_000

            bucket = by_day[day]
            bucket["input"]   += inp
            bucket["output"]  += out
            bucket["cache_w"] += cw
            bucket["cache_r"] += cr
            bucket["cost"]    += cost
            bucket["messages"] += 1
            bucket["by_model"][fam] += tok

            mb = by_model[fam]
            mb["input"] += inp; mb["output"] += out
            mb["cache_w"] += cw; mb["cache_r"] += cr
            mb["cost"] += cost; mb["messages"] += 1

            pb = by_project[project_short]
            pb["tokens"] += tok; pb["cost"] += cost; pb["messages"] += 1

            # Heatmap: day_offset = days back from today (0=today, 6=oldest in 7d window)
            day_offset = (today_date - local.date()).days
            if 0 <= day_offset <= 6:
                heatmap[(day_offset, local.hour)] += tok

            if local >= burn_cut_min:
                burn_tokens += tok
                burn_cost += cost

            totals["input"]   += inp
            totals["output"]  += out
            totals["cache_w"] += cw
            totals["cache_r"] += cr
            totals["cost"]    += cost
            totals["messages"] += 1
            sid = rec.get("sessionId")
            if isinstance(sid, str) and sid:
                totals["sessions"].add(sid)

    by_day_out = {
        d: {**{k: v for k, v in b.items() if k != "by_model"},
            "by_model": dict(b["by_model"])}
        for d, b in by_day.items()
    }
    heatmap_grid = [[heatmap.get((d, h), 0) for h in range(24)] for d in range(7)]
    top_projects = sorted(
        ({"name": k, **v} for k, v in by_project.items()),
        key=lambda r: r["tokens"], reverse=True,
    )[:6]
    top_tools = sorted(
        ({"name": k, "count": v} for k, v in tool_use.items()),
        key=lambda r: r["count"], reverse=True,
    )[:8]

    return {
        "by_day":     by_day_out,
        "by_model":   {k: dict(v) for k, v in by_model.items()},
        "projects":   top_projects,
        "heatmap":    heatmap_grid,
        "tools":      top_tools,
        "burn": {"tokens_last_60min": burn_tokens, "cost_last_60min": burn_cost},
        "totals":     _finalise_totals(totals),
        "found":      True,
    }


def _finalise_totals(t: dict) -> dict:
    out = dict(t)
    out["sessions"] = len(t["sessions"]) if isinstance(t["sessions"], set) else t["sessions"]
    return out


def as_int(v) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError, OverflowError):
        return 0


def claude_window_tokens(hours: float) -> int:
    """Sum Claude tokens (input+output+cache) over the last N hours from JSONLs."""
    cut = datetime.now().astimezone() - timedelta(hours=hours)
    total = 0
    seen_ids: set[str] = set()
    if not CLAUDE_PROJECTS.exists():
        return 0
    for jsonl in CLAUDE_PROJECTS.rglob("*.jsonl"):
        for rec in iter_jsonl(jsonl):
            msg = rec.get("message") or {}
            if not isinstance(msg, dict):
                continue
            usage = msg.get("usage")
            if not isinstance(usage, dict) or not usage:
                continue
            ts = parse_ts(rec.get("timestamp"))
            if not ts or ts.astimezone() < cut:
                continue
            mid = msg.get("id")
            if isinstance(mid, str) and mid:
                if mid in seen_ids:
                    continue
                seen_ids.add(mid)
            total += (as_int(usage.get("input_tokens"))
                      + as_int(usage.get("output_tokens"))
                      + as_int(usage.get("cache_creation_input_tokens"))
                      + as_int(usage.get("cache_read_input_tokens")))
    return total

"""
AI Usage data collector.

Reads local Claude Code and Codex CLI telemetry and emits the normalized JSON
consumed by the tray icon and native stats dashboard.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

HOME = Path.home()
CLAUDE_PROJECTS = HOME / ".claude" / "projects"
CODEX_SESSIONS = HOME / ".codex" / "sessions"

# Per-million-token list prices (USD). Conservative public figures —
# these are display estimates, not billing-grade numbers.
CLAUDE_PRICES = {
    "opus":   {"in": 15.00, "cache_w": 18.75, "cache_r": 1.50, "out": 75.00},
    "sonnet": {"in":  3.00, "cache_w":  3.75, "cache_r": 0.30, "out": 15.00},
    "haiku":  {"in":  0.80, "cache_w":  1.00, "cache_r": 0.08, "out":  4.00},
}


def family(model: str) -> str:
    m = (model or "").lower()
    if "opus" in m:
        return "opus"
    if "sonnet" in m:
        return "sonnet"
    if "haiku" in m:
        return "haiku"
    return "sonnet"


def parse_ts(s: str) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
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
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue
    except OSError:
        return


# ---------- Claude ----------

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

            # Tool-use frequency (rough)
            for block in (msg.get("content") or []):
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    name = block.get("name") or "?"
                    tool_use[name] += 1

            usage = msg.get("usage")
            if not usage:
                continue
            msg_id = msg.get("id")
            if msg_id and msg_id in seen_msg_ids:
                continue
            if msg_id:
                seen_msg_ids.add(msg_id)

            ts = parse_ts(rec.get("timestamp"))
            if not ts:
                continue
            local = ts.astimezone()
            day = local.strftime("%Y-%m-%d")

            inp = int(usage.get("input_tokens") or 0)
            out = int(usage.get("output_tokens") or 0)
            cw  = int(usage.get("cache_creation_input_tokens") or 0)
            cr  = int(usage.get("cache_read_input_tokens") or 0)
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
            if sid:
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


# ---------- Codex ----------

def as_int(v) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def codex_usage_totals(total_usage: dict) -> dict:
    inp = as_int(total_usage.get("input_tokens"))
    out = as_int(total_usage.get("output_tokens"))
    rea = as_int(total_usage.get("reasoning_output_tokens"))
    cac = as_int(total_usage.get("cached_input_tokens"))
    tokens = as_int(total_usage.get("total_tokens")) or (inp + out)
    return {
        "input": inp,
        "output": out,
        "reasoning": rea,
        "cached": cac,
        "tokens": tokens,
    }


def codex_usage_delta(current: dict, previous: dict | None) -> dict:
    if previous is None:
        return dict(current)
    # Codex reports cumulative counters inside a rollout. If a future CLI
    # resets a counter within one file, count the new counter rather than a
    # negative delta.
    return {
        k: (current[k] - previous[k]) if current[k] >= previous[k] else current[k]
        for k in current
    }


def merge_codex_rate_limits(candidates: list[tuple[datetime, dict]]) -> tuple[dict | None, datetime | None]:
    if not candidates:
        return None, None

    latest_ts, latest_rate = max(candidates, key=lambda item: item[0])
    merged = dict(latest_rate)

    for bucket in ("primary", "secondary"):
        bucket_candidates = [
            (ts, rate, rate.get(bucket))
            for ts, rate in candidates
            if isinstance(rate.get(bucket), dict)
        ]
        if not bucket_candidates:
            continue

        bucket_ts, _bucket_rate, latest_bucket = max(bucket_candidates, key=lambda item: item[0])
        reset_at = latest_bucket.get("resets_at")
        same_window = [
            b for _ts, _rate, b in bucket_candidates
            if b.get("resets_at") == reset_at
        ]

        out = dict(latest_bucket)
        try:
            out["used_percent"] = max(float(b.get("used_percent") or 0) for b in same_window)
        except (TypeError, ValueError):
            pass
        merged[bucket] = out

        if bucket_ts > latest_ts:
            latest_ts = bucket_ts

    return merged, latest_ts


def scan_codex() -> dict:
    """
    Codex writes one rollout JSONL per session. token_count events are
    cumulative within a session, so we sum per-event deltas. This keeps daily
    totals accurate for sessions that cross midnight and avoids stale
    concurrent sessions lowering the displayed rate-limit percentage.
    """
    by_day: dict[str, dict] = defaultdict(lambda: {
        "input": 0, "output": 0, "reasoning": 0, "cached": 0, "tokens": 0,
        "sessions": 0,
    })
    totals = {"input": 0, "output": 0, "reasoning": 0, "cached": 0,
              "tokens": 0, "sessions": 0, "plan": None, "model": None}
    rate_candidates: list[tuple[datetime, dict]] = []

    if not CODEX_SESSIONS.exists():
        return {"by_day": {}, "totals": totals, "rate_limits": None, "found": False}

    for jsonl in CODEX_SESSIONS.rglob("rollout-*.jsonl"):
        previous_usage = None
        session_has_usage = False
        session_days: set[str] = set()
        session_model = None
        for rec in iter_jsonl(jsonl):
            rtype = rec.get("type")
            payload = rec.get("payload") or {}
            if rtype == "turn_context":
                session_model = payload.get("model") or session_model
            if rtype == "event_msg" and payload.get("type") == "token_count":
                ts = parse_ts(rec.get("timestamp")) or datetime.now(timezone.utc)
                info = payload.get("info") or {}
                current_usage = codex_usage_totals(info.get("total_token_usage") or {})
                delta = codex_usage_delta(current_usage, previous_usage)
                previous_usage = current_usage

                day = ts.astimezone().strftime("%Y-%m-%d")
                b = by_day[day]
                for key in ("input", "output", "reasoning", "cached", "tokens"):
                    b[key] += delta[key]
                    totals[key] += delta[key]
                if day not in session_days:
                    b["sessions"] += 1
                    session_days.add(day)
                if not session_has_usage:
                    totals["sessions"] += 1
                    session_has_usage = True

                rl = payload.get("rate_limits")
                if rl:
                    rate_candidates.append((ts, rl))
                    totals["plan"] = rl.get("plan_type") or totals["plan"]

        if session_has_usage:
            totals["model"] = session_model or totals["model"]

    latest_rate, latest_rate_at = merge_codex_rate_limits(rate_candidates)
    if latest_rate:
        totals["plan"] = latest_rate.get("plan_type") or totals["plan"]

    return {
        "by_day": dict(by_day),
        "totals": totals,
        "rate_limits": latest_rate,
        "rate_limits_at": latest_rate_at.isoformat() if latest_rate_at else None,
        "found": True,
    }


# ---------- Render ----------

def fmt_num(n: float) -> str:
    n = float(n)
    if n >= 1_000_000_000:
        return f"{n/1_000_000_000:.2f}B"
    if n >= 1_000_000:
        return f"{n/1_000_000:.2f}M"
    if n >= 1_000:
        return f"{n/1_000:.1f}k"
    return f"{n:.0f}"


def fmt_money(n: float) -> str:
    return f"${n:,.2f}"


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
            usage = msg.get("usage")
            if not usage:
                continue
            ts = parse_ts(rec.get("timestamp"))
            if not ts or ts.astimezone() < cut:
                continue
            mid = msg.get("id")
            if mid:
                if mid in seen_ids:
                    continue
                seen_ids.add(mid)
            total += (int(usage.get("input_tokens") or 0)
                      + int(usage.get("output_tokens") or 0)
                      + int(usage.get("cache_creation_input_tokens") or 0)
                      + int(usage.get("cache_read_input_tokens") or 0))
    return total


def build_view(claude: dict, codex: dict) -> dict:
    today = datetime.now().astimezone().strftime("%Y-%m-%d")
    week_cut = (datetime.now().astimezone() - timedelta(days=6)).date()

    c_today = claude["by_day"].get(today, {})
    x_today = codex["by_day"].get(today, {})

    def claude_sum(day_dict: dict, key: str) -> float:
        return float(day_dict.get(key, 0) or 0)

    # 7-day series for sparklines
    def series(by_day: dict, key: str) -> list[float]:
        vals = []
        for i in range(6, -1, -1):
            d = (datetime.now().astimezone() - timedelta(days=i)).strftime("%Y-%m-%d")
            vals.append(float(by_day.get(d, {}).get(key, 0) or 0))
        return vals

    week_claude_cost = sum(
        float(b.get("cost", 0))
        for d, b in claude["by_day"].items()
        if datetime.fromisoformat(d).date() >= week_cut
    )
    week_codex_tokens = sum(
        b.get("tokens", b.get("input", 0) + b.get("output", 0))
        for d, b in codex["by_day"].items()
        if datetime.fromisoformat(d).date() >= week_cut
    )

    # Sliding-window usage so the tray can show fallback counts when the
    # Claude rate-limit probe is unavailable.
    claude_5h    = claude_window_tokens(5)
    claude_week  = claude_window_tokens(24 * 7)

    # Plan-aware %s from probe_claude.py (cached). Best-effort.
    claude_plan = {}
    try:
        from tempfile import gettempdir
        cache = Path(gettempdir()) / "claude_ratelimits.json"
        if cache.exists():
            d = json.loads(cache.read_text(encoding="utf-8"))
            for k in ("anthropic-ratelimit-unified-5h-utilization",
                      "anthropic-ratelimit-unified-7d-utilization",
                      "anthropic-ratelimit-unified-5h-reset",
                      "anthropic-ratelimit-unified-7d-reset"):
                if k in d:
                    claude_plan[k.replace("anthropic-ratelimit-unified-", "")] = d[k]
    except Exception:
        pass

    days_7 = [(datetime.now().astimezone() - timedelta(days=i)).strftime("%Y-%m-%d")
              for i in range(6, -1, -1)]

    # Per-day richer row for the table.
    daily_table = []
    for d in days_7:
        b = claude["by_day"].get(d, {})
        daily_table.append({
            "date":     d,
            "tokens":   (b.get("input", 0) + b.get("output", 0)
                         + b.get("cache_w", 0) + b.get("cache_r", 0)),
            "input":    b.get("input", 0),
            "output":   b.get("output", 0),
            "cache_r":  b.get("cache_r", 0),
            "cost":     b.get("cost", 0.0),
            "messages": b.get("messages", 0),
        })

    return {
        "generated_at": datetime.now().astimezone().strftime("%a %b %d · %I:%M %p"),
        "today_label": today,
        "windows": {
            "claude_5h_tokens":   claude_5h,
            "claude_week_tokens": claude_week,
            "claude_plan":        claude_plan,
        },
        "claude": {
            "found": claude["found"],
            "today": {
                "tokens": claude_sum(c_today, "input") + claude_sum(c_today, "output")
                          + claude_sum(c_today, "cache_w") + claude_sum(c_today, "cache_r"),
                "input": claude_sum(c_today, "input"),
                "output": claude_sum(c_today, "output"),
                "cache_r": claude_sum(c_today, "cache_r"),
                "cache_w": claude_sum(c_today, "cache_w"),
                "cost": claude_sum(c_today, "cost"),
                "messages": int(c_today.get("messages", 0) or 0),
            },
            "totals":      claude["totals"],
            "by_model":    claude.get("by_model", {}),
            "projects":    claude.get("projects", []),
            "heatmap":     claude.get("heatmap", []),
            "tools":       claude.get("tools", []),
            "burn":        claude.get("burn", {}),
            "daily":       daily_table,
            "week_cost":   week_claude_cost,
            "series_cost": series(claude["by_day"], "cost"),
            "series_tokens": [r["tokens"] for r in daily_table],
        },
        "codex": {
            "found": codex["found"],
            "today": {
                "tokens": float(x_today.get("tokens", x_today.get("input", 0) + x_today.get("output", 0))),
                "input": float(x_today.get("input", 0)),
                "output": float(x_today.get("output", 0)),
                "reasoning": float(x_today.get("reasoning", 0)),
                "cached": float(x_today.get("cached", 0)),
                "sessions": int(x_today.get("sessions", 0) or 0),
            },
            "totals": codex["totals"],
            "rate_limits": codex.get("rate_limits"),
            "rate_limits_at": codex.get("rate_limits_at"),
            "week_tokens": week_codex_tokens,
            "series_tokens": series(codex["by_day"], "tokens"),
        },
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true",
                    help="Emit compact machine-readable JSON.")
    ap.add_argument("--summary", action="store_true",
                    help="Print a short human-readable summary.")
    args = ap.parse_args()

    claude = scan_claude()
    codex  = scan_codex()
    view   = build_view(claude, codex)

    if args.json:
        sys.stdout.write(json.dumps(view, default=str))
        return 0

    if args.summary:
        print(f"  Claude today: {fmt_num(view['claude']['today']['tokens'])} tokens · "
              f"{fmt_money(view['claude']['today']['cost'])}")
        print(f"  Codex today:  {fmt_num(view['codex']['today']['tokens'])} tokens · "
              f"{view['codex']['today']['sessions']} sessions")
    else:
        print(json.dumps(view, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())

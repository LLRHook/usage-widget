"""
AI Usage data collector.

Reads local Claude Code and Codex CLI telemetry and emits the normalized JSON
consumed by the tray icon and native stats dashboard.
"""

from __future__ import annotations

import argparse
import json
import math
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

# OpenAI per-million-token list prices (USD). These are API-value display
# estimates, not billing-grade numbers for a ChatGPT/Codex subscription.
CODEX_PRICES = {
    "gpt-5.5":            {"in": 5.00, "cached": 0.500, "out": 30.00},
    "gpt-5.4-mini":      {"in": 0.75, "cached": 0.075, "out":  4.50},
    "gpt-5.4":           {"in": 2.50, "cached": 0.250, "out": 15.00},
    "gpt-5.2-codex":     {"in": 1.75, "cached": 0.175, "out": 14.00},
    "gpt-5.2":           {"in": 1.75, "cached": 0.175, "out": 14.00},
    "gpt-5.1-codex-max": {"in": 1.25, "cached": 0.125, "out": 10.00},
    "gpt-5.1-codex":     {"in": 1.25, "cached": 0.125, "out": 10.00},
    "gpt-5.1":           {"in": 1.25, "cached": 0.125, "out": 10.00},
    "gpt-5-codex":       {"in": 1.25, "cached": 0.125, "out": 10.00},
    "gpt-5-mini":        {"in": 0.25, "cached": 0.025, "out":  2.00},
    "gpt-5-nano":        {"in": 0.05, "cached": 0.005, "out":  0.40},
    "gpt-5":             {"in": 1.25, "cached": 0.125, "out": 10.00},
    "gpt-4.1-mini":      {"in": 0.40, "cached": 0.100, "out":  1.60},
    "gpt-4.1-nano":      {"in": 0.10, "cached": 0.025, "out":  0.40},
    "gpt-4.1":           {"in": 2.00, "cached": 0.500, "out":  8.00},
    "gpt-4o-mini":       {"in": 0.15, "cached": 0.075, "out":  0.60},
    "gpt-4o":            {"in": 2.50, "cached": 1.250, "out": 10.00},
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


# ---------- Claude ----------

def _scan_claude_batch(root: Path | None = None, *, now: datetime | None = None,
                       windows: tuple[float, ...] = (5, 24 * 7), summary: bool = True) -> tuple[dict, list[int]]:
    """Normalize host-specific telemetry once, then reduce one in-process batch.

    Python owns JSON/coercion and local-time/DST behavior. Rust owns summary and
    per-window dedup, token/cost buckets, stable top-N ranking, heatmap and burn.
    """
    from usage_core import aggregate_claude

    root = CLAUDE_PROJECTS if root is None else Path(root)
    now_local = (now or datetime.now()).astimezone()
    cutoffs = [now_local - timedelta(hours=hours) for hours in windows]
    if not root.exists():
        return {"by_day": {}, "totals": {
            "input": 0, "output": 0, "cache_w": 0, "cache_r": 0,
            "cost": 0.0, "messages": 0, "sessions": 0,
        }, "found": False}, [0] * len(windows)

    burn_cut = now_local - timedelta(minutes=60)
    records = []

    def opaque(value: str) -> str:
        # JSON telemetry can contain lone surrogates; Rust strings cannot.
        # IDs and display names are opaque UTF-8/surrogatepass hex on the wire.
        return value.encode("utf-8", errors="surrogatepass").hex()

    for jsonl in root.rglob("*.jsonl"):
        project_name = jsonl.parent.name.replace("C--", "C:/").replace("-", "/")
        project_short = "/".join(project_name.split("/")[-2:]) if "/" in project_name else project_name
        for rec in iter_jsonl(jsonl):
            msg = rec.get("message") or {}
            if not isinstance(msg, dict):
                continue
            tools = []
            content = msg.get("content")
            for block in content if isinstance(content, list) else []:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    name = block.get("name") or "?"
                    if isinstance(name, str):
                        tools.append(name)
            usage = msg.get("usage")
            valid_usage = isinstance(usage, dict) and bool(usage)
            local = None
            time_error = None
            if valid_usage:
                try:
                    ts = parse_ts(rec.get("timestamp"))
                    local = ts.astimezone() if ts else None
                except (ValueError, OverflowError, OSError) as error:
                    # The reducer must decide whether this ID is already seen
                    # before a summary raises a timestamp-conversion error.
                    time_error = {"error": f"{type(error).__name__}:{error}"}
            records.append({
                "tools": [opaque(name) for name in tools],
                "usage": [str(as_int(usage.get(key))) for key in (
                    "input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens",
                )] if valid_usage else None,
                "id": opaque(msg["id"]) if isinstance(msg.get("id"), str) else None,
                "family": family(msg.get("model")),
                "project": opaque(project_short),
                "session": opaque(rec["sessionId"]) if isinstance(rec.get("sessionId"), str) else None,
                "time": {
                    "day": local.strftime("%Y-%m-%d"),
                    "offset": (now_local.date() - local.date()).days,
                    "hour": local.hour, "burn": local >= burn_cut,
                    "windows": [local >= cutoff for cutoff in cutoffs],
                } if local else time_error,
            })
    prices = {name: [price[key] for key in ("in", "out", "cache_w", "cache_r")]
              for name, price in CLAUDE_PRICES.items()}
    result = aggregate_claude(records, prices, len(windows), summary=summary)
    return result["summary"], result["windows"]


def scan_claude(root: Path | None = None, *, now: datetime | None = None) -> dict:
    """Collect Claude totals using the required Rust batch reducer."""
    return _scan_claude_batch(root, now=now, windows=())[0]


# ---------- Codex ----------

def as_int(v) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError, OverflowError):
        return 0


def usage_percent(value, scale: float = 1.0) -> float | None:
    """Return a finite percentage, keeping absent or invalid usage unknown."""
    try:
        percent = float(value) * scale
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(percent):
        return None
    return min(100.0, max(0.0, percent))


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


def codex_price_for_model(model: str | None) -> tuple[str, dict | None]:
    name = model.lower() if isinstance(model, str) else ""
    for key in sorted(CODEX_PRICES, key=len, reverse=True):
        if name == key or name.startswith(key + "-"):
            return key, CODEX_PRICES[key]
    return name or "unknown", None


def codex_estimated_cost(usage: dict, price: dict | None) -> float:
    if not price:
        return 0.0
    input_tokens = max(as_int(usage.get("input")), 0)
    cached_tokens = min(max(as_int(usage.get("cached")), 0), input_tokens)
    uncached_input = input_tokens - cached_tokens
    output_tokens = max(as_int(usage.get("output")), 0)
    return (
        uncached_input * price["in"]
        + cached_tokens * price["cached"]
        + output_tokens * price["out"]
    ) / 1_000_000


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
            merged.pop(bucket, None)
            continue

        bucket_ts, _bucket_rate, latest_bucket = max(bucket_candidates, key=lambda item: item[0])
        reset_at = latest_bucket.get("resets_at")
        same_window = [
            b for _ts, _rate, b in bucket_candidates
            if b.get("resets_at") == reset_at
        ] if reset_at is not None else [latest_bucket]

        out = dict(latest_bucket)
        percentages = [usage_percent(b.get("used_percent")) for b in same_window]
        valid_percentages = [value for value in percentages if value is not None]
        out["used_percent"] = max(valid_percentages) if valid_percentages else None
        merged[bucket] = out

        if bucket_ts > latest_ts:
            latest_ts = bucket_ts

    return merged, latest_ts


def scan_codex(root: Path | None = None) -> dict:
    """
    Codex writes one rollout JSONL per session. token_count events are
    cumulative within a session, so we sum per-event deltas. This keeps daily
    totals accurate for sessions that cross midnight and avoids stale
    concurrent sessions lowering the displayed rate-limit percentage.
    """
    by_day: dict[str, dict] = defaultdict(lambda: {
        "input": 0, "output": 0, "reasoning": 0, "cached": 0, "tokens": 0,
        "cost": 0.0, "sessions": 0,
    })
    by_model: dict[str, dict] = defaultdict(lambda: {
        "input": 0, "output": 0, "reasoning": 0, "cached": 0, "tokens": 0,
        "cost": 0.0, "messages": 0,
    })
    totals = {"input": 0, "output": 0, "reasoning": 0, "cached": 0,
              "tokens": 0, "cost": 0.0, "sessions": 0, "plan": None,
              "model": None, "pricing_model": None}
    rate_candidates: list[tuple[datetime, dict]] = []

    root = CODEX_SESSIONS if root is None else Path(root)
    if not root.exists():
        return {"by_day": {}, "totals": totals, "rate_limits": None, "found": False}

    for jsonl in root.rglob("rollout-*.jsonl"):
        previous_usage = None
        session_has_usage = False
        session_days: set[str] = set()
        session_model = None
        for rec in iter_jsonl(jsonl):
            rtype = rec.get("type")
            payload = rec.get("payload") or {}
            if not isinstance(payload, dict):
                continue
            if rtype == "turn_context":
                session_model = payload.get("model") or session_model
            if rtype == "event_msg" and payload.get("type") == "token_count":
                ts = parse_ts(rec.get("timestamp")) or datetime.now(timezone.utc)
                rl = payload.get("rate_limits")
                if isinstance(rl, dict) and rl:
                    rate_candidates.append((ts, rl))
                    totals["plan"] = rl.get("plan_type") or totals["plan"]

                info = payload.get("info") or {}
                total_usage = info.get("total_token_usage") if isinstance(info, dict) else None
                # Rate-limit-only events have no usage counters. Treating them
                # as zero resets the baseline and double counts the next event.
                if not isinstance(total_usage, dict) or not total_usage:
                    continue
                current_usage = codex_usage_totals(total_usage)
                delta = codex_usage_delta(current_usage, previous_usage)
                previous_usage = current_usage
                price_model, price = codex_price_for_model(session_model)
                cost = codex_estimated_cost(delta, price)

                day = ts.astimezone().strftime("%Y-%m-%d")
                b = by_day[day]
                for key in ("input", "output", "reasoning", "cached", "tokens"):
                    b[key] += delta[key]
                    totals[key] += delta[key]
                b["cost"] += cost
                totals["cost"] += cost
                if price:
                    totals["pricing_model"] = price_model
                if day not in session_days:
                    b["sessions"] += 1
                    session_days.add(day)
                if not session_has_usage:
                    totals["sessions"] += 1
                    session_has_usage = True

                mb = by_model[price_model]
                for key in ("input", "output", "reasoning", "cached", "tokens"):
                    mb[key] += delta[key]
                mb["cost"] += cost
                mb["messages"] += 1

        if session_has_usage:
            totals["model"] = session_model or totals["model"]

    latest_rate, latest_rate_at = merge_codex_rate_limits(rate_candidates)
    if latest_rate:
        totals["plan"] = latest_rate.get("plan_type") or totals["plan"]

    return {
        "by_day": dict(by_day),
        "by_model": {k: dict(v) for k, v in by_model.items()},
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


def claude_window_tokens(hours: float, root: Path | None = None, *, now: datetime | None = None) -> int:
    """Sum deduplicated Claude tokens over the last N hours via the native core."""
    return _scan_claude_batch(root, now=now, windows=(hours,), summary=False)[1][0]


def build_view(claude: dict, codex: dict, *, claude_windows: list[int] | None = None,
               claude_cache: Path | None = None) -> dict:
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
    week_codex_cost = sum(
        float(b.get("cost", 0))
        for d, b in codex["by_day"].items()
        if datetime.fromisoformat(d).date() >= week_cut
    )

    # Sliding-window usage so the tray can show fallback counts when the
    # Claude rate-limit probe is unavailable.
    claude_5h, claude_week = (claude_windows if claude_windows is not None else
                              [claude_window_tokens(5), claude_window_tokens(24 * 7)])

    # Plan-aware %s from probe_claude.py (cached). Best-effort.
    claude_plan = {}
    try:
        from tempfile import gettempdir
        cache = Path(claude_cache) if claude_cache is not None else Path(gettempdir()) / "claude_ratelimits.json"
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

    # Per-day richer rows for the dashboard tables.
    daily_table = []
    codex_daily_table = []
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
        xb = codex["by_day"].get(d, {})
        codex_daily_table.append({
            "date":      d,
            "tokens":    xb.get("tokens", xb.get("input", 0) + xb.get("output", 0)),
            "input":     xb.get("input", 0),
            "output":    xb.get("output", 0),
            "cached":    xb.get("cached", 0),
            "reasoning": xb.get("reasoning", 0),
            "cost":      xb.get("cost", 0.0),
            "sessions":  xb.get("sessions", 0),
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
                "cost": float(x_today.get("cost", 0)),
                "sessions": int(x_today.get("sessions", 0) or 0),
            },
            "totals": codex["totals"],
            "by_model": codex.get("by_model", {}),
            "rate_limits": codex.get("rate_limits"),
            "rate_limits_at": codex.get("rate_limits_at"),
            "daily": codex_daily_table,
            "week_tokens": week_codex_tokens,
            "week_cost": week_codex_cost,
            "series_tokens": series(codex["by_day"], "tokens"),
            "series_cost": series(codex["by_day"], "cost"),
        },
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true",
                    help="Emit compact machine-readable JSON.")
    ap.add_argument("--summary", action="store_true",
                    help="Print a short human-readable summary.")
    ap.add_argument("--claude-projects", type=Path, help="Override the Claude JSONL root.")
    ap.add_argument("--codex-sessions", type=Path, help="Override the Codex JSONL root.")
    ap.add_argument("--claude-cache", type=Path, help="Override the read-only Claude rate-limit cache.")
    args = ap.parse_args()

    from usage_core import NativeCoreError

    try:
        claude, windows = _scan_claude_batch(args.claude_projects)
        codex = scan_codex(args.codex_sessions)
        view = build_view(claude, codex, claude_windows=windows, claude_cache=args.claude_cache)
    except NativeCoreError as error:
        print(str(error), file=sys.stderr)
        return 1

    if args.json:
        sys.stdout.write(json.dumps(view, default=str))
        return 0

    if args.summary:
        print(f"  Claude today: {fmt_num(view['claude']['today']['tokens'])} tokens · "
              f"{fmt_money(view['claude']['today']['cost'])}")
        print(f"  Codex today:  {fmt_num(view['codex']['today']['tokens'])} tokens · "
              f"{fmt_money(view['codex']['today']['cost'])} · "
              f"{view['codex']['today']['sessions']} sessions")
    else:
        print(json.dumps(view, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())

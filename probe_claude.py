"""
Probes the Anthropic API to read live plan rate-limit headers, since Claude
Code doesn't persist them locally. Uses the OAuth access token Claude Code
already stored in ~/.claude/.credentials.json.

Cached to %TEMP%\\claude_ratelimits.json for `cache_seconds` to avoid
hammering. Picks claude-haiku for cheapest probe (max_tokens=1).

Usage:
    python probe_claude.py              # prints JSON with rate-limit data
    python probe_claude.py --force      # bypass cache TTL, honor retry cooldown
    python probe_claude.py --no-network # cache-only; returns {} if cold
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from tempfile import NamedTemporaryFile, gettempdir

CREDS = Path.home() / ".claude" / ".credentials.json"
CACHE = Path(gettempdir()) / "claude_ratelimits.json"
CACHE_SECONDS = 300  # 5 minutes

# Anthropic public OAuth client id used by Claude Code. Embedded in the
# distributed CLI; not a secret. We only need it to refresh tokens.
ANTHROPIC_OAUTH_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"


def read_creds() -> dict:
    return json.loads(CREDS.read_text(encoding="utf-8"))


def write_creds(c: dict) -> None:
    CREDS.write_text(json.dumps(c, indent=2), encoding="utf-8")


PROACTIVE_REFRESH_SECONDS = 1800   # rotate every 30 min even when token is valid


def refresh_token_if_needed(creds: dict) -> str:
    """Return a valid OAuth access token. Refreshes via Claude Code's own
    OAuth endpoint when:
      - within 5 min of expiry, OR
      - the credentials file is older than PROACTIVE_REFRESH_SECONDS.

    Endpoint + client_id extracted from claude.exe (binary constants).
    """
    oauth = creds["claudeAiOauth"]
    expires_at = oauth.get("expiresAt", 0)
    need_refresh = False

    # 1) Near expiry?
    if not expires_at or (expires_at / 1000) - time.time() <= 300:
        need_refresh = True

    # 2) Proactive rotation: refresh every 30 min regardless.
    try:
        creds_mtime = CREDS.stat().st_mtime
        if time.time() - creds_mtime >= PROACTIVE_REFRESH_SECONDS:
            need_refresh = True
    except OSError:
        pass

    if not need_refresh:
        return oauth["accessToken"]

    body = json.dumps({
        "grant_type":    "refresh_token",
        "refresh_token": oauth["refreshToken"],
        "client_id":     ANTHROPIC_OAUTH_CLIENT_ID,
    }).encode()
    req = urllib.request.Request(
        "https://platform.claude.com/v1/oauth/token",
        data=body,
        headers={
            "content-type": "application/json",
            "user-agent":   "claude-cli/2.1.128 (external, cli)",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            resp = json.loads(r.read())
    except urllib.error.HTTPError as e:
        # Bubble up the body so we can see why
        with e:
            raise RuntimeError(
                f"refresh failed: HTTP {e.code} — {e.read()[:300].decode('utf-8', errors='replace')}"
            )

    oauth["accessToken"]  = resp["access_token"]
    oauth["refreshToken"] = resp.get("refresh_token", oauth["refreshToken"])
    oauth["expiresAt"]    = int(time.time() * 1000) + int(resp.get("expires_in", 3600)) * 1000
    creds["claudeAiOauth"] = oauth
    write_creds(creds)
    return oauth["accessToken"]


def parse_headers(headers: dict) -> dict:
    """Pull just the anthropic-ratelimit-* values out of a headers dict."""
    out = {"fetched_at": datetime.now(timezone.utc).isoformat()}
    for k, v in headers.items():
        lk = k.lower()
        if lk.startswith("anthropic-ratelimit-") or lk in ("anthropic-organization-id",):
            out[lk] = v
    return out


def _read_cache() -> dict:
    try:
        cached = json.loads(CACHE.read_text(encoding="utf-8"))
        return cached if isinstance(cached, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_cache(data: dict) -> None:
    """Replace the cache atomically so concurrent readers never see half a JSON."""
    temporary = None
    try:
        with NamedTemporaryFile(mode="w", encoding="utf-8", dir=CACHE.parent,
                                prefix=CACHE.name + ".", suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(data, stream, indent=2)
        os.replace(temporary, CACHE)
    except OSError:
        pass
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


@contextmanager
def _cache_lock():
    """Lock a stable sidecar; replacing the JSON must not replace the lock."""
    with CACHE.with_name(CACHE.name + ".lock").open("a+b") as stream:
        if sys.platform == "win32":
            import msvcrt
            # locking() can lock beyond EOF, so the sidecar may stay empty.
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(stream, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(stream, fcntl.LOCK_UN)


def _update_cache(data: dict, cached: dict | None = None) -> dict:
    """Serialize success writes and failure read/merge/write transactions."""
    def merge(latest):
        if cached is None:
            # A generation also distinguishes forced successes at equal times.
            result = dict(data)
            result["_measurement_generation"] = int(_number(latest.get("_measurement_generation"))) + 1
            return result
        if latest and (
            latest.get("_measurement_generation") != cached.get("_measurement_generation")
            or _number(latest.get("_fetched_at_unix")) > _number(cached.get("_fetched_at_unix"))
        ):
            # Another probe saved a measurement after this request's snapshot.
            # Its success also supersedes this older request's failure cooldown.
            return latest
        result = dict(latest or cached)
        result.update(data)
        result["_retry_at_unix"] = max(
            _number(latest.get("_retry_at_unix")), _number(data.get("_retry_at_unix"))
        )
        return result

    try:
        with _cache_lock():
            result = merge(_read_cache())
            _write_cache(result)
            return result
    except OSError:
        # An unwritable/unavailable cache must not break offline fallback or
        # trigger an uncoordinated write after failing to acquire the lock.
        return merge(_read_cache())


def _number(value) -> float:
    try:
        result = float(value)
        return result if math.isfinite(result) else 0.0
    except (TypeError, ValueError, OverflowError):
        return 0.0


def _retry_at(headers: dict, now: float) -> float:
    value = next((v for k, v in headers.items() if k.lower() == "retry-after"), None)
    delay = _number(value)
    if value and not delay:
        try:
            delay = parsedate_to_datetime(value).timestamp() - now
        except (TypeError, ValueError, OverflowError):
            pass
    return now + max(CACHE_SECONDS, delay)


def _failed_probe(cached: dict, reason: str, now: float, headers: dict | None = None) -> dict:
    return _update_cache({
        "_stale_error" if cached else "_error": reason,
        "_stale_at_unix": now,
        "_retry_at_unix": _retry_at(headers or {}, now),
    }, cached)


def probe(force: bool = False) -> dict:
    cached = _read_cache()
    now = time.time()
    # A forced refresh may bypass the normal cache TTL, but must still honor
    # a server-directed cooldown or a failed attempt's retry window.
    if _number(cached.get("_retry_at_unix")) > now:
        return cached
    fetched = _number(cached.get("_fetched_at_unix"))
    cache_fresh = fetched > 0 and 0 <= now - fetched < CACHE_SECONDS
    if cache_fresh and not force:
        return cached

    try:
        creds = read_creds()
        token = refresh_token_if_needed(creds)
    except (OSError, ValueError, KeyError, TypeError, RuntimeError) as e:
        # Credentials may be temporarily absent or being updated by Claude.
        # Keep the last measurements without exposing any credential content.
        return _failed_probe(cached, f"credentials unavailable ({type(e).__name__})", now)

    body = json.dumps({
        "model": "claude-haiku-4-5-20251001",
        "max_tokens": 1,
        "messages": [{"role": "user", "content": "."}],
    }).encode()

    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=body,
        headers={
            "Authorization":     f"Bearer {token}",
            "anthropic-version": "2023-06-01",
            "anthropic-beta":    "oauth-2025-04-20",
            "content-type":      "application/json",
            "user-agent":        "ai-usage-widget/1.0",
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            _ = r.read()
            headers = dict(r.headers.items())
            http_status = 200
    except urllib.error.HTTPError as e:
        with e:
            headers = dict(e.headers.items()) if e.headers else {}
            http_status = e.code
    except (urllib.error.URLError, OSError) as e:
        return _failed_probe(cached, str(e), time.time())

    parsed = parse_headers(headers)
    parsed["_fetched_at_unix"] = time.time()
    parsed["_http_status"] = http_status

    # A 429 can carry the freshest utilization numbers. Keep them, while
    # persisting Retry-After so each tray poll does not issue another request.
    has_usage = any(f"anthropic-ratelimit-unified-{window}-utilization" in parsed
                    for window in ("5h", "7d"))
    if http_status not in (200, 429) or not has_usage:
        return _failed_probe(cached, f"probe http {http_status}", time.time(), headers)
    if http_status == 429:
        parsed["_retry_at_unix"] = _retry_at(headers, time.time())
    return _update_cache(parsed)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--force",      action="store_true", help="Bypass cache TTL, honoring retry cooldowns.")
    ap.add_argument("--no-network", action="store_true", help="Cache-only.")
    args = ap.parse_args()

    if args.no_network:
        result = _read_cache()
    else:
        result = probe(force=args.force)

    json.dump(result, sys.stdout, indent=2, default=str)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""
Probes the Anthropic API to read live plan rate-limit headers, since Claude
Code doesn't persist them locally. Uses the OAuth access token Claude Code
already stored in ~/.claude/.credentials.json.

Cached to %TEMP%\\claude_ratelimits.json for `cache_seconds` to avoid
hammering. Picks claude-haiku for cheapest probe (max_tokens=1).

Usage:
    python probe_claude.py              # prints JSON with rate-limit data
    python probe_claude.py --force      # ignore cache, hit API now
    python probe_claude.py --no-network # cache-only; returns {} if cold
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from tempfile import gettempdir

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
    if not CACHE.exists():
        return {}
    try:
        return json.loads(CACHE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def probe(force: bool = False) -> dict:
    cached = _read_cache()
    fetched = cached.get("_fetched_at_unix", 0)
    cache_fresh = (time.time() - fetched < CACHE_SECONDS) if cached else False
    if cache_fresh and not force:
        return cached

    creds = read_creds()
    token = refresh_token_if_needed(creds)

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
        headers = dict(e.headers.items()) if e.headers else {}
        http_status = e.code
    except (urllib.error.URLError, OSError) as e:
        # Network-level failure — keep showing the last good numbers.
        if cached and "5h-utilization" in cached or "anthropic-ratelimit-unified-5h-utilization" in (cached or {}):
            cached["_stale_error"] = str(e)
            return cached
        return {"_error": str(e), "_fetched_at_unix": time.time()}

    parsed = parse_headers(headers)
    parsed["_fetched_at_unix"] = time.time()
    parsed["_http_status"] = http_status

    # 401 / 403 / 5xx mean our probe didn't return real utilization. Don't
    # overwrite the cache — keep serving the last good numbers, but flag them.
    if http_status != 200 or "anthropic-ratelimit-unified-5h-utilization" not in parsed:
        if cached:
            cached = dict(cached)
            cached["_stale_reason"] = f"probe http {http_status}"
            cached["_stale_at_unix"] = time.time()
            return cached
        return parsed

    try:
        CACHE.write_text(json.dumps(parsed, indent=2), encoding="utf-8")
    except OSError:
        pass
    return parsed


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--force",      action="store_true", help="Bypass cache and hit API.")
    ap.add_argument("--no-network", action="store_true", help="Cache-only.")
    args = ap.parse_args()

    if args.no_network:
        result = json.loads(CACHE.read_text(encoding="utf-8")) if CACHE.exists() else {}
    else:
        result = probe(force=args.force)

    json.dump(result, sys.stdout, indent=2, default=str)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())

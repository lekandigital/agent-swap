"""Codex usage and login renewal.

Usage comes from ``GET https://chatgpt.com/backend-api/wham/usage`` with the
account's access token and ``ChatGPT-Account-Id`` (measured 2026-11-19: the
body carries ``plan_type``, ``rate_limit.primary_window`` and
``secondary_window`` with ``used_percent``, ``limit_window_seconds`` and
``reset_at`` epoch seconds, plus ``rate_limit_reset_credits.available_count``).
Windows are told apart by length, not position: 5h is 18000 s, 7d 604800 s.

Renewal uses Codex's own refresh grant: ``POST
https://auth.openai.com/oauth/token`` with client id
``app_EMoamEEZ73f0CkXaXp7hrann`` (both strings are in the Codex CLI binary
bundled with ChatGPT.app, build 26.1103). The refresh token rotates, so a
renewal is only done for a slot whose stored copy is the sole holder of its
login: an inactive slot with no `run` session.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, Callable

from agent_swap.codex.auth import CodexAuth, jwt_claims, parse_auth

USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
TOKEN_URL = "https://auth.openai.com/oauth/token"
CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
TIMEOUT_S = 15
USER_AGENT = "agent-swap"

Opener = Callable[..., Any]


class UsageError(Exception):
    def __init__(self, kind: str, detail: str = ""):
        super().__init__(f"{kind}: {detail}" if detail else kind)
        self.kind = kind


def _iso(epoch: float | None) -> str | None:
    if not epoch:
        return None
    return datetime.fromtimestamp(float(epoch), timezone.utc).isoformat()


def normalize(body: dict[str, Any]) -> dict[str, Any]:
    """The usage body in claude-swap's window shape (``pct`` + ``resets_at``)."""
    usage: dict[str, Any] = {}
    rate = body.get("rate_limit") or {}
    for window in (rate.get("primary_window"), rate.get("secondary_window")):
        if not isinstance(window, dict):
            continue
        seconds = window.get("limit_window_seconds") or 0
        entry = {
            "pct": float(window.get("used_percent") or 0),
            "resets_at": _iso(window.get("reset_at")),
        }
        if seconds and seconds <= 6 * 3600:
            usage["five_hour"] = entry
        elif seconds >= 6 * 86400:
            usage["seven_day"] = entry
    usage["plan"] = body.get("plan_type")
    usage["limit_reached"] = bool(rate.get("limit_reached")) or rate.get("allowed") is False
    credits = body.get("rate_limit_reset_credits") or {}
    usage["reset_credits"] = credits.get("available_count")
    return usage


def headroom(usage: dict[str, Any] | None) -> float | None:
    """Percent left in the tighter window, None when unknown."""
    if not usage:
        return None
    used = [w["pct"] for w in (usage.get("five_hour"), usage.get("seven_day")) if w]
    if not used:
        return None
    if usage.get("limit_reached"):
        return 0.0
    return max(0.0, 100.0 - max(used))


def fetch_usage(auth: CodexAuth, *, opener: Opener = urllib.request.urlopen) -> dict[str, Any]:
    if auth.mode != "chatgpt":
        raise UsageError("api-key")
    tokens = auth.tokens
    request = urllib.request.Request(
        USAGE_URL,
        headers={
            "Authorization": f"Bearer {tokens.get('access_token', '')}",
            "ChatGPT-Account-Id": str(auth.account_id or tokens.get("account_id") or ""),
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
        },
    )
    try:
        with opener(request, timeout=TIMEOUT_S) as response:
            body = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            raise UsageError("unauthorized", str(exc.code)) from exc
        raise UsageError(f"http-{exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise UsageError("network", type(exc).__name__) from exc
    except json.JSONDecodeError as exc:
        raise UsageError("bad-response") from exc
    if not isinstance(body, dict):
        raise UsageError("bad-response")
    return normalize(body)


def access_expired(auth: CodexAuth, now: float | None = None) -> bool:
    exp = auth.access_expires_at
    return exp is not None and exp <= (now or time.time()) + 60


def refresh_login(auth: CodexAuth, *, opener: Opener = urllib.request.urlopen) -> CodexAuth:
    """Renew ``auth`` with its refresh token; returns the updated login.

    Raises :class:`UsageError` with kind ``relogin`` when the refresh token
    is no longer accepted.
    """
    refresh_token = auth.tokens.get("refresh_token")
    if not refresh_token:
        raise UsageError("relogin", "no refresh token")
    payload = json.dumps(
        {
            "client_id": CLIENT_ID,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "scope": "openid profile email",
        }
    ).encode()
    request = urllib.request.Request(
        TOKEN_URL,
        data=payload,
        headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
        method="POST",
    )
    try:
        with opener(request, timeout=TIMEOUT_S) as response:
            body = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        if exc.code in (400, 401):
            raise UsageError("relogin", str(exc.code)) from exc
        raise UsageError(f"http-{exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise UsageError("network", type(exc).__name__) from exc
    except json.JSONDecodeError as exc:
        raise UsageError("bad-response") from exc
    if not isinstance(body, dict) or not body.get("access_token"):
        raise UsageError("bad-response")
    raw = json.loads(json.dumps(auth.raw))
    tokens = raw.setdefault("tokens", {})
    for name in ("id_token", "access_token", "refresh_token"):
        if body.get(name):
            tokens[name] = body[name]
    raw["last_refresh"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    updated = parse_auth(raw)
    if updated.key != auth.key:
        raise UsageError("bad-response", "renewed login belongs to another account")
    return updated


def fetch_all(
    jobs: dict[int, Callable[[], dict[str, Any]]], workers: int = 8
) -> dict[int, dict[str, Any] | UsageError]:
    """Run per-slot fetchers in parallel; each result is usage or the error."""
    results: dict[int, dict[str, Any] | UsageError] = {}
    if not jobs:
        return results

    def run(slot: int) -> tuple[int, dict[str, Any] | UsageError]:
        try:
            return slot, jobs[slot]()
        except UsageError as exc:
            return slot, exc
        except Exception as exc:  # one account's failure never sinks the list
            return slot, UsageError("error", type(exc).__name__)

    with ThreadPoolExecutor(max_workers=min(workers, len(jobs))) as pool:
        for slot, result in pool.map(run, list(jobs)):
            results[slot] = result
    return results


def token_expiry_note(auth: CodexAuth) -> str | None:
    claims = jwt_claims(auth.tokens.get("access_token"))
    exp = claims.get("exp")
    if not isinstance(exp, (int, float)):
        return None
    return _iso(exp)

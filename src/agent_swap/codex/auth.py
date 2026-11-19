"""Read Codex ``auth.json`` logins.

Codex keeps one login per ``CODEX_HOME`` in ``auth.json``. A ChatGPT login
holds ``tokens`` (``id_token``, ``access_token``, ``refresh_token``,
``account_id``) and ``last_refresh``; an API-key login holds
``OPENAI_API_KEY``. Identity comes from the unverified ``id_token`` claims,
the same claims Codex itself reads: the email, and under
``https://api.openai.com/auth`` the ChatGPT user and account ids and the plan.

Nothing here prints or logs a token.
"""

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

AUTH_CLAIMS = "https://api.openai.com/auth"


class InvalidAuth(ValueError):
    """An ``auth.json`` that holds no usable Codex login."""


def jwt_claims(token: str | None) -> dict[str, Any]:
    """The payload of a JWT, unverified; ``{}`` when it cannot be decoded."""
    if not token or token.count(".") < 2:
        return {}
    payload = token.split(".")[1]
    payload += "=" * (-len(payload) % 4)
    try:
        claims = json.loads(base64.urlsafe_b64decode(payload))
    except (ValueError, json.JSONDecodeError):
        return {}
    return claims if isinstance(claims, dict) else {}


def _epoch(value: Any) -> float | None:
    if isinstance(value, (int, float)) and value > 0:
        return float(value)
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return None


@dataclass(frozen=True)
class CodexAuth:
    """One parsed login. ``raw`` is the file's JSON, kept for writing back."""

    raw: dict[str, Any]
    mode: str  # "chatgpt" or "apikey"
    email: str | None
    plan: str | None
    user_id: str | None
    account_id: str | None
    access_expires_at: float | None
    plan_expires_at: float | None

    @property
    def key(self) -> str:
        """Stable identity: one ChatGPT user in one workspace, or one API key."""
        if self.mode == "apikey":
            api_key = str(self.raw.get("OPENAI_API_KEY") or "")
            return "apikey::" + hashlib.sha256(api_key.encode()).hexdigest()[:16]
        return f"{self.user_id}::{self.account_id}"

    @property
    def tokens(self) -> dict[str, Any]:
        tokens = self.raw.get("tokens")
        return tokens if isinstance(tokens, dict) else {}

    def to_bytes(self) -> bytes:
        return (json.dumps(self.raw, indent=2) + "\n").encode("utf-8")


def parse_auth(data: bytes | str | dict[str, Any]) -> CodexAuth:
    """Parse an ``auth.json`` body. Raises :class:`InvalidAuth`."""
    if isinstance(data, dict):
        raw = data
    else:
        try:
            raw = json.loads(data)
        except (ValueError, json.JSONDecodeError) as exc:
            raise InvalidAuth("not JSON") from exc
    if not isinstance(raw, dict):
        raise InvalidAuth("not a JSON object")

    tokens = raw.get("tokens")
    if isinstance(tokens, dict) and tokens.get("id_token"):
        claims = jwt_claims(tokens.get("id_token"))
        auth = claims.get(AUTH_CLAIMS) if isinstance(claims.get(AUTH_CLAIMS), dict) else {}
        user_id = auth.get("chatgpt_user_id") or auth.get("user_id") or claims.get("sub")
        account_id = auth.get("chatgpt_account_id") or tokens.get("account_id")
        email = claims.get("email")
        if not email:
            raise InvalidAuth("id_token has no email")
        if not user_id or not account_id:
            raise InvalidAuth("id_token has no ChatGPT user or account id")
        access = jwt_claims(tokens.get("access_token"))
        return CodexAuth(
            raw=raw,
            mode="chatgpt",
            email=str(email),
            plan=auth.get("chatgpt_plan_type"),
            user_id=str(user_id),
            account_id=str(account_id),
            access_expires_at=_epoch(access.get("exp")),
            plan_expires_at=_epoch(auth.get("chatgpt_subscription_active_until")),
        )

    if raw.get("OPENAI_API_KEY"):
        return CodexAuth(
            raw=raw,
            mode="apikey",
            email=None,
            plan=None,
            user_id=None,
            account_id=None,
            access_expires_at=None,
            plan_expires_at=None,
        )
    raise InvalidAuth("no ChatGPT tokens and no API key")


def api_key_auth(api_key: str) -> CodexAuth:
    """The ``auth.json`` Codex writes for ``codex login --with-api-key``."""
    return parse_auth({"OPENAI_API_KEY": api_key, "auth_mode": "apikey", "tokens": None})

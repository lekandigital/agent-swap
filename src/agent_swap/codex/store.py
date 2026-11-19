"""The Codex account store.

Layout under ``$AGENT_SWAP_HOME`` (default ``~/.agent-swap``)::

    codex/sequence.json            slots, aliases, active slot (no secrets)
    codex/homes/<slot>-<email>/    one CODEX_HOME per slot; its auth.json is the
                                   slot's stored login, and `run` uses the dir
                                   as CODEX_HOME so a session refreshes it in place
    codex/mappings.json            directory -> slot, for `run` without a slot
    codex/runs/<slot>.json         pid of a `run` session per slot
    codex/backups/                 the last few live auth.json files replaced by a switch
    codex/cache/usage.json         last good usage per slot

The live login is ``$CODEX_HOME/auth.json`` (default ``~/.codex``). While a
slot is active, the live file is the newer copy of its login: Codex rotates
the refresh token on refresh, so before every operation :func:`sync_live`
copies the live file back into the active slot. A slot's stored copy is only
ever refreshed by agent-swap while the slot is inactive and no `run` session
holds it, so no two writers share a token family.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator
from contextlib import contextmanager

from claude_swap.fsutil import atomic_write_bytes, atomic_write_text
from claude_swap.locking import FileLock
from claude_swap.session import slugify_email

from agent_swap.codex.auth import CodexAuth, InvalidAuth, parse_auth

BACKUPS_KEPT = 5


class StoreError(Exception):
    """A user-facing failure (bad selector, missing slot, ...)."""


def agent_swap_root() -> Path:
    override = os.environ.get("AGENT_SWAP_HOME")
    return Path(override).expanduser() if override else Path.home() / ".agent-swap"


def codex_home() -> Path:
    override = os.environ.get("CODEX_HOME")
    return Path(override).expanduser() if override else Path.home() / ".codex"


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class Account:
    slot: int
    email: str
    key: str
    mode: str = "chatgpt"
    alias: str | None = None
    plan: str | None = None
    disabled: bool = False
    added: str | None = None
    home: str = ""

    def label(self) -> str:
        return f"{self.alias} ({self.email})" if self.alias else self.email

    def to_json(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "email": self.email,
            "key": self.key,
            "mode": self.mode,
            "plan": self.plan,
            "home": self.home,
            "added": self.added,
        }
        if self.alias:
            data["alias"] = self.alias
        if self.disabled:
            data["disabled"] = True
        return data

    @classmethod
    def from_json(cls, slot: int, data: dict[str, Any]) -> "Account":
        return cls(
            slot=slot,
            email=str(data.get("email") or ""),
            key=str(data.get("key") or ""),
            mode=str(data.get("mode") or "chatgpt"),
            alias=data.get("alias") or None,
            plan=data.get("plan"),
            disabled=bool(data.get("disabled")),
            added=data.get("added"),
            home=str(data.get("home") or ""),
        )


@dataclass
class Sequence:
    active: int | None = None
    accounts: dict[int, Account] = field(default_factory=dict)

    def ordered(self) -> list[Account]:
        return [self.accounts[s] for s in sorted(self.accounts)]

    def by_key(self, key: str) -> Account | None:
        return next((a for a in self.accounts.values() if a.key == key), None)

    def next_free_slot(self) -> int:
        slot = 1
        while slot in self.accounts:
            slot += 1
        return slot


class Store:
    def __init__(self, root: Path | None = None, live_home: Path | None = None):
        self.root = (root or agent_swap_root()) / "codex"
        self.live_home = live_home or codex_home()

    # ── paths ─────────────────────────────────────────────────────────────
    @property
    def sequence_path(self) -> Path:
        return self.root / "sequence.json"

    @property
    def live_auth_path(self) -> Path:
        return self.live_home / "auth.json"

    def home_dir(self, account: Account) -> Path:
        return self.root / "homes" / account.home

    def auth_path(self, account: Account) -> Path:
        return self.home_dir(account) / "auth.json"

    @contextmanager
    def locked(self) -> Iterator[None]:
        self.root.mkdir(parents=True, exist_ok=True)
        lock = FileLock(self.root / ".lock", timeout=15.0)
        if not lock.acquire():
            raise StoreError("another agent-swap codex command is running; try again")
        try:
            yield
        finally:
            lock.release()

    # ── sequence.json ─────────────────────────────────────────────────────
    def load(self) -> Sequence:
        try:
            data = json.loads(self.sequence_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return Sequence()
        except (OSError, json.JSONDecodeError) as exc:
            raise StoreError(f"cannot read {self.sequence_path}: {exc}") from exc
        accounts = {
            int(slot): Account.from_json(int(slot), entry)
            for slot, entry in (data.get("accounts") or {}).items()
        }
        active = data.get("activeAccountNumber")
        return Sequence(active=int(active) if active is not None else None, accounts=accounts)

    def save(self, seq: Sequence) -> None:
        data = {
            "version": 1,
            "activeAccountNumber": seq.active,
            "sequence": sorted(seq.accounts),
            "accounts": {str(s): a.to_json() for s, a in sorted(seq.accounts.items())},
            "lastUpdated": _now_iso(),
        }
        self.root.mkdir(parents=True, exist_ok=True)
        atomic_write_text(self.sequence_path, json.dumps(data, indent=2) + "\n")

    # ── credentials ───────────────────────────────────────────────────────
    def read_live(self) -> CodexAuth | None:
        try:
            return parse_auth(self.live_auth_path.read_bytes())
        except FileNotFoundError:
            return None
        except InvalidAuth:
            return None

    def read_stored(self, account: Account) -> CodexAuth:
        try:
            return parse_auth(self.auth_path(account).read_bytes())
        except FileNotFoundError as exc:
            raise StoreError(f"slot {account.slot} has no stored login") from exc
        except InvalidAuth as exc:
            raise StoreError(f"slot {account.slot}'s stored login is unreadable: {exc}") from exc

    def write_stored(self, account: Account, body: bytes) -> None:
        home = self.home_dir(account)
        home.mkdir(parents=True, exist_ok=True)
        os.chmod(home, 0o700)
        atomic_write_bytes(self.auth_path(account), body, mode=0o600)

    def write_live(self, body: bytes) -> None:
        self.live_home.mkdir(parents=True, exist_ok=True)
        if self.live_auth_path.exists():
            backups = self.root / "backups"
            backups.mkdir(parents=True, exist_ok=True)
            os.chmod(backups, 0o700)
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
            atomic_write_bytes(backups / f"auth.json.{stamp}", self.live_auth_path.read_bytes(), mode=0o600)
            for old in sorted(backups.glob("auth.json.*"))[:-BACKUPS_KEPT]:
                old.unlink(missing_ok=True)
        atomic_write_bytes(self.live_auth_path, body, mode=0o600)

    # ── account lifecycle ─────────────────────────────────────────────────
    def _new_account(self, seq: Sequence, auth: CodexAuth, slot: int | None, alias: str | None) -> Account:
        if slot is None:
            slot = seq.next_free_slot()
        if slot < 1:
            raise StoreError("slot numbers start at 1")
        if slot in seq.accounts:
            raise StoreError(f"slot {slot} is already used by {seq.accounts[slot].email}")
        email = auth.email or f"api-key-{slot}@token.local"
        account = Account(
            slot=slot,
            email=email,
            key=auth.key,
            mode=auth.mode,
            alias=alias,
            plan=auth.plan,
            added=_now_iso(),
            home=f"{slot}-{slugify_email(email)}",
        )
        if alias:
            self._check_alias(seq, alias, slot)
        seq.accounts[slot] = account
        self.write_stored(account, auth.to_bytes())
        return account

    def add(self, seq: Sequence, auth: CodexAuth, *, slot: int | None = None, alias: str | None = None) -> tuple[Account, bool]:
        """Store ``auth``; an identity already stored is updated in place.

        Returns ``(account, created)``.
        """
        existing = seq.by_key(auth.key)
        if existing is not None:
            if slot is not None and slot != existing.slot:
                raise StoreError(f"{existing.email} is already stored in slot {existing.slot}")
            self.write_stored(existing, auth.to_bytes())
            existing.plan = auth.plan or existing.plan
            if alias:
                self._check_alias(seq, alias, existing.slot)
                existing.alias = alias
            return existing, False
        return self._new_account(seq, auth, slot, alias), True

    def sync_live(self, seq: Sequence) -> Account | None:
        """Copy the live login into its slot; adopt an unknown live login.

        Returns the slot the live login belongs to (now ``seq.active``), or
        None when there is no readable live login.
        """
        live = self.read_live()
        if live is None:
            return None
        account = seq.by_key(live.key)
        if account is None:
            account, _ = self.add(seq, live)
        else:
            path = self.auth_path(account)
            body = live.to_bytes()
            current = path.read_bytes() if path.exists() else None
            if current is None or parse_auth(current).raw != live.raw:
                self.write_stored(account, body)
            account.plan = live.plan or account.plan
        seq.active = account.slot
        return account

    def remove(self, seq: Sequence, account: Account) -> None:
        home = self.home_dir(account)
        auth = home / "auth.json"
        auth.unlink(missing_ok=True)
        _remove_tree(home)
        (self.root / "runs" / f"{account.slot}.json").unlink(missing_ok=True)
        del seq.accounts[account.slot]
        if seq.active == account.slot:
            seq.active = None
        mappings = self.load_mappings()
        kept = {d: s for d, s in mappings.items() if s != account.slot}
        if kept != mappings:
            self.save_mappings(kept)

    def renumber(self, seq: Sequence, account: Account, new_slot: int) -> None:
        """Move ``account`` to ``new_slot`` (which must be free)."""
        old_home = self.home_dir(account)
        old_slot = account.slot
        del seq.accounts[old_slot]
        account.slot = new_slot
        account.home = f"{new_slot}-{slugify_email(account.email)}"
        new_home = self.home_dir(account)
        if old_home.exists() and old_home != new_home:
            new_home.parent.mkdir(parents=True, exist_ok=True)
            old_home.rename(new_home)
        seq.accounts[new_slot] = account
        if seq.active == old_slot:
            seq.active = new_slot
        runs = self.root / "runs"
        if (runs / f"{old_slot}.json").exists():
            (runs / f"{old_slot}.json").rename(runs / f"{new_slot}.json")
        mappings = self.load_mappings()
        self.save_mappings({d: (new_slot if s == old_slot else s) for d, s in mappings.items()})

    def swap(self, seq: Sequence, a: Account, b: Account) -> None:
        slot_a, slot_b = a.slot, b.slot
        temp = max(seq.accounts) + 1000
        mappings = self.load_mappings()
        self.renumber(seq, a, temp)
        self.renumber(seq, b, slot_a)
        self.renumber(seq, a, slot_b)
        swapped = {d: {slot_a: slot_b, slot_b: slot_a}.get(s, s) for d, s in mappings.items()}
        self.save_mappings(swapped)

    @staticmethod
    def _check_alias(seq: Sequence, alias: str, slot: int) -> None:
        if alias.isdigit():
            raise StoreError("an alias cannot be a number (numbers select slots)")
        for other in seq.accounts.values():
            if other.slot != slot and other.alias and other.alias.lower() == alias.lower():
                raise StoreError(f"Alias '{alias}' is already used by account {other.slot}")

    def set_alias(self, seq: Sequence, account: Account, alias: str | None) -> None:
        if alias:
            self._check_alias(seq, alias, account.slot)
        account.alias = alias or None

    # ── selectors ─────────────────────────────────────────────────────────
    @staticmethod
    def resolve(seq: Sequence, selector: str) -> Account:
        """A slot number, alias or email (a unique fragment of either also works)."""
        sel = selector.strip()
        if sel.isdigit():
            account = seq.accounts.get(int(sel))
            if account is None:
                raise StoreError(f"no account in slot {sel}")
            return account
        low = sel.lower()
        exact = [a for a in seq.accounts.values() if low in {(a.alias or "").lower(), a.email.lower()}]
        if len(exact) == 1:
            return exact[0]
        partial = [
            a for a in seq.accounts.values()
            if low in a.email.lower() or (a.alias and low in a.alias.lower())
        ]
        if len(partial) == 1:
            return partial[0]
        if not partial:
            raise StoreError(f"no account matches '{selector}'")
        names = ", ".join(f"{a.slot}: {a.label()}" for a in partial)
        raise StoreError(f"'{selector}' matches several accounts ({names}); use the slot number")

    # ── mappings and run sessions ─────────────────────────────────────────
    @property
    def mappings_path(self) -> Path:
        return self.root / "mappings.json"

    def load_mappings(self) -> dict[str, int]:
        try:
            data = json.loads(self.mappings_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return {}
        return {str(k): int(v) for k, v in data.items()}

    def save_mappings(self, mappings: dict[str, int]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        atomic_write_text(self.mappings_path, json.dumps(mappings, indent=2, sort_keys=True) + "\n")

    def mapped_slot(self, directory: Path) -> tuple[str, int] | None:
        """The mapping that covers ``directory`` (deepest mapped ancestor wins)."""
        mappings = self.load_mappings()
        current = directory.resolve()
        for candidate in (current, *current.parents):
            slot = mappings.get(str(candidate))
            if slot is not None:
                return str(candidate), slot
        return None

    def record_run(self, slot: int, pid: int) -> None:
        runs = self.root / "runs"
        runs.mkdir(parents=True, exist_ok=True)
        atomic_write_text(runs / f"{slot}.json", json.dumps({"pid": pid, "started": time.time()}) + "\n")

    def live_run_pid(self, slot: int) -> int | None:
        """The pid of a `run` session still holding ``slot``, if any."""
        path = self.root / "runs" / f"{slot}.json"
        try:
            pid = int(json.loads(path.read_text(encoding="utf-8"))["pid"])
        except (FileNotFoundError, ValueError, KeyError, json.JSONDecodeError):
            return None
        if pid == os.getpid() or not _pid_alive(pid):
            path.unlink(missing_ok=True)
            return None
        return pid


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _remove_tree(path: Path) -> None:
    """Remove a slot home without following its shared-state links."""
    if not path.exists() and not path.is_symlink():
        return
    if path.is_symlink() or path.is_file():
        path.unlink()
        return
    for child in path.iterdir():
        if child.is_symlink() or child.is_file():
            child.unlink()
        else:
            _remove_tree(child)
    path.rmdir()

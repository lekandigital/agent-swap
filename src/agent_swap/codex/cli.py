"""``agent-swap codex <command>``: claude-swap's command set for Codex accounts."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable

from claude_swap.fsutil import atomic_write_text, make_dir_link, make_file_link
from claude_swap.printer import accent, bold_accent, bolded, dimmed, format_age, muted, warning, yellowed
from claude_swap.switcher import _format_usage_lines

from agent_swap.codex import daemon, usage as usage_mod
from agent_swap.codex.auth import CodexAuth, InvalidAuth, api_key_auth, parse_auth
from agent_swap.codex.store import Account, Sequence, Store, StoreError, _remove_tree

PRODUCT = "codex"

# Files and folders a `run` session home links back to the live CODEX_HOME,
# so config, instructions, skills and chat history are shared across accounts
# while each home keeps its own auth.json and its own SQLite state.
SHARED_STATE = (
    "config.toml",
    "AGENTS.md",
    "rules",
    "skills",
    "prompts",
    "memories",
    "plugins",
    "sessions",
    "archived_sessions",
    "history.jsonl",
)

CLAUDE_ONLY = {
    "auto": "auto-switching",
    "tui": "the dashboard",
    "watch": "the dashboard",
    "menubar": "the menu bar app",
    "config": "settings",
    "unclaimed": "stashed credentials",
}

PLAN_LABELS = {
    "free": "Free",
    "plus": "Plus",
    "pro": "Pro",
    "prolite": "Pro Lite",
    "team": "Team",
    "business": "Business",
    "enterprise": "Enterprise",
    "edu": "Edu",
}

ERROR_NOTES = {
    "unauthorized": "login token expired - switching to this account renews it",
    "relogin": "re-login needed - sign in with `codex login`, then `agent-swap codex add`",
    "network": "network unavailable",
    "api-key": "API key (no quota)",
    "no-login": "no stored login",
}


def _prog() -> str:
    return os.environ.get("AGENT_SWAP_PROG", "agent-swap") + " codex"


HELP = """usage: {p} <command> [args] [options]

Multi-account switcher for Codex (same commands as claude-swap)

Commands:
  {p} help                       show this help
  {p} list                       list accounts with their usage
  {p} status                     show the current account
  {p} switch                     rotate to the next account
  {p} switch <num|email>         switch to a specific account
  {p} add                        add the current Codex login
  {p} login [--device-auth]      sign in to another account and add it
  {p} add-token [KEY|-]          register an OpenAI API key
  {p} remove <num|email>         remove an account
  {p} disable <num|email>        hold an account out of rotation
  {p} enable <num|email>         return a disabled account to rotation
  {p} run <num|email> [-- ...]   run codex as an account, this terminal only
  {p} run                        run the current dir's mapped account
  {p} map <num|email> [path]     map a directory to an account
  {p} map                        list directory mappings
  {p} unmap [path]               remove a directory mapping
  {p} alias <num|email> <name>   set a short alias for an account
  {p} alias <num|email> --unset  remove an account's alias
  {p} alias                      list all aliases
  {p} swap <a> <b>               exchange two accounts' slot numbers
  {p} move <a> <slot>            assign an account to a slot (swaps if taken)
  {p} export <path>              export accounts (includes logins)
  {p} import <path>              import accounts, auth.json files or a folder of them
  {p} purge                      remove all agent-swap Codex data

Aliases: ls=list  rm=remove

Options:
  --json                 machine-readable output (list, status, switch)
  --strategy {{best,next-available}}
                         with bare 'switch': 'best' picks the account with the
                         most quota left; 'next-available' rotates, skipping
                         accounts at their limit
  --force                with 'switch': switch even while a 'run' session holds
                         the target; with 'import': overwrite stored accounts
  --slot NUM             slot for 'add', 'login' or 'add-token'
  --alias NAME           alias for 'add', 'login' or 'add-token'
  --email EMAIL          label for 'add-token'
  --account NUM|EMAIL    limit 'export' to one account
  --yes                  skip confirmation prompts

In a terminal, every switch ends with the account list.
"""


def _plan_label(plan: str | None) -> str:
    if not plan:
        return ""
    return PLAN_LABELS.get(plan.lower(), plan.replace("_", " ").title())


def _stdout_tty() -> bool:
    try:
        return sys.stdout.isatty()
    except (AttributeError, ValueError):
        return False


def _confirm(question: str, assume_yes: bool) -> bool:
    if assume_yes:
        return True
    if not sys.stdin.isatty():
        raise StoreError(f"{question} - rerun with --yes to confirm")
    return input(f"{question} [y/N] ").strip().lower() in {"y", "yes"}


# ── usage collection ─────────────────────────────────────────────────────


class UsageView:
    def __init__(self, usage: dict | None, error: str | None, fetched_at: float | None, stale: bool):
        self.usage = usage
        self.error = error
        self.fetched_at = fetched_at
        self.stale = stale


class UsageCache:
    """Last good usage per account identity, for 'last seen' on a failed fetch."""

    def __init__(self, store: Store):
        self.path = store.root / "cache" / "usage.json"
        try:
            self.data: dict[str, Any] = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            self.data = {}

    def get(self, key: str) -> tuple[dict | None, float | None]:
        entry = self.data.get(key) or {}
        return entry.get("usage"), entry.get("fetchedAt")

    def put(self, key: str, usage: dict, fetched_at: float) -> None:
        self.data[key] = {"usage": usage, "fetchedAt": fetched_at}

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(self.path, json.dumps(self.data, indent=1) + "\n")


def _store_refreshed(store: Store, slot: int, old: CodexAuth, new: CodexAuth) -> None:
    """Publish a renewed login, never over a copy someone else changed."""
    old_refresh = old.tokens.get("refresh_token")
    with store.locked():
        seq = store.load()
        account = seq.accounts.get(slot)
        if account is None or account.key != new.key:
            return
        if seq.active == slot:
            live = store.read_live()
            if live is not None and live.tokens.get("refresh_token") == old_refresh:
                store.write_live(new.to_bytes())
        try:
            stored = store.read_stored(account)
        except StoreError:
            stored = None
        if stored is None or stored.tokens.get("refresh_token") == old_refresh:
            store.write_stored(account, new.to_bytes())


def collect_usage(
    store: Store,
    seq: Sequence,
    *,
    fetch: Callable[..., dict] | None = None,
    refresh: Callable[..., CodexAuth] | None = None,
) -> dict[int, UsageView]:
    """Usage for every account; network runs outside the store lock."""
    fetch = fetch or usage_mod.fetch_usage
    refresh = refresh or usage_mod.refresh_login
    cache = UsageCache(store)
    jobs: dict[int, Callable[[], dict]] = {}
    views: dict[int, UsageView] = {}
    for account in seq.ordered():
        if account.mode != "chatgpt":
            views[account.slot] = UsageView(None, "api-key", None, False)
            continue
        try:
            auth = store.read_live() if account.slot == seq.active else store.read_stored(account)
        except StoreError:
            auth = None
        if auth is None or auth.key != account.key:
            try:
                auth = store.read_stored(account)
            except StoreError:
                views[account.slot] = UsageView(None, "no-login", None, False)
                continue

        def job(account: Account = account, auth: CodexAuth = auth) -> dict:
            current = auth
            inactive = account.slot != seq.active and store.live_run_pid(account.slot) is None
            if inactive and usage_mod.access_expired(current):
                renewed = refresh(current)
                _store_refreshed(store, account.slot, current, renewed)
                current = renewed
            return fetch(current)

        jobs[account.slot] = job

    now = time.time()
    for slot, result in usage_mod.fetch_all(jobs).items():
        account = seq.accounts[slot]
        if isinstance(result, usage_mod.UsageError):
            last, fetched_at = cache.get(account.key)
            views[slot] = UsageView(last, result.kind, fetched_at, last is not None)
        else:
            cache.put(account.key, result, now)
            views[slot] = UsageView(result, None, now, False)
            if result.get("plan"):
                account.plan = result["plan"]
    try:
        cache.save()
    except OSError:
        pass
    return views


# ── rendering ────────────────────────────────────────────────────────────


def _usage_lines(view: UsageView | None) -> list[str]:
    if view is None:
        return []
    out: list[str] = []
    if view.usage and not view.stale:
        lines = _format_usage_lines(view.usage, view.fetched_at)
        return [f"{dimmed('└' if i == len(lines) - 1 else '├')} {muted(line)}" for i, line in enumerate(lines)]
    detail = ERROR_NOTES.get(view.error or "", view.error or "")
    if view.error == "api-key":
        return [dimmed(detail)]
    out.append(dimmed(f"usage unavailable ({detail})" if detail else "usage unavailable"))
    if view.stale and view.usage:
        room = usage_mod.headroom(view.usage)
        if room is not None and view.fetched_at:
            out.append(
                f"{dimmed('└')} {muted(f'last seen {100 - room:.0f}% used · ' + format_age(int(view.fetched_at * 1000)))}"
            )
    return out


def render_list(seq: Sequence, views: dict[int, UsageView]) -> None:
    if not seq.accounts:
        print(dimmed("No Codex accounts are managed yet."))
        print(dimmed(f"Sign in with `codex login`, then run `{_prog()} add`."))
        return
    print(bolded("Accounts:"))
    ordered = seq.ordered()
    for i, account in enumerate(ordered):
        label = f"{accent(account.alias)} ({account.email})" if account.alias else account.email
        markers = ""
        if account.slot == seq.active:
            markers += f" {bold_accent('(active)')}"
        if account.disabled:
            markers += f" {muted('(disabled)')}"
        plan = _plan_label(account.plan)
        if plan:
            markers += f" {muted('·')} {muted(plan)}"
        view = views.get(account.slot)
        current = view.usage if view and view.usage and not view.stale else {}
        credits = current.get("reset_credits")
        if credits:
            markers += f" {muted('·')} {muted(str(credits) + (' reset credit' if credits == 1 else ' reset credits'))}"
        if current.get("limit_reached"):
            markers += f" {yellowed('(limit reached)')}"
        print(f"  {account.slot}: {label}{markers}")
        for line in _usage_lines(views.get(account.slot)):
            print(f"     {line}")
        if i < len(ordered) - 1:
            print()


def _window_json(window: dict | None) -> dict | None:
    if not window:
        return None
    return {"pct": window.get("pct"), "resetsAt": window.get("resets_at")}


def account_json(account: Account, active: int | None, view: UsageView | None) -> dict[str, Any]:
    data: dict[str, Any] = {
        "number": account.slot,
        "email": account.email,
        "alias": account.alias,
        "plan": account.plan,
        "mode": account.mode,
        "active": account.slot == active,
        "disabled": account.disabled,
    }
    if view is not None:
        usage = view.usage or {}
        data["usage"] = {
            "fiveHour": _window_json(usage.get("five_hour")),
            "sevenDay": _window_json(usage.get("seven_day")),
            "limitReached": usage.get("limit_reached"),
            "resetCredits": usage.get("reset_credits"),
            "fetchedAt": view.fetched_at,
            "stale": view.stale,
            "error": view.error,
        }
    return data


def list_payload(seq: Sequence, views: dict[int, UsageView]) -> dict[str, Any]:
    return {
        "product": PRODUCT,
        "schemaVersion": 1,
        "activeAccountNumber": seq.active,
        "accounts": [account_json(a, seq.active, views.get(a.slot)) for a in seq.ordered()],
    }


# ── commands ─────────────────────────────────────────────────────────────


class Context:
    def __init__(self, store: Store | None = None):
        self.store = store or Store()

    def sync(self, seq: Sequence) -> Account | None:
        """Fold the live login into ``seq`` (caller holds the lock)."""
        live = self.store.read_live()
        known = live is not None and seq.by_key(live.key) is not None
        account = self.store.sync_live(seq)
        if account is not None and not known:
            print(dimmed(f"Saved the current Codex login ({account.email}) as account {account.slot}."))
        return account

    def synced(self) -> Sequence:
        """Load the store with the live login folded in."""
        with self.store.locked():
            seq = self.store.load()
            self.sync(seq)
            self.store.save(seq)
        return seq


def cmd_list(ctx: Context, args: argparse.Namespace) -> int:
    seq = ctx.synced()
    views = collect_usage(ctx.store, seq)
    with ctx.store.locked():
        fresh = ctx.store.load()
        for slot, account in seq.accounts.items():
            if slot in fresh.accounts and account.plan:
                fresh.accounts[slot].plan = account.plan
        ctx.store.save(fresh)
    if args.json:
        print(json.dumps(list_payload(seq, views), indent=2))
    else:
        render_list(seq, views)
    return 0


def cmd_status(ctx: Context, args: argparse.Namespace) -> int:
    seq = ctx.synced()
    account = seq.accounts.get(seq.active) if seq.active else None
    if account is None:
        if args.json:
            print(json.dumps({"product": PRODUCT, "schemaVersion": 1, "active": None}))
        else:
            print(dimmed("No Codex login is active."))
        return 0
    views = collect_usage(ctx.store, Sequence(active=seq.active, accounts={account.slot: account}))
    if args.json:
        print(json.dumps({"product": PRODUCT, "schemaVersion": 1, "active": account_json(account, seq.active, views.get(account.slot))}, indent=2))
        return 0
    plan = _plan_label(account.plan)
    print(f"Current Codex account: {account.slot}: {account.label()}" + (f" {muted('· ' + plan)}" if plan else ""))
    for line in _usage_lines(views.get(account.slot)):
        print(f"   {line}")
    return 0


def _rotation_target(seq: Sequence, strategy: str | None, views: dict[int, UsageView] | None) -> Account:
    enabled = [a for a in seq.ordered() if not a.disabled]
    if not enabled:
        raise StoreError("every account is disabled; enable one first")
    if strategy == "best":
        scored = [(usage_mod.headroom(views[a.slot].usage) if views and a.slot in views and views[a.slot].usage else None, a) for a in enabled]
        known = [(h, a) for h, a in scored if h is not None]
        if not known:
            raise StoreError("no account's usage is known; use plain 'switch'")
        return max(known, key=lambda pair: (pair[0], -pair[1].slot))[1]
    order = [a.slot for a in enabled]
    if seq.active in order:
        start = order.index(seq.active)
    else:
        start = next((i - 1 for i, s in enumerate(order) if s > (seq.active or 0)), len(order) - 1)
    candidates = [enabled[(start + step) % len(enabled)] for step in range(1, len(enabled) + 1)]
    if strategy == "next-available":
        for account in candidates:
            view = views.get(account.slot) if views else None
            room = usage_mod.headroom(view.usage) if view and view.usage and not view.stale else None
            if account.slot != seq.active and (room is None or room > 0):
                return account
        raise StoreError("every other account is at its limit")
    target = candidates[0]
    if target.slot == seq.active:
        raise StoreError("there is no other account to switch to; add one with `login` or `add`")
    return target


def cmd_switch(ctx: Context, args: argparse.Namespace) -> int:
    store = ctx.store
    views = None
    if args.strategy and not args.target:
        views = collect_usage(store, ctx.synced())
    with store.locked():
        seq = store.load()
        ctx.sync(seq)
        if args.target:
            target = store.resolve(seq, args.target)
        else:
            target = _rotation_target(seq, args.strategy, views)
        if target.slot == seq.active:
            store.save(seq)
            message = f"Account {target.slot} ({target.label()}) is already active."
            if args.json:
                print(json.dumps({"product": PRODUCT, "schemaVersion": 1, "switched": False, "active": account_json(target, seq.active, None)}))
            else:
                print(message)
            return 0
        pid = store.live_run_pid(target.slot)
        if pid is not None and not args.force:
            raise StoreError(
                f"account {target.slot} is in use by a `run` session (pid {pid}); "
                "close it first, or pass --force"
            )
        auth = store.read_stored(target)
        if auth.key != target.key:
            raise StoreError(f"slot {target.slot}'s stored login belongs to another account; re-add it")
        store.write_live(auth.to_bytes())
        seq.active = target.slot
        store.save(seq)

    outcome = daemon.coordinate(store.live_home)
    if args.json:
        print(json.dumps({
            "product": PRODUCT,
            "schemaVersion": 1,
            "switched": True,
            "active": account_json(target, target.slot, None),
            "daemon": outcome,
        }))
        return 0
    print(f"Switched to account {target.slot}: {target.label()}")
    note = daemon.MESSAGES.get(outcome)
    if note:
        print(dimmed(note))
    print(dimmed("Open Codex windows (the app, VS Code) use the new account after a restart or window reload."))
    if _stdout_tty():
        print()
        seq_now = ctx.synced()
        render_list(seq_now, collect_usage(store, seq_now))
    return 0


def cmd_add(ctx: Context, args: argparse.Namespace) -> int:
    store = ctx.store
    live = store.read_live()
    if live is None:
        raise StoreError(f"no Codex login found in {store.live_auth_path}; sign in with `codex login` (or `{_prog()} login`)")
    with store.locked():
        seq = store.load()
        account, created = store.add(seq, live, slot=args.slot, alias=args.alias)
        seq.active = account.slot
        store.save(seq)
    verb = "Added" if created else "Updated"
    print(f"{verb} account {account.slot}: {account.label()}")
    return 0


def cmd_login(ctx: Context, args: argparse.Namespace) -> int:
    store = ctx.store
    codex = shutil.which("codex")
    if codex is None:
        raise StoreError("the codex CLI is not on PATH; install it with `npm i -g @openai/codex`")
    if store.read_live() is not None:
        ctx.synced()
    command = [codex, "login"] + (["--device-auth"] if args.device_auth else [])
    env = dict(os.environ, CODEX_HOME=str(store.live_home))
    if subprocess.run(command, env=env).returncode != 0:
        raise StoreError("codex login did not finish; nothing was added")
    return cmd_add(ctx, args)


def cmd_add_token(ctx: Context, args: argparse.Namespace) -> int:
    key = args.token
    if key in (None, "-"):
        key = sys.stdin.readline().strip() if not sys.stdin.isatty() or key == "-" else input("OpenAI API key: ").strip()
    if not key:
        raise StoreError("no API key given")
    auth = api_key_auth(key)
    store = ctx.store
    with store.locked():
        seq = store.load()
        account, created = store.add(seq, auth, slot=args.slot, alias=args.alias)
        if args.email:
            account.email = args.email
        store.save(seq)
    print(f"{'Added' if created else 'Updated'} API-key account {account.slot}: {account.label()}")
    return 0


def cmd_remove(ctx: Context, args: argparse.Namespace) -> int:
    store = ctx.store
    with store.locked():
        seq = store.load()
        ctx.sync(seq)
        account = store.resolve(seq, args.target)
        if not _confirm(f"Remove account {account.slot} ({account.label()})?", args.yes):
            print("Nothing removed.")
            return 1
        was_active = account.slot == seq.active
        store.remove(seq, account)
        store.save(seq)
    print(f"Removed account {account.slot}: {account.label()}")
    if was_active:
        print(dimmed("It is still the live Codex login; the next agent-swap command saves it again unless you switch first."))
    return 0


def cmd_enable(ctx: Context, args: argparse.Namespace, disabled: bool) -> int:
    store = ctx.store
    with store.locked():
        seq = store.load()
        account = store.resolve(seq, args.target)
        account.disabled = disabled
        store.save(seq)
    state = "held out of rotation" if disabled else "back in rotation"
    print(f"Account {account.slot} ({account.label()}) is {state}.")
    return 0


def _prepare_session_home(store: Store, account: Account) -> Path:
    home = store.home_dir(account)
    home.mkdir(parents=True, exist_ok=True)
    for name in SHARED_STATE:
        source = store.live_home / name
        link = home / name
        if not source.exists() or link.exists() or link.is_symlink():
            continue
        if source.is_dir():
            make_dir_link(source, link)
        else:
            make_file_link(source, link)
    return home


def cmd_run(ctx: Context, argv: list[str]) -> int:
    store = ctx.store
    selector = None
    passthrough: list[str] = []
    if "--" in argv:
        index = argv.index("--")
        head, passthrough = argv[:index], argv[index + 1:]
    else:
        head = argv
    if len(head) > 1:
        raise StoreError(f"usage: {_prog()} run [num|email] [-- codex args...]")
    if head:
        selector = head[0]
    codex = shutil.which("codex")
    if codex is None:
        raise StoreError("the codex CLI is not on PATH")
    with store.locked():
        seq = store.load()
        ctx.sync(seq)
        store.save(seq)
        if selector is None:
            mapped = store.mapped_slot(Path.cwd())
            if mapped is None:
                raise StoreError(f"no account is mapped to this directory; `{_prog()} map <num|email>` maps one")
            account = seq.accounts.get(mapped[1])
            if account is None:
                raise StoreError(f"{mapped[0]} is mapped to slot {mapped[1]}, which no longer exists")
        else:
            account = store.resolve(seq, selector)
        env = dict(os.environ)
        if account.slot == seq.active:
            env["CODEX_HOME"] = str(store.live_home)
        else:
            env["CODEX_HOME"] = str(_prepare_session_home(store, account))
            store.record_run(account.slot, os.getpid())
    print(dimmed(f"Running codex as account {account.slot}: {account.label()}"), file=sys.stderr)
    os.execve(codex, [codex, *passthrough], env)
    return 0  # unreachable outside tests


def cmd_map(ctx: Context, args: argparse.Namespace) -> int:
    store = ctx.store
    if not args.target:
        mappings = store.load_mappings()
        seq = store.load()
        if not mappings:
            print(dimmed("No directory mappings."))
            return 0
        for directory, slot in sorted(mappings.items()):
            account = seq.accounts.get(slot)
            print(f"  {directory} -> {slot}: {account.label() if account else '(missing)'}")
        return 0
    with store.locked():
        seq = store.load()
        account = store.resolve(seq, args.target)
        directory = str(Path(args.path or ".").expanduser().resolve())
        mappings = store.load_mappings()
        mappings[directory] = account.slot
        store.save_mappings(mappings)
    print(f"Mapped {directory} to account {account.slot}: {account.label()}")
    return 0


def cmd_unmap(ctx: Context, args: argparse.Namespace) -> int:
    store = ctx.store
    directory = str(Path(args.path or ".").expanduser().resolve())
    with store.locked():
        mappings = store.load_mappings()
        if directory not in mappings:
            raise StoreError(f"{directory} is not mapped")
        del mappings[directory]
        store.save_mappings(mappings)
    print(f"Unmapped {directory}")
    return 0


def cmd_alias(ctx: Context, args: argparse.Namespace) -> int:
    store = ctx.store
    if not args.target:
        seq = store.load()
        named = [a for a in seq.ordered() if a.alias]
        if not named:
            print(dimmed("No aliases set."))
        for account in named:
            print(f"  {account.slot}: {accent(account.alias)} ({account.email})")
        return 0
    with store.locked():
        seq = store.load()
        account = store.resolve(seq, args.target)
        if args.unset:
            store.set_alias(seq, account, None)
            store.save(seq)
            print(f"Removed the alias from account {account.slot}")
            return 0
        if not args.name:
            raise StoreError(f"usage: {_prog()} alias <num|email> <name> | --unset")
        store.set_alias(seq, account, args.name)
        store.save(seq)
    print(f"Account {account.slot} is now {accent(args.name)} ({account.email})")
    return 0


def cmd_swap(ctx: Context, args: argparse.Namespace) -> int:
    store = ctx.store
    with store.locked():
        seq = store.load()
        ctx.sync(seq)
        a, b = store.resolve(seq, args.a), store.resolve(seq, args.b)
        if a.slot == b.slot:
            raise StoreError("those are the same account")
        slot_a, slot_b = a.slot, b.slot
        store.swap(seq, a, b)
        store.save(seq)
    print(f"Swapped slots {slot_a} and {slot_b}")
    return 0


def cmd_move(ctx: Context, args: argparse.Namespace) -> int:
    store = ctx.store
    if not args.slot.isdigit() or int(args.slot) < 1:
        raise StoreError("the destination must be a slot number (1 or more)")
    new_slot = int(args.slot)
    with store.locked():
        seq = store.load()
        ctx.sync(seq)
        account = store.resolve(seq, args.target)
        if account.slot == new_slot:
            print(f"Account {account.label()} is already in slot {new_slot}")
            return 0
        old_slot = account.slot
        if new_slot in seq.accounts:
            store.swap(seq, account, seq.accounts[new_slot])
        else:
            store.renumber(seq, account, new_slot)
        store.save(seq)
    print(f"Moved {account.label()} from slot {old_slot} to {new_slot}")
    return 0


EXPORT_FORMAT = "agent-swap-codex-export"


def cmd_export(ctx: Context, args: argparse.Namespace) -> int:
    seq = ctx.synced()
    accounts = [ctx.store.resolve(seq, args.account)] if args.account else seq.ordered()
    payload = {
        "format": EXPORT_FORMAT,
        "version": 1,
        "accounts": [
            {
                "slot": a.slot,
                "email": a.email,
                "alias": a.alias,
                "disabled": a.disabled,
                "auth": ctx.store.read_stored(a).raw,
            }
            for a in accounts
        ],
    }
    path = Path(args.path).expanduser()
    atomic_write_text(path, json.dumps(payload, indent=2) + "\n", mode=0o600)
    warning(f"{path} holds {len(accounts)} Codex login(s) unencrypted; keep it private and delete it after use.")
    return 0


def _import_entries(path: Path) -> list[tuple[CodexAuth, dict[str, Any]]]:
    """(login, metadata) pairs from an export, an auth.json, or a folder of them.

    A folder from codex-auth (``~/.codex/accounts``) also carries aliases in
    its ``registry.json``; they are applied by email.
    """
    entries: list[tuple[CodexAuth, dict[str, Any]]] = []
    if path.is_dir():
        aliases: dict[str, str] = {}
        registry = path / "registry.json"
        if registry.exists():
            try:
                data = json.loads(registry.read_text(encoding="utf-8"))
                for record in data.get("accounts") or []:
                    if record.get("alias") and record.get("email"):
                        aliases[str(record["email"]).lower()] = str(record["alias"])
            except (OSError, json.JSONDecodeError):
                pass
        for file in sorted(path.glob("*.json")):
            if file.name == "registry.json" or ".bak" in file.name:
                continue
            try:
                auth = parse_auth(file.read_bytes())
            except (InvalidAuth, OSError):
                continue
            entries.append((auth, {"alias": aliases.get((auth.email or "").lower())}))
        entries.sort(key=lambda pair: (pair[0].email or "").lower())
        return entries
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict) and data.get("format") == EXPORT_FORMAT:
        for record in data.get("accounts") or []:
            entries.append((parse_auth(record["auth"]), record))
        return entries
    return [(parse_auth(data), {})]


def cmd_import(ctx: Context, args: argparse.Namespace) -> int:
    store = ctx.store
    entries = _import_entries(Path(args.path).expanduser())
    if not entries:
        raise StoreError(f"no Codex logins found in {args.path}")
    imported = 0
    with store.locked():
        seq = store.load()
        ctx.sync(seq)
        for auth, meta in entries:
            existing = seq.by_key(auth.key)
            if existing is not None and not args.force:
                print(dimmed(f"Skipped {existing.email}: already account {existing.slot} (use --force to overwrite)"))
                continue
            if existing is not None and existing.slot == seq.active:
                print(dimmed(f"Skipped {existing.email}: it is the live login, which is newer"))
                continue
            slot = meta.get("slot") if existing is None and meta.get("slot") not in seq.accounts else None
            alias = meta.get("alias")
            if alias and any(a.alias and a.alias.lower() == alias.lower() and a.key != auth.key for a in seq.accounts.values()):
                alias = None
            account, _ = store.add(seq, auth, slot=slot, alias=alias)
            if meta.get("disabled"):
                account.disabled = True
            imported += 1
            print(f"Imported account {account.slot}: {account.label()}")
        store.save(seq)
    print(f"{imported} account(s) imported.")
    return 0


def cmd_purge(ctx: Context, args: argparse.Namespace) -> int:
    store = ctx.store
    if not store.root.exists():
        print(dimmed("No agent-swap Codex data."))
        return 0
    if not _confirm(f"Delete every stored Codex login in {store.root}? The live login stays.", args.yes):
        print("Nothing deleted.")
        return 1
    _remove_tree(store.root)
    print(f"Removed {store.root}")
    return 0


# ── entry point ──────────────────────────────────────────────────────────


def _parser(verb: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=f"{_prog()} {verb}", add_help=True)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--yes", "-y", action="store_true")
    return parser


def main(argv: list[str], store: Store | None = None) -> int:
    ctx = Context(store)
    if not argv or argv[0] in {"help", "-h", "--help"}:
        print(HELP.format(p=_prog()), end="")
        return 0
    verb, rest = argv[0], argv[1:]
    verb = {"ls": "list", "rm": "remove"}.get(verb, verb)
    if verb in CLAUDE_ONLY:
        print(
            f"{verb}: {CLAUDE_ONLY[verb]} is Claude-only for now; "
            f"`agent-swap claude {verb}` works.",
            file=sys.stderr,
        )
        return 2
    if verb in {"upgrade", "update"}:
        print("Run `agent-swap upgrade`; it upgrades every product at once.", file=sys.stderr)
        return 2

    try:
        if verb == "run":
            return cmd_run(ctx, rest)
        parser = _parser(verb)
        if verb == "list":
            return cmd_list(ctx, parser.parse_args(rest))
        if verb == "status":
            return cmd_status(ctx, parser.parse_args(rest))
        if verb == "switch":
            parser.add_argument("target", nargs="?")
            parser.add_argument("--strategy", choices=["best", "next-available"])
            parser.add_argument("--force", action="store_true")
            return cmd_switch(ctx, parser.parse_args(rest))
        if verb in {"add", "login"}:
            parser.add_argument("--slot", type=int)
            parser.add_argument("--alias")
            if verb == "login":
                parser.add_argument("--device-auth", action="store_true")
                return cmd_login(ctx, parser.parse_args(rest))
            return cmd_add(ctx, parser.parse_args(rest))
        if verb == "add-token":
            parser.add_argument("token", nargs="?")
            parser.add_argument("--slot", type=int)
            parser.add_argument("--alias")
            parser.add_argument("--email")
            return cmd_add_token(ctx, parser.parse_args(rest))
        if verb == "remove":
            parser.add_argument("target")
            return cmd_remove(ctx, parser.parse_args(rest))
        if verb in {"disable", "enable"}:
            parser.add_argument("target")
            return cmd_enable(ctx, parser.parse_args(rest), disabled=verb == "disable")
        if verb == "map":
            parser.add_argument("target", nargs="?")
            parser.add_argument("path", nargs="?")
            return cmd_map(ctx, parser.parse_args(rest))
        if verb == "unmap":
            parser.add_argument("path", nargs="?")
            return cmd_unmap(ctx, parser.parse_args(rest))
        if verb == "alias":
            parser.add_argument("target", nargs="?")
            parser.add_argument("name", nargs="?")
            parser.add_argument("--unset", action="store_true")
            return cmd_alias(ctx, parser.parse_args(rest))
        if verb == "swap":
            parser.add_argument("a")
            parser.add_argument("b")
            return cmd_swap(ctx, parser.parse_args(rest))
        if verb == "move":
            parser.add_argument("target")
            parser.add_argument("slot")
            return cmd_move(ctx, parser.parse_args(rest))
        if verb == "export":
            parser.add_argument("path")
            parser.add_argument("--account")
            return cmd_export(ctx, parser.parse_args(rest))
        if verb == "import":
            parser.add_argument("path")
            parser.add_argument("--force", action="store_true")
            return cmd_import(ctx, parser.parse_args(rest))
        if verb == "purge":
            return cmd_purge(ctx, parser.parse_args(rest))
    except StoreError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    print(f"Error: unknown command '{verb}'. Run `{_prog()} help`.", file=sys.stderr)
    return 2

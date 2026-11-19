"""agent-swap: the Codex product and the product dispatcher."""

from __future__ import annotations

import base64
import json
import os
import stat
import time
from pathlib import Path

import pytest

from agent_swap import cli as dispatcher
from agent_swap.codex import cli as codex_cli
from agent_swap.codex import daemon, usage
from agent_swap.codex.auth import InvalidAuth, api_key_auth, parse_auth
from agent_swap.codex.store import Store, StoreError


def _b64(data: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(data).encode()).rstrip(b"=").decode()


def make_auth(email: str, *, n: int, plan: str = "plus", exp: float | None = None, refresh: str | None = None) -> dict:
    claims = {
        "email": email,
        "https://api.openai.com/auth": {
            "chatgpt_user_id": f"user-{n}",
            "chatgpt_account_id": f"acct-{n}",
            "chatgpt_plan_type": plan,
        },
    }
    access = {"exp": exp if exp is not None else time.time() + 86400}
    return {
        "OPENAI_API_KEY": None,
        "auth_mode": "chatgpt",
        "tokens": {
            "id_token": f"{_b64({'alg': 'none'})}.{_b64(claims)}.sig",
            "access_token": f"{_b64({'alg': 'none'})}.{_b64(access)}.sig",
            "refresh_token": refresh or f"refresh-{n}",
            "account_id": f"acct-{n}",
        },
        "last_refresh": "2026-11-10T00:00:00Z",
    }


@pytest.fixture
def env(tmp_path, monkeypatch):
    root = tmp_path / "agent-swap"
    live = tmp_path / "codex-home"
    live.mkdir()
    monkeypatch.setenv("AGENT_SWAP_HOME", str(root))
    monkeypatch.setenv("CODEX_HOME", str(live))
    monkeypatch.setattr(codex_cli, "_stdout_tty", lambda: False)
    monkeypatch.setattr(codex_cli.daemon, "coordinate", lambda home, **kw: "not_running")
    return Store(), live


def write_live(live: Path, auth: dict) -> None:
    (live / "auth.json").write_text(json.dumps(auth))


def live_email(live: Path) -> str:
    return parse_auth((live / "auth.json").read_bytes()).email


def seed(store: Store, live: Path, emails: list[str], active: int = 1) -> None:
    """Store ``emails`` as slots 1..n with account ``active`` live."""
    for n, email in enumerate(emails, start=1):
        auth = parse_auth(make_auth(email, n=n))
        with store.locked():
            seq = store.load()
            store.add(seq, auth)
            store.save(seq)
    write_live(live, make_auth(emails[active - 1], n=active))
    with store.locked():
        seq = store.load()
        store.sync_live(seq)
        store.save(seq)


# ── auth ────────────────────────────────────────────────────────────────


def test_parse_chatgpt_login_reads_identity_from_id_token():
    auth = parse_auth(make_auth("a@example.com", n=1, plan="pro"))
    assert (auth.mode, auth.email, auth.plan) == ("chatgpt", "a@example.com", "pro")
    assert auth.key == "user-1::acct-1"
    assert auth.access_expires_at is not None


def test_parse_api_key_login_and_rejects_empty():
    auth = api_key_auth("sk-test")
    assert auth.mode == "apikey" and auth.key.startswith("apikey::")
    with pytest.raises(InvalidAuth):
        parse_auth({"tokens": None})


# ── switching ───────────────────────────────────────────────────────────


def test_bare_switch_rotates_2_to_3_and_wraps_3_to_1(env, capsys):
    store, live = env
    seed(store, live, ["a@example.com", "b@example.com", "c@example.com"], active=2)
    expected = ["c@example.com", "a@example.com", "b@example.com"]
    for email in expected:
        assert codex_cli.main(["switch"]) == 0
        assert live_email(live) == email
    out = capsys.readouterr().out
    assert "Switched to account 3: c@example.com" in out
    assert "Switched to account 1: a@example.com" in out


def test_switch_skips_disabled_accounts(env):
    store, live = env
    seed(store, live, ["a@example.com", "b@example.com", "c@example.com"], active=1)
    assert codex_cli.main(["disable", "2"]) == 0
    assert codex_cli.main(["switch"]) == 0
    assert live_email(live) == "c@example.com"


def test_switch_to_alias_email_and_number(env):
    store, live = env
    seed(store, live, ["a@example.com", "b@example.com"], active=1)
    assert codex_cli.main(["alias", "2", "work"]) == 0
    assert codex_cli.main(["switch", "work"]) == 0
    assert live_email(live) == "b@example.com"
    assert codex_cli.main(["switch", "a@example.com"]) == 0
    assert live_email(live) == "a@example.com"
    assert codex_cli.main(["switch", "2"]) == 0
    assert live_email(live) == "b@example.com"


def test_switch_saves_the_outgoing_accounts_rotated_tokens(env):
    store, live = env
    seed(store, live, ["a@example.com", "b@example.com"], active=1)
    write_live(live, make_auth("a@example.com", n=1, refresh="rotated-by-codex"))
    assert codex_cli.main(["switch"]) == 0
    seq = store.load()
    stored = store.read_stored(seq.accounts[1])
    assert stored.tokens["refresh_token"] == "rotated-by-codex"


def test_an_unknown_live_login_is_adopted_before_switching(env, capsys):
    store, live = env
    seed(store, live, ["a@example.com", "b@example.com"], active=1)
    write_live(live, make_auth("new@example.com", n=9))
    assert codex_cli.main(["switch", "1"]) == 0
    seq = store.load()
    assert any(a.email == "new@example.com" for a in seq.accounts.values())
    assert "Saved the current Codex login (new@example.com)" in capsys.readouterr().out


def test_single_account_switch_explains(env, capsys):
    store, live = env
    seed(store, live, ["a@example.com"], active=1)
    assert codex_cli.main(["switch"]) == 1
    assert "no other account" in capsys.readouterr().err


def test_switch_refuses_a_slot_held_by_a_run_session(env, monkeypatch, capsys):
    store, live = env
    seed(store, live, ["a@example.com", "b@example.com"], active=1)
    store.record_run(2, 999999)
    monkeypatch.setattr("agent_swap.codex.store._pid_alive", lambda pid: True)
    assert codex_cli.main(["switch", "2"]) == 1
    assert "in use by a `run` session" in capsys.readouterr().err
    assert codex_cli.main(["switch", "2", "--force"]) == 0


def test_switch_in_a_terminal_ends_with_the_account_list(env, monkeypatch, capsys):
    store, live = env
    seed(store, live, ["a@example.com", "b@example.com"], active=1)
    monkeypatch.setattr(codex_cli, "_stdout_tty", lambda: True)
    monkeypatch.setattr(codex_cli, "collect_usage", lambda store, seq, **kw: {})
    assert codex_cli.main(["switch"]) == 0
    out = capsys.readouterr().out
    assert out.index("Switched to account 2") < out.index("Accounts:")
    assert "a@example.com" in out.split("Accounts:")[1]


def test_switch_json_is_one_document(env, capsys):
    store, live = env
    seed(store, live, ["a@example.com", "b@example.com"], active=1)
    assert codex_cli.main(["switch", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["switched"] is True and payload["active"]["number"] == 2


# ── slots ───────────────────────────────────────────────────────────────


def test_swap_and_move_keep_logins_with_their_accounts(env):
    store, live = env
    seed(store, live, ["a@example.com", "b@example.com"], active=1)
    assert codex_cli.main(["swap", "1", "2"]) == 0
    seq = store.load()
    assert seq.accounts[1].email == "b@example.com"
    assert store.read_stored(seq.accounts[1]).email == "b@example.com"
    assert seq.active == 2  # a@ is live and now lives in slot 2
    assert codex_cli.main(["move", "a@example.com", "5"]) == 0
    seq = store.load()
    assert seq.accounts[5].email == "a@example.com" and seq.active == 5
    assert store.read_stored(seq.accounts[5]).email == "a@example.com"


def test_remove_deletes_the_stored_login(env):
    store, live = env
    seed(store, live, ["a@example.com", "b@example.com"], active=1)
    home = store.home_dir(store.load().accounts[2])
    assert codex_cli.main(["remove", "2", "--yes"]) == 0
    assert 2 not in store.load().accounts
    assert not home.exists()


def test_alias_must_be_unique_and_not_numeric(env):
    store, live = env
    seed(store, live, ["a@example.com", "b@example.com"], active=1)
    assert codex_cli.main(["alias", "1", "main"]) == 0
    assert codex_cli.main(["alias", "2", "main"]) == 1
    assert codex_cli.main(["alias", "2", "42"]) == 1


def test_stored_logins_are_private(env):
    store, live = env
    seed(store, live, ["a@example.com"], active=1)
    path = store.auth_path(store.load().accounts[1])
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


# ── import ──────────────────────────────────────────────────────────────


def test_import_a_codex_auth_folder_with_its_aliases(env, tmp_path):
    store, live = env
    folder = tmp_path / "codex-auth-accounts"
    folder.mkdir()
    (folder / "x.auth.json").write_text(json.dumps(make_auth("main@example.com", n=1)))
    (folder / "y.auth.json").write_text(json.dumps(make_auth("alt@example.com", n=2)))
    (folder / "registry.json").write_text(json.dumps({"accounts": [
        {"email": "main@example.com", "alias": "main"},
        {"email": "alt@example.com", "alias": "alt"},
    ]}))
    write_live(live, make_auth("main@example.com", n=1))
    assert codex_cli.main(["import", str(folder)]) == 0
    seq = store.load()
    by_email = {a.email: a for a in seq.accounts.values()}
    assert by_email["alt@example.com"].alias == "alt"
    assert seq.accounts[seq.active].email == "main@example.com"


def test_export_then_import_round_trips(env, tmp_path):
    store, live = env
    seed(store, live, ["a@example.com", "b@example.com"], active=1)
    out = tmp_path / "export.json"
    assert codex_cli.main(["export", str(out)]) == 0
    assert stat.S_IMODE(out.stat().st_mode) == 0o600
    assert codex_cli.main(["remove", "2", "--yes"]) == 0
    assert codex_cli.main(["import", str(out)]) == 0
    assert any(a.email == "b@example.com" for a in store.load().accounts.values())


# ── usage ───────────────────────────────────────────────────────────────


def test_normalize_tells_windows_apart_by_length():
    body = {
        "plan_type": "plus",
        "rate_limit": {
            "allowed": True,
            "limit_reached": False,
            "primary_window": {"used_percent": 14, "limit_window_seconds": 18000, "reset_at": 1795109249},
            "secondary_window": {"used_percent": 18, "limit_window_seconds": 604800, "reset_at": 1795651708},
        },
        "rate_limit_reset_credits": {"available_count": 1},
    }
    result = usage.normalize(body)
    assert result["five_hour"]["pct"] == 14 and result["seven_day"]["pct"] == 18
    assert result["reset_credits"] == 1 and usage.headroom(result) == 82


def test_inactive_expired_login_is_renewed_but_the_active_one_never_is(env):
    store, live = env
    seed(store, live, ["a@example.com", "b@example.com"], active=1)
    seq = store.load()
    expired = parse_auth(make_auth("b@example.com", n=2, exp=time.time() - 10))
    store.write_stored(seq.accounts[2], expired.to_bytes())
    write_live(live, make_auth("a@example.com", n=1, exp=time.time() - 10))
    renewed_for = []

    def refresh(auth):
        renewed_for.append(auth.email)
        raw = make_auth(auth.email, n=2, refresh="renewed")
        return parse_auth(raw)

    views = codex_cli.collect_usage(store, store.load(), fetch=lambda a: {"five_hour": {"pct": 5, "resets_at": None}}, refresh=refresh)
    assert renewed_for == ["b@example.com"]
    assert store.read_stored(store.load().accounts[2]).tokens["refresh_token"] == "renewed"
    assert views[1].usage and views[2].usage


def test_list_shows_the_percentage_left_in_each_window(env, capsys):
    store, live = env
    seed(store, live, ["a@example.com"], active=1)
    reading = {"five_hour": {"pct": 36, "resets_at": None}, "seven_day": {"pct": 100, "resets_at": None}}
    views = codex_cli.collect_usage(store, store.load(), fetch=lambda a: reading)
    codex_cli.render_list(store.load(), views)
    out = capsys.readouterr().out
    assert "5h:  64% left" in out and "7d:   0% left" in out
    payload = codex_cli.list_payload(store.load(), views)
    assert payload["accounts"][0]["usage"]["fiveHour"] == {"pct": 36, "pctLeft": 64, "resetsAt": None}


def test_failed_fetch_falls_back_to_the_last_good_reading(env):
    store, live = env
    seed(store, live, ["a@example.com"], active=1)
    good = {"five_hour": {"pct": 40, "resets_at": None}}
    codex_cli.collect_usage(store, store.load(), fetch=lambda a: good)

    def fail(auth):
        raise usage.UsageError("network")

    views = codex_cli.collect_usage(store, store.load(), fetch=fail)
    assert views[1].stale and views[1].usage == good and views[1].error == "network"


# ── run ─────────────────────────────────────────────────────────────────


def test_run_uses_a_private_codex_home_for_an_inactive_account(env, monkeypatch, tmp_path):
    store, live = env
    seed(store, live, ["a@example.com", "b@example.com"], active=1)
    (live / "config.toml").write_text("model = 'x'\n")
    (live / "sessions").mkdir()
    fake = tmp_path / "bin" / "codex"
    fake.parent.mkdir()
    fake.write_text("#!/bin/sh\n")
    fake.chmod(0o755)
    monkeypatch.setattr(codex_cli.shutil, "which", lambda name: str(fake))
    calls = []
    monkeypatch.setattr(codex_cli.os, "execve", lambda path, argv, env: calls.append((argv, env)))

    codex_cli.main(["run", "2", "--", "exec", "hi"])
    argv, env_vars = calls[-1]
    home = Path(env_vars["CODEX_HOME"])
    assert argv[1:] == ["exec", "hi"]
    assert home == store.home_dir(store.load().accounts[2])
    assert (home / "config.toml").is_symlink() and (home / "sessions").is_symlink()
    assert parse_auth((home / "auth.json").read_bytes()).email == "b@example.com"

    codex_cli.main(["run", "1"])
    assert calls[-1][1]["CODEX_HOME"] == str(live)


def test_run_without_a_slot_uses_the_directory_mapping(env, monkeypatch, tmp_path):
    store, live = env
    seed(store, live, ["a@example.com", "b@example.com"], active=1)
    project = tmp_path / "project"
    (project / "src").mkdir(parents=True)
    assert codex_cli.main(["map", "2", str(project)]) == 0
    monkeypatch.chdir(project / "src")
    monkeypatch.setattr(codex_cli.shutil, "which", lambda name: "/bin/echo")
    calls = []
    monkeypatch.setattr(codex_cli.os, "execve", lambda path, argv, env: calls.append(env))
    codex_cli.main(["run"])
    assert Path(calls[-1]["CODEX_HOME"]).name.startswith("2-")
    assert codex_cli.main(["unmap", str(project)]) == 0


# ── daemon ──────────────────────────────────────────────────────────────


def _fake_daemon(tmp_path: Path, reload_stdout: str = "", reload_code: int = 0, reload_err: str = "") -> Path:
    home = tmp_path / "home"
    (home / "app-server-control").mkdir(parents=True)
    (home / "app-server-control" / "app-server-control.sock").write_text("")
    exe = home / "packages" / "standalone" / "current" / "bin" / "codex"
    exe.parent.mkdir(parents=True)
    exe.write_text(
        "#!/bin/sh\n"
        'case "$3" in\n'
        '  version) echo \'{"status":"running","appServerVersion":"1"}\' ;;\n'
        f"  reload-auth) echo '{reload_stdout}'; echo '{reload_err}' >&2; exit {reload_code} ;;\n"
        "  restart) exit 0 ;;\n"
        "esac\n"
    )
    exe.chmod(0o755)
    return home


@pytest.mark.skipif(os.name == "nt", reason="shell script stands in for codex")
def test_daemon_outcomes(tmp_path):
    assert daemon.coordinate(tmp_path / "none") == "not_running"
    assert daemon.coordinate(_fake_daemon(tmp_path / "a")) == "reloaded"
    assert daemon.coordinate(_fake_daemon(tmp_path / "b", reload_stdout="skipped the auth reload")) == "deferred"
    unsupported = _fake_daemon(tmp_path / "c", reload_code=2, reload_err="unrecognized subcommand")
    assert daemon.coordinate(unsupported) == "restarted"
    assert daemon.coordinate(unsupported, allow_restart=False) == "unknown"
    assert daemon.coordinate(_fake_daemon(tmp_path / "d", reload_code=1, reload_err="boom")) == "reload_failed"


# ── dispatcher ──────────────────────────────────────────────────────────


def test_dispatcher_routes_products_and_rejects_unknown(env, capsys):
    store, live = env
    seed(store, live, ["a@example.com", "b@example.com"], active=1)
    assert dispatcher.main(["codex", "switch"]) == 0
    assert live_email(live) == "b@example.com"
    assert dispatcher.main(["cursor", "switch"]) == 2
    assert "isn't supported yet" in capsys.readouterr().err
    assert dispatcher.main(["nope"]) == 2


def test_claude_switch_in_a_terminal_ends_with_the_list(monkeypatch):
    calls = []
    monkeypatch.setattr(dispatcher, "_call_claude_swap", lambda prog, args: calls.append(args) or 0)
    monkeypatch.setattr(dispatcher, "_stdout_tty", lambda: True)
    assert dispatcher.run_claude(["switch"], "agent-swap claude") == 0
    assert calls == [["switch"], ["list"]]
    calls.clear()
    assert dispatcher.run_claude(["switch", "--json"], "agent-swap claude") == 0
    assert calls == [["switch", "--json"]]


def test_claude_only_commands_say_so_for_codex(env, capsys):
    assert codex_cli.main(["menubar"]) == 2
    assert "Claude-only" in capsys.readouterr().err

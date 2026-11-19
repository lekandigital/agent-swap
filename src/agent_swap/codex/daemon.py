"""Tell a running Codex app-server daemon about a switched login.

Codex clients can share a background app-server daemon that caches the login,
so rewriting ``auth.json`` alone can leave it on the old account. The daemon
is driven through the Codex CLI: ``codex app-server daemon version`` probes it
(JSON with ``status: "running"`` and ``appServerVersion``), ``reload-auth``
re-reads ``auth.json`` without dropping attached sessions (stdout says
"skipped the auth reload" while a turn runs), and ``restart`` is the fallback
for a daemon that cannot reload. It is only reachable while
``$CODEX_HOME/app-server-control/app-server-control.sock`` exists. This
follows codex-auth 0.4's daemon coordination (src/workflows/codex_daemon.zig).

Desktop and IDE clients that run their own app-server over stdio are not
reached this way; they pick up the new login when restarted.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

PROBE_TIMEOUT_S = 5
RELOAD_TIMEOUT_S = 10
RESTART_TIMEOUT_S = 120

_UNSUPPORTED = (
    "does not implement account/reload",
    "unrecognized subcommand",
    "wasn't recognized",
    "wasn't expected",
)

MESSAGES = {
    "not_running": None,
    "reloaded": "Codex daemon reloaded the new login.",
    "deferred": "Codex daemon is mid-turn and kept the old login; run the switch again when the turn ends.",
    "restarted": "Codex daemon restarted on the new login.",
    "reload_failed": "Codex daemon could not reload the login; restart Codex.",
    "restart_failed": "Codex daemon did not restart; restart Codex yourself.",
    "timed_out": "Codex daemon restart timed out; restart Codex yourself.",
    "unknown": "Could not confirm the Codex daemon's account; restart Codex if it still uses the old one.",
}


def control_socket(codex_home: Path) -> Path:
    return codex_home / "app-server-control" / "app-server-control.sock"


def resolve_executable(codex_home: Path) -> str | None:
    name = "codex.exe" if os.name == "nt" else "codex"
    for layout in (
        ("packages", "app-server-daemon", "current", "bin"),
        ("packages", "standalone", "current", "bin"),
        ("packages", "standalone", "current"),
    ):
        candidate = codex_home.joinpath(*layout, name)
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    found = shutil.which(name)
    return str(Path(found).resolve()) if found else None


def _run(executable: str, command: str, codex_home: Path, timeout: float) -> subprocess.CompletedProcess | None:
    env = dict(os.environ, CODEX_HOME=str(codex_home))
    try:
        return subprocess.run(
            [executable, "app-server", "daemon", command],
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return None
    except OSError:
        return subprocess.CompletedProcess([executable], 127, "", "")


def coordinate(codex_home: Path, *, allow_restart: bool = True) -> str:
    """Apply a switched ``auth.json`` to a running daemon; returns the outcome."""
    if not control_socket(codex_home).exists():
        return "not_running"
    executable = resolve_executable(codex_home)
    if executable is None:
        return "unknown"

    probe = _run(executable, "version", codex_home, PROBE_TIMEOUT_S)
    if probe is None or probe.returncode != 0:
        return "unknown"
    try:
        state = json.loads(probe.stdout)
    except json.JSONDecodeError:
        return "unknown"
    if not (isinstance(state, dict) and state.get("status") == "running" and state.get("appServerVersion")):
        return "unknown"

    reload = _run(executable, "reload-auth", codex_home, RELOAD_TIMEOUT_S)
    if reload is None:
        return "unknown"
    if reload.returncode == 0:
        return "deferred" if "skipped the auth reload" in reload.stdout else "reloaded"
    if not any(marker in reload.stderr for marker in _UNSUPPORTED):
        return "reload_failed"
    if not allow_restart:
        return "unknown"

    restart = _run(executable, "restart", codex_home, RESTART_TIMEOUT_S)
    if restart is None:
        return "timed_out"
    return "restarted" if restart.returncode == 0 else "restart_failed"

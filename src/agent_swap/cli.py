"""``agent-swap <product> <command>``: one account switcher for several AI coding agents.

``agent-swap claude ...`` is claude-swap, unchanged; ``agent-swap codex ...``
gives Codex the same commands. ``claude-swap`` and ``cswap`` stay as
shortcuts for ``agent-swap claude``. In a terminal, every successful switch
ends with that product's account list.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys

from agent_swap import __version__

REPO_URL = "https://github.com/lekandigital/agent-swap"

PRODUCTS = {
    "claude": "claude",
    "claude-code": "claude",
    "cc": "claude",
    "codex": "codex",
    "cx": "codex",
}

# Recognised so they get a clear "not yet" instead of "unknown command".
PLANNED = ("cursor", "gemini", "antigravity", "copilot", "windsurf", "opencode", "amp", "kiro")

HELP = """usage: {p} <product> <command> [args] [options]

Multi-account switcher for AI coding agents.

Products:
  claude    Claude Code   (every claude-swap command; `cswap` still works)
  codex     OpenAI Codex  (the same commands for Codex logins)

Examples:
  {p} claude switch            rotate Claude Code to the next account
  {p} codex switch             rotate Codex to the next account
  {p} codex switch 2           switch Codex to account 2
  {p} codex list               Codex accounts with their usage
  {p} claude run 6 -- -p hi    run Claude Code as account 6 in this terminal
  {p} <product> help           every command for that product

Across products:
  {p} list                     every product's accounts
  {p} status                   every product's current account
  {p} upgrade                  reinstall the latest agent-swap from GitHub
  {p} --version

Not supported yet: {planned}.
"""


def _prog() -> str:
    name = os.path.basename(sys.argv[0] or "") or "agent-swap"
    return name if name in {"agent-swap", "aswap"} else "agent-swap"


def _stdout_tty() -> bool:
    try:
        return sys.stdout.isatty()
    except (AttributeError, ValueError):
        return False


def _silence_upstream_update_check() -> None:
    """claude-swap's PyPI update notice would point at the upstream package."""
    try:
        from claude_swap import update_check

        update_check.check_for_update = lambda *_args, **_kwargs: None
    except ImportError:
        pass


def _call_claude_swap(prog: str, args: list[str]) -> int:
    import claude_swap.cli as claude_cli

    saved = sys.argv
    sys.argv = [prog, *args]
    try:
        claude_cli.main()
        return 0
    except SystemExit as exc:
        if exc.code is None:
            return 0
        return exc.code if isinstance(exc.code, int) else 1
    finally:
        sys.argv = saved


def _is_switch(args: list[str]) -> bool:
    return bool(args) and args[0] in {"switch", "--switch", "--switch-to"}


def run_claude(args: list[str], prog: str) -> int:
    if args and (args[0] in {"upgrade", "update", "--upgrade"}):
        return upgrade()
    _silence_upstream_update_check()
    code = _call_claude_swap(prog, args)
    if code == 0 and _is_switch(args) and "--json" not in args and _stdout_tty():
        print()
        _call_claude_swap(prog, ["list"])
    return code


def run_codex(args: list[str], prog: str) -> int:
    from agent_swap.codex import cli as codex_cli

    base = prog[: -len(" codex")] if prog.endswith(" codex") else prog
    os.environ["AGENT_SWAP_PROG"] = base
    return codex_cli.main(args)


def upgrade() -> int:
    uv = shutil.which("uv")
    spec = f"agent-swap @ git+{REPO_URL}"
    if uv is None:
        print(f"Install uv, then run: uv tool install --force --reinstall '{spec}'", file=sys.stderr)
        return 1
    print(f"Reinstalling agent-swap from {REPO_URL} ...")
    return subprocess.run([uv, "tool", "install", "--force", "--reinstall", spec]).returncode


def _all_products(verb: str, prog: str) -> int:
    from claude_swap.printer import bolded

    worst = 0
    for title, runner, product in (
        ("Claude Code", run_claude, "claude"),
        ("Codex", run_codex, "codex"),
    ):
        print(bolded(f"── {title} ──"))
        code = runner([verb], f"{prog} {product}")
        worst = max(worst, code)
        print()
    return worst


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    prog = _prog()
    if not args or args[0] in {"help", "-h", "--help"}:
        print(HELP.format(p=prog, planned=", ".join(PLANNED)), end="")
        return 0
    head, rest = args[0], args[1:]
    if head in {"--version", "version"}:
        print(f"{prog} {__version__}")
        return 0
    if head in {"upgrade", "update"}:
        return upgrade()
    if head in {"list", "ls", "status"}:
        return _all_products("list" if head == "ls" else head, prog)
    product = PRODUCTS.get(head.lower())
    if product == "claude":
        return run_claude(rest, f"{prog} claude")
    if product == "codex":
        return run_codex(rest, f"{prog} codex")
    if head.lower() in PLANNED:
        print(f"{head} isn't supported yet. Supported: claude, codex.", file=sys.stderr)
        return 2
    print(
        f"Unknown product '{head}'. Use `{prog} claude <command>` or `{prog} codex <command>`; "
        f"`{prog} help` lists everything.",
        file=sys.stderr,
    )
    return 2


def entry() -> None:
    sys.exit(main())


def claude_entry() -> None:
    """``claude-swap`` / ``cswap``: the Claude product under its old names."""
    prog = os.path.basename(sys.argv[0] or "") or "cswap"
    for ext in (".exe", ".pyw", ".py"):
        if prog.lower().endswith(ext):
            prog = prog[: -len(ext)]
    sys.exit(run_claude(sys.argv[1:], prog))

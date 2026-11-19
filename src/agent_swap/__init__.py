"""Multi-account switcher for AI coding agents (Claude Code, Codex)."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("agent-swap")
except PackageNotFoundError:  # running from a source checkout
    __version__ = "0+unknown"

__all__ = ["__version__"]

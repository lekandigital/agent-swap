# Codex accounts

`agent-swap codex <command>` has claude-swap's commands for OpenAI Codex logins. Run `agent-swap codex help` for the full list.

## Where logins live

| What | Where |
|---|---|
| The live login Codex uses | `$CODEX_HOME/auth.json` (default `~/.codex`) |
| Stored logins, one per slot | `~/.agent-swap/codex/homes/<slot>-<email>/auth.json` (`$AGENT_SWAP_HOME` moves the root) |
| Slots, aliases, active slot | `~/.agent-swap/codex/sequence.json` (no secrets) |
| Directory mappings for `run` | `~/.agent-swap/codex/mappings.json` |
| Last live logins a switch replaced | `~/.agent-swap/codex/backups/` (5 kept) |

Codex rotates the refresh token every time it renews a login, so two copies of one login cannot both stay valid. agent-swap keeps exactly one writer per login:

- Before every command, the live `auth.json` is copied back into the active slot. A switch therefore never leaves the outgoing account with an old token.
- A live login agent-swap has not seen before is saved as a new slot automatically.
- agent-swap renews a stored login itself only when its access token has expired, the slot is not active, and no `run` session holds it. Renewal uses Codex's own refresh grant (`https://auth.openai.com/oauth/token`, client `app_EMoamEEZ73f0CkXaXp7hrann`).
- Never copy a login to a second machine. Sign in there separately (`agent-swap codex login --device-auth`).

## Switching

`switch` writes the target's login to `$CODEX_HOME/auth.json`, then tells a running Codex app-server daemon to reload it (`codex app-server daemon reload-auth`, falling back to `restart`). Clients that run their own app-server, such as the ChatGPT desktop app and the VS Code extension, pick up the new account when restarted or when the window reloads.

- `switch` with no target rotates to the next enabled slot and wraps around.
- `--strategy best` picks the account with the most quota left. `--strategy next-available` rotates but skips accounts at their limit.
- A switch to a slot held by a `run` session is refused unless `--force` is given.

## Usage

`list` and `status` read `GET https://chatgpt.com/backend-api/wham/usage` for each account in parallel: the 5-hour and weekly windows (percent used and reset time), the plan, and available reset credits. A failed read shows the last good reading with its age.

## run and map

`run <num|email> [-- codex args]` starts `codex` as that account in this terminal only, without switching the global login. The account's slot folder becomes `CODEX_HOME`. It links `config.toml`, `AGENTS.md`, `rules`, `skills`, `prompts`, `memories`, `plugins`, `sessions`, `archived_sessions` and `history.jsonl` back to `~/.codex`, so settings and chat history are shared while the login stays separate. For the active account, `run` just uses `~/.codex`.

`map <num|email> [path]` ties a directory to an account, and a bare `run` inside it uses that account.

## Moving from codex-auth

`agent-swap codex import ~/.codex/accounts` imports codex-auth's stored logins and its aliases. Remove codex-auth's copies afterwards so only one store holds each login.

## Not available for Codex yet

`auto`, `tui`, `watch`, `menubar`, `config` and `unclaimed` are Claude-only for now.

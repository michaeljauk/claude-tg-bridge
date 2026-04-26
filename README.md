# claude-tg-bridge

Bring your Claude Max plan to Telegram. A minimal bridge that subprocesses the official `claude` CLI, so traffic routes through your Plan tokens (not OAuth/API credits).

Use this if:
- You pay for **Claude Max** and want a phone-accessible AI assistant
- You don't want to pay extra for an API key on top of your plan
- You don't want a third-party OAuth bridge between your subscription and your bot

## Why subprocess instead of API?

The Anthropic API and the Claude Max plan use **different billing pools**.

- `claude --print` (the official CLI in Claude Code) → Plan tokens
- Direct Anthropic API calls (even with an OAuth token) → API credits / Extra Usage

Several existing Telegram bridges hit the API path and burn through Extra Usage budget. This bridge wraps the CLI as a subprocess, which keeps you on Plan tokens.

## Features

- **Allowlist-only** — no public bot, only listed Telegram user IDs may chat
- **Per-chat session continuity** — picks up `session_id` from `--output-format=json` and `--resume`s on the next message
- **Optional persona** — drop a `USER.md` into the state dir, it's appended as system prompt
- **Slash commands** — `/new` (fresh session), `/status`, `/help`
- **Long replies** — Telegram's 4096-char cap is handled (split into chunks)
- **systemd unit included** — auto-restart, log to file

## Requirements

- Python 3.10+ with `requests` (`pip install requests`)
- Claude CLI installed and authed via `claude login` (Max plan)
- A Telegram bot from `@BotFather`
- Your Telegram user ID (ask `@userinfobot`)

## Install

```bash
git clone https://github.com/YOUR_GH/claude-tg-bridge.git ~/claude-tg-bridge
cd ~/claude-tg-bridge
pip install --user requests   # or use a venv
```

## Configure

Create `~/.claude-tg-bridge/env`:

```env
TELEGRAM_BOT_TOKEN=123456:ABC...
TELEGRAM_ALLOWLIST=12345678,87654321
CLAUDE_BIN=/path/to/claude          # optional, falls back to $PATH
CLAUDE_MODEL=sonnet                 # optional (sonnet, opus, haiku, ...)
CLAW_WORK_DIR=/home/you/notes       # optional, the cwd the CLI runs in
CLAUDE_TIMEOUT_SEC=600              # optional
```

```bash
chmod 600 ~/.claude-tg-bridge/env
```

Optionally drop a `USER.md` in `~/.claude-tg-bridge/USER.md` — it's appended as system prompt on every call (persona, conventions, etc.).

## Run (foreground)

```bash
set -a && source ~/.claude-tg-bridge/env && set +a
python3 bridge.py
```

## Run (systemd user service)

```bash
mkdir -p ~/.config/systemd/user
cp claude-tg-bridge.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now claude-tg-bridge
journalctl --user -u claude-tg-bridge -f
```

The included unit tails to `~/.claude-tg-bridge/bridge.log`.

## Slash commands

- `/new` (or `/reset`, `/start`) — drop the saved session, start fresh
- `/status` — show current session id + config
- `/help` — list commands

Anything else is forwarded to claude.

## Security notes

- **Allowlist is mandatory.** The bridge refuses to start without `TELEGRAM_ALLOWLIST` set.
- The Claude CLI runs with `--permission-mode bypassPermissions`, meaning tools fire without prompting. Don't expose this to people you don't trust, and consider what `CLAW_WORK_DIR` lets the agent see and modify.
- Bot tokens in env files: chmod 600.
- This is a Plan-token bridge — it does NOT bypass any rate limit or quota Anthropic enforces on the Max plan. If you hit your plan's rate limit, you'll get errors back through the bridge.

## Limitations / not goals

- **Single user/chat conversational** — the bridge is designed for personal use, not multi-tenant.
- **No streaming** — the bridge calls `--output-format=json` (single result). Add streaming if you want progress updates from inside long-running Claude work.
- **Telegram only** — the bridge layer is small enough that you could swap Telegram for Slack/Discord/iMessage with ~50 lines.

## License

MIT — see [LICENSE](LICENSE).

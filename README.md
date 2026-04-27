# claude-tg-bridge

A small, personal Telegram bridge for the Claude CLI. Chat with `claude` from your phone the same way you do in your terminal — same context, same files, same tools.

Run it on a small VPS (or a Pi). Lock it to your own Telegram user ID. Point it at a working directory you trust. That's it.

## What you get

- A Telegram bot that you can DM, that replies via `claude --print`
- **Allowlist-only** — refuses to start without an allowlist; refuses to reply to anyone else
- **Per-chat session continuity** — picks up `session_id` from the CLI and `--resume`s on the next message, so the conversation keeps its memory
- **Forum-topic aware** — in supergroups with Topics enabled, each topic carries its own session and main-slot, so parallel conversations don't bleed into each other (one topic = email triage, another = code review, etc.)
- **Topic management** — `/topic <name>` creates a topic, `/topic close` closes the current one, `/topic list` shows known sessions. Bot needs Manage Topics admin permission. Ships `tg-topic` shell helper so the assistant itself can spawn topics + cross-post from inside its subprocess.
- **Optional persona** — drop a `USER.md` into the state dir; it's appended as system prompt
- **Slash commands** — `/new` (fresh session), `/status`, `/help`
- **Long replies handled** — Telegram's 4096-char limit is split into chunks
- **Photo / image-document input** — sent images are downloaded and `@`-referenced in the prompt so claude reads them via the Read tool
- **Concurrent turns with main/sidebar split** — fire follow-up messages while a long task is still running; the first in-flight turn per chat owns the session-continuity slot, follow-ups run as sidebars (full parent context, but their session id is discarded so they can't fork the main chain). Cap concurrency with `CLAW_MAX_CONCURRENT`.
- **systemd unit included** — auto-restart, log to file
- **Single Python file**, ~250 lines, only dependency is `requests`

## Quick start

```bash
git clone https://github.com/YOUR_GH/claude-tg-bridge.git ~/claude-tg-bridge
cd ~/claude-tg-bridge
pip install --user requests
mkdir -p ~/.claude-tg-bridge
cp env.example ~/.claude-tg-bridge/env
chmod 600 ~/.claude-tg-bridge/env
# edit ~/.claude-tg-bridge/env with your bot token + Telegram user ID
python3 bridge.py
```

## Requirements

- Python 3.10+
- The official Claude CLI installed and authed (`claude login`)
- A Telegram bot from `@BotFather`
- Your Telegram user ID (ask `@userinfobot`)

## Configure

`~/.claude-tg-bridge/env`:

```env
TELEGRAM_BOT_TOKEN=123456:ABC...
TELEGRAM_ALLOWLIST=12345678,87654321
CLAUDE_BIN=/path/to/claude          # optional, falls back to $PATH
CLAUDE_MODEL=sonnet                 # optional
CLAW_WORK_DIR=/home/you/notes       # optional, the cwd the CLI runs in
CLAUDE_TIMEOUT_SEC=600              # optional
```

Optional: drop a `USER.md` in `~/.claude-tg-bridge/USER.md` — it's appended as system prompt on every call (persona, conventions, project context). See [`USER.md.example`](USER.md.example) for bridge-specific patterns worth keeping (Telegram output formatting, background-dispatch for long tasks, progress pings).

## Run as a systemd user service

```bash
mkdir -p ~/.config/systemd/user
cp claude-tg-bridge.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now claude-tg-bridge
journalctl --user -u claude-tg-bridge -f
```

The included unit appends to `~/.claude-tg-bridge/bridge.log`.

## Slash commands

| Command | What it does |
|---------|---|
| `/new`, `/reset`, `/start` | Drop the saved session and start fresh |
| `/status` | Show current session id, model, work-dir |
| `/help` | List commands |

Anything else is forwarded to `claude --print`.

## Why this design

The bridge invokes the official Claude CLI as a subprocess (`claude --print --output-format=json`) and forwards stdin/stdout. That keeps three things simple:

- **No model client.** No retry/streaming/auth code in the bridge — the CLI handles all of that, the same way it does in your terminal.
- **No re-implementation of CLI features.** `claude` already does file reads, bash, MCP servers, slash commands, plugin discovery, CLAUDE.md auto-discovery. The bridge inherits all of it for free by setting `cwd` correctly.
- **Same auth as your terminal.** Whatever you logged into with `claude login` is what the bridge uses. No extra credentials to manage.

A small implementation note: because subprocess invocations of the CLI behave like normal CLI usage (and not like direct API calls), the bridge naturally inherits your CLI auth's billing behavior. There's no separate API-key path to configure.

## Security notes

- **The allowlist is mandatory.** The bridge refuses to start without `TELEGRAM_ALLOWLIST` set.
- The CLI runs with `--permission-mode bypassPermissions`, meaning tools fire without prompting. Don't expose this to people you don't trust, and think about what `CLAW_WORK_DIR` lets the agent see and modify.
- `chmod 600` your env file. Bot tokens grant full bot control.
- Per-chat sessions are persisted in `~/.claude-tg-bridge/state.json`. Treat that like a private chat history.

## Limitations

- **Single user / personal scale.** Designed for one person, possibly a small allowlist. Not multi-tenant.
- **No streaming inside Telegram.** The bridge calls `--output-format=json` (single result). For tasks that take >30s, the bot will just sit on `status: thinking` until the CLI returns. Streaming with `--output-format=stream-json` and `editMessageText` updates is a future option.
- **Telegram only.** The bridge layer is small — swapping for Slack/Discord/iMessage is a ~50-line job.

## License

MIT — see [LICENSE](LICENSE).

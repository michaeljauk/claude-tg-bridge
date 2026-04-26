#!/usr/bin/env python3
"""claude-tg-bridge — Telegram <-> claude CLI.

A small, personal Telegram bridge for the official Claude CLI. The bot
subprocesses `claude --print --output-format=json` and forwards results
back to Telegram, so it behaves like running claude in your terminal —
same context, same files, same auth.

Allowlist-gated. Per-chat session continuity via state.json (resume by
session_id returned in --output-format=json).

Configuration via environment variables:
  TELEGRAM_BOT_TOKEN   (required)  — Telegram bot token
  TELEGRAM_ALLOWLIST   (required)  — comma-separated user IDs that may use the bot
  CLAUDE_BIN           (optional)  — path to the claude CLI (default: looks in PATH)
  CLAUDE_MODEL         (optional)  — model alias passed to --model (default: sonnet)
  CLAW_WORK_DIR        (optional)  — working dir for the claude subprocess
  CLAW_STATE_DIR       (optional)  — where to keep state.json + USER.md (default: ~/.claude-tg-bridge)
  CLAUDE_TIMEOUT_SEC   (optional)  — subprocess timeout (default: 600)

Optional file at $CLAW_STATE_DIR/USER.md is appended as system prompt.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import traceback
from pathlib import Path

import requests

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
ALLOWLIST = {
    int(x.strip())
    for x in os.environ.get("TELEGRAM_ALLOWLIST", "").split(",")
    if x.strip()
}
CLAUDE_BIN = os.environ.get("CLAUDE_BIN") or shutil.which("claude") or "claude"
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "sonnet")
WORK_DIR = Path(os.environ.get("CLAW_WORK_DIR", str(Path.home())))
STATE_DIR = Path(os.environ.get("CLAW_STATE_DIR", str(Path.home() / ".claude-tg-bridge")))
STATE_FILE = STATE_DIR / "state.json"
USER_MD = STATE_DIR / "USER.md"
CLAUDE_TIMEOUT_SEC = int(os.environ.get("CLAUDE_TIMEOUT_SEC", "600"))

LOG_PREFIX = "[claude-tg-bridge]"
API = f"https://api.telegram.org/bot{BOT_TOKEN}"
TG_MAX = 4000

STATE_DIR.mkdir(parents=True, exist_ok=True)


def log(*parts: object) -> None:
    print(LOG_PREFIX, *parts, flush=True)


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except json.JSONDecodeError:
            log("state.json corrupt — starting fresh")
    return {}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2))


def tg(method: str, payload: dict) -> dict:
    r = requests.post(f"{API}/{method}", json=payload, timeout=30)
    return r.json()


def send(chat_id: int, text: str) -> int | None:
    res = tg("sendMessage", {"chat_id": chat_id, "text": text[:TG_MAX]})
    if not res.get("ok"):
        log("sendMessage failed", res)
        return None
    return res["result"]["message_id"]


def edit(chat_id: int, message_id: int, text: str) -> None:
    res = tg(
        "editMessageText",
        {"chat_id": chat_id, "message_id": message_id, "text": text[:TG_MAX]},
    )
    if not res.get("ok"):
        log("editMessageText failed", res)


def send_chunked(chat_id: int, text: str) -> None:
    if len(text) <= TG_MAX:
        send(chat_id, text or "(empty reply)")
        return
    for i in range(0, len(text), TG_MAX):
        send(chat_id, text[i : i + TG_MAX])


def run_claude(message: str, state: dict, chat_key: str) -> tuple[str, str | None]:
    args = [
        CLAUDE_BIN,
        "--print",
        "--output-format",
        "json",
        "--permission-mode",
        "bypassPermissions",
        "--model",
        CLAUDE_MODEL,
    ]
    if USER_MD.exists():
        args += ["--append-system-prompt", USER_MD.read_text()]
    if chat_key in state:
        args += ["--resume", state[chat_key]]
    args.append(message)

    log(f"running claude (resume={state.get(chat_key)})")
    proc = subprocess.run(
        args,
        capture_output=True,
        text=True,
        cwd=str(WORK_DIR),
        timeout=CLAUDE_TIMEOUT_SEC,
    )
    if proc.returncode != 0:
        return f"[claude exit {proc.returncode}]\n{proc.stderr[:1500]}", None

    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return f"[non-JSON output]\n{proc.stdout[:2000]}", None

    reply = data.get("result") or data.get("response") or "(no result key in JSON)"
    new_session = data.get("session_id")
    return reply, new_session


def handle_command(chat_id: int, chat_key: str, state: dict, text: str) -> bool:
    cmd = text.strip().lower()
    if cmd in ("/new", "/reset", "/start"):
        state.pop(chat_key, None)
        save_state(state)
        send(chat_id, "fresh session ready.")
        return True
    if cmd == "/status":
        sid = state.get(chat_key, "(none)")
        send(chat_id, f"session: {sid}\nmodel: {CLAUDE_MODEL}\nwork-dir: {WORK_DIR}")
        return True
    if cmd == "/help":
        send(
            chat_id,
            "/new — start a fresh session\n"
            "/status — current session id + config\n"
            "/help — this message\n"
            "anything else — sent to claude",
        )
        return True
    return False


def handle_update(update: dict, state: dict) -> None:
    msg = update.get("message") or update.get("edited_message")
    if not msg:
        return
    chat_id = msg["chat"]["id"]
    user_id = msg.get("from", {}).get("id")
    text = msg.get("text", "")

    if user_id not in ALLOWLIST:
        log(f"reject user_id={user_id}")
        return
    if not text:
        return

    chat_key = str(chat_id)

    if text.startswith("/") and handle_command(chat_id, chat_key, state, text):
        return

    status_id = send(chat_id, "status: thinking")
    try:
        reply, new_session = run_claude(text, state, chat_key)
        if new_session:
            state[chat_key] = new_session
            save_state(state)
        if status_id and len(reply) <= TG_MAX:
            edit(chat_id, status_id, reply or "(empty)")
        else:
            if status_id:
                edit(chat_id, status_id, "status: done")
            send_chunked(chat_id, reply)
    except subprocess.TimeoutExpired:
        msg_text = f"[timeout after {CLAUDE_TIMEOUT_SEC}s]"
        if status_id:
            edit(chat_id, status_id, msg_text)
        else:
            send(chat_id, msg_text)
    except Exception as e:  # noqa: BLE001
        log("handler error", traceback.format_exc())
        err = f"[bridge error] {e}"
        if status_id:
            edit(chat_id, status_id, err)
        else:
            send(chat_id, err)


def main() -> int:
    log(f"starting; allowlist={sorted(ALLOWLIST)}; cwd={WORK_DIR}; model={CLAUDE_MODEL}")
    if not ALLOWLIST:
        log("FATAL: TELEGRAM_ALLOWLIST is empty — refusing to run")
        return 2
    if not CLAUDE_BIN or not Path(CLAUDE_BIN).exists():
        log(f"FATAL: claude CLI not found at {CLAUDE_BIN!r}; set CLAUDE_BIN")
        return 2

    state = load_state()
    offset = 0
    while True:
        try:
            r = requests.get(
                f"{API}/getUpdates",
                params={"offset": offset, "timeout": 30},
                timeout=40,
            )
            data = r.json()
            if not data.get("ok"):
                log("getUpdates not ok", data)
                time.sleep(5)
                continue
            for u in data["result"]:
                offset = u["update_id"] + 1
                handle_update(u, state)
        except requests.RequestException as e:
            log("network error", e)
            time.sleep(5)
        except KeyboardInterrupt:
            log("interrupted")
            return 0
        except Exception:
            log("loop error", traceback.format_exc())
            time.sleep(5)


if __name__ == "__main__":
    sys.exit(main())

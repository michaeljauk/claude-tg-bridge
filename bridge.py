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

import html
import json
import os
import re
import shutil
import subprocess
import sys
import threading
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
UPLOAD_DIR = STATE_DIR / "uploads"
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


_state_lock = threading.Lock()


def save_state(state: dict) -> None:
    with _state_lock:
        tmp = STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=2))
        tmp.replace(STATE_FILE)


def tg(method: str, payload: dict) -> dict:
    r = requests.post(f"{API}/{method}", json=payload, timeout=30)
    return r.json()


_FILENAME_SAFE_RE = re.compile(r"[^A-Za-z0-9._-]+")


def download_telegram_file(file_id: str, dest_dir: Path) -> Path | None:
    """Fetch a Telegram file by file_id and save it under dest_dir.

    Returns the local path on success, or None if anything goes wrong (no file
    path, HTTP error, write error). Capped at Telegram's 20 MB getFile limit.
    """
    res = tg("getFile", {"file_id": file_id})
    if not res.get("ok"):
        log("getFile failed", res)
        return None
    rel = res["result"].get("file_path")
    if not rel:
        return None
    suffix = Path(rel).suffix or ".bin"
    safe_stem = _FILENAME_SAFE_RE.sub("_", Path(rel).stem) or "file"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{int(time.time())}_{safe_stem}{suffix}"
    url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{rel}"
    try:
        with requests.get(url, timeout=60, stream=True) as r:
            if r.status_code != 200:
                log(f"file download failed: HTTP {r.status_code}")
                return None
            with open(dest, "wb") as f:
                for chunk in r.iter_content(64 * 1024):
                    f.write(chunk)
    except requests.RequestException as e:
        log("file download error", e)
        return None
    return dest


def extract_message_input(msg: dict) -> tuple[str, list[Path]]:
    """Pull text/caption + any attached images from a Telegram message.

    Photos: take the largest size variant. Image documents (mime_type
    image/*): download as-is. Everything else is ignored — we don't try to
    feed PDFs, audio, or video to claude.
    """
    text = (msg.get("text") or msg.get("caption") or "").strip()
    files: list[Path] = []

    photos = msg.get("photo") or []
    if photos:
        largest = photos[-1]
        f = download_telegram_file(largest["file_id"], UPLOAD_DIR)
        if f:
            files.append(f)

    doc = msg.get("document") or {}
    if doc and (doc.get("mime_type") or "").startswith("image/"):
        f = download_telegram_file(doc["file_id"], UPLOAD_DIR)
        if f:
            files.append(f)

    return text, files


_TABLE_RE = re.compile(
    r"(^\|.*\|[ \t]*\n^\|[\s\-:|]+\|[ \t]*\n(?:^\|.*\|[ \t]*\n?)+)",
    re.MULTILINE,
)
_FENCE_RE = re.compile(r"```[a-zA-Z0-9_+\-]*\n?(.*?)```", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")
_BOLD_RE = re.compile(r"\*\*([^*\n]+)\*\*")
_HEADING_RE = re.compile(r"^#{1,6}\s+(.+?)\s*$", re.MULTILINE)
_LINK_RE = re.compile(r"\[([^\]\n]+)\]\(([^)\s]+)\)")


def md_to_html(text: str) -> str:
    """Convert Claude's markdown to Telegram-flavored HTML.

    Tables become <pre> blocks (Telegram has no table primitive). Code fences
    and inline code are escaped + wrapped. Bold, headings, and links are
    converted. Everything else is HTML-escaped so stray <>& don't break parse.
    """
    blocks: list[tuple[str, str]] = []

    def stash(content: str, tag: str) -> str:
        blocks.append((tag, content))
        return f"\x00{len(blocks) - 1}\x00"

    text = _TABLE_RE.sub(lambda m: stash(m.group(1).rstrip(), "pre"), text)
    text = _FENCE_RE.sub(lambda m: stash(m.group(1), "pre"), text)
    text = _INLINE_CODE_RE.sub(lambda m: stash(m.group(1), "code"), text)

    text = html.escape(text, quote=False)

    text = _HEADING_RE.sub(r"<b>\1</b>", text)
    text = _BOLD_RE.sub(r"<b>\1</b>", text)
    text = _LINK_RE.sub(r'<a href="\2">\1</a>', text)

    def restore(m: re.Match) -> str:
        tag, content = blocks[int(m.group(1))]
        return f"<{tag}>{html.escape(content, quote=False)}</{tag}>"

    return re.sub(r"\x00(\d+)\x00", restore, text)


def _send_or_edit(method: str, payload: dict) -> dict:
    """Try with HTML parse_mode; on parse error retry as plain text."""
    res = tg(method, {**payload, "parse_mode": "HTML"})
    if res.get("ok"):
        return res
    desc = (res.get("description") or "").lower()
    if "parse" in desc and ("entit" in desc or "tag" in desc):
        # malformed HTML — fall back to plain text so the message still lands
        return tg(method, payload)
    return res


def send(chat_id: int, text: str) -> int | None:
    payload = {"chat_id": chat_id, "text": md_to_html(text[:TG_MAX])}
    res = _send_or_edit("sendMessage", payload)
    if not res.get("ok"):
        log("sendMessage failed", res)
        return None
    return res["result"]["message_id"]


def edit(chat_id: int, message_id: int, text: str) -> None:
    payload = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": md_to_html(text[:TG_MAX]),
    }
    res = _send_or_edit("editMessageText", payload)
    if not res.get("ok"):
        # "message is not modified" is benign — Telegram rejects no-op edits.
        if "not modified" in (res.get("description") or "").lower():
            return
        log("editMessageText failed", res)


def send_chunked(chat_id: int, text: str) -> None:
    if len(text) <= TG_MAX:
        send(chat_id, text or "(empty reply)")
        return
    for i in range(0, len(text), TG_MAX):
        send(chat_id, text[i : i + TG_MAX])


EDIT_THROTTLE_SEC = 1.5
TICKER_INTERVAL_SEC = 5.0
TOOL_INPUT_PREVIEW_LEN = 80


def _fmt_elapsed(seconds: float) -> str:
    sec = int(seconds)
    if sec < 60:
        return f"{sec}s"
    m, s = divmod(sec, 60)
    if m < 60:
        return f"{m}m {s}s"
    h, m = divmod(m, 60)
    return f"{h}h {m}m"


def _summarize_tool_input(name: str, raw_input: dict) -> str:
    """Pick a short, human-readable hint from a tool_use block's input."""
    if not isinstance(raw_input, dict):
        return name
    for key in ("description", "command", "pattern", "file_path", "path", "url", "query", "prompt"):
        v = raw_input.get(key)
        if isinstance(v, str) and v:
            v = v.replace("\n", " ").strip()
            if len(v) > TOOL_INPUT_PREVIEW_LEN:
                v = v[:TOOL_INPUT_PREVIEW_LEN - 1] + "…"
            return f"{name}({v})"
    return name


def run_claude_streaming(
    message: str,
    state: dict,
    chat_key: str,
    chat_id: int,
    status_id: int | None,
) -> tuple[str, str | None]:
    """Run claude with stream-json, edit Telegram message as events arrive.

    Returns (final_text, session_id).
    """
    args = [
        CLAUDE_BIN,
        "--print",
        "--output-format",
        "stream-json",
        "--verbose",
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

    log(f"running claude streaming (resume={state.get(chat_key)})")

    proc = subprocess.Popen(
        args,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
        cwd=str(WORK_DIR),
    )

    start_ts = time.monotonic()
    deadline = start_ts + CLAUDE_TIMEOUT_SEC
    new_session: str | None = None
    state_box = {"text": "", "status": "started"}
    last_rendered = ""
    last_edit_ts = 0.0
    edit_lock = threading.Lock()

    def render() -> str:
        elapsed = _fmt_elapsed(time.monotonic() - start_ts)
        footer = f"⏱ {elapsed} · {state_box['status']}"
        text = state_box["text"]
        if text:
            avail = TG_MAX - len(footer) - 4
            return f"{text[:avail]}\n\n{footer}"
        return footer

    def push(force: bool = False) -> None:
        nonlocal last_edit_ts, last_rendered
        if not status_id:
            return
        now = time.monotonic()
        if not force and (now - last_edit_ts) < EDIT_THROTTLE_SEC:
            return
        rendered = render()
        if rendered == last_rendered and not force:
            return
        edit(chat_id, status_id, rendered)
        last_rendered = rendered
        last_edit_ts = now

    ticker_stop = threading.Event()

    def ticker() -> None:
        while not ticker_stop.wait(TICKER_INTERVAL_SEC):
            with edit_lock:
                push()

    ticker_thread = threading.Thread(target=ticker, daemon=True)
    ticker_thread.start()

    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            if time.monotonic() > deadline:
                proc.kill()
                return f"[timeout after {CLAUDE_TIMEOUT_SEC}s]", new_session

            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue

            sid = ev.get("session_id")
            if sid:
                new_session = sid

            t = ev.get("type")

            if t == "system" and ev.get("subtype") == "init":
                with edit_lock:
                    state_box["status"] = "started"
                    push(force=True)
                continue

            if t == "assistant":
                content = ev.get("message", {}).get("content", []) or []
                for block in content:
                    btype = block.get("type")
                    if btype == "text":
                        txt = block.get("text", "")
                        if txt and txt != state_box["text"]:
                            with edit_lock:
                                state_box["text"] = txt
                                push()
                    elif btype == "tool_use":
                        label = _summarize_tool_input(block.get("name", "tool"), block.get("input", {}))
                        if label != state_box["status"]:
                            with edit_lock:
                                state_box["status"] = label
                                push(force=True)
                    elif btype == "thinking":
                        if state_box["status"] != "thinking":
                            with edit_lock:
                                state_box["status"] = "thinking"
                                push()
                continue

            if t == "result":
                final_from_result = ev.get("result")
                if final_from_result:
                    state_box["text"] = final_from_result
                break

        proc.wait(timeout=10)
        if proc.returncode and proc.returncode != 0:
            err = (proc.stderr.read() if proc.stderr else "") or ""
            return f"[claude exit {proc.returncode}]\n{err[:1500]}", new_session

    except subprocess.TimeoutExpired:
        proc.kill()
        return f"[timeout after {CLAUDE_TIMEOUT_SEC}s]", new_session
    finally:
        ticker_stop.set()
        if proc.poll() is None:
            proc.kill()

    return state_box["text"] or "(no text produced)", new_session


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

    if user_id not in ALLOWLIST:
        log(f"reject user_id={user_id}")
        return

    chat_key = str(chat_id)

    text, files = extract_message_input(msg)

    if text.startswith("/") and handle_command(chat_id, chat_key, state, text):
        return

    if not text and not files:
        return

    if files:
        refs = "\n".join(f"@{p}" for p in files)
        prompt_caption = text or "What's in this image?"
        prompt = f"{refs}\n\n{prompt_caption}"
    else:
        prompt = text

    status_id = send(chat_id, "⏱ 0s · starting")
    try:
        reply, new_session = run_claude_streaming(prompt, state, chat_key, chat_id, status_id)
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

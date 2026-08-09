#!/usr/bin/env python3
"""
tg_bridge.py - a Telegram bot that is a phone remote for a local Claude Code agent.

Each incoming Telegram message is fed to a single persistent headless Claude Code
conversation running in a directory of your choice (your notes vault, a repo, a homelab
config tree — wherever its CLAUDE.md and tools live). The reply is sent back to Telegram,
streamed live: the bot posts a preview bubble and edits it in place as text arrives, with
each tool call shown as a code box. State (session id + update offset) is persisted, so
the bot is restart-safe and every message continues the same conversation.

Posture: read + write files, web research, run a whitelisted scripts dir. The agent may
NOT run arbitrary Bash. One whitelisted Telegram user only.

Extras: voice notes are transcribed locally (faster-whisper, optional), and photos/PDFs
are downloaded so the agent can Read them.

Setup:
  1. Create a bot with @BotFather, copy the token.
  2. Put your token + Telegram numeric user id in .secrets/tg_bridge.json:
        { "bot_token": "123:ABC...", "allowed_user_id": 0 }
     (Leave allowed_user_id 0 to discover it: message the bot once and it will
      reply with your id; paste it in and restart.)
  3. Run:  python tg_bridge.py   (or under pm2, see README)
  Keep this process (and the machine) running to use it while away.

No third-party deps required (faster-whisper only if you want voice). Stdlib otherwise.
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
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
# The working directory the agent runs in (its "brain": notes, code, whatever you point it
# at). Defaults to this repo's directory; override with TG_BRIDGE_CWD.
AGENT_CWD = Path(os.environ.get("TG_BRIDGE_CWD", str(BASE_DIR)))
SECRETS_DIR = BASE_DIR / ".secrets"
CONFIG_PATH = SECRETS_DIR / "tg_bridge.json"
STATE_PATH = SECRETS_DIR / "tg_bridge.state.json"
MEDIA_DIR = BASE_DIR / "_media"  # gitignored: images + voice notes stay local

# What the remote agent is allowed to do without a human to approve prompts.
# "Read + write files": recall, capture, research, run your scripts dir. No free Bash.
# The Bash(python _tools/*) entries are an EXAMPLE of whitelisting one scripts directory
# (relative to AGENT_CWD) — point them at your own automation dir or delete them.
ALLOWED_TOOLS = [
    "Read", "Grep", "Glob", "Edit", "Write", "TodoWrite",
    "WebSearch", "WebFetch",
    "Bash(python _tools/*)", "Bash(py _tools/*)",
]
PERMISSION_MODE = "acceptEdits"
CLAUDE_TIMEOUT_SEC = 900          # a single turn may take a while
TG_LONGPOLL_SEC = 50
TG_MAX_CHARS = 4000               # Telegram hard cap is 4096; leave headroom
WHISPER_MODEL_SIZE = "small"      # faster-whisper model for voice transcription


# --------------------------------------------------------------------------- io
def log(msg: str) -> None:
    sys.stdout.buffer.write((msg + "\n").encode("utf-8", "replace"))
    sys.stdout.flush()


def load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_state(state: dict) -> None:
    SECRETS_DIR.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2), encoding="utf-8")


_whisper_model = None  # lazily loaded; reused across turns (load is expensive)


def transcribe_audio(path: Path):
    """Transcribe an audio file locally with faster-whisper. Returns (text, error):
    exactly one is non-None, so the caller can tell the user *why* it failed."""
    global _whisper_model
    try:
        if _whisper_model is None:
            from faster_whisper import WhisperModel
            log(f"[tg_bridge] loading whisper model '{WHISPER_MODEL_SIZE}' (first use)…")
            _whisper_model = WhisperModel(WHISPER_MODEL_SIZE, device="cpu",
                                          compute_type="int8")
        segments, _info = _whisper_model.transcribe(str(path), beam_size=5)
        text = " ".join(s.text.strip() for s in segments).strip()
        return (text, None) if text else (None, "empty transcript (silent clip?)")
    except ImportError:
        log("[warn] faster-whisper not installed: pip install faster-whisper")
        return (None, "faster-whisper not installed (pip install faster-whisper)")
    except Exception as e:
        log(f"[warn] transcription failed: {e}")
        return (None, f"{type(e).__name__}: {e}")


def find_claude() -> str:
    for name in ("claude", "claude.exe", "claude.cmd"):
        p = shutil.which(name)
        if p:
            return p
    fallback = Path.home() / ".local" / "bin" / "claude.EXE"
    if fallback.exists():
        return str(fallback)
    raise RuntimeError("Could not find the 'claude' executable on PATH.")


# ----------------------------------------------------------- markdown -> html
def md_to_tg_html(text: str) -> str:
    """Render the subset of Markdown the agent emits into Telegram-safe HTML.

    Telegram has no Markdown headers/lists and its MarkdownV2 needs heavy escaping,
    so we target HTML parse_mode: escape the prose, then map **bold**, `code`, ```fences```,
    ## headers, [text](url), [[wikilinks]] and - bullets onto the tags Telegram supports."""
    # 1) Stash code spans first so inline markdown inside them isn't touched.
    blocks: list[str] = []

    def stash(m):
        blocks.append(m.group(0))
        return f"\x00{len(blocks) - 1}\x00"

    text = re.sub(r"```[\s\S]*?```", stash, text)
    text = re.sub(r"`[^`\n]+`", stash, text)

    # 2) Escape HTML-significant chars in the remaining prose.
    text = html.escape(text, quote=False)

    # 3) Block + inline conversions.
    text = re.sub(r"(?m)^\s{0,3}#{1,6}\s+(.+?)\s*$", r"<b>\1</b>", text)   # headers
    text = re.sub(r"(?m)^\s*([-*_])\1{2,}\s*$", "", text)                  # --- rules
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)                    # **bold**
    text = re.sub(r"(?<!\w)__(.+?)__(?!\w)", r"<b>\1</b>", text)           # __bold__
    text = re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+)\)",
                  r'<a href="\2">\1</a>', text)                            # [t](url)
    text = re.sub(r"\[\[([^\]]+)\]\]", r"<b>\1</b>", text)                 # [[wikilink]]
    text = re.sub(r"(?m)^(\s*)[-*]\s+", r"\1• ", text)               # bullets -> •

    # 4) Restore code as <pre>/<code>.
    def unstash(m):
        raw = blocks[int(m.group(1))]
        if raw.startswith("```"):
            inner = re.sub(r"^```[^\n]*\n?", "", raw)
            inner = re.sub(r"\n?```$", "", inner)
            return f"<pre>{html.escape(inner, quote=False)}</pre>"
        return f"<code>{html.escape(raw.strip('`'), quote=False)}</code>"

    return re.sub(r"\x00(\d+)\x00", unstash, text)


# --------------------------------------------------------------- telegram api
class Telegram:
    def __init__(self, token: str):
        self.base = f"https://api.telegram.org/bot{token}"
        self.file_base = f"https://api.telegram.org/file/bot{token}"

    def download_file(self, file_id: str, dest_dir: Path, stem: str) -> Path | None:
        """Resolve a Telegram file_id and download it into dest_dir/<stem><ext>."""
        try:
            res = self._call("getFile", {"file_id": file_id})
            if not res.get("ok"):
                return None
            fpath = res["result"]["file_path"]
            ext = Path(fpath).suffix or ".bin"
            dest_dir.mkdir(parents=True, exist_ok=True)
            dest = dest_dir / f"{stem}{ext}"
            with urllib.request.urlopen(f"{self.file_base}/{fpath}", timeout=120) as r:
                dest.write_bytes(r.read())
            return dest
        except Exception as e:
            log(f"[warn] download_file failed: {e}")
            return None

    def _call(self, method: str, params: dict, timeout: int = 60) -> dict:
        data = urllib.parse.urlencode(params).encode("utf-8")
        req = urllib.request.Request(f"{self.base}/{method}", data=data)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def get_updates(self, offset: int) -> list[dict]:
        params = {"timeout": TG_LONGPOLL_SEC, "offset": offset,
                  "allowed_updates": json.dumps(["message"])}
        try:
            res = self._call("getUpdates", params, timeout=TG_LONGPOLL_SEC + 15)
        except Exception as e:  # network blip: back off and retry
            log(f"[warn] getUpdates failed: {e}")
            time.sleep(3)
            return []
        return res.get("result", []) if res.get("ok") else []

    def send(self, chat_id: int, text: str) -> None:
        text = text or "(empty response)"
        # Chunk the raw text, then render each chunk so a split never breaks a tag.
        for i in range(0, len(text), TG_MAX_CHARS):
            chunk = text[i:i + TG_MAX_CHARS]
            params = {"chat_id": chat_id, "text": md_to_tg_html(chunk),
                      "parse_mode": "HTML", "disable_web_page_preview": "true"}
            try:
                res = self._call("sendMessage", params)
                if not res.get("ok"):
                    raise ValueError(res.get("description", "ok=false"))
            except Exception as e:
                # Bad markup shouldn't drop the message: resend as plain text.
                log(f"[warn] HTML send failed ({e}); retrying plain")
                try:
                    self._call("sendMessage", {"chat_id": chat_id, "text": chunk})
                except Exception as e2:
                    log(f"[warn] plain send failed: {e2}")

    def typing(self, chat_id: int) -> None:
        try:
            self._call("sendChatAction", {"chat_id": chat_id, "action": "typing"})
        except Exception:
            pass

    def send_status(self, chat_id: int, text: str) -> int | None:
        """Send a plain status message and return its id (for later edit/delete)."""
        try:
            res = self._call("sendMessage", {"chat_id": chat_id, "text": text})
            if res.get("ok"):
                return res["result"]["message_id"]
        except Exception as e:
            log(f"[warn] send_status failed: {e}")
        return None

    def send_html(self, chat_id: int, html_text: str) -> int | None:
        """Send a pre-rendered HTML message (live preview); return its id."""
        try:
            res = self._call("sendMessage",
                             {"chat_id": chat_id, "text": html_text, "parse_mode": "HTML",
                              "disable_web_page_preview": "true"})
            if res.get("ok"):
                return res["result"]["message_id"]
        except Exception as e:
            log(f"[warn] send_html failed: {e}")
        return None

    def edit_html(self, chat_id: int, message_id: int, html_text: str) -> bool:
        """Edit a message with pre-rendered HTML (live preview)."""
        try:
            res = self._call("editMessageText",
                             {"chat_id": chat_id, "message_id": message_id,
                              "text": html_text, "parse_mode": "HTML",
                              "disable_web_page_preview": "true"})
            return bool(res.get("ok"))
        except Exception:
            return False

    def edit_message(self, chat_id: int, message_id: int, text: str) -> None:
        try:
            self._call("editMessageText",
                       {"chat_id": chat_id, "message_id": message_id, "text": text})
        except Exception:
            pass

    def edit_message_html(self, chat_id: int, message_id: int, text: str) -> bool:
        """Edit a message with rendered HTML. Returns True on success (for finalize)."""
        try:
            res = self._call("editMessageText",
                             {"chat_id": chat_id, "message_id": message_id,
                              "text": md_to_tg_html(text), "parse_mode": "HTML",
                              "disable_web_page_preview": "true"})
            return bool(res.get("ok"))
        except Exception:
            return False

    def delete_message(self, chat_id: int, message_id: int) -> None:
        try:
            self._call("deleteMessage", {"chat_id": chat_id, "message_id": message_id})
        except Exception:
            pass


# -------------------------------------------------------------- claude bridge
def run_claude(claude: str, message: str, session_id: str | None) -> tuple[str, str | None]:
    """Run one headless turn. Returns (reply_text, new_session_id)."""
    cmd = [claude, "-p", "--output-format", "json",
           "--permission-mode", PERMISSION_MODE, "--allowedTools", *ALLOWED_TOOLS]
    if session_id:
        cmd += ["--resume", session_id]
    else:
        session_id = str(uuid.uuid4())
        cmd += ["--session-id", session_id]

    env = dict(os.environ, PYTHONUTF8="1", CLAUDE_BRAIN_BRIDGE="1")
    try:
        proc = subprocess.run(
            cmd, input=message, cwd=str(AGENT_CWD),
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=CLAUDE_TIMEOUT_SEC, env=env,
            # Hidden window: under pm2 this child would otherwise flash a console per turn.
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired:
        return ("[bridge] Claude timed out on that turn. Try a smaller ask.", session_id)

    if proc.returncode != 0:
        # A resume can fail if the session was lost; signal caller to reset.
        err = (proc.stderr or proc.stdout or "").strip()[:500]
        if session_id and ("No conversation" in err or "not found" in err.lower()):
            return ("__RESET__", None)
        return (f"[bridge] claude exited {proc.returncode}: {err}", session_id)

    try:
        out = json.loads(proc.stdout)
        return (out.get("result", "").strip(), out.get("session_id", session_id))
    except json.JSONDecodeError:
        return (proc.stdout.strip()[:TG_MAX_CHARS] or "(no output)", session_id)


def _deliver(tg, chat_id, bubble_id, text: str) -> None:
    """Turn the live preview bubble into the final, properly formatted answer.
    In-place HTML edit for single-message replies (no flicker); else delete + send."""
    if bubble_id is not None and len(text) <= TG_MAX_CHARS and tg.edit_message_html(
            chat_id, bubble_id, text):
        return
    if bubble_id is not None:
        tg.delete_message(chat_id, bubble_id)
    tg.send(chat_id, text)


def tool_summary(name, inp):
    """A live, human-readable line for a tool call in the stream view — shows the code
    for Edit/Write, the command for Bash, the pattern for Grep/Glob, etc."""
    inp = inp or {}

    def clip(s, limit=700):
        s = s or ""
        return s if len(s) <= limit else s[:limit] + "\n…"

    if name == "Edit":
        return f"Edit {inp.get('file_path', '')}\n{clip(inp.get('new_string', ''))}"
    if name == "Write":
        return f"Write {inp.get('file_path', '')}\n{clip(inp.get('content', ''))}"
    if name == "Read":
        return f"Read {inp.get('file_path', '')}"
    if name == "Bash":
        return f"$ {clip(inp.get('command', ''), 300)}"
    if name in ("Grep", "Glob"):
        return f"{name} {inp.get('pattern', '')}"
    if name == "WebSearch":
        return f"WebSearch {inp.get('query', '')}"
    if name == "WebFetch":
        return f"WebFetch {inp.get('url', '')}"
    return f"{name or 'tool'}"


def run_claude_streaming(tg, chat_id, claude, message, session_id):
    """Stream one turn live to Telegram by editing a message as text arrives, then
    finalize with proper formatting. Returns (session_id, reset_needed).

    Best-effort: the live view is a bonus, but if the stream fails to produce an
    answer we fall back to the proven non-streaming run_claude so a reply is never lost."""
    cmd = [claude, "-p", "--output-format", "stream-json", "--include-partial-messages",
           "--verbose", "--permission-mode", PERMISSION_MODE, "--allowedTools", *ALLOWED_TOOLS]
    if session_id:
        cmd += ["--resume", session_id]
    else:
        session_id = str(uuid.uuid4())
        cmd += ["--session-id", session_id]
    env = dict(os.environ, PYTHONUTF8="1", CLAUDE_BRAIN_BRIDGE="1")

    try:
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, encoding="utf-8",
                                errors="replace", cwd=str(AGENT_CWD), env=env, bufsize=1,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except Exception as e:
        tg.send(chat_id, f"[bridge] failed to start claude: {e}")
        return (session_id, False)

    killer = threading.Timer(CLAUDE_TIMEOUT_SEC, proc.kill)
    killer.start()
    try:
        proc.stdin.write(message)
        proc.stdin.close()
    except Exception:
        pass

    tg.typing(chat_id)
    segments = []            # ordered [("text", str) | ("tool", str)] shown live
    bubble_id = None
    last_edit = 0.0
    final_result = None
    new_sid = session_id
    seen_tools = set()       # dedup tool markers across event shapes

    def render():
        """Build live-view HTML: escaped answer text + each tool as a <pre> code box."""
        parts = []
        for kind, txt in segments:
            if kind == "tool":
                parts.append(f"\n<pre>{html.escape(txt)}</pre>\n")
            else:
                parts.append(html.escape(txt))
        # Trim from the front (whole segments) if too long, so we never cut a tag.
        while len("".join(parts)) > TG_MAX_CHARS and len(parts) > 1:
            parts.pop(0)
        return "".join(parts)[:TG_MAX_CHARS]

    def preview():
        nonlocal bubble_id
        body = render()
        if not body.strip():
            return
        if bubble_id is None:
            bubble_id = tg.send_html(chat_id, body)
        else:
            tg.edit_html(chat_id, bubble_id, body)

    def add_text(t):
        if segments and segments[-1][0] == "text":
            segments[-1] = ("text", segments[-1][1] + t)
        else:
            segments.append(("text", t))

    def mark_tool(tool_id, box):
        nonlocal last_edit
        if not tool_id or tool_id in seen_tools:
            return
        seen_tools.add(tool_id)
        segments.append(("tool", box))
        preview()                          # show tool activity immediately
        last_edit = time.monotonic()

    for raw in iter(proc.stdout.readline, ""):   # readline = real-time on a live pipe
        line = raw.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        kind = ev.get("type")
        if kind == "system":
            new_sid = ev.get("session_id", new_sid)
        elif kind == "stream_event":
            e = ev.get("event", {})
            et = e.get("type")
            if et == "content_block_delta":
                d = e.get("delta", {})
                if d.get("type") == "text_delta":
                    add_text(d.get("text", ""))
                    now = time.monotonic()
                    if now - last_edit >= 0.9:   # throttle text edits (rate limit)
                        preview()
                        last_edit = now
                        tg.typing(chat_id)
            # tool_use is marked from the consolidated `assistant` event below, which
            # carries the full input (content_block_start fires before input streams).
        elif kind == "assistant":          # consolidated message: full tool input here
            for block in (ev.get("message") or {}).get("content", []):
                if block.get("type") == "tool_use":
                    mark_tool(block.get("id"), tool_summary(block.get("name"), block.get("input")))
        elif kind == "result":
            new_sid = ev.get("session_id", new_sid)
            final_result = ev.get("result")

    killer.cancel()
    proc.wait()
    err = ((proc.stderr.read() if proc.stderr else "") or "").strip()
    streamed_text = "".join(t for k, t in segments if k == "text")
    final = (final_result if final_result is not None else streamed_text).strip()

    if proc.returncode not in (0, None) and not final:
        if "No conversation" in err or "not found" in err.lower():
            if bubble_id is not None:
                tg.delete_message(chat_id, bubble_id)
            return (None, True)                      # stale session -> caller retries fresh
        # Hard failure: fall back to the proven non-streaming path.
        log(f"[warn] stream failed (rc={proc.returncode}); falling back. {err[:200]}")
        if bubble_id is not None:
            tg.delete_message(chat_id, bubble_id)
        reply, sid = run_claude(claude, message, session_id)
        if reply == "__RESET__":
            return (None, True)
        tg.send(chat_id, reply)
        return (sid, False)

    _deliver(tg, chat_id, bubble_id, final or "(no output)")
    return (new_sid, False)


# --------------------------------------------------------------------- driver
def main() -> int:
    cfg = load_json(CONFIG_PATH, {})
    token = cfg.get("bot_token") or os.environ.get("TG_BRIDGE_BOT_TOKEN")
    allowed = cfg.get("allowed_user_id") or int(os.environ.get("TG_BRIDGE_ALLOWED_USER_ID", "0") or 0)
    if not token:
        log(f"[fatal] No bot_token. Create {CONFIG_PATH} with your @BotFather token.")
        return 1

    claude = find_claude()
    tg = Telegram(token)
    state = load_json(STATE_PATH, {"offset": 0, "session_id": None})
    offset = state.get("offset", 0)
    session_id = state.get("session_id")

    log(f"[tg_bridge] up. agent_cwd={AGENT_CWD} claude={claude}")
    log(f"[tg_bridge] allowed_user_id={allowed or '(discovery mode)'}")

    while True:
        for upd in tg.get_updates(offset):
            offset = upd["update_id"] + 1
            state["offset"] = offset
            save_state(state)

            msg = upd.get("message") or {}
            text = (msg.get("text") or "").strip()
            caption = (msg.get("caption") or "").strip()
            chat_id = (msg.get("chat") or {}).get("id")
            from_id = (msg.get("from") or {}).get("id")
            photos = msg.get("photo") or []
            doc = msg.get("document") or {}
            has_image = bool(photos) or doc.get("mime_type", "").startswith("image/")
            has_doc = bool(doc.get("file_id")) and not has_image  # non-image document (PDF, etc.)
            voice = msg.get("voice") or msg.get("audio") or {}
            has_audio = bool(voice)
            if chat_id is None or not (text or caption or has_image or has_audio or has_doc):
                continue

            # Discovery / whitelist enforcement.
            if not allowed:
                log(f"[setup] message from user id {from_id}. Put this in tg_bridge.json.")
                tg.send(chat_id, f"Your Telegram id is {from_id}. Add it as "
                                 f"\"allowed_user_id\" in tg_bridge.json and restart.")
                continue
            if from_id != allowed:
                log(f"[warn] ignored message from non-whitelisted user {from_id}")
                continue

            # Lightweight commands.
            if text in ("/start", "/help"):
                tg.send(chat_id, "Brain online. Send text, photos, files, or voice notes.\n"
                                 "/new starts a fresh conversation.\n"
                                 "/reload restarts the bridge to pick up code changes.")
                continue
            if text == "/new":
                session_id = None
                state["session_id"] = None
                save_state(state)
                tg.send(chat_id, "Started a fresh conversation.")
                continue
            if text == "/reload":
                # Offset is already persisted above, so this message won't replay.
                save_state(state)
                tg.send(chat_id, "Reloading the bridge…")
                log("[tg_bridge] /reload: re-executing in place.")
                sys.stdout.flush()
                os.execv(sys.executable, [sys.executable, *sys.argv])

            tg.typing(chat_id)
            user_text = text or caption
            stem = str(upd["update_id"])
            note_lines = []  # context lines prepended for the agent

            # Image: download into the gitignored media folder, tell the agent to Read it.
            if has_image:
                file_id = photos[-1]["file_id"] if photos else doc.get("file_id")
                p = tg.download_file(file_id, MEDIA_DIR, stem) if file_id else None
                if not p:
                    tg.send(chat_id, "[bridge] Couldn't download that image. Try resending.")
                    continue
                note_lines.append(f"[The user sent an image via Telegram, saved at:\n{p}\n"
                                  f"Use the Read tool to view it.]")

            # Document (PDF, etc.): keep the ORIGINAL name (sanitized) so a relayed file reads as
            # "Invoice 1101.pdf" not a numeric id — but under a per-update-id subdir, because two
            # files sharing a name (a second "Invoice 1101.pdf") would otherwise overwrite the
            # first at MEDIA_DIR/<name>. The update_id is unique per message, so it cannot collide.
            if has_doc:
                fname = doc.get("file_name") or ""
                base = re.sub(r"[^\w .()\-]+", "_", os.path.splitext(fname)[0]).strip()
                p = tg.download_file(doc["file_id"], MEDIA_DIR / stem, base or stem)
                if not p:
                    # Download failed — do NOT drop the message. The caption is often the actual
                    # instruction ("here's the invoice"), so relay it with a marker rather than
                    # letting the whole turn vanish.
                    tg.send(chat_id, "[bridge] Couldn't download that file — relaying your text only.")
                    if caption:
                        note_lines.append(f"[The user sent a file ({fname or 'document'}) via Telegram "
                                          f"with this caption, but the file download FAILED — act on "
                                          f"the caption alone:]")
                    else:
                        continue
                else:
                    note_lines.append(f"[The user sent a file ({fname or doc.get('mime_type') or 'document'}) via Telegram, saved at:\n{p}\n"
                                      f"Use the Read tool to view it (PDFs and other documents supported).]")

            # Voice: download, transcribe locally, use the transcript as the user's words.
            if has_audio:
                a = tg.download_file(voice["file_id"], MEDIA_DIR, stem)
                transcript, terr = transcribe_audio(a) if a else (None, "download failed")
                if not transcript:
                    tg.send(chat_id, f"[bridge] Couldn't transcribe that voice note — {terr}")
                    continue
                note_lines.append("[The text below was transcribed from a voice message.]")
                user_text = f"{user_text}\n\n{transcript}".strip() if user_text else transcript

            message = ("\n".join(note_lines) + "\n\n" + user_text).strip() if note_lines else user_text
            if not message:
                continue

            log(f"[msg] {message[:120]}")
            # Streams the reply live and delivers it itself; returns (sid, reset_needed).
            session_id, reset = run_claude_streaming(tg, chat_id, claude, message, session_id)
            if reset:  # stale session; retry once fresh
                session_id, _ = run_claude_streaming(tg, chat_id, claude, message, None)
            state["session_id"] = session_id
            save_state(state)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        log("\n[tg_bridge] stopped.")

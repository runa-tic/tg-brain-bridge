# tg-brain-bridge

A Telegram bot that is a **phone remote for a local [Claude Code](https://claude.com/claude-code)
agent**. Text it, and a persistent headless Claude conversation runs on your machine — in your
notes vault, a repo, a homelab config tree, wherever you point it — and the answer streams back
into the chat, edited live into the same bubble as it's written, tool calls shown as little
code boxes.

The Telegram sibling of [sms-brain-bridge](https://github.com/runa-tic/sms-brain-bridge):
this one is the daily driver (rich formatting, images, voice, streaming); the SMS one is the
orthogonal last-resort channel for when the internet path itself is what broke. Together they
kept a live production box operable for a month, unattended, from abroad.

```
your phone (Telegram) ──► bot long-poll ──► tg_bridge.py ──► one `claude -p` turn
        ▲                                        │              (persistent session)
        └── live-edited preview bubble ◄─────────┘
            (text streams in, tool calls appear as code boxes)
```

## What it does

- **One persistent conversation.** Session id and update offset are persisted, so a restart
  (or `/reload`) continues the same brain mid-thought. `/new` starts fresh.
- **Live streaming.** Replies stream token-by-token into a single message via
  `editMessageText`, throttled under Telegram's rate limits, then finalize with proper
  formatting in place — no flicker, no message spam. If the stream path fails, it falls back
  to a plain non-streaming turn so a reply is never lost.
- **Voice notes** are transcribed locally with [faster-whisper](https://github.com/SYSTRAN/faster-whisper)
  (optional dep; everything else is stdlib).
- **Photos and documents** (PDFs etc.) are downloaded — documents under their original
  filename — and handed to the agent to `Read`.
- **Markdown → Telegram HTML** renderer for the subset agents actually emit (bold, code,
  fences, headers, links, `[[wikilinks]]`, bullets), with code spans stashed first so
  markdown inside them survives, and a plain-text resend fallback so bad markup never drops
  a message.
- **Locked down.** One whitelisted Telegram numeric user id (discovery mode tells you yours
  on first message), and a restricted `--allowedTools` set for the agent turn — files,
  search, web, one whitelisted scripts dir, no arbitrary Bash.

## Setup

1. Create a bot with [@BotFather](https://t.me/BotFather), copy the token.
2. Run `python tg_bridge.py` once — then create `.secrets/tg_bridge.json`:
   ```json
   { "bot_token": "123:ABC...", "allowed_user_id": 0 }
   ```
   Leave `allowed_user_id` at 0, message the bot, and it replies with your id; paste it in
   and restart. (Env vars `TG_BRIDGE_BOT_TOKEN` / `TG_BRIDGE_ALLOWED_USER_ID` also work.)
3. Point the agent at its brain: set `TG_BRIDGE_CWD` to the directory whose `CLAUDE.md`
   and files the agent should live in (defaults to this repo's dir).
4. Optional voice: `pip install faster-whisper` (CPU int8; the `small` model is the default).
5. Keep it running under [pm2](https://pm2.keymetrics.io/) or any supervisor:
   ```js
   // ecosystem.config.js
   module.exports = { apps: [{ name: "tg-bridge", script: "tg_bridge.py",
                               interpreter: "python", cwd: __dirname,
                               env: { TG_BRIDGE_CWD: "/path/to/your/vault" } }] };
   ```

## Design notes, earned the hard way

- **ACK-by-offset before processing.** The update offset persists *before* the turn runs, so
  a crash mid-turn (or `/reload`) never replays a message into the agent twice.
- **Edit throttling at ~0.9s** keeps the live preview under Telegram's edit rate limits;
  tool-call boxes bypass the throttle so activity shows instantly while text batches.
- **Trim the preview from the front, whole segments at a time** — never mid-tag — when the
  live view outgrows one message; the finalize step then delivers the full text properly.
- **Session-loss self-heal:** a failed `--resume` (session GC'd, machine moved) is detected
  from stderr and retried once with a fresh session instead of erroring at you forever.
- **`CREATE_NO_WINDOW`** on the claude child, or a Windows box under pm2 flashes a console
  at whoever is logged in, every single turn.
- **HTML parse_mode, not MarkdownV2.** MarkdownV2's escaping rules are a minefield;
  escape-then-tag HTML is deterministic, and the plain-text fallback catches the rest.

## Limitations

- One operator, one conversation, serialized turns — it's a remote for *your* agent, not a
  bot platform.
- Long-poll only (no webhook server, nothing listens on any port — that's a feature).
- Voice transcription wants a Python with faster-whisper wheels available; without it,
  voice notes fail gracefully with a hint and everything else still works.

## License

MIT

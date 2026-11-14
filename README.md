# claude-telegram-bot

Use Claude Code from Telegram on your normal Claude **subscription** (Pro/Max), with no Anthropic API billing.
A small always-on Linux server runs the official `claude` CLI, and this bot passes your messages to `claude -p`.

Single file, **Python standard library only** (no pip packages).

## Features

- Chat with Claude; conversations continue (`--resume <session_id>`).
- 🆕 New conversation · 📋 list and pick earlier conversations (10 per page, with older/newer buttons) · 📊 5-hour and weekly usage (Tehran time, Jalali dates)
- File and photo upload (≤ 20 MB, a Telegram bot limit): Claude reads them from `work/uploads/`.
- 📤 "Send me the file": the `send-file` skill (`skills/send-file/SKILL.md`, copied into every user's
  Claude config dir at startup) makes Claude answer with `[[SEND_FILE: path]]`; the bot strips the marker
  and uploads the file (≤ 50 MB). Friends and guests can only send files from their own work folder.
- 🎤 Voice messages: Persian speech-to-text with Google Gemini (free tier), then sent to Claude.
- 🔊 Voice in, voice out: a voice message gets a voice reply only (Gemini TTS, Opus via `ffmpeg`); long replies (>1500 chars) come as text.
- Multi-user, admin only:
  - ➕ **Friend**: logs in with *their own* Claude subscription. The bot runs `claude setup-token` in a pty, sends them the login link, and takes the code back. Separate config dir, history, and queue.
  - 👥 **Guest**: the admin and each logged-in friend can host **one** guest on *their own* account
    (separate folder and session list, host's login). The admin is notified and can remove any guest.
    ⚠️ Sharing an account goes against Anthropic's consumer terms, so use it at your own risk.
  - ➖ Remove: revokes access and deletes that user's files and token.
- Long polling: no webhook and no open ports.

## Setup (Ubuntu 24.04)

```bash
# as a non-root user "claude"
curl -fsSL https://claude.ai/install.sh | bash   # (root: apt install ffmpeg, for voice replies)
claude            # log in once with your subscription (not an API key)
mkdir -p ~/bot && cp -r bot.py test_bot.py skills ~/bot/
cp .env.example ~/bot/.env && chmod 600 ~/bot/.env   # fill in the values

# as root
cp claude-telegram.service /etc/systemd/system/
systemctl daemon-reload && systemctl enable --now claude-telegram
```

If IPv6 to Telegram is broken on your VPS (every request hangs), prefer IPv4:
`echo 'precedence ::ffff:0:0/96  100' >> /etc/gai.conf`

## Test

```bash
cd ~/bot && TG_TOKEN=x ALLOWED_ID=1 python3 test_bot.py   # prints "ok"
```

## Limits

- Tool access (`--permission-mode acceptEdits --allowedTools ...`):
  - admin: web search, web fetch, **any shell command**, and file edits in the work folder.
  - friends and guest: web search, web fetch, and file edits in their own folder. **No Bash**, so they can't read
    `.env` or other users' tokens (tested: reads outside the working folder are blocked).
  - Claude is told to use WebSearch/WebFetch (not curl) for the web, so search works for everyone.
  - **Approvals from Telegram**: when Claude is blocked, the admin gets a card with the exact command and
    `✅ once` / `✅ always` / `❌ no`. Approving resumes the same conversation with a narrow rule
    (e.g. `Bash(curl:*)` allows curl but not wget). Friends can't approve their own requests, because
    a shell command could read the bot's secrets. `🔐` lists and deletes saved "always" rules.
- Messages from one user run one at a time. Each user has their own queue.
- Gemini free tier: Google may use the audio to improve its products.

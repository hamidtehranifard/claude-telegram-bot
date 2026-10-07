# claude-telegram-bot

Use Claude Code from Telegram on your normal Claude **subscription** (Pro/Max), with no Anthropic API billing.
A small always-on Linux server runs the official `claude` CLI, and this bot passes your messages to `claude -p`.

Single file, **Python standard library only** (no pip packages).

## Features

- Chat with Claude; conversations continue (`--resume <session_id>`).
- 🆕 New conversation · 📋 list and pick earlier conversations · 📊 5-hour and weekly usage (Tehran time, Jalali dates)
- File and photo upload (≤ 20 MB, a Telegram bot limit): Claude reads them from `work/uploads/`.
- 🎤 Voice messages: Persian speech-to-text with Google Gemini (free tier), then sent to Claude.
- 🔊 Voice in, voice out: the reply is also read aloud (Gemini TTS, converted to Opus with `ffmpeg`).
- Multi-user, admin only:
  - ➕ **Friend**: logs in with *their own* Claude subscription. The bot runs `claude setup-token` in a pty, sends them the login link, and takes the code back. Separate config dir, history, and queue.
  - 👥 **Guest** (max 1): uses the admin's account, in a separate folder. ⚠️ Sharing an account goes against Anthropic's consumer terms, so use it at your own risk.
  - ➖ Remove: revokes access and deletes that user's files and token.
- Long polling: no webhook and no open ports.

## Setup (Ubuntu 24.04)

```bash
# as a non-root user "claude"
curl -fsSL https://claude.ai/install.sh | bash   # (root: apt install ffmpeg, for voice replies)
claude            # log in once with your subscription (not an API key)
mkdir -p ~/bot && cp bot.py test_bot.py ~/bot/
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

- `claude -p` cannot use tools that need approval (editing files, running commands), so the bot is for chat and reading files.
- Messages from one user run one at a time. Each user has their own queue.
- Gemini free tier: Google may use the audio to improve its products.

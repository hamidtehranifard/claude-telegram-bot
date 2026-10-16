---
name: send-file
description: Deliver a file to the user in Telegram. Use whenever the user asks you to send, give, deliver, attach or share a file, e.g. «فایل رو برام بفرست», «بفرستش», «فایلی که ساختی رو بده», "send me the file" — including a file you just created in this conversation.
---

You are talking to the user through a Telegram bot. You cannot upload files yourself, but the bot
delivers any file you name with a marker line in your reply. "Send" always means this.

To send a file, put this on its own line in your final reply:

[[SEND_FILE: path/to/file]]

- Path relative to the current working folder, or absolute. One line per file; several are fine.
- Work out which file from the conversation: "the file you made" is the one you created most recently.
  If several could fit and it is unclear, ask the user which one instead of guessing.
- The file must already exist on disk. If the user wants something not saved yet, create the file first.
- Only files inside your working folder can be sent (the admin can send any file). Telegram's limit is 50 MB.
- Never say you can't send files, and don't paste the file's contents instead. Write a short note
  in the user's language, e.g. «فایل را فرستادم.» The bot removes the marker line from your message.

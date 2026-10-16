# Run: TG_TOKEN=x ALLOWED_ID=1 python3 test_bot.py
import re
from datetime import datetime
import bot

assert bot.g2j(2026, 10, 7) == (1405, 7, 15)
assert bot.g2j(2025, 3, 21) == (1404, 1, 1)   # Nowruz
assert bot.g2j(2024, 3, 20) == (1403, 1, 1)   # Nowruz after leap Esfand
assert bot.g2j(2026, 1, 1) == (1404, 10, 11)

now = datetime(2026, 10, 7, tzinfo=bot.TZ)
out = bot.fa_usage_line("Current session: 22% used · resets Oct 7, 5:20pm (Asia/Tehran)", now)
assert out == "سشن ۵ ساعته: ۲۲٪ مصرف شده\n   ریست: ۱۵ مهر ۱۴۰۵، ساعت ۱۷:۲۰", out
out = bot.fa_usage_line("Current week (all models): 5% used · resets Oct 13, 11:30am (Asia/Tehran)", now)
assert "۲۱ مهر ۱۴۰۵، ساعت ۱۱:۳۰" in out, out
assert "ساعت ۰۰:۰۰" in bot.fa_usage_line("Current session: 1% used · resets Oct 8, 12am (x)", now)
dec = datetime(2026, 12, 30, tzinfo=bot.TZ)  # January reset seen in December -> next year
assert "۱۲ دی ۱۴۰۵" in bot.fa_usage_line("Current week (all models): 1% used · resets Jan 2, 8am (x)", dec)
assert bot.fa_usage_line("something else", now) == "something else"
# the "what's contributing" breakdown lines say "usage", not "% used · resets": they must not count as limit lines
assert not bot.USAGE_RE.match("99% of your usage was at >150k context")

# pty output: ANSI codes stripped, wrapped URL/token re-joined
raw = b"\x1b[1mhttps://claude.com/cai/oauth/authorize?code=true&client_id=9d1c\r\n250a&state=abc\x1b[0m\r\n\r\nPaste code here >"
url = re.search(r"https://claude\.com/cai/oauth/authorize\S+", bot.clean(raw)).group(0)
assert url.startswith("https://claude.com/cai/oauth/authorize?code=true&client_id=9d1c250a&state=abc"), url
fake = "sk-ant-" + "oat01-" + "FAKE" * 6  # built in pieces so secret scanners don't flag test data
tok = re.search(r"sk-ant-oat\w*-[A-Za-z0-9_\-]{20,}", bot.clean(b"Your token:\r\n" + fake.encode() + b"\r\n")).group(0)
assert tok == fake, tok

# blocked actions -> narrow allow rules for the approval card
rules = dict(bot.denial_rules([
    {"tool_name": "Bash", "tool_input": {"command": "cd x && LANG=C curl -s https://a.b | /usr/bin/jq . ; wget y"}},
    {"tool_name": "Write", "tool_input": {"file_path": "/etc/hosts"}},
]))
assert set(rules) == {"Bash(cd:*)", "Bash(curl:*)", "Bash(jq:*)", "Bash(wget:*)", "Write"}, rules
assert "/etc/hosts" in rules["Write"]

# [[SEND_FILE: ...]] markers: friends/guests only from their own work folder, no symlink escapes
import os, tempfile
with tempfile.TemporaryDirectory() as tmp:
    work, secret = os.path.join(tmp, "work"), os.path.join(tmp, ".env")
    os.makedirs(work)
    open(os.path.join(work, "گزارش.docx"), "w").close()
    open(secret, "w").close()
    os.symlink(secret, os.path.join(work, "link"))
    reply = ("فایل را فرستادم.\n[[SEND_FILE: گزارش.docx]]\n[[SEND_FILE: ../.env]]\n"
             "[[SEND_FILE: link]]\n[[SEND_FILE: nope.txt]]")
    text, paths, errors = bot.outgoing_files(reply, work, admin=False)
    assert text == "فایل را فرستادم.", text
    assert paths == [os.path.realpath(os.path.join(work, "گزارش.docx"))], paths
    assert len(errors) == 3 and sum("⛔" in e for e in errors) == 2, errors
    _, paths, _ = bot.outgoing_files("[[SEND_FILE: ../.env]]", work, admin=True)  # admin: anything
    assert paths == [os.path.realpath(secret)]
print("ok")

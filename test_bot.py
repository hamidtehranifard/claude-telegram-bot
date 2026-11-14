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
# 📋 list: 10 per page, with buttons to older and newer pages; any conversation can be picked
import json as _json, os as _os, tempfile as _tempfile
with _tempfile.TemporaryDirectory() as _tmp:
    _d = _os.path.join(_tmp, "projects", "-p")
    _os.makedirs(_d)
    for _i in range(23):
        _f = _os.path.join(_d, f"p{_i:02d}.jsonl")
        with open(_f, "w") as _fh:
            _fh.write(_json.dumps({"type": "user", "message": {"content": f"chat {_i}"}}) + "\n")
        _os.utime(_f, (1000 + _i, 1000 + _i))
    _user = bot.User.__new__(bot.User)
    _user.uid, _user.cfgdir, _user.workdir, _user.current = 7, _tmp, "/p", "p12"
    _sent = []
    bot.call = lambda method, **p: _sent.append((method, p))

    def _page(n, edit=None):
        _sent.clear()
        _user.show_list(n, edit)
        method, p = _sent[-1]
        rows = _json.loads(p["reply_markup"])["inline_keyboard"]
        return method, [b["callback_data"] for r in rows for b in r], rows
    _method, _data, _rows = _page(0)
    assert _method == "sendMessage" and _data == [f"s:p{i:02d}" for i in range(22, 12, -1)] + ["l:1"], _data
    _method, _data, _rows = _page(1, edit=55)
    assert _method == "editMessageText" and _sent[-1][1]["message_id"] == 55 and _data[-2:] == ["l:0", "l:2"], _data
    assert _rows[0][0]["text"].startswith("✅ ") and _data[0] == "s:p12"
    assert _page(2)[1] == ["s:p02", "s:p01", "s:p00", "l:1"] and _page(9)[1][-1] == "l:1"  # past the end -> first page
    _user.handle("pick", "p00")
    assert _user.current == "p00"
print("ok")

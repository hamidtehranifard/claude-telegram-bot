#!/usr/bin/env python3
"""Telegram -> claude -p bridge. Long polling, stdlib only. One admin + a few friends,
each friend on their own Claude subscription (token from `claude setup-token`)."""
import fcntl, glob, json, os, pty, queue, re, select, shutil, signal, struct, subprocess, termios, threading, time
import urllib.error, urllib.parse, urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo

TOKEN = os.environ["TG_TOKEN"]
ADMIN = int(os.environ["ALLOWED_ID"])
API = f"https://api.telegram.org/bot{TOKEN}/"
CLAUDE = os.path.expanduser("~/.local/bin/claude")
HOME = os.path.expanduser("~")
USERS_FILE = os.path.join(HOME, "bot", "users.json")
TZ = ZoneInfo("Asia/Tehran")
# "send me the file": this skill teaches Claude to answer with [[SEND_FILE: path]]; the bot uploads it
SKILL_SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "skills", "send-file", "SKILL.md")
SEND_RE = re.compile(r"\[\[SEND_FILE:\s*(.+?)\s*\]\]")
SEND_MAX = 50 * 1024 * 1024  # Telegram bot upload limit

NEW_BTN, LIST_BTN, USAGE_BTN = "🆕 گفتگوی جدید", "📋 گفتگوها", "📊 مصرف"
ADD_BTN, GUEST_BTN, DEL_BTN = "➕ افزودن دوست", "👥 افزودن مهمان", "➖ حذف دوست"
DEL_GUEST_BTN = "➖ حذف مهمان"
PERM_BTN = "🔐 دسترسی‌ها"
BASE_ROW = [{"text": NEW_BTN}, {"text": LIST_BTN}, {"text": USAGE_BTN}]
KB_GUEST = json.dumps({"keyboard": [BASE_ROW], "resize_keyboard": True, "is_persistent": True})
KB_FRIEND = json.dumps({"keyboard": [BASE_ROW, [{"text": GUEST_BTN}, {"text": DEL_GUEST_BTN}]],
                        "resize_keyboard": True, "is_persistent": True})
KB_ADMIN = json.dumps({"keyboard": [BASE_ROW, [{"text": ADD_BTN}, {"text": GUEST_BTN}, {"text": DEL_BTN}],
                                    [{"text": PERM_BTN}]],
                       "resize_keyboard": True, "is_persistent": True})


# ---------- Telegram ----------

def call(method, **params):
    data = urllib.parse.urlencode(params).encode()
    with urllib.request.urlopen(API + method, data, timeout=70) as r:
        return json.load(r)["result"]


def send(chat, text, markup=None):
    text = text.strip() or "(empty reply)"
    markup = markup or (KB_ADMIN if chat == ADMIN else KB_FRIEND if str(chat) in STORE["friends"] else KB_GUEST)
    for i in range(0, len(text), 4000):  # Telegram limit is 4096
        call("sendMessage", chat_id=chat, text=text[i:i + 4000], reply_markup=markup)


# ---------- users ----------

store_lock = threading.RLock()  # STORE is shared by the main loop and user threads


def load_store():
    try:
        with open(USERS_FILE) as f:
            s = json.load(f)
    except FileNotFoundError:
        s = {"friends": {}, "pending": []}
    # guests: {host uid: {"name": "@x", "id": guest uid once they said hello}}; one per host (admin or friend)
    s.setdefault("guests", {})
    if "guest" in s:  # old format: a single guest of the admin
        s["guests"][str(ADMIN)] = s.pop("guest")
    return s


STORE = load_store()


def save_store():
    with store_lock:
        tmp = USERS_FILE + ".tmp"
        with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
            json.dump(STORE, f, ensure_ascii=False, indent=1)
        os.replace(tmp, USERS_FILE)


def guest_base(host_uid):
    # the admin's guest keeps its original folder (~/guest), friends' guests live under ~/guests/<host>
    return os.path.join(HOME, "guest") if host_uid == ADMIN else os.path.join(HOME, "guests", str(host_uid))


class User:
    def __init__(self, uid, token=None, host=None):
        self.uid = uid
        self.admin = uid == ADMIN
        # host = the User whose Claude account this guest shares (own folder + session list, host's login)
        self.host = host
        if self.admin:
            base = HOME
        elif host:
            base = guest_base(host.uid)
        else:
            base = os.path.join(HOME, "friends", str(uid))
        self.workdir = os.path.join(base, "work")
        self.cfgdir = (host.cfgdir if host else
                       os.path.join(HOME, ".claude") if self.admin else os.path.join(base, ".claude"))
        self.token = token
        self.login = None  # (pid, fd) while `claude setup-token` waits for the code
        self.q = queue.Queue()
        os.makedirs(self.workdir, exist_ok=True)
        install_skill(self.cfgdir)
        latest = self.sessions(1)
        self.current = latest[0][0] if latest else None  # resume where we left off after a restart
        threading.Thread(target=self.worker, daemon=True).start()

    @property
    def shared(self):  # runs on someone else's (or the admin's own) login: no /login, no token of its own
        return self.admin or self.host is not None

    def env(self):
        if self.host:
            return self.host.env()  # follows the host's current token
        if self.admin:
            return None
        # separate config dir = separate history/settings; token = their own subscription
        return {**os.environ, "CLAUDE_CONFIG_DIR": self.cfgdir, "CLAUDE_CODE_OAUTH_TOKEN": self.token or ""}

    def sessions(self, limit=10):
        """Newest-first [(session_id, mtime, first user message)]."""
        d = os.path.join(self.cfgdir, "projects", re.sub(r"[^A-Za-z0-9]", "-", self.workdir))
        files = sorted(glob.glob(os.path.join(d, "*.jsonl")), key=os.path.getmtime, reverse=True)
        out = []
        for path in files[:limit]:
            title = "(empty)"
            with open(path, encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    try:
                        j = json.loads(line)
                    except ValueError:
                        continue
                    c = j.get("message", {}).get("content") if j.get("type") == "user" else None
                    if isinstance(c, str) and not c.startswith("<"):
                        title = c.replace("I uploaded a file: ", "📎 ").replace("\n", " ")
                        break
            out.append((os.path.basename(path)[:-6], os.path.getmtime(path), title))
        return out

    # one thread per user: their messages run in order, other users never wait on them
    def worker(self):
        while True:
            kind, payload = self.q.get()
            try:
                self.handle(kind, payload)
            except Exception as e:
                print(f"user {self.uid} error:", e, flush=True)
                try:
                    send(self.uid, f"⚠️ خطا: {e}")
                except Exception:
                    pass

    def handle(self, kind, payload):
        if kind == "pick":
            known = {s[0]: s[2] for s in self.sessions(50)}
            if payload in known:
                self.current = payload
                send(self.uid, f"✅ ادامه‌ی گفتگو: {known[payload][:80]}")
            else:
                send(self.uid, "⚠️ این گفتگو پیدا نشد.")
            return
        if kind == "login":
            return self.start_login()
        if kind == "approve":
            return self.approved(*payload)
        if kind == "denied":
            return send(self.uid, "❌ اجازه داده نشد.")
        m = payload
        text = m.get("text") or m.get("caption") or ""
        cmd = text.strip()
        if self.login:  # waiting for the code from the browser
            return self.finish_login(cmd)
        if not self.shared and not self.token:
            return send(self.uid, "ابتدا با /login وارد حساب Claude خود شوید.")
        if self.host and not self.host.admin and not self.host.token:
            return send(self.uid, "میزبان شما هنوز وارد حساب Claude نشده است.")
        path = save_upload(m, self.workdir)
        if path and m.get("voice"):
            if not GEMINI_KEY:
                return send(self.uid, "⚠️ تبدیل صدا فعال نیست (کلید Gemini تنظیم نشده).")
            call("sendChatAction", chat_id=self.uid, action="typing")
            heard, model, secs = transcribe(path)
            send(self.uid, f"🎤 شنیدم:\n{heard}")
            text = heard + (f"\n\n{text}" if text else "") + VOICE_HINT
        elif path:
            text = f"I uploaded a file: {os.path.relpath(path, self.workdir)}\n\n" + (text or "Read it and tell me briefly what it is.")
        elif cmd in ("/new", NEW_BTN):
            self.current = None
            return send(self.uid, "🆕 پیام بعدی یک گفتگوی تازه شروع می‌کند.")
        elif cmd in ("/sessions", LIST_BTN):
            return self.show_list()
        elif cmd in ("/usage", USAGE_BTN):
            call("sendChatAction", chat_id=self.uid, action="typing")
            return send(self.uid, usage(self.env()))
        elif cmd == "/start":
            return send(self.uid, "آماده‌ام. پیام یا فایل بفرستید. با دکمه‌های پایین گفتگوی تازه بسازید یا گفتگوی قبلی را انتخاب کنید.")
        if not text:
            return
        self.run(text, voice=bool(m.get("voice")))

    def run(self, text, extra=(), voice=False):
        call("sendChatAction", chat_id=self.uid, action="typing")
        tools = (TOOLS_ADMIN if self.admin else TOOLS_OTHERS) + saved_rules(self.uid) + list(extra)
        reply, self.current, denials = ask(text, self.current, self.workdir, self.env(), tools)
        reply, files, errors = outgoing_files(reply, self.workdir, self.admin)
        rules = denial_rules(denials)
        spoken = False
        # spoken in -> spoken out (voice only); long replies, errors, permission requests or a TTS failure -> text
        if (voice and GEMINI_KEY and reply and not rules and len(reply) <= TTS_MAX_CHARS
                and not reply.startswith(("⚠️", "⏱"))):
            call("sendChatAction", chat_id=self.uid, action="record_voice")
            try:
                send_voice(self.uid, tts(reply))
                spoken = True
            except Exception as e:
                print(f"user {self.uid} tts error:", e, flush=True)
        if not spoken and (reply or not files):
            send(self.uid, reply)
        for path in files:
            call("sendChatAction", chat_id=self.uid, action="upload_document")
            try:
                with open(path, "rb") as f:
                    upload("sendDocument", "document", self.uid, os.path.basename(path), f.read())
            except Exception as e:
                errors.append(f"⚠️ {os.path.basename(path)}: ارسال نشد ({e})")
        if errors:
            send(self.uid, "\n".join(errors))
        if rules:
            self.request(rules)

    def request(self, rules):
        """Blocked actions -> approval card for the admin (friends can't approve their own: no isolation)."""
        rid = os.urandom(4).hex()
        with pending_lock:
            PENDING[rid] = {"uid": self.uid, "rules": rules, "sid": self.current}
        lines = "\n".join("• " + desc for _, desc in rules)
        warn = ""
        if not self.admin and any(r.startswith("Bash") for r, _ in rules):
            warn = ("\n\n⚠️ این کاربر با همان کاربر لینوکسی بات اجرا می‌شود. دستور شل می‌تواند توکن بات، "
                    "کلید Gemini و توکن دوستان را بخواند. فقط اگر به او اعتماد دارید تأیید کنید.")
        kb = json.dumps({"inline_keyboard": [
            [{"text": "✅ فقط این بار", "callback_data": "p1:" + rid}, {"text": "✅ همیشه", "callback_data": "pA:" + rid}],
            [{"text": "❌ نه", "callback_data": "pN:" + rid}]]})
        send(ADMIN, f"🔐 درخواست دسترسی از {user_label(self.uid)}:\n{lines}{warn}", kb)
        if not self.admin:
            send(self.uid, "⏳ این کار اجازه می‌خواهد. درخواست برای ادمین فرستاده شد.")

    def approved(self, req, always):
        rules = [r for r, _ in req["rules"]]
        if always:
            with store_lock:
                mine = STORE.setdefault("perms", {}).setdefault(str(self.uid), [])
                mine += [r for r in rules if r not in mine]
                save_store()
        self.current = req["sid"]  # continue the conversation that asked
        send(self.uid, "✅ اجازه داده شد. ادامه می‌دهم…")
        descs = "; ".join(d for _, d in req["rules"])
        self.run(f"The user has now approved the previously blocked action(s): {descs}. "
                 "Retry them and finish the original task.", extra=rules)

    def show_list(self):
        rows = []
        for sid, mtime, title in self.sessions():
            when = datetime.fromtimestamp(mtime, TZ).strftime("%m/%d %H:%M")
            mark = "✅ " if sid == self.current else ""
            rows.append([{"text": f"{mark}{when} · {title[:40]}", "callback_data": "s:" + sid}])
        if not rows:
            return send(self.uid, "هنوز گفتگویی نیست.")
        send(self.uid, "یک گفتگو را انتخاب کنید:", json.dumps({"inline_keyboard": rows}))

    # ---------- login via `claude setup-token` in a pseudo-terminal ----------

    def start_login(self):
        self.kill_login()
        os.makedirs(self.cfgdir, exist_ok=True)
        pid, fd = pty.fork()
        if pid == 0:
            os.environ["TERM"] = "xterm"
            os.environ["CLAUDE_CONFIG_DIR"] = self.cfgdir  # never touch the admin's own login
            os.execv(CLAUDE, ["claude", "setup-token"])
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", 50, 2000, 0, 0))  # wide: URL on one line
        self.login = (pid, fd)
        out = read_until(fd, re.compile(r"https://claude\.com/cai/oauth/authorize\S+"), 60)
        url = re.search(r"https://claude\.com/cai/oauth/authorize\S+", out)
        if not url:
            self.kill_login()
            return send(self.uid, "⚠️ لینک ورود ساخته نشد. بعداً دوباره /login بزنید.")
        send(self.uid, "🔐 ورود به حساب Claude شما\n\n"
                       "۱. این لینک را در مرورگر باز کنید و با حساب Claude خودتان وارد شوید (اشتراک Pro یا Max لازم است).\n"
                       "۲. کدی را که نمایش داده می‌شود کپی کنید و همین‌جا بفرستید.\n\n" + url.group(0))

    def finish_login(self, code):
        pid, fd = self.login
        if not code or code.startswith("/"):
            return send(self.uid, "منتظر کد ورود هستم. اگر لینک کار نکرد، /login را دوباره بزنید.")
        before = read_until(fd, re.compile(r"(?!x)x"), 0.3)  # never matches: just drain what's on screen
        os.write(fd, code.encode())
        time.sleep(1)  # let the terminal input box take the pasted code before Enter
        os.write(fd, b"\r")
        out = read_until(fd, re.compile(r"sk-ant-oat\w*-[A-Za-z0-9_\-]{20,}"), 90)
        tok = re.search(r"sk-ant-oat\w*-[A-Za-z0-9_\-]{20,}", out)
        self.kill_login()
        if not tok:  # no token was issued, so the screen text is safe to show for debugging
            tail = out[-700:] or before[-300:]
            print(f"user {self.uid} login failed, setup-token said: {tail!r}", flush=True)
            send(ADMIN, f"⚠️ ورود {user_label(self.uid)} کامل نشد. خروجی setup-token:\n\n{tail}")
            return send(self.uid, "⚠️ ورود کامل نشد (کد اشتباه یا منقضی). دوباره /login بزنید و کد جدید را کامل کپی کنید.")
        with store_lock:
            f = STORE["friends"].get(str(self.uid))
            if f is None:  # removed by the admin while logging in
                return
            f["token"] = self.token = tok.group(0)
            save_store()
        send(self.uid, "✅ وارد شدید. حالا پیام یا فایل بفرستید.")
        send(ADMIN, f"✅ {f.get('name', self.uid)} وارد حساب Claude شد.")

    def kill_login(self):
        if self.login:
            pid, fd = self.login
            self.login = None
            for f in (lambda: os.kill(pid, signal.SIGKILL), lambda: os.waitpid(pid, 0), lambda: os.close(fd)):
                try:
                    f()
                except OSError:
                    pass


def read_until(fd, pattern, timeout):
    """Read pty output (ANSI codes stripped, line breaks joined) until pattern appears."""
    buf, end = b"", time.time() + timeout
    while time.time() < end:
        r, _, _ = select.select([fd], [], [], 1)
        if r:
            try:
                buf += os.read(fd, 65536)
            except OSError:
                break
            txt = clean(buf)
            if pattern.search(txt):
                return txt
    return clean(buf)


def clean(buf):
    txt = re.sub(rb"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07]*\x07|\x1b[=>]", b"", buf).decode("utf-8", "replace")
    return re.sub(r"\r?\n", "", txt)  # undo terminal wrapping inside long URLs/tokens


# ---------- claude ----------

GEMINI_KEY = os.environ.get("GEMINI_KEY")
# first that works wins. Measured on a real Persian voice (2026-10-07): 3.5-flash 5.5s, 2.5-flash 1.6s;
# gemini-3.5-transcribe -> HTTP 400 "Thinking is not enabled"; 3.8-flash / flash-latest -> no answer in 60-120s.
STT_MODELS = ["gemini-3.5-flash", "gemini-2.5-flash"]
STT_PROMPT = ("Transcribe this audio verbatim. It is usually Persian (Farsi): write Persian in Persian script, "
              "keep English words and technical terms in English. Output only the transcript.")


def transcribe(path):
    """Voice file -> (text, model, seconds) via Gemini (free tier). Raises if every model fails."""
    import base64
    audio = base64.b64encode(open(path, "rb").read()).decode()
    body = json.dumps({"contents": [{"parts": [{"text": STT_PROMPT},
                                               {"inline_data": {"mime_type": "audio/ogg", "data": audio}}]}],
                       "generationConfig": {"thinkingConfig": {"thinkingBudget": 0}}}).encode()  # thinking = minutes of delay
    errors = []
    for model in STT_MODELS:
        t = time.time()
        req = urllib.request.Request(
            f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
            body, {"Content-Type": "application/json", "x-goog-api-key": GEMINI_KEY})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                d = json.load(r)
            text = "".join(p.get("text", "") for p in d["candidates"][0]["content"]["parts"]).strip()
            if text:
                return text, model, time.time() - t
            errors.append(f"{model}: empty")
        except urllib.error.HTTPError as e:
            errors.append(f"{model}: HTTP {e.code} {e.read()[:200].decode('utf-8', 'replace')}")
        except Exception as e:
            errors.append(f"{model}: {e}")
    raise RuntimeError("تبدیل صدا به متن نشد:\n" + "\n".join(errors))


TTS_MODELS = ["gemini-3.8-flash-tts", "gemini-3.8-flash-lite-tts"]  # both tested good for Persian
TTS_MAX_CHARS = 1500  # longer replies stay text-only (slow to synthesize, tiring to listen to)
VOICE_HINT = ("\n\n(The user sent this as a voice message and will hear your reply read aloud. "
              "Answer in short, natural spoken sentences in the user's language: no markdown, tables, "
              "lists or code unless they are essential.)")


def tts(text):
    """Text -> OGG/Opus bytes for Telegram sendVoice, via Gemini TTS + ffmpeg."""
    import base64, io, wave
    text = re.sub(r"[*_#`>|]+", " ", text)  # markdown symbols would be read aloud
    body = json.dumps({"contents": [{"parts": [{"text": text}]}],
                       "generationConfig": {"responseModalities": ["AUDIO"],
                                            "speechConfig": {"voiceConfig": {"prebuiltVoiceConfig": {"voiceName": "Kore"}}}}}).encode()
    errors = []
    for model in TTS_MODELS:
        req = urllib.request.Request(
            f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
            body, {"Content-Type": "application/json", "x-goog-api-key": GEMINI_KEY})
        try:
            with urllib.request.urlopen(req, timeout=90) as r:
                part = json.load(r)["candidates"][0]["content"]["parts"][0]["inlineData"]
            audio = base64.b64decode(part["data"])
            if "wav" not in part["mimeType"].lower():  # raw 16-bit PCM, 24 kHz mono: add a WAV header
                buf = io.BytesIO()
                with wave.open(buf, "wb") as w:
                    w.setnchannels(1), w.setsampwidth(2), w.setframerate(24000), w.writeframes(audio)
                audio = buf.getvalue()
            p = subprocess.run(["ffmpeg", "-loglevel", "error", "-i", "pipe:0", "-c:a", "libopus", "-b:a", "32k",
                                "-f", "ogg", "pipe:1"], input=audio, capture_output=True, timeout=60)
            if p.returncode == 0 and p.stdout:
                return p.stdout
            errors.append(f"{model}: ffmpeg {p.stderr[-200:].decode('utf-8', 'replace')}")
        except urllib.error.HTTPError as e:
            errors.append(f"{model}: HTTP {e.code}")
        except Exception as e:
            errors.append(f"{model}: {e}")
    raise RuntimeError("; ".join(errors))


def upload(method, field, chat, name, data, ctype="application/octet-stream"):
    """Multipart upload (sendVoice / sendDocument)."""
    b = os.urandom(16).hex()
    name = re.sub(r'["\r\n]', "", name)  # it goes inside a quoted header
    body = (f"--{b}\r\nContent-Disposition: form-data; name=\"chat_id\"\r\n\r\n{chat}\r\n"
            f"--{b}\r\nContent-Disposition: form-data; name=\"{field}\"; filename=\"{name}\"\r\n"
            f"Content-Type: {ctype}\r\n\r\n").encode() + data + f"\r\n--{b}--\r\n".encode()
    req = urllib.request.Request(API + method, body, {"Content-Type": f"multipart/form-data; boundary={b}"})
    with urllib.request.urlopen(req, timeout=300) as r:
        return json.load(r)["result"]


def send_voice(chat, ogg):
    return upload("sendVoice", "voice", chat, "reply.ogg", ogg, "audio/ogg")


def install_skill(cfgdir):
    """Every Claude config dir (admin, each friend; guests share their host's) gets the send-file skill."""
    dest = os.path.join(cfgdir, "skills", "send-file", "SKILL.md")
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    shutil.copyfile(SKILL_SRC, dest)


def outgoing_files(reply, workdir, admin):
    """Pull [[SEND_FILE: path]] markers out of Claude's reply -> (reply text, paths to upload, error lines).
    Friends and guests may only send files inside their own work folder (realpath: no symlink or ../ escapes)."""
    root, paths, errors = os.path.realpath(workdir), [], []
    for p in dict.fromkeys(SEND_RE.findall(reply)):
        full = os.path.realpath(os.path.join(workdir, os.path.expanduser(p)))
        if not admin and os.path.commonpath([root, full]) != root:
            errors.append(f"⛔ {p}: فقط فایل‌های پوشه‌ی خودتان فرستاده می‌شوند.")
        elif not os.path.isfile(full):
            errors.append(f"⚠️ {p}: فایل پیدا نشد.")
        elif os.path.getsize(full) > SEND_MAX:
            errors.append(f"⚠️ {p}: بیشتر از ۵۰ مگابایت است و تلگرام اجازه‌ی ارسال نمی‌دهد.")
        else:
            paths.append(full)
    return SEND_RE.sub("", reply).strip(), paths, errors


def save_upload(m, workdir):
    """Download a document/photo/voice into workdir/uploads; return its path or None."""
    if m.get("document"):
        f = m["document"]
        name = os.path.basename(f.get("file_name") or "file")  # basename blocks ../ tricks
    elif m.get("photo"):
        f = m["photo"][-1]  # last = largest size
        name = "photo.jpg"
    elif m.get("voice"):
        f = m["voice"]
        name = "voice.ogg"
    else:
        return None
    if f.get("file_size", 0) > 20 * 1024 * 1024:
        raise ValueError("Telegram bots can only download files up to 20 MB.")
    tg_path = call("getFile", file_id=f["file_id"])["file_path"]
    dest = os.path.join(workdir, "uploads", time.strftime("%Y%m%d-%H%M%S-") + name)
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    with urllib.request.urlopen(f"https://api.telegram.org/file/bot{TOKEN}/{tg_path}", timeout=120) as r, open(dest, "wb") as out:
        out.write(r.read())
    return dest


# Tool access per role (tested 2026-10-07). acceptEdits = create/edit files inside the working folder.
# Without "Bash", claude -p still runs read-only commands but only inside its working folder, so
# friends/guest can't read ~/bot/.env or users.json. "Bash" = any command as the claude user: admin only.
TOOLS_ADMIN = ["WebSearch", "WebFetch", "Bash"]
TOOLS_OTHERS = ["WebSearch", "WebFetch"]
SEARCH_HINT = "For web search and reading web pages, use the WebSearch and WebFetch tools, not curl or wget."


def denial_rules(denials):
    """permission_denials -> [(rule, human description)]. Bash gets one prefix rule per command word
    (e.g. 'Bash(curl:*)' allows curl but not wget); other tools are allowed as a whole."""
    out = {}
    for d in denials:
        tool, inp = d.get("tool_name", "?"), d.get("tool_input") or {}
        if tool == "Bash":
            cmd = inp.get("command", "")
            for part in re.split(r"&&|\|\||[;|\n]", cmd):
                words = [w for w in part.split() if "=" not in w.split("/")[0]]  # skip VAR=x prefixes
                if words:
                    word = os.path.basename(words[0])
                    out[f"Bash({word}:*)"] = f"دستور شل {word}: {cmd[:120]}"
        else:
            target = inp.get("file_path") or inp.get("url") or inp.get("path") or ""
            out[tool] = f"ابزار {tool}" + (f": {target[:120]}" if target else "")
    return list(out.items())


def ask(prompt, sid, workdir, env, tools=TOOLS_OTHERS):
    """Run claude; return (reply, session_id, permission_denials). sid=None starts a new session."""
    args = [CLAUDE, "-p", prompt, "--output-format", "json", "--permission-mode", "acceptEdits",
            "--append-system-prompt", SEARCH_HINT,
            "--allowedTools", ",".join(tools)] + (["--resume", sid] if sid else [])
    try:
        p = subprocess.run(args, cwd=workdir, env=env, capture_output=True, text=True, timeout=900)
    except subprocess.TimeoutExpired:
        return "⏱ Claude did not answer within 15 minutes.", sid, []
    try:
        d = json.loads(p.stdout)
    except ValueError:  # auth expiry, crash, etc.: no JSON, show raw error
        return f"⚠️ claude exited {p.returncode}:\n{(p.stderr or p.stdout)[-1500:]}", sid, []
    reply = d.get("result") or ""
    if d.get("is_error"):
        return f"⚠️ {reply}", d.get("session_id") or sid, []
    return reply, d.get("session_id") or sid, d.get("permission_denials") or []


# ---------- permission requests (approved by the admin from Telegram) ----------

PENDING = {}  # request id -> dict(uid, rules, sid)
pending_lock = threading.Lock()


def user_label(uid):
    if uid == ADMIN:
        return "شما"
    for host, g in STORE["guests"].items():
        if g.get("id") == uid:
            return f"مهمان {g['name']}" + ("" if int(host) == ADMIN else f" (از طرف {user_label(int(host))})")
    return (STORE["friends"].get(str(uid)) or {}).get("name", str(uid))


def saved_rules(uid):
    return list((STORE.get("perms") or {}).get(str(uid), []))


JMONTHS = ["فروردین", "اردیبهشت", "خرداد", "تیر", "مرداد", "شهریور",
           "مهر", "آبان", "آذر", "دی", "بهمن", "اسفند"]
FA_DIGITS = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")
LABELS = {"Current session": "سشن ۵ ساعته", "Current week (all models)": "هفتگی (همه‌ی مدل‌ها)"}
USAGE_RE = re.compile(r"^(.*?): (\d+)% used · resets (\w{3}) (\d{1,2})(?:, (\d{1,2})(?::(\d{2}))?([ap]m))?")


def g2j(gy, gm, gd):
    """Gregorian -> Jalali (standard jdf arithmetic)."""
    gdm = [0, 31, 59, 90, 120, 151, 181, 212, 243, 273, 304, 334]
    gy2 = gy + 1 if gm > 2 else gy
    days = 355666 + 365 * gy + (gy2 + 3) // 4 - (gy2 + 99) // 100 + (gy2 + 399) // 400 + gd + gdm[gm - 1]
    jy = -1595 + 33 * (days // 12053)
    days %= 12053
    jy += 4 * (days // 1461)
    days %= 1461
    if days > 365:
        jy += (days - 1) // 365
        days = (days - 1) % 365
    if days < 186:
        return jy, 1 + days // 31, 1 + days % 31
    return jy, 7 + (days - 186) // 30, 1 + (days - 186) % 30


def fa_usage_line(line, now):
    """'Current session: 22% used · resets Oct 7, 5:20pm (..)' -> Persian with Jalali date."""
    m = USAGE_RE.match(line.strip())
    if not m:
        return line
    label, pct, mon, day, hh, mm, ampm = m.groups()
    month = datetime.strptime(mon, "%b").month
    year = now.year + (1 if month < now.month - 6 else 0)  # "Jan" seen in December = next year
    jy, jm, jd = g2j(year, month, int(day))
    when = f"{jd} {JMONTHS[jm - 1]} {jy}"
    if hh:
        h = int(hh) % 12 + (12 if ampm == "pm" else 0)
        when += f"، ساعت {h:02d}:{int(mm or 0):02d}"
    return f"{LABELS.get(label, label)}: {pct}٪ مصرف شده\n   ریست: {when}".translate(FA_DIGITS)


def usage(env=None):
    """Claude Code's own /usage report (5-hour session + weekly limits)."""
    env = {**(env or os.environ), "TZ": "Asia/Tehran"}  # claude prints reset times in TZ
    try:
        # cwd=/tmp so this throwaway run doesn't show up in the session list
        p = subprocess.run([CLAUDE, "-p", "/usage"], cwd="/tmp", capture_output=True, text=True, timeout=120, env=env)
    except subprocess.TimeoutExpired:
        return "⏱ گزارش مصرف در ۲ دقیقه نیامد."
    now = datetime.now(TZ)
    raw = p.stdout or p.stderr
    lines = [fa_usage_line(l, now) for l in raw.splitlines() if USAGE_RE.match(l.strip())]
    if lines:
        return "📊 مصرف اشتراک (به وقت تهران)\n\n" + "\n\n".join(lines)
    # Claude Code sometimes omits the limit bars and prints only a "what's contributing" breakdown
    return ("📊 Claude Code این بار درصد مصرف ۵ ساعته و هفتگی را گزارش نکرد.\n"
            "درصد دقیق را در claude.ai ← Settings ← Usage ببینید. خروجی خود Claude Code:\n\n" + raw[:1500])


# ---------- main loop: routing + admin actions ----------

def main():
    try:  # "Menu" button commands; not critical if it fails
        call("setMyCommands", commands=json.dumps([
            {"command": "new", "description": "گفتگوی جدید"},
            {"command": "sessions", "description": "فهرست گفتگوها"},
            {"command": "usage", "description": "مصرف ۵ ساعته و هفتگی"},
            {"command": "login", "description": "ورود به حساب Claude"}]))
    except Exception as e:
        print("setMyCommands failed:", e, flush=True)
    store = STORE
    users = {ADMIN: User(ADMIN)}
    for fid, f in store["friends"].items():
        users[int(fid)] = User(int(fid), f.get("token"))
    for host, g in store["guests"].items():
        if g.get("id") and int(host) in users:
            users[g["id"]] = User(g["id"], host=users[int(host)])
    awaiting = {}  # uid -> "friend" | "guest": that user was asked for a username
    all_buttons = (NEW_BTN, LIST_BTN, USAGE_BTN, ADD_BTN, GUEST_BTN, DEL_BTN, DEL_GUEST_BTN, PERM_BTN)

    def drop_guest(host):
        """Remove host's guest (pending or active), their saved rules and files. Returns the record or None."""
        with store_lock:
            g = store["guests"].pop(str(host), None)
            if g and g.get("id"):
                store.get("perms", {}).pop(str(g["id"]), None)
            save_store()
        if g:
            if g.get("id"):
                users.pop(g["id"], None)
                try:
                    send(g["id"], "دسترسی شما به این بات قطع شد.", json.dumps({"remove_keyboard": True}))
                except Exception:
                    pass
            shutil.rmtree(guest_base(host), ignore_errors=True)
        return g

    offset = 0
    while True:
        try:
            for u in call("getUpdates", offset=offset, timeout=50):
                offset = u["update_id"] + 1

                q = u.get("callback_query")
                if q:
                    uid, data = q["from"]["id"], q.get("data", "")
                    call("answerCallbackQuery", callback_query_id=q["id"])
                    if uid in users and data.startswith("s:"):
                        users[uid].q.put(("pick", data[2:]))
                    elif uid == ADMIN and data.startswith("g:"):  # remove a guest ("g:<host>"; bare "g:" = admin's)
                        host = int(data[2:] or ADMIN)
                        g = drop_guest(host)
                        if g:
                            send(ADMIN, f"🗑 دسترسی مهمان {g.get('name', '')} قطع شد و فایل‌هایش پاک شد.")
                            if host != ADMIN and host in users:
                                send(host, f"ادمین مهمان شما ({g.get('name', '')}) را حذف کرد.")
                    elif uid == ADMIN and data.startswith("d:"):
                        fid = data[2:]
                        g = drop_guest(int(fid))  # a friend's guest goes with them
                        with store_lock:
                            f = store["friends"].pop(fid, None)
                            store.get("perms", {}).pop(fid, None)
                            save_store()
                        if f:
                            gone = users.pop(int(fid), None)
                            if gone:
                                gone.kill_login()
                            shutil.rmtree(os.path.join(HOME, "friends", fid), ignore_errors=True)
                            send(ADMIN, f"🗑 {f.get('name', fid)} حذف شد و توکن و فایل‌هایش پاک شد."
                                 + (f" مهمانش {g.get('name', '')} هم حذف شد." if g else ""))
                    elif uid == ADMIN and data[:3] in ("p1:", "pA:", "pN:"):  # approval card
                        with pending_lock:
                            req = PENDING.pop(data[3:], None)
                        msg = q.get("message") or {}
                        if not req or req["uid"] not in users:
                            status = "⌛ این درخواست دیگر معتبر نیست."
                        elif data[:2] == "pN":
                            users[req["uid"]].q.put(("denied", None))
                            status = "❌ رد شد."
                        else:
                            always = data[:2] == "pA"
                            users[req["uid"]].q.put(("approve", (req, always)))
                            status = "✅ تأیید شد" + (" (همیشه)." if always else " (فقط این بار).")
                        try:  # replace the buttons with the decision, so it can't be pressed twice
                            call("editMessageText", chat_id=ADMIN, message_id=msg.get("message_id"),
                                 text=(msg.get("text") or "") + "\n\n" + status)
                        except Exception:
                            send(ADMIN, status)
                    elif uid == ADMIN and data.startswith("pr:"):  # delete a saved rule
                        _, ruid, idx, head = data.split(":", 3)
                        rule = None
                        with store_lock:  # index + name prefix must both match, so a stale button can't delete the wrong rule
                            rules = store.get("perms", {}).get(ruid, [])
                            if idx.isdigit() and int(idx) < len(rules) and rules[int(idx)][:30] == head:
                                rule = rules.pop(int(idx))
                                save_store()
                        send(ADMIN, f"🗑 قاعده‌ی {rule} برای {user_label(int(ruid))} پاک شد." if rule
                             else "⚠️ این قاعده پیدا نشد. دوباره دکمه‌ی 🔐 را بزنید.")
                    continue

                m = u.get("message") or {}
                frm = m.get("from", {})
                uid = frm.get("id")
                if m.get("chat", {}).get("type") != "private":
                    continue  # private chats only, so chat id == user id
                cmd = (m.get("text") or "").strip()

                is_host = uid == ADMIN or str(uid) in store["friends"]  # admin and friends may host one guest
                if is_host and cmd in ("/guest", GUEST_BTN):
                    awaiting.pop(uid, None)
                    g = store["guests"].get(str(uid))
                    if uid != ADMIN and not users[uid].token:
                        send(uid, "اول با /login وارد حساب Claude خودتان شوید.")
                    elif g:
                        send(uid, f"⚠️ فقط یک مهمان مجاز است و {g['name']} ثبت شده. اول با "
                                  f"«{DEL_BTN if uid == ADMIN else DEL_GUEST_BTN}» او را حذف کنید.")
                    else:
                        awaiting[uid] = "guest"
                        send(uid, "یوزرنیم تلگرام مهمان را بفرستید (مثلاً @ali).\n⚠️ او با حساب Claude شما کار می‌کند "
                                  "و از سهمیه‌ی شما مصرف می‌کند. شرایط استفاده‌ی Anthropic اشتراک‌گذاری حساب را مجاز نمی‌داند.")
                    continue
                if is_host and uid != ADMIN and cmd in ("/delguest", DEL_GUEST_BTN):
                    awaiting.pop(uid, None)
                    g = drop_guest(uid)
                    send(uid, f"🗑 مهمان {g['name']} حذف شد و فایل‌هایش پاک شد." if g else "مهمانی ثبت نشده.")
                    if g:
                        send(ADMIN, f"🗑 {user_label(uid)} مهمانش {g['name']} را حذف کرد.")
                    continue

                if uid == ADMIN:
                    if cmd in ("/add", ADD_BTN):
                        awaiting[uid] = "friend"
                        send(ADMIN, "یوزرنیم تلگرام دوستتان را بفرستید (مثلاً @ali).\nاو با حساب Claude خودش وارد می‌شود.")
                        continue
                    if cmd in ("/perms", PERM_BTN):
                        awaiting.pop(uid, None)
                        rows = [[{"text": f"❌ {user_label(int(ruid))}: {rule}", "callback_data": f"pr:{ruid}:{i}:{rule[:30]}"}]
                                for ruid, rules in store.get("perms", {}).items() for i, rule in enumerate(rules)]
                        base = ("دسترسی پایه:\n• شما: جست‌وجو، خواندن وب، هر دستور شل، ویرایش فایل در پوشه‌ی کاری\n"
                                "• دوستان و مهمان: جست‌وجو، خواندن وب، ویرایش فایل در پوشه‌ی خودشان")
                        if rows:
                            send(ADMIN, base + "\n\nقاعده‌های «همیشه» (برای پاک کردن، روی هر کدام بزنید):",
                                 json.dumps({"inline_keyboard": rows}))
                        else:
                            send(ADMIN, base + "\n\nهنوز قاعده‌ی «همیشه» ثبت نشده.")
                        continue
                    if cmd in ("/remove", DEL_BTN):
                        awaiting.pop(uid, None)
                        rows = [[{"text": f.get("name", fid), "callback_data": "d:" + fid}]
                                for fid, f in store["friends"].items()]
                        for host, g in store["guests"].items():
                            owner = "" if int(host) == ADMIN else f" (از طرف {user_label(int(host))})"
                            rows.append([{"text": f"👥 مهمان: {g['name']}{owner}" + ("" if g.get("id") else " (در انتظار)"),
                                          "callback_data": "g:" + host}])
                        pend = "، ".join("@" + p for p in store["pending"])
                        if pend:
                            with store_lock:
                                store["pending"] = []
                                save_store()
                            send(ADMIN, f"در انتظارها لغو شدند: {pend}")
                        if rows:
                            send(ADMIN, "کدام دوست حذف شود؟", json.dumps({"inline_keyboard": rows}))
                        elif not pend:
                            send(ADMIN, "دوستی ثبت نشده.")
                        continue

                # a host (admin or friend) was asked for a username and this message is the answer
                if uid in awaiting and cmd:
                    if cmd.startswith("/") or cmd in all_buttons:
                        awaiting.pop(uid)  # moved on: cancel, handle the command normally
                    else:
                        kind = awaiting.pop(uid)
                        name = cmd.lstrip("@").lower()
                        if not re.fullmatch(r"[a-z0-9_]{4,32}", name):
                            send(uid, "⚠️ یوزرنیم معتبر نیست. دوباره دکمه‌ی افزودن را بزنید.")
                            continue
                        with store_lock:
                            if kind == "guest":
                                store["guests"][str(uid)] = {"name": "@" + name}
                            elif name not in store["pending"]:
                                store["pending"].append(name)
                            save_store()
                        send(uid, f"✅ @{name} در انتظار است. از او بخواهید به @{call('getMe')['username']} پیام /start بدهد.")
                        if uid != ADMIN:
                            send(ADMIN, f"👥 {user_label(uid)} مهمان @{name} را دعوت کرد (روی حساب Claude خودش).")
                        continue

                if uid not in users:
                    name = (frm.get("username") or "").lower()
                    host = next((int(h) for h, g in store["guests"].items()
                                 if not g.get("id") and g.get("name") == "@" + name), None) if name else None
                    if host is not None and host in users:  # invited guest says hello
                        with store_lock:
                            store["guests"][str(host)]["id"] = uid
                            save_store()
                        users[uid] = User(uid, host=users[host])
                        whose = "شما" if host == ADMIN else user_label(host)
                        send(ADMIN, f"👥 مهمان @{name} فعال شد و با حساب Claude {whose} کار می‌کند.")
                        if host != ADMIN:
                            send(host, f"👥 مهمان شما @{name} فعال شد.")
                        send(uid, "👋 خوش آمدید. پیام یا فایل بفرستید تا Claude جواب بدهد.")
                        continue
                    if name and name in store["pending"]:  # invited friend says hello: register + login
                        with store_lock:
                            store["pending"].remove(name)
                            store["friends"][str(uid)] = {"name": "@" + name}
                            save_store()
                        users[uid] = User(uid)
                        send(ADMIN, f"👋 @{name} به بات پیام داد. ورود او شروع شد.")
                        users[uid].q.put(("login", None))
                    continue  # strangers are ignored silently

                if cmd == "/login":
                    if users[uid].shared:  # admin and guests run on an existing login
                        send(uid, "ورود لازم نیست؛ شما با حساب Claude " + ("خودتان" if uid == ADMIN else "میزبانتان") + " کار می‌کنید.")
                    else:
                        users[uid].q.put(("login", None))
                    continue
                users[uid].q.put(("msg", m))
        except Exception as e:  # network blips: wait and keep polling
            print("error:", e, flush=True)
            time.sleep(5)


if __name__ == "__main__":
    main()

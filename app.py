from flask import Flask, render_template, request, redirect, url_for, session, jsonify, g
import subprocess
import os
import signal
import time
import threading
import sqlite3
import hashlib
import secrets
import urllib.parse
import urllib.request
import json
from functools import wraps
from datetime import datetime, timedelta

app = Flask(__name__)
app.secret_key = "jisan_premium_tcp_bot_secret_key_2026_v3"

# ================= CONFIG =================
BOT_DIR = os.path.join(os.path.dirname(__file__), "bot")
BOT_SCRIPT = os.path.join(BOT_DIR, "main.py")
DB_PATH = os.path.join(os.path.dirname(__file__), "panel.db")
ADMIN_EMAIL = "admin@jisan.host"
ADMIN_PASS  = "admin123"

# Arolinks Ad system
AROLINKS_API_TOKEN = "499f2e5ff99a70a8bdf83c635ab48c7fa4329225"
AROLINKS_API = "https://arolinks.com/api"
FREE_HOURS = 12                # Hours given after completing ad
# Pending ad tokens (token -> {user_id, created})
pending_ad_tokens = {}
# ==========================================

bot_processes = {}
process_lock = threading.Lock()
PID_DIR = os.path.join(os.path.dirname(__file__), "pids")
os.makedirs(PID_DIR, exist_ok=True)


def pid_file(user_id):
    return os.path.join(PID_DIR, f"bot_{user_id}.pid")


def save_pid(user_id, pid):
    try:
        with open(pid_file(user_id), "w") as f:
            f.write(str(pid))
    except Exception:
        pass


def load_pid(user_id):
    try:
        path = pid_file(user_id)
        if os.path.exists(path):
            with open(path) as f:
                return int(f.read().strip())
    except Exception:
        pass
    return None


def clear_pid(user_id):
    try:
        path = pid_file(user_id)
        if os.path.exists(path):
            os.remove(path)
    except Exception:
        pass


def is_pid_alive(pid):
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except Exception:
        return False


def kill_pid(pid):
    """Kill process and its group by pid number — fast, no wait."""
    if not pid:
        return False
    ok = False
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(pid, sig)
            ok = True
        except Exception:
            try:
                os.kill(pid, sig)
                ok = True
            except Exception:
                pass
        if sig == signal.SIGTERM and not is_pid_alive(pid):
            return True
    return ok or not is_pid_alive(pid)


def get_db():
    db = getattr(g, "_database", None)
    if db is None:
        db = g._database = sqlite3.connect(DB_PATH)
        db.row_factory = sqlite3.Row
    return db


@app.teardown_appcontext
def close_connection(exception):
    db = getattr(g, "_database", None)
    if db is not None:
        db.close()


def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            name TEXT,
            is_admin INTEGER DEFAULT 0,
            is_banned INTEGER DEFAULT 0,
            game_uid TEXT DEFAULT '',
            game_pass TEXT DEFAULT '',
            time_expire TEXT DEFAULT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    # Migrate old DB (add missing columns safely)
    cols = [r[1] for r in c.execute("PRAGMA table_info(users)").fetchall()]
    if "is_banned" not in cols:
        c.execute("ALTER TABLE users ADD COLUMN is_banned INTEGER DEFAULT 0")
    if "time_expire" not in cols:
        c.execute("ALTER TABLE users ADD COLUMN time_expire TEXT DEFAULT NULL")
    if "game_uid" not in cols:
        c.execute("ALTER TABLE users ADD COLUMN game_uid TEXT DEFAULT ''")
    if "game_pass" not in cols:
        c.execute("ALTER TABLE users ADD COLUMN game_pass TEXT DEFAULT ''")

    c.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)
    # Default admin
    c.execute("SELECT id FROM users WHERE email = ?", (ADMIN_EMAIL,))
    if not c.fetchone():
        pw_hash = hash_password(ADMIN_PASS)
        c.execute(
            "INSERT INTO users (email, password_hash, name, is_admin) VALUES (?, ?, ?, 1)",
            (ADMIN_EMAIL, pw_hash, "Admin")
        )
    # Default Arolinks token
    c.execute("SELECT value FROM settings WHERE key = 'arolinks_token'")
    if not c.fetchone():
        c.execute("INSERT INTO settings (key, value) VALUES ('arolinks_token', ?)", (AROLINKS_API_TOKEN,))
    # Public base URL (optional) — needed when panel is behind localhost
    c.execute("SELECT value FROM settings WHERE key = 'public_base_url'")
    if not c.fetchone():
        c.execute("INSERT INTO settings (key, value) VALUES ('public_base_url', '')")
    conn.commit()
    conn.close()


def create_arolinks_short(long_url: str):
    """
    Call Arolinks API.
    Returns (shortened_url or None, error_message or None)
    """
    token = get_setting("arolinks_token", AROLINKS_API_TOKEN)
    if not token:
        return None, "Arolinks API token not set. Go to Admin Panel and save token."

    # Localhost / private IPs usually rejected by link shorteners
    low = long_url.lower()
    if "127.0.0.1" in low or "localhost" in low or "0.0.0.0" in low:
        return None, (
            "Arolinks cannot use localhost. "
            "Set your public Panel URL in Admin (example: http://YOUR_VPS_IP:5000) "
            "or open the panel using your public IP / domain."
        )

    try:
        # Match official PHP sample: api + url (urlencode handled by urlencode)
        qs = urllib.parse.urlencode({"api": token, "url": long_url})
        api_url = f"{AROLINKS_API}?{qs}"
        print(f"[Arolinks] Request: {api_url[:120]}...")
        req = urllib.request.Request(api_url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read().decode("utf-8", errors="ignore")
        print(f"[Arolinks] Response: {raw[:300]}")
        data = json.loads(raw)

        if isinstance(data, dict):
            if data.get("status") == "error":
                return None, data.get("message") or "Arolinks returned error"
            short = data.get("shortenedUrl") or data.get("shorturl") or data.get("url")
            if short and str(short).startswith("http"):
                return str(short), None
            return None, f"Unexpected Arolinks response: {raw[:200]}"

        if isinstance(data, str) and data.startswith("http"):
            return data, None

        return None, f"Unexpected Arolinks response: {raw[:200]}"
    except Exception as e:
        print(f"[Arolinks] Exception: {e}")
        return None, f"Arolinks API request failed: {e}"


def grant_free_hours(user_id: int, hours: int = FREE_HOURS):
    """Add hours to user's time_expire (stacks if still active)."""
    db = get_db()
    user = db.execute("SELECT time_expire FROM users WHERE id = ?", (user_id,)).fetchone()
    now = datetime.now()
    if user and user["time_expire"]:
        try:
            current_expire = datetime.fromisoformat(user["time_expire"])
            if current_expire > now:
                new_expire = current_expire + timedelta(hours=hours)
            else:
                new_expire = now + timedelta(hours=hours)
        except Exception:
            new_expire = now + timedelta(hours=hours)
    else:
        new_expire = now + timedelta(hours=hours)
    db.execute(
        "UPDATE users SET time_expire = ? WHERE id = ?",
        (new_expire.isoformat(), user_id)
    )
    db.commit()
    return new_expire


def hash_password(password: str) -> str:
    salt = "jisan_salt_2026"
    return hashlib.sha256((password + salt).encode()).hexdigest()


def get_setting(key, default=""):
    db = get_db()
    row = db.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(key, value):
    db = get_db()
    db.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = ?",
        (key, value, value)
    )
    db.commit()


def get_user_row(user_id):
    db = get_db()
    return db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()


def is_time_active(user_id):
    """Return True if user still has free time remaining. Admins always True."""
    user = get_user_row(user_id)
    if not user:
        return False
    if user["is_admin"]:
        return True
    if user["is_banned"]:
        return False
    if not user["time_expire"]:
        return False
    try:
        expire = datetime.fromisoformat(user["time_expire"])
        return datetime.now() < expire
    except Exception:
        return False


def remaining_seconds(user_id):
    user = get_user_row(user_id)
    if not user:
        return 0
    if user["is_admin"]:
        return 999999999
    if not user["time_expire"]:
        return 0
    try:
        expire = datetime.fromisoformat(user["time_expire"])
        left = (expire - datetime.now()).total_seconds()
        return max(0, int(left))
    except Exception:
        return 0


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("user_id"):
            return redirect(url_for("login"))
        # Check ban
        user = get_user_row(session["user_id"])
        if user and user["is_banned"] and not user["is_admin"]:
            session.clear()
            return redirect(url_for("login", banned=1))
        return f(*args, **kwargs)
    return decorated


def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("user_id"):
            return redirect(url_for("login"))
        if session.get("is_admin"):
            return f(*args, **kwargs)
        user = get_user_row(session["user_id"])
        if not user or not user["is_admin"]:
            return redirect(url_for("dashboard"))
        session["is_admin"] = True
        return f(*args, **kwargs)
    return decorated


def add_user_log(user_id, msg):
    with process_lock:
        info = bot_processes.get(user_id)
        if info is None:
            return
        timestamp = time.strftime("%H:%M:%S")
        info["logs"].append(f"[{timestamp}] {msg}")
        if len(info["logs"]) > 80:
            info["logs"] = info["logs"][-80:]


def get_user_status(user_id):
    with process_lock:
        info = bot_processes.get(user_id)
        pid = load_pid(user_id)
        alive = False
        if info and info.get("process") is not None:
            try:
                if info["process"].poll() is None:
                    alive = True
                else:
                    info["status"] = "stopped"
                    info["process"] = None
            except Exception:
                pass
        if not alive and pid and is_pid_alive(pid):
            alive = True
        if not alive:
            clear_pid(user_id)
            if info:
                info["status"] = "stopped"
                info["process"] = None
            return "stopped", (info.get("uid", "") if info else ""), (info.get("pass", "") if info else ""), (info.get("logs", [])[:] if info else [])
        if info:
            info["status"] = "running"
            return "running", info.get("uid", ""), info.get("pass", ""), info.get("logs", [])[:]
        return "running", "", "", []


def force_stop_bot(user_id, reason="Time expired"):
    pid = load_pid(user_id)
    with process_lock:
        info = bot_processes.get(user_id)
        proc = info.get("process") if info else None
        if proc is not None:
            try:
                pid = proc.pid or pid
            except Exception:
                pass
            try:
                if proc.poll() is None:
                    try:
                        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                    except Exception:
                        try:
                            proc.kill()
                        except Exception:
                            pass
            except Exception:
                pass
        if pid:
            kill_pid(pid)
        clear_pid(user_id)
        if info:
            info["status"] = "stopped"
            info["process"] = None
            add_user_log(user_id, reason)


def time_watcher():
    """Background thread: stop bots when time expires"""
    while True:
        time.sleep(30)
        try:
            conn = sqlite3.connect(DB_PATH)
            conn.row_factory = sqlite3.Row
            users = conn.execute(
                "SELECT id, time_expire, is_admin, is_banned FROM users WHERE is_admin = 0"
            ).fetchall()
            conn.close()
            now = datetime.now()
            for u in users:
                if u["is_banned"]:
                    force_stop_bot(u["id"], "Account banned")
                    continue
                if not u["time_expire"]:
                    force_stop_bot(u["id"], "No active time")
                    continue
                try:
                    expire = datetime.fromisoformat(u["time_expire"])
                    if now >= expire:
                        force_stop_bot(u["id"], "12-hour time expired. Watch ad again to continue.")
                except Exception:
                    pass
        except Exception:
            pass


# Start background time checker
threading.Thread(target=time_watcher, daemon=True).start()


# ================= ROUTES =================

@app.route("/")
def index():
    if session.get("user_id"):
        return redirect(url_for("dashboard"))
    return redirect(url_for("login"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if session.get("user_id"):
        return redirect(url_for("dashboard"))

    error = None
    if request.args.get("banned"):
        error = "Your account has been banned by admin."

    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "").strip()

        if not email or not password:
            error = "Email and password are required"
        else:
            db = get_db()
            user = db.execute(
                "SELECT id, email, password_hash, name, is_admin, is_banned FROM users WHERE email = ?",
                (email,)
            ).fetchone()
            if user and user["password_hash"] == hash_password(password):
                if user["is_banned"] and not user["is_admin"]:
                    error = "Your account has been banned by admin."
                else:
                    session["user_id"] = user["id"]
                    session["email"] = user["email"]
                    session["name"] = user["name"] or user["email"].split("@")[0]
                    session["is_admin"] = bool(user["is_admin"])
                    return redirect(url_for("dashboard"))
            else:
                error = "Invalid email or password"

    return render_template("login.html", error=error, mode="login")


@app.route("/signup", methods=["GET", "POST"])
def signup():
    if session.get("user_id"):
        return redirect(url_for("dashboard"))

    error = None
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "").strip()
        name = request.form.get("name", "").strip()
        confirm = request.form.get("confirm", "").strip()

        if not email or not password:
            error = "Email and password are required"
        elif "@" not in email or "." not in email.split("@")[-1]:
            error = "Please enter a valid email (Gmail recommended)"
        elif len(password) < 6:
            error = "Password must be at least 6 characters"
        elif password != confirm:
            error = "Passwords do not match"
        else:
            db = get_db()
            existing = db.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()
            if existing:
                error = "This email is already registered"
            else:
                pw_hash = hash_password(password)
                display_name = name or email.split("@")[0]
                db.execute(
                    "INSERT INTO users (email, password_hash, name) VALUES (?, ?, ?)",
                    (email, pw_hash, display_name)
                )
                db.commit()
                return render_template("login.html", error=None,
                                       success="Account created! Please sign in.", mode="login")

    return render_template("login.html", error=error, mode="signup")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/dashboard")
@login_required
def dashboard():
    user_id = session["user_id"]
    status, uid, password, _ = get_user_status(user_id)

    if not uid:
        db = get_db()
        row = db.execute("SELECT game_uid, game_pass FROM users WHERE id = ?", (user_id,)).fetchone()
        if row:
            uid = row["game_uid"] or ""
            password = row["game_pass"] or ""

    has_time = is_time_active(user_id)
    left = remaining_seconds(user_id)

    return render_template(
        "dashboard.html",
        status=status,
        uid=uid,
        password=password,
        username=session.get("name", "User"),
        email=session.get("email", ""),
        is_admin=session.get("is_admin", False),
        has_time=has_time,
        remaining=left,
        free_hours=FREE_HOURS
    )


@app.route("/watch-ad")
@login_required
def watch_ad():
    """
    Create a unique claim destination, shorten it with Arolinks,
    then show page with the short link. User must open Arolinks
    (watch their ads), then they land on /ad-callback and get 12 hours.
    """
    if session.get("is_admin"):
        return redirect(url_for("dashboard"))

    user_id = session["user_id"]
    token = secrets.token_hex(20)
    pending_ad_tokens[token] = {"user_id": user_id, "created": time.time()}

    # Cleanup old tokens (> 1 hour)
    now_ts = time.time()
    for t in list(pending_ad_tokens.keys()):
        if now_ts - pending_ad_tokens[t]["created"] > 3600:
            pending_ad_tokens.pop(t, None)

    # Prefer admin-configured public URL (required for Arolinks if running on localhost)
    public_base = (get_setting("public_base_url", "") or "").strip().rstrip("/")
    if public_base:
        base = public_base
    else:
        base = request.url_root.rstrip("/")

    callback_url = f"{base}/ad-callback?token={token}"

    short_url, short_error = create_arolinks_short(callback_url)

    return render_template(
        "ad.html",
        short_url=short_url or "",
        callback_url=callback_url,
        free_hours=FREE_HOURS,
        has_short=bool(short_url),
        error_msg=short_error or ""
    )


@app.route("/ad-callback")
def ad_callback():
    """
    Destination after user finishes Arolinks ads.
    Grants 12 hours if token is valid.
    """
    token = request.args.get("token", "").strip()
    info = pending_ad_tokens.pop(token, None) if token else None

    if not info:
        # Maybe already claimed or invalid
        if session.get("user_id"):
            return redirect(url_for("dashboard"))
        return redirect(url_for("login"))

    user_id = info["user_id"]
    # Must still be logged in as same user
    if session.get("user_id") != user_id:
        # Still grant time to the user who started the ad
        pass

    try:
        new_expire = grant_free_hours(user_id, FREE_HOURS)
        msg = f"Success! You got {FREE_HOURS} hours of hosting."
        ok = True
    except Exception as e:
        msg = f"Error granting time: {e}"
        ok = False
        new_expire = None

    # If logged in, go dashboard with flash-like query
    if session.get("user_id"):
        return redirect(url_for("dashboard", claimed=1 if ok else 0))

    return render_template(
        "ad_done.html",
        success=ok,
        message=msg,
        free_hours=FREE_HOURS
    )


@app.route("/api/claim-time", methods=["POST"])
@login_required
def api_claim_time():
    """Fallback: not used with Arolinks redirect flow."""
    return jsonify({
        "success": False,
        "message": "Please use the Arolinks ad link to claim time."
    })


@app.route("/admin")
@admin_required
def admin_panel():
    db = get_db()
    users = db.execute(
        "SELECT id, email, name, is_admin, is_banned, game_uid, time_expire, created_at FROM users ORDER BY id"
    ).fetchall()

    user_list = []
    running_count = 0
    for u in users:
        status, uid, _, _ = get_user_status(u["id"])
        if status == "running":
            running_count += 1

        left = 0
        if u["is_admin"]:
            left = -1  # unlimited
        elif u["time_expire"]:
            try:
                expire = datetime.fromisoformat(u["time_expire"])
                left = max(0, int((expire - datetime.now()).total_seconds()))
            except Exception:
                left = 0

        user_list.append({
            "id": u["id"],
            "email": u["email"],
            "name": u["name"],
            "is_admin": bool(u["is_admin"]),
            "is_banned": bool(u["is_banned"]),
            "game_uid": uid or u["game_uid"] or "-",
            "status": status,
            "remaining": left,
            "created_at": u["created_at"]
        })

    arolinks_token = get_setting("arolinks_token", AROLINKS_API_TOKEN)
    public_base_url = get_setting("public_base_url", "")

    return render_template(
        "admin.html",
        users=user_list,
        total_users=len(user_list),
        running_count=running_count,
        username=session.get("name", "Admin"),
        arolinks_token=arolinks_token,
        public_base_url=public_base_url
    )


# ================= API =================

@app.route("/api/status")
@login_required
def api_status():
    user_id = session["user_id"]
    status, uid, _, logs = get_user_status(user_id)
    return jsonify({
        "status": status,
        "uid": uid,
        "logs": logs[-30:],
        "has_time": is_time_active(user_id),
        "remaining": remaining_seconds(user_id)
    })


@app.route("/api/start", methods=["POST"])
@login_required
def api_start():
    user_id = session["user_id"]

    if not is_time_active(user_id):
        return jsonify({
            "success": False,
            "message": "No active time. Please watch an ad to get 12 hours.",
            "need_ad": True
        })

    data = request.json or {}
    uid = (data.get("uid") or "").strip()
    password = (data.get("password") or "").strip()
    if not uid or not password:
        return jsonify({"success": False, "message": "UID and Password required"})

    # Kill previous bot for this user (PID file + memory)
    old_pid = load_pid(user_id)
    with process_lock:
        info = bot_processes.get(user_id)
        if info and info.get("process") is not None:
            try:
                old_pid = info["process"].pid or old_pid
            except Exception:
                pass
            try:
                _kill_proc(info["process"])
            except Exception:
                pass
        if old_pid:
            kill_pid(old_pid)
        clear_pid(user_id)
        if info:
            info["status"] = "stopped"
            info["process"] = None

    db = get_db()
    db.execute(
        "UPDATE users SET game_uid = ?, game_pass = ? WHERE id = ?",
        (uid, password, user_id)
    )
    db.commit()

    import sys
    py = sys.executable or "python3"
    env = os.environ.copy()
    env["BOT_UID"] = uid
    env["BOT_PW"] = password
    env["BOT_REGION"] = data.get("region", "BD")

    # Log file instead of PIPE — avoids pipe-buffer freeze / lag
    log_path = os.path.join(PID_DIR, f"bot_{user_id}.log")
    try:
        log_f = open(log_path, "w", encoding="utf-8", errors="ignore")
    except Exception:
        log_f = subprocess.DEVNULL

    try:
        proc = subprocess.Popen(
            [py, "-u", BOT_SCRIPT],
            cwd=BOT_DIR,
            stdout=log_f,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            env=env
        )
    except Exception as e:
        return jsonify({"success": False, "message": f"Start failed: {e}"})

    save_pid(user_id, proc.pid)

    with process_lock:
        bot_processes[user_id] = {
            "process": proc,
            "status": "running",
            "uid": uid,
            "pass": password,
            "logs": [f"[{time.strftime('%H:%M:%S')}] Bot started pid={proc.pid}"],
            "started_at": time.time(),
            "log_path": log_path,
        }

    # Background: tail log file into memory logs
    def tail_log(uid_key=user_id, path=log_path, process=proc):
        try:
            # wait for file
            for _ in range(20):
                if os.path.exists(path):
                    break
                time.sleep(0.05)
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                while True:
                    line = f.readline()
                    if line:
                        add_user_log(uid_key, line.strip()[:150])
                        continue
                    if process.poll() is not None:
                        # drain rest
                        rest = f.read()
                        for ln in rest.splitlines():
                            if ln.strip():
                                add_user_log(uid_key, ln.strip()[:150])
                        break
                    time.sleep(0.3)
        except Exception:
            pass
        with process_lock:
            if uid_key in bot_processes:
                bot_processes[uid_key]["status"] = "stopped"
                bot_processes[uid_key]["process"] = None
        clear_pid(uid_key)
        add_user_log(uid_key, "Bot process ended")

    threading.Thread(target=tail_log, daemon=True).start()

    return jsonify({"success": True, "message": "Bot started", "pid": proc.pid})


def _kill_proc(proc):
    if not proc:
        return
    try:
        pid = proc.pid
    except Exception:
        pid = None
    try:
        if proc.poll() is None:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
    except Exception:
        pass
    if pid:
        kill_pid(pid)


@app.route("/api/stop", methods=["POST"])
@login_required
def api_stop():
    user_id = session["user_id"]
    pid = load_pid(user_id)
    with process_lock:
        info = bot_processes.get(user_id)
        proc = info.get("process") if info else None
        if proc is not None:
            try:
                pid = proc.pid or pid
            except Exception:
                pass
            try:
                _kill_proc(proc)
            except Exception:
                pass
        if pid:
            kill_pid(pid)
        # last resort: pkill by script name + env not easy; kill pid file done
        clear_pid(user_id)
        if info:
            info["status"] = "stopped"
            info["process"] = None
            add_user_log(user_id, "Bot stopped by user")
        elif user_id not in bot_processes:
            bot_processes[user_id] = {
                "process": None, "status": "stopped", "uid": "", "pass": "",
                "logs": [f"[{time.strftime('%H:%M:%S')}] Bot stopped"], "started_at": 0
            }
    return jsonify({"success": True, "message": "Bot stopped", "pid": pid})


@app.route("/api/restart", methods=["POST"])
@login_required
def api_restart():
    user_id = session["user_id"]
    if not is_time_active(user_id):
        return jsonify({
            "success": False,
            "message": "No active time. Please watch an ad to get 12 hours.",
            "need_ad": True
        })
    # stop without extra sleep
    pid = load_pid(user_id)
    with process_lock:
        info = bot_processes.get(user_id)
        proc = info.get("process") if info else None
        if proc is not None:
            try:
                pid = proc.pid or pid
            except Exception:
                pass
            try:
                _kill_proc(proc)
            except Exception:
                pass
        if pid:
            kill_pid(pid)
        clear_pid(user_id)
        if info:
            info["status"] = "stopped"
            info["process"] = None
            add_user_log(user_id, "Restarting...")
    return api_start()


@app.route("/api/admin/stats")
@admin_required
def api_admin_stats():
    db = get_db()
    total = db.execute("SELECT COUNT(*) as c FROM users").fetchone()["c"]
    running = 0
    with process_lock:
        for uid, info in bot_processes.items():
            if info.get("status") == "running" and info.get("process") and info["process"].poll() is None:
                running += 1
    return jsonify({"total_users": total, "running": running})


@app.route("/api/admin/stop_user/<int:target_id>", methods=["POST"])
@admin_required
def api_admin_stop_user(target_id):
    force_stop_bot(target_id, "Stopped by Admin")
    return jsonify({"success": True})


@app.route("/api/admin/ban/<int:target_id>", methods=["POST"])
@admin_required
def api_admin_ban(target_id):
    db = get_db()
    user = db.execute("SELECT is_admin FROM users WHERE id = ?", (target_id,)).fetchone()
    if not user:
        return jsonify({"success": False, "message": "User not found"})
    if user["is_admin"]:
        return jsonify({"success": False, "message": "Cannot ban admin"})
    db.execute("UPDATE users SET is_banned = 1 WHERE id = ?", (target_id,))
    db.commit()
    force_stop_bot(target_id, "Account banned by Admin")
    return jsonify({"success": True, "message": "User banned"})


@app.route("/api/admin/unban/<int:target_id>", methods=["POST"])
@admin_required
def api_admin_unban(target_id):
    db = get_db()
    db.execute("UPDATE users SET is_banned = 0 WHERE id = ?", (target_id,))
    db.commit()
    return jsonify({"success": True, "message": "User unbanned"})


@app.route("/api/admin/set_arolinks_token", methods=["POST"])
@admin_required
def api_admin_set_arolinks_token():
    data = request.json or {}
    token = data.get("token", "").strip()
    if not token or len(token) < 10:
        return jsonify({"success": False, "message": "Valid Arolinks API token required"})
    set_setting("arolinks_token", token)
    return jsonify({"success": True, "message": "Arolinks API token saved"})


@app.route("/api/admin/set_public_url", methods=["POST"])
@admin_required
def api_admin_set_public_url():
    data = request.json or {}
    url = (data.get("url") or "").strip().rstrip("/")
    if url and not (url.startswith("http://") or url.startswith("https://")):
        return jsonify({"success": False, "message": "URL must start with http:// or https://"})
    # Reject pure localhost as public URL (won't work with Arolinks)
    if url and ("localhost" in url.lower() or "127.0.0.1" in url):
        return jsonify({
            "success": False,
            "message": "Use your VPS public IP or domain, not localhost. Example: http://123.45.67.89:5000"
        })
    set_setting("public_base_url", url)
    return jsonify({"success": True, "message": "Public panel URL saved"})


# ================= INIT =================

# Always init DB when app loads (needed for Gunicorn / Railway)
try:
    init_db()
except Exception as e:
    print(f"[DB] init warning: {e}")

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print("\n" + "=" * 55)
    print("  JISAN Premium TCP Bot Hosting Panel (Multi-User)")
    print("  Admin Login : admin@jisan.host / admin123")
    print(f"  Running on  : http://0.0.0.0:{port}")
    print("  Features    : Ban/Block + 12h Ad System + Arolinks")
    print("=" * 55 + "\n")
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)

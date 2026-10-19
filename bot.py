import os
import sqlite3
import time
import logging
import json
import random
from datetime import datetime, timezone, timedelta, date
import requests

# ============================================================
# Telegram Referral & Points Bot — POWER v1
# - Referral reward: 5 points (immediate, after group-join verify)
# - Referral MILESTONES: 5/10/25/50/100 refs -> 10/25/60/150/400 bonus
# - Daily bonus with STREAK: consecutive days increase the bonus
# - Colored inline buttons (Bot API 9.4 style: primary/danger/success)
# - Minimum withdrawal: 120 points
# - Withdrawal identifier: Telegram username (@username)
# - Withdrawal points are reserved/deducted immediately
# - Admin must approve within 72h; otherwise points are refunded
# - Withdrawal processing message: 12-72 hours
# - Manual payout: no bKash/Nagad API is included
# ============================================================

def _load_env(path):
    """Minimal .env loader (no extra dependency)."""
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k = k.strip()
                v = v.strip().strip('"').strip("'")
                if k and k not in os.environ:
                    os.environ[k] = v
    except FileNotFoundError:
        pass


_load_env(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))


BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_IDS = {int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()}
DB_PATH = os.getenv("DB_PATH", "bot.db")
IMAGE_PATH = os.getenv("REFERRAL_IMAGE", os.path.join(os.path.dirname(__file__), "referral_banner.png"))

if not BOT_TOKEN:
    raise SystemExit("BOT_TOKEN is missing.")
if not ADMIN_IDS:
    raise SystemExit("ADMIN_IDS is missing. Example: ADMIN_IDS=123456789")

API = f"https://api.telegram.org/bot{BOT_TOKEN}"
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")

db = sqlite3.connect(DB_PATH, check_same_thread=False)
db.row_factory = sqlite3.Row

# ---------------- Database ----------------

def column_exists(table, column):
    rows = db.execute(f"PRAGMA table_info({table})").fetchall()
    return any(r[1] == column for r in rows)


def add_column(table, column, definition):
    if not column_exists(table, column):
        db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def init_db():
    db.executescript("""
    CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        username TEXT,
        first_name TEXT,
        balance REAL NOT NULL DEFAULT 0,
        wallet TEXT DEFAULT '',
        referrer_id INTEGER,
        referrals INTEGER NOT NULL DEFAULT 0,
        referral_earned REAL NOT NULL DEFAULT 0,
        total_earned REAL NOT NULL DEFAULT 0,
        total_withdrawn REAL NOT NULL DEFAULT 0,
        joined_at TEXT NOT NULL,
        last_bonus TEXT
    );

    CREATE TABLE IF NOT EXISTS withdrawals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        amount REAL NOT NULL,
        wallet TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending',
        created_at TEXT NOT NULL,
        processed_at TEXT,
        admin_id INTEGER
    );

    CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS states (
        user_id INTEGER PRIMARY KEY,
        state TEXT NOT NULL,
        data TEXT DEFAULT ''
    );

    CREATE TABLE IF NOT EXISTS point_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        amount REAL NOT NULL,
        kind TEXT NOT NULL,
        note TEXT NOT NULL,
        created_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS admin_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        admin_id INTEGER NOT NULL,
        action TEXT NOT NULL,
        details TEXT NOT NULL,
        created_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS pending_referrals (
        referred_user_id INTEGER PRIMARY KEY,
        referrer_id INTEGER NOT NULL,
        created_at TEXT NOT NULL,
        rewarded_at TEXT
    );

    CREATE TABLE IF NOT EXISTS wheel_spins (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        spin_date TEXT NOT NULL,
        prize REAL NOT NULL,
        created_at TEXT NOT NULL,
        UNIQUE(user_id, spin_date)
    );

    CREATE TABLE IF NOT EXISTS wheel_draws (
        period_start TEXT PRIMARY KEY,
        period_end TEXT NOT NULL,
        winner_id INTEGER,
        created_at TEXT NOT NULL
    );
    """)

    # Migrate the older bot database safely.
    add_column("withdrawals", "expires_at", "TEXT")
    add_column("withdrawals", "refund_at", "TEXT")
    # POWER v1: daily streak counter + claimed referral milestones.
    add_column("users", "bonus_streak", "INTEGER DEFAULT 0")
    add_column("users", "milestones", "TEXT DEFAULT ''")

    defaults = {
        "referral_reward": "5",
        "daily_bonus": "1",
        "streak_step": "1",
        "min_withdraw": "120",
        "currency": "POINTS",
        "group_link": "https://t.me/realicamservice",
        "delivery_text": "Withdrawals are processed within 12-72 hours.",
        "welcome_text": "Invite friends, earn points, and request withdrawals.",
        "support_text": "Please contact the bot administrator for support.",
    }
    for k, v in defaults.items():
        db.execute("INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)", (k, v))
    # Anchor five-day prize periods when this feature is first enabled.
    db.execute("INSERT OR IGNORE INTO settings(key,value) VALUES('wheel_draw_anchor',?)", (date.today().isoformat(),))

    # Apply the user's requested initial settings on this upgraded project.
    # This changes the old currency/minimum to points/120 while keeping other settings intact.
    db.execute("UPDATE settings SET value='5' WHERE key='referral_reward'")
    db.execute("UPDATE settings SET value='120' WHERE key='min_withdraw'")
    db.execute("UPDATE settings SET value='POINTS' WHERE key='currency'")
    db.execute("UPDATE settings SET value='https://t.me/realicamservice' WHERE key='group_link'")
    db.commit()


def setting(key):
    row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else ""


def set_setting(key, value):
    db.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))
    db.commit()


def now_dt():
    return datetime.now(timezone.utc)


def now():
    return now_dt().strftime("%Y-%m-%d %H:%M:%S UTC")


def expires_in_72h():
    return (now_dt() + timedelta(hours=72)).strftime("%Y-%m-%d %H:%M:%S UTC")


def parse_dt(value):
    try:
        return datetime.strptime(value, "%Y-%m-%d %H:%M:%S UTC").replace(tzinfo=timezone.utc)
    except Exception:
        return None


def get_user(user_id):
    return db.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()


def upsert_user(tg_user):
    uid = tg_user["id"]
    username = tg_user.get("username") or ""
    first_name = tg_user.get("first_name") or ""
    row = get_user(uid)
    if row:
        db.execute("UPDATE users SET username=?, first_name=? WHERE user_id=?", (username, first_name, uid))
        db.commit()
        return False
    db.execute("INSERT INTO users(user_id,username,first_name,joined_at) VALUES(?,?,?,?)", (uid, username, first_name, now()))
    db.commit()
    return True


def set_state(uid, state, data=""):
    db.execute("INSERT INTO states(user_id,state,data) VALUES(?,?,?) ON CONFLICT(user_id) DO UPDATE SET state=excluded.state,data=excluded.data", (uid, state, data))
    db.commit()


def get_state(uid):
    return db.execute("SELECT * FROM states WHERE user_id=?", (uid,)).fetchone()


def clear_state(uid):
    db.execute("DELETE FROM states WHERE user_id=?", (uid,))
    db.commit()

# ---------------- Telegram API ----------------

def tg(method, data=None, files=None):
    try:
        if files:
            r = requests.post(f"{API}/{method}", data=data or {}, files=files, timeout=45)
        else:
            r = requests.post(f"{API}/{method}", json=data or {}, timeout=35)
        payload = r.json()
        if not payload.get("ok"):
            logging.error("Telegram API error: %s", payload)
        return payload
    except Exception:
        logging.exception("Telegram request failed")
        return {"ok": False}


def send(chat_id, text, reply_markup=None, parse_mode=None):
    data = {"chat_id": chat_id, "text": text}
    if reply_markup:
        data["reply_markup"] = reply_markup
    if parse_mode:
        data["parse_mode"] = parse_mode
    return tg("sendMessage", data)


def send_photo(chat_id, caption, reply_markup=None):
    if not os.path.exists(IMAGE_PATH):
        return send(chat_id, caption, reply_markup)
    try:
        with open(IMAGE_PATH, "rb") as f:
            data = {"chat_id": chat_id, "caption": caption}
            if reply_markup:
                data["reply_markup"] = json.dumps(reply_markup, ensure_ascii=False)
            return tg("sendPhoto", data, files={"photo": f})
    except Exception:
        logging.exception("Could not send referral image")
        return send(chat_id, caption, reply_markup)


def answer_callback(callback_id, text=""):
    return tg("answerCallbackQuery", {"callback_query_id": callback_id, "text": text, "show_alert": False})


def edit(chat_id, message_id, text, reply_markup=None):
    data = {"chat_id": chat_id, "message_id": message_id, "text": text}
    if reply_markup:
        data["reply_markup"] = reply_markup
    return tg("editMessageText", data)

# ---------------- Keyboards ----------------

def main_menu():
    # Colored reply keyboard (Bot API 9.4 style on KeyboardButton) — compact 3 per row
    return {"keyboard": [
        [{"text": "💎 Points", "style": "primary"}, {"text": "👥 Referral", "style": "success"}, {"text": "💸 Withdraw", "style": "success"}],
        [{"text": "👤 Username", "style": "primary"}, {"text": "🎁 Daily Bonus", "style": "success"}, {"text": "🎡 Luck Wheel", "style": "primary"}],
        [{"text": "📊 Statistics", "style": "primary"}, {"text": "📜 History", "style": "success"}, {"text": "🏆 Leaderboard", "style": "primary"}],
        [{"text": "🚨 JOIN GROUP", "style": "danger"}, {"text": "☎️ Support", "style": "danger"}],
    ], "resize_keyboard": True}


def admin_menu():
    # Colored reply keyboard (Bot API 9.4 style on KeyboardButton)
    return {"keyboard": [
        [{"text": "📥 Pending Withdrawals", "style": "primary"}, {"text": "👤 Users", "style": "success"}],
        [{"text": "⚙️ Settings", "style": "primary"}, {"text": "📊 Admin Stats", "style": "success"}],
        [{"text": "➕ Add Points", "style": "success"}, {"text": "➖ Deduct Points", "style": "danger"}],
        [{"text": "📢 Broadcast", "style": "primary"}, {"text": "📝 Admin Logs", "style": "success"}],
        [{"text": "🏆 Leaderboard", "style": "primary"}, {"text": "⬅️ User Menu", "style": "danger"}],
    ], "resize_keyboard": True}


# কোনো state-এর মাঝে এই বাটন/কমান্ড চাপলে state থেকে বেরিয়ে normal কাজ হবে
MENU_BUTTONS = {
    "💎 Points", "💰 Balance", "👥 Referral", "💸 Withdraw", "👤 Username",
    "🎁 Daily Bonus", "🎁 Bonus", "🎡 Luck Wheel", "📊 Statistics",
    "📜 History", "🏆 Leaderboard", "📢 Join Group", "☎️ Support",
    "🚨 JOIN GROUP",
    "📥 Pending Withdrawals", "👤 Users", "⚙️ Settings", "📊 Admin Stats",
    "➕ Add Points", "➖ Deduct Points", "📢 Broadcast", "📝 Admin Logs",
    "⬅️ User Menu",
}


def withdraw_actions(wid):
    # Colored: green = approve, red = reject (Bot API 9.4 style)
    return {"inline_keyboard": [[
        {"text": "✅ Approve", "callback_data": f"wa:{wid}:approve", "style": "success"},
        {"text": "❌ Reject", "callback_data": f"wa:{wid}:reject", "style": "danger"}
    ]]}


def group_button():
    # Colored: red = join, green = check (Bot API 9.4 style) — 2 per row
    return {"inline_keyboard": [[
        {"text": "🎯 Join Now", "url": setting("group_link"), "style": "danger"},
        {"text": "✅ Check", "callback_data": "checkjoin:0", "style": "success"},
    ]]}


def referral_buttons(uid):
    me = tg("getMe")
    username = me.get("result", {}).get("username", "") if me.get("ok") else ""
    link = f"https://t.me/{username}?start={uid}" if username else ""
    share_text = "🎁 Points জিতো! Bot open করে group-এ join করো:"
    share_url = f"https://t.me/share/url?url={requests.utils.quote(link, safe='')}&text={requests.utils.quote(share_text, safe='')}" if link else setting("group_link")
    row1 = [{"text": "📤 শেয়ার করুন", "url": share_url, "style": "primary"}]
    if link:
        # Telegram clients that support copy_text will show a native copy action.
        row1.append({"text": "📋 লিংক কপি", "copy_text": {"text": link}, "style": "primary"})
    buttons = [row1]
    buttons.append([
        {"text": "🎯 Join Now", "url": setting("group_link"), "style": "danger"},
        {"text": "✅ Check", "callback_data": f"checkjoin:{uid}", "style": "success"},
    ])
    return {"inline_keyboard": buttons}

# ---------------- Group membership / referral verification ----------------

GROUP_CHAT = "@realicamservice"

def is_group_member(user_id):
    """Returns True when Telegram confirms the user is in the required group."""
    res = tg("getChatMember", {"chat_id": GROUP_CHAT, "user_id": user_id})
    if not res.get("ok"):
        logging.warning("Could not verify group membership: %s", res)
        return None
    member = res.get("result", {})
    status = member.get("status")
    if status in ("creator", "administrator", "member"):
        return True
    if status == "restricted":
        return bool(member.get("is_member"))
    return False


# Referral milestones: (referrals needed, bonus points)
MILESTONES = [(5, 10), (10, 25), (25, 60), (50, 150), (100, 400)]

def check_milestones(uid):
    """Award bonus points when the user crosses a referral milestone (once each)."""
    row = get_user(uid)
    refs = row["referrals"]
    claimed = set(x for x in (row["milestones"] or "").split(",") if x)
    for need, bonus in MILESTONES:
        if refs >= need and str(need) not in claimed:
            add_points(uid, bonus, "milestone", f"Referral milestone: {need} referrals")
            claimed.add(str(need))
            db.execute("UPDATE users SET milestones=? WHERE user_id=?",
                       (",".join(sorted(claimed, key=int)), uid))
            db.commit()
            send(uid,
                 f"🎯 MILESTONE REACHED!\n\n👥 {need} referrals complete!\n"
                 f"💎 +{points(bonus)} bonus added to your balance.\n\n"
                 f"Keep inviting to hit the next milestone! 🚀",
                 main_menu())

def reward_pending_referral(referred_uid):
    pending = db.execute("SELECT * FROM pending_referrals WHERE referred_user_id=? AND rewarded_at IS NULL", (referred_uid,)).fetchone()
    if not pending:
        return False, "No pending referral."
    referrer = get_user(pending["referrer_id"])
    if not referrer:
        return False, "Referrer not found."
    reward = float(setting("referral_reward"))
    add_points(pending["referrer_id"], reward, "referral", f"Completed referral: {referred_uid}")
    db.execute("UPDATE users SET referral_earned=referral_earned+?, referrals=referrals+1 WHERE user_id=?", (reward, pending["referrer_id"]))
    db.execute("UPDATE pending_referrals SET rewarded_at=? WHERE referred_user_id=?", (now(), referred_uid))
    db.commit()
    check_milestones(pending["referrer_id"])
    send(pending["referrer_id"],
         f"🎉 Referral completed!\n\n💎 +{points(reward)} points added instantly.\n"
         f"👥 Your friend joined the required group.")
    return True, "Referral rewarded."


# ---------------- Points/history ----------------

def points(x):
    return f"{float(x):.0f} points"


def add_points(uid, amount, kind="credit", note=""):
    if amount <= 0:
        return
    db.execute("UPDATE users SET balance=balance+?, total_earned=total_earned+? WHERE user_id=?", (amount, amount, uid))
    db.execute("INSERT INTO point_history(user_id,amount,kind,note,created_at) VALUES(?,?,?,?,?)", (uid, amount, kind, note, now()))
    db.commit()


def deduct_points(uid, amount, kind="debit", note=""):
    if amount <= 0:
        return
    db.execute("UPDATE users SET balance=MAX(0,balance-?) WHERE user_id=?", (amount, uid))
    db.execute("INSERT INTO point_history(user_id,amount,kind,note,created_at) VALUES(?,?,?,?,?)", (uid, -amount, kind, note, now()))
    db.commit()


def log_admin(admin_id, action, details):
    db.execute("INSERT INTO admin_logs(admin_id,action,details,created_at) VALUES(?,?,?,?)", (admin_id, action, details, now()))
    db.commit()

# ---------------- User screens ----------------

def home(uid):
    row = get_user(uid)
    txt = (f"👋 Welcome, {row['first_name'] or 'User'}!\n\n"
           f"💎 Points: {points(row['balance'])}\n"
           f"👥 Referrals: {row['referrals']}\n\n"
           f"{setting('welcome_text')}\n\nChoose an option below.")
    send(uid, txt, main_menu())


def process_start(msg):
    u = msg["from"]
    uid = u["id"]
    clear_state(uid)
    is_new = upsert_user(u)
    args = (msg.get("text") or "").split(maxsplit=1)
    ref_id = int(args[1].strip()) if len(args) == 2 and args[1].strip().isdigit() else None

    # A referral is only completed after the referred user joins the required group.
    # The referrer is stored now, but the 5-point reward waits for membership verification.
    # Gate: referred users see ONLY the join prompt (no menu) until they join.
    if ref_id and ref_id != uid:
        ref = get_user(ref_id)
        current = get_user(uid)
        if ref and current and not current["referrer_id"]:
            db.execute("UPDATE users SET referrer_id=? WHERE user_id=?", (ref_id, uid))
            db.execute(
                "INSERT OR IGNORE INTO pending_referrals(referred_user_id,referrer_id,created_at) VALUES(?,?,?)",
                (uid, ref_id, now())
            )
            db.commit()
            send(uid,
                 "🎁 Points পেতে ৩টা step:\n\n"
                 "1️⃣ নিচের 🎯 Join Now চাপো\n"
                 "2️⃣ Group-এ Join করো\n"
                 "3️⃣ ✅ Check চাপো\n\n"
                 "Done! 🎉",
                 group_button())
            return

    home(uid)


def show_balance(uid):
    row = get_user(uid)
    send(uid, f"💎 Your Points\n\nAvailable: {points(row['balance'])}\nTotal earned: {points(row['total_earned'])}\nTotal withdrawn: {points(row['total_withdrawn'])}", main_menu())


def show_referral(uid):
    row = get_user(uid)
    me = tg("getMe")
    bot_username = me.get("result", {}).get("username", "") if me.get("ok") else ""
    link = f"https://t.me/{bot_username}?start={uid}" if bot_username else "Referral link unavailable."

    rw = points(setting('referral_reward'))
    rw = points(setting('referral_reward'))
    rw = points(setting('referral_reward'))
    rw = points(setting('referral_reward'))
    rw = points(setting('referral_reward'))
    caption = (
        "👥 <b>REFERRAL PROGRAM</b>\n"
        f"💎 প্রতি সফল Referral-এ <b>{rw}</b>\n\n"
        "📌 <b>কীভাবে পাবে:</b>\n"
        "1️⃣ তোমার link-টা বন্ধুদের পাঠাও\n"
        "2️⃣ বন্ধু bot চালু করে Join Group-এ click করবে\n"
        "3️⃣ Group-এ join হয়ে bot-এ ফিরে আসবে\n"
        f"4️⃣ Check বাটনে click করলেই <b>তোমার {rw}!</b>\n\n"
        "⚠️ <b>মনে রেখো:</b> বন্ধু group-এ join না করলে\n"
        "তুমি কোনো points পাবে না।\n\n"
        f"🔗 {link}"
    )
    send(uid, caption, referral_buttons(uid), parse_mode="HTML")


def show_stats(uid):
    row = get_user(uid)
    send(uid, f"📊 My Statistics\n\n👥 Referrals: {row['referrals']}\n💎 Total earned: {points(row['total_earned'])}\n💸 Total withdrawn: {points(row['total_withdrawn'])}\n💎 Current balance: {points(row['balance'])}", main_menu())


def show_history(uid):
    rows = db.execute("SELECT amount,kind,note,created_at FROM point_history WHERE user_id=? ORDER BY id DESC LIMIT 15", (uid,)).fetchall()
    if not rows:
        send(uid, "📜 No point history yet.", main_menu())
        return
    lines = ["📜 Point History\n"]
    for r in rows:
        sign = "+" if float(r["amount"]) > 0 else ""
        lines.append(f"{sign}{float(r['amount']):.0f} • {r['kind']} • {r['note']}\n{r['created_at']}")
    send(uid, "\n\n".join(lines), main_menu())


def show_leaderboard(uid):
    rows = db.execute("SELECT username,first_name,referrals,referral_earned FROM users ORDER BY referrals DESC, referral_earned DESC LIMIT 10").fetchall()
    lines = ["🏆 Referral Leaderboard\n"]
    for i, r in enumerate(rows, 1):
        name = ("@" + r["username"]) if r["username"] else (r["first_name"] or "User")
        lines.append(f"{i}. {name} — {r['referrals']} referrals")
    send(uid, "\n".join(lines), main_menu())


def claim_bonus(uid):
    row = get_user(uid)
    today = date.today().isoformat()
    if row["last_bonus"] == today:
        send(uid, "🎁 You already claimed today's bonus.", main_menu())
        return
    # POWER v1: streak — consecutive days grow the bonus
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    streak = (row["bonus_streak"] or 0) + 1 if row["last_bonus"] == yesterday else 1
    base = float(setting("daily_bonus"))
    step = float(setting("streak_step") or 1)
    bonus = base + (streak - 1) * step
    db.execute("UPDATE users SET last_bonus=?, bonus_streak=? WHERE user_id=?", (today, streak, uid))
    db.commit()
    add_points(uid, bonus, "daily_bonus", f"Daily bonus (streak day {streak})")
    streak_line = f"🔥 Streak: Day {streak}!" + (" Keep it going! 🚀" if streak > 1 else "")
    send(uid, f"🎁 Daily Bonus\n\n{streak_line}\n💎 +{points(bonus)} added to your balance.", main_menu())


def process_completed_wheel_draws(current_date=None):
    """Award 100 points to one random wheel participant for each completed 5-day period."""
    current_date = current_date or date.today()
    anchor_text = setting("wheel_draw_anchor") or current_date.isoformat()
    try:
        anchor = date.fromisoformat(anchor_text)
    except ValueError:
        anchor = current_date
        set_setting("wheel_draw_anchor", anchor.isoformat())

    if current_date < anchor:
        return

    completed_periods = (current_date - anchor).days // 5
    for period_index in range(completed_periods):
        period_start = anchor + timedelta(days=period_index * 5)
        period_end = period_start + timedelta(days=5)
        start_text = period_start.isoformat()
        end_text = period_end.isoformat()
        already_drawn = db.execute(
            "SELECT 1 FROM wheel_draws WHERE period_start=?", (start_text,)
        ).fetchone()
        if already_drawn:
            continue

        participants = db.execute(
            "SELECT DISTINCT user_id FROM wheel_spins WHERE spin_date>=? AND spin_date<?",
            (start_text, end_text)
        ).fetchall()
        winner_id = random.choice(participants)["user_id"] if participants else None
        db.execute(
            "INSERT INTO wheel_draws(period_start,period_end,winner_id,created_at) VALUES(?,?,?,?)",
            (start_text, end_text, winner_id, now())
        )
        db.commit()
        if winner_id is not None:
            add_points(winner_id, 100, "luck_wheel_5day", f"5-day Luck Wheel draw ({start_text} to {end_text})")
            send(winner_id,
                 "🏆🎉 অভিনন্দন!\n\nআপনি ৫ দিনের Luck Wheel ড্র-তে নির্বাচিত হয়েছেন এবং 100 points পেয়েছেন!\n💎 পুরস্কার আপনার ব্যালেন্সে যোগ হয়েছে।",
                 main_menu())


def play_luck_wheel(uid):
    """One free spin per calendar day; prizes are 50/20/30/20/10 points."""
    today = date.today().isoformat()
    process_completed_wheel_draws(date.today())
    previous = db.execute(
        "SELECT prize FROM wheel_spins WHERE user_id=? AND spin_date=?",
        (uid, today)
    ).fetchone()
    if previous:
        send(uid,
             f"🎡 আপনি আজকের Luck Wheel ইতিমধ্যে খেলেছেন।\n\n"
             f"আজকের পুরস্কার: {points(previous['prize'])}\n"
             "আগামীকাল আবার চেষ্টা করুন!",
             main_menu())
        return

    # Equal chance among the requested prizes; duplicate 20 means it has twice the chance.
    prize = random.choice([50, 20, 30, 20, 10])
    try:
        db.execute(
            "INSERT INTO wheel_spins(user_id,spin_date,prize,created_at) VALUES(?,?,?,?)",
            (uid, today, prize, now())
        )
        add_points(uid, prize, "luck_wheel", f"Daily Luck Wheel prize: {prize} points")
        db.commit()
    except sqlite3.IntegrityError:
        db.rollback()
        send(uid, "🎡 আপনি আজকের Luck Wheel ইতিমধ্যে খেলেছেন। আগামীকাল আবার চেষ্টা করুন!", main_menu())
        return

    send(uid,
         f"🎡✨ Luck Wheel ফলাফল!\n\n🎉 আপনি পেয়েছেন {points(prize)}!\n"
         "💎 পুরস্কার আপনার ব্যালেন্সে যোগ হয়েছে।\n"
         "প্রতি ৫ দিনে অংশগ্রহণকারীদের মধ্য থেকে একজন 100 points-এর বোনাস জিতবেন।\n"
         "আগামীকাল আবার ঘুরাতে পারবেন।",
         main_menu())


def show_username(uid):
    row = get_user(uid)
    username = row["username"] or ""
    if not username:
        send(uid, "⚠️ You do not have a Telegram username.\n\nOpen Telegram Settings → Edit → Username and create one, then try again.", main_menu())
        return
    username = "@" + username.lstrip("@")
    db.execute("UPDATE users SET wallet=? WHERE user_id=?", (username, uid))
    db.commit()
    send(uid, f"👤 Telegram Username\n\n{username}\n\nThis username will be used for withdrawal requests. No bKash/Nagad number is required.", main_menu())


def show_group(uid):
    send(uid, "📢 Join our Telegram group for updates, announcements and support.", group_button())


def begin_withdraw(uid):
    row = get_user(uid)
    minimum = float(setting("min_withdraw"))
    if float(row["balance"]) < minimum:
        send(uid, f"⚠️ সর্বনিম্ন উইথড্র: {points(minimum)}\n💎 তোমার ব্যালেন্স: {points(row['balance'])}", main_menu())
        return
    username = row["username"] or ""
    if not username:
        send(uid, "⚠️ উইথড্র করতে Telegram username লাগবে।\n\nTelegram Settings-এ username সেট করে আবার চেষ্টা করো।", main_menu())
        return
    clear_state(uid)
    set_state(uid, "withdraw_amount", "@" + username.lstrip("@"))
    send(uid,
        "💸 <b>উইথড্র রিকোয়েস্ট</b>\n\n"
        f"👤 Username: @{username.lstrip('@')}\n"
        f"💎 ব্যালেন্স: {points(row['balance'])}\n\n"
        f"{setting('delivery_text')}\n\n"
        "কত পয়েন্ট নিতে চাও? লিখে পাঠাও।\n"
        f"সর্বনিম্ন: {points(minimum)}\n\n"
        "বাতিল করতে /cancel লিখো.",
        parse_mode="HTML")



def create_withdrawal(uid, username, amount):
    row = get_user(uid)
    minimum = float(setting("min_withdraw"))
    balance = float(row["balance"])
    if amount < minimum:
        send(uid, f"❌ Minimum withdrawal is {points(minimum)}.", main_menu())
        return
    if amount > balance:
        send(uid, "❌ Insufficient points.", main_menu())
        return
    username = username.strip()
    if not username.startswith("@"):
        username = "@" + username
    if len(username) < 2 or " " in username or username.count("@") != 1:
        send(uid, "❌ Invalid Telegram username. Example: @yourusername", main_menu())
        return
    created = now()
    expires = expires_in_72h()
    db.execute("INSERT INTO withdrawals(user_id,amount,wallet,status,created_at,expires_at) VALUES(?,?,?,'pending',?,?)", (uid, amount, username, created, expires))
    wid = db.execute("SELECT last_insert_rowid()").fetchone()[0]
    db.execute("UPDATE users SET wallet=?, balance=balance-? WHERE user_id=?", (username, amount, uid))
    db.execute("INSERT INTO point_history(user_id,amount,kind,note,created_at) VALUES(?,?,?,?,?)", (uid, -amount, "withdraw_reserved", f"Withdrawal #{wid}", created))
    db.commit()
    clear_state(uid)

    send(uid, f"✅ Withdrawal request #{wid} submitted.\n\n👤 Username: {username}\n💎 Points reserved: {points(amount)}\n⏳ {setting('delivery_text')}\n\nIf the request is not approved within 72 hours, your points will be returned automatically.", main_menu())

    for admin in ADMIN_IDS:
        send(admin, f"💸 NEW WITHDRAWAL #{wid}\n\n👤 User ID: {uid}\n👤 Username: {username}\n💎 Amount: {points(amount)}\n⏰ Created: {created}\n⌛ Auto-refund after: {expires}", withdraw_actions(wid))


def show_withdrawals(uid):
    rows = db.execute("SELECT id,amount,wallet,status,created_at,processed_at FROM withdrawals WHERE user_id=? ORDER BY id DESC LIMIT 15", (uid,)).fetchall()
    if not rows:
        send(uid, "📜 No withdrawal history yet.", main_menu())
        return
    lines = ["📜 Withdrawal History\n"]
    for r in rows:
        lines.append(f"#{r['id']} • {points(r['amount'])} • {r['status']}\n👤 {r['wallet']}\n{r['created_at']}")
    send(uid, "\n\n".join(lines), main_menu())

# ---------------- Admin ----------------

def is_admin(uid):
    return uid in ADMIN_IDS


def show_pending(admin_id):
    rows = db.execute("SELECT w.*,u.username AS actual_username,u.first_name FROM withdrawals w LEFT JOIN users u ON u.user_id=w.user_id WHERE w.status='pending' ORDER BY w.id DESC LIMIT 50").fetchall()
    if not rows:
        send(admin_id, "📥 No pending withdrawals.", admin_menu())
        return
    for r in rows:
        send(admin_id, f"💸 Withdrawal #{r['id']}\n\n👤 User ID: {r['user_id']}\n👤 Username: {r['wallet']}\n💎 Amount: {points(r['amount'])}\n⏰ Created: {r['created_at']}\n⌛ Auto-refund: {r['expires_at']}", withdraw_actions(r["id"]))


def admin_stats(uid):
    users = db.execute("SELECT COUNT(*) c FROM users").fetchone()["c"]
    pending = db.execute("SELECT COUNT(*) c FROM withdrawals WHERE status='pending'").fetchone()["c"]
    approved = db.execute("SELECT COALESCE(SUM(amount),0) s FROM withdrawals WHERE status='approved'").fetchone()["s"]
    refunded = db.execute("SELECT COALESCE(SUM(amount),0) s FROM withdrawals WHERE status IN ('rejected','expired')").fetchone()["s"]
    total_bal = db.execute("SELECT COALESCE(SUM(balance),0) s FROM users").fetchone()["s"]
    refs = db.execute("SELECT COALESCE(SUM(referrals),0) s FROM users").fetchone()["s"]
    send(uid, f"📊 Admin Statistics\n\n👤 Users: {users}\n👥 Total referrals: {refs}\n⏳ Pending withdrawals: {pending}\n✅ Approved points: {points(approved)}\n↩️ Refunded points: {points(refunded)}\n💎 User balances: {points(total_bal)}", admin_menu())


def show_settings(uid):
    send(uid, f"⚙️ Settings\n\n🎯 Referral reward: {points(setting('referral_reward'))}\n🎁 Daily bonus: {points(setting('daily_bonus'))}\n💸 Minimum withdrawal: {points(setting('min_withdraw'))}\n💎 Currency label: {setting('currency')}\n🔗 Group: {setting('group_link')}\n⏳ Delivery text: {setting('delivery_text')}\n\nCommands:\n/setref <points>\n/setbonus <points>\n/setmin <points>\n/setcurrency <text>\n/setgroup <url>\n/setdelivery <text>\n/setwelcome <text>\n/setsupport <text>", admin_menu())


def show_admin_logs(uid):
    rows = db.execute("SELECT * FROM admin_logs ORDER BY id DESC LIMIT 20").fetchall()
    if not rows:
        send(uid, "📝 No admin logs yet.", admin_menu())
        return
    lines = ["📝 Admin Logs\n"]
    for r in rows:
        lines.append(f"#{r['id']} • {r['action']} • Admin {r['admin_id']}\n{r['details']}\n{r['created_at']}")
    send(uid, "\n\n".join(lines), admin_menu())


def show_users(uid):
    rows = db.execute("SELECT user_id,username,first_name,balance,referrals FROM users ORDER BY joined_at DESC LIMIT 30").fetchall()
    if not rows:
        send(uid, "👤 No users.", admin_menu())
        return
    lines = ["👤 Recent Users (30)\n"]
    for r in rows:
        name = "@" + r["username"] if r["username"] else (r["first_name"] or "User")
        lines.append(f"{r['user_id']} • {name}\n💎 {points(r['balance'])} • 👥 {r['referrals']}")
    send(uid, "\n\n".join(lines), admin_menu())


def admin_leaderboard(uid):
    rows = db.execute("SELECT username,first_name,referrals,referral_earned FROM users ORDER BY referrals DESC LIMIT 20").fetchall()
    lines = ["🏆 Admin Referral Leaderboard\n"]
    for i, r in enumerate(rows, 1):
        name = "@" + r["username"] if r["username"] else (r["first_name"] or "User")
        lines.append(f"{i}. {name} — {r['referrals']} referrals — {points(r['referral_earned'])}")
    send(uid, "\n".join(lines), admin_menu())


def approve_withdraw(wid, admin_id):
    row = db.execute("SELECT * FROM withdrawals WHERE id=?", (wid,)).fetchone()
    if not row or row["status"] != "pending":
        return False, "Already processed or not found."
    db.execute("UPDATE withdrawals SET status='approved',processed_at=?,admin_id=? WHERE id=? AND status='pending'", (now(), admin_id, wid))
    db.execute("UPDATE users SET total_withdrawn=total_withdrawn+? WHERE user_id=?", (row["amount"], row["user_id"]))
    db.execute("INSERT INTO point_history(user_id,amount,kind,note,created_at) VALUES(?,?,?,?,?)", (row["user_id"], 0, "withdraw_approved", f"Withdrawal #{wid} approved", now()))
    db.commit()
    log_admin(admin_id, "approve_withdrawal", f"Withdrawal #{wid}, user {row['user_id']}, {row['amount']} points")
    send(row["user_id"], f"✅ Withdrawal #{wid} approved.\n\n👤 Username: {row['wallet']}\n💎 Amount: {points(row['amount'])}\n\nPlease allow the stated 12-72 hour processing window for the payout.")
    return True, "Approved."


def reject_withdraw(wid, admin_id, expired=False):
    row = db.execute("SELECT * FROM withdrawals WHERE id=?", (wid,)).fetchone()
    if not row or row["status"] != "pending":
        return False, "Already processed or not found."
    status = "expired" if expired else "rejected"
    db.execute("UPDATE withdrawals SET status=?,processed_at=?,admin_id=?,refund_at=? WHERE id=? AND status='pending'", (status, now(), admin_id, now(), wid))
    db.execute("UPDATE users SET balance=balance+? WHERE user_id=?", (row["amount"], row["user_id"]))
    db.execute("INSERT INTO point_history(user_id,amount,kind,note,created_at) VALUES(?,?,?,?,?)", (row["user_id"], row["amount"], "withdraw_refund", f"Withdrawal #{wid} {'auto-refund' if expired else 'rejected'}", now()))
    db.commit()
    log_admin(admin_id, "auto_refund" if expired else "reject_withdrawal", f"Withdrawal #{wid}, user {row['user_id']}, refund {row['amount']} points")
    if expired:
        send(row["user_id"], f"⏳ Server Busy\n\nYour withdrawal request #{wid} was not approved within 72 hours.\n💎 {points(row['amount'])} have been returned to your balance.\n\nPlease submit a new request later.", main_menu())
        return True, "Expired and refunded."
    send(row["user_id"], f"❌ Withdrawal #{wid} rejected.\n\n💎 {points(row['amount'])} have been returned to your balance.", main_menu())
    return True, "Rejected and refunded."


def expire_old_withdrawals():
    rows = db.execute("SELECT id FROM withdrawals WHERE status='pending' AND expires_at IS NOT NULL").fetchall()
    for r in rows:
        row = db.execute("SELECT * FROM withdrawals WHERE id=?", (r["id"],)).fetchone()
        exp = parse_dt(row["expires_at"])
        if exp and now_dt() >= exp:
            reject_withdraw(row["id"], 0, expired=True)

# ---------------- State handling ----------------

def handle_state(uid, text):
    state = get_state(uid)
    if not state:
        return False
    if text.startswith("/") or text in MENU_BUTTONS:
        clear_state(uid)
        return False

    if state["state"] == "withdraw_amount":
        try:
            amount = float(text.replace(",", "").strip())
        except ValueError:
            send(uid, "❌ Enter a valid point amount, e.g. 120")
            return True
        if amount <= 0:
            send(uid, "❌ Amount must be greater than 0.")
            return True
        create_withdrawal(uid, state["data"], amount)
        return True

    if state["state"] == "add_points":
        try:
            target, amount = text.split()[0], float(text.split()[1])
            target = int(target)
        except Exception:
            send(uid, "Format: USER_ID POINTS\nExample: 123456789 100")
            return True
        if amount <= 0 or not get_user(target):
            send(uid, "Invalid user or amount.")
            return True
        add_points(target, amount, "admin_credit", f"Admin {uid} added points")
        clear_state(uid)
        log_admin(uid, "add_points", f"User {target}: +{amount}")
        send(uid, f"✅ Added {points(amount)} to {target}.", admin_menu())
        send(target, f"💎 Admin added {points(amount)} to your balance.")
        return True

    if state["state"] == "deduct_points":
        try:
            target, amount = text.split()[0], float(text.split()[1])
            target = int(target)
        except Exception:
            send(uid, "Format: USER_ID POINTS\nExample: 123456789 100")
            return True
        row = get_user(target)
        if amount <= 0 or not row or float(row["balance"]) < amount:
            send(uid, "Invalid user or insufficient user balance.")
            return True
        deduct_points(target, amount, "admin_debit", f"Admin {uid} deducted points")
        clear_state(uid)
        log_admin(uid, "deduct_points", f"User {target}: -{amount}")
        send(uid, f"✅ Deducted {points(amount)} from {target}.", admin_menu())
        send(target, f"ℹ️ {points(amount)} were deducted from your balance by admin.")
        return True

    if state["state"] == "broadcast":
        clear_state(uid)
        rows = db.execute("SELECT user_id FROM users").fetchall()
        sent_count = 0
        for r in rows:
            result = send(r["user_id"], "📢 Announcement\n\n" + text, main_menu())
            if result.get("ok"):
                sent_count += 1
            time.sleep(0.03)
        log_admin(uid, "broadcast", f"Sent to {sent_count}/{len(rows)} users")
        send(uid, f"📢 Broadcast finished. Sent to {sent_count}/{len(rows)} users.", admin_menu())
        return True

    return False

# ---------------- Commands ----------------

def handle_command(msg):
    uid = msg["from"]["id"]
    text = msg.get("text", "")
    cmd = text.split()[0].split("@")[0].lower()
    parts = text.split(maxsplit=1)

    if cmd == "/start":
        process_start(msg)
    elif cmd == "/cancel":
        clear_state(uid); send(uid, "Cancelled.", admin_menu() if is_admin(uid) else main_menu())
    elif cmd == "/admin" and is_admin(uid):
        send(uid, "👑 Admin Panel", admin_menu())
    elif cmd in {"/setref", "/setbonus", "/setmin"} and is_admin(uid):
        if len(parts) != 2:
            send(uid, f"Usage: {cmd} 5", admin_menu()); return
        key = {"/setref":"referral_reward", "/setbonus":"daily_bonus", "/setmin":"min_withdraw"}[cmd]
        try: val = float(parts[1])
        except ValueError: send(uid, "Enter a valid number.", admin_menu()); return
        if val < 0: send(uid, "Value cannot be negative.", admin_menu()); return
        set_setting(key, val); log_admin(uid, cmd, f"{key}={val}"); send(uid, "✅ Setting updated.", admin_menu())
    elif cmd == "/setcurrency" and is_admin(uid):
        if len(parts) == 2: set_setting("currency", parts[1][:20]); log_admin(uid, cmd, parts[1][:20]); send(uid, "✅ Currency label updated.", admin_menu())
        else: send(uid, "Usage: /setcurrency POINTS", admin_menu())
    elif cmd == "/setgroup" and is_admin(uid):
        if len(parts) == 2 and parts[1].startswith("https://t.me/"): set_setting("group_link", parts[1]); log_admin(uid, cmd, parts[1]); send(uid, "✅ Group link updated.", admin_menu())
        else: send(uid, "Usage: /setgroup https://t.me/yourgroup", admin_menu())
    elif cmd == "/setdelivery" and is_admin(uid):
        if len(parts) == 2: set_setting("delivery_text", parts[1][:300]); log_admin(uid, cmd, parts[1][:300]); send(uid, "✅ Delivery message updated.", admin_menu())
        else: send(uid, "Usage: /setdelivery Withdrawals are processed within 12-72 hours.", admin_menu())
    elif cmd == "/setwelcome" and is_admin(uid):
        if len(parts) == 2: set_setting("welcome_text", parts[1][:300]); log_admin(uid, cmd, parts[1][:300]); send(uid, "✅ Welcome text updated.", admin_menu())
        else: send(uid, "Usage: /setwelcome Your welcome message", admin_menu())
    elif cmd == "/setsupport" and is_admin(uid):
        if len(parts) == 2: set_setting("support_text", parts[1][:300]); log_admin(uid, cmd, parts[1][:300]); send(uid, "✅ Support text updated.", admin_menu())
        else: send(uid, "Usage: /setsupport Contact admin for support.", admin_menu())
    else:
        send(uid, "Use the buttons below.", admin_menu() if is_admin(uid) else main_menu())


def handle_message(msg):
    if msg.get("chat", {}).get("type") != "private":
        return
    uid = msg["from"]["id"]
    text = (msg.get("text") or "").strip()
    if not text:
        return
    upsert_user(msg["from"])
    if text.startswith("/"):
        handle_command(msg); return
    if handle_state(uid, text): return

    if text in {"💎 Points", "💰 Balance"}: show_balance(uid)
    elif text == "👥 Referral": show_referral(uid)
    elif text == "💸 Withdraw": begin_withdraw(uid)
    elif text == "👤 Username": show_username(uid)
    elif text in {"🎁 Daily Bonus", "🎁 Bonus"}: claim_bonus(uid)
    elif text == "🎡 Luck Wheel": play_luck_wheel(uid)
    elif text == "📊 Statistics": show_stats(uid)
    elif text == "📜 History": show_history(uid)
    elif text == "🏆 Leaderboard": show_leaderboard(uid)
    elif text in {"🚨 JOIN GROUP", "📢 Join Group"}: show_group(uid)
    elif text == "☎️ Support": send(uid, "☎️ Support\n\n" + setting("support_text"), main_menu())
    elif is_admin(uid) and text == "📥 Pending Withdrawals": show_pending(uid)
    elif is_admin(uid) and text == "👤 Users": show_users(uid)
    elif is_admin(uid) and text == "⚙️ Settings": show_settings(uid)
    elif is_admin(uid) and text == "📊 Admin Stats": admin_stats(uid)
    elif is_admin(uid) and text == "➕ Add Points": set_state(uid, "add_points") or send(uid, "Send: USER_ID POINTS\nExample: 123456789 100\n/cancel to stop.")
    elif is_admin(uid) and text == "➖ Deduct Points": set_state(uid, "deduct_points") or send(uid, "Send: USER_ID POINTS\nExample: 123456789 100\n/cancel to stop.")
    elif is_admin(uid) and text == "📢 Broadcast": set_state(uid, "broadcast") or send(uid, "Send the broadcast message.\n/cancel to stop.")
    elif is_admin(uid) and text == "📝 Admin Logs": show_admin_logs(uid)
    elif is_admin(uid) and text == "🏆 Leaderboard": admin_leaderboard(uid)
    elif is_admin(uid) and text == "⬅️ User Menu": send(uid, "👤 User Menu", main_menu())
    else: send(uid, "Please choose an option.", admin_menu() if is_admin(uid) else main_menu())


def handle_callback(q):
    uid = q["from"]["id"]
    data = q.get("data", "")

    if data.startswith("checkjoin:"):
        answer_callback(q["id"], "Membership checking...")
        ok = is_group_member(uid)
        if ok is True:
            rewarded, result = reward_pending_referral(uid)
            if rewarded:
                send(uid, "✅ Group membership verified.\n\nYour referral is now complete. The referrer has received the points.", main_menu())
                return
            send(uid, "✅ Group membership verified.\n\nNo pending referral reward is waiting for this account.", main_menu())
            return
        if ok is False:
            send(uid,
                 "⚠️ Group Join আবশ্যক।\n\n"
                 "আগে আমাদের Telegram Group-এ Join করুন, তারপর আবার “গ্রুপে Join করেছি — Check” চাপুন.",
                 group_button())
            return
        send(uid,
             "⚠️ Membership check could not be completed right now.\n"
             "Please try again in a moment.",
             group_button())
        return

    if not data.startswith("wa:") or not is_admin(uid):
        answer_callback(q["id"], "Not authorized.")
        return
    answer_callback(q["id"])
    parts = data.split(":")
    if len(parts) != 3:
        return
    wid = int(parts[1])
    action = parts[2]
    ok, result = approve_withdraw(wid, uid) if action == "approve" else reject_withdraw(wid, uid)
    chat_id = q["message"]["chat"]["id"]
    message_id = q["message"]["message_id"]
    edit(chat_id, message_id, f"💸 Withdrawal #{wid}\n\n{'Processed' if ok else result}")
    send(uid, result, admin_menu())


# ---------------- Polling ----------------

def main():
    init_db()
    tg("deleteWebhook", {"drop_pending_updates": False})
    offset = None
    last_expiry_check = 0
    last_wheel_draw_check = 0
    logging.info("Bot started.")
    # Catch up on any completed 5-day periods after a restart, then keep
    # checking on a timer so the draw does not depend on someone spinning.
    try:
        process_completed_wheel_draws(date.today())
        last_wheel_draw_check = time.time()
    except Exception:
        logging.exception("Initial Luck Wheel draw check failed")
    while True:
        try:
            if time.time() - last_expiry_check >= 60:
                expire_old_withdrawals()
                last_expiry_check = time.time()
            if time.time() - last_wheel_draw_check >= 30:
                process_completed_wheel_draws(date.today())
                last_wheel_draw_check = time.time()
            payload = tg("getUpdates", {"timeout": 30, "offset": offset, "allowed_updates": ["message", "callback_query"]})
            if not payload.get("ok"):
                time.sleep(3); continue
            for update in payload.get("result", []):
                offset = update["update_id"] + 1
                try:
                    if "message" in update: handle_message(update["message"])
                    elif "callback_query" in update: handle_callback(update["callback_query"])
                except Exception: logging.exception("Error processing update")
        except KeyboardInterrupt:
            break
        except Exception:
            logging.exception("Polling error")
            time.sleep(3)

if __name__ == "__main__":
    main()

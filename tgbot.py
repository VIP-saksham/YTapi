"""
tgbot.py
--------
HellAPI Commercial Telegram Bot v5.0

Features:
  - /start     → auto key + welcome
  - /mykey     → quick key display
  - /status    → API server status check
  - /upgrade   → admin: upgrade user plan
  - /reply     → admin: reply to ticket
  - /payments  → admin: pending payment requests
  - /approve   → admin: approve payment & upgrade
  - /reject    → admin: reject payment
  - /broadcast → admin: send message to all users
  - /admin     → admin panel
  - Inline keyboard: View Key, Usage, Plans, Upgrade, Ticket, Feedback, Help
  - Payment flow with UPI/bank details
  - Expiry warnings (sent at key creation + 3 days before expiry)
"""

import os
import logging
import asyncio
import time
import httpx
import pytz

from datetime import datetime
from dotenv import load_dotenv

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    BotCommand,
)
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
from telegram.constants import ParseMode, ChatType
from telegram.error import TelegramError, BadRequest

import database as db

load_dotenv()

# ── Config ───────────────────────────────────────────────────────────────────
BOT_TOKEN     = os.getenv("TELEGRAM_BOT_TOKEN", "")
BOT_URL       = os.getenv("BOT_URL", "")                    # e.g. https://t.me/HellAPIBot
APP_URL       = os.getenv("APP_URL", "http://localhost:8000").rstrip("/")
API_BASE_URL  = os.getenv("API_BASE_URL", APP_URL).rstrip("/")

# Admin-set URL persist hota hai (restart-proof) — /apiurl se set hota hai
API_URL_FILE  = os.path.join(os.path.dirname(os.path.abspath(__file__)), "api_url.txt")
try:
    if os.path.exists(API_URL_FILE):
        API_BASE_URL = open(API_URL_FILE, encoding="utf-8").read().strip() or API_BASE_URL
except Exception:
    pass
BOT_NAME      = os.getenv("BOT_NAME", "HellAPI")
SUPPORT_GROUP = os.getenv("SUPPORT_GROUP", "https://t.me/hellupdates1")
CHANNEL       = os.getenv("CHANNEL", "https://t.me/hellupdates1")
ADMIN_IDS     = [int(x) for x in os.getenv("ADMIN_IDS", "0").split(",") if x.strip().isdigit()]

# Owner + Logger chat — sab important events yahan jaate hain
OWNER_ID      = int(os.getenv("OWNER_ID", "8538341106") or 0)
LOGGER_ID     = int(os.getenv("LOGGER_ID", "-1003705956703") or 0)
if OWNER_ID and OWNER_ID not in ADMIN_IDS:
    ADMIN_IDS.insert(0, OWNER_ID)

# Payment details — fill in .env
UPI_ID        = os.getenv("UPI_ID", "yourname@upi")
UPI_NAME      = os.getenv("UPI_NAME", "HellAPI")
BANK_NAME     = os.getenv("BANK_NAME", "")
BANK_ACCOUNT  = os.getenv("BANK_ACCOUNT", "")
BANK_IFSC     = os.getenv("BANK_IFSC", "")

if not BOT_TOKEN:
    raise RuntimeError("Set TELEGRAM_BOT_TOKEN in .env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger(__name__)
IST = pytz.timezone("Asia/Kolkata")


# ── Keyboards ────────────────────────────────────────────────────────────────

def main_menu_kb() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("🔑 My API Key",       callback_data="view_key")],
        [InlineKeyboardButton("📊 My Usage",         callback_data="my_usage")],
        [InlineKeyboardButton("💎 Plans & Pricing",  callback_data="plans")],
        [
            InlineKeyboardButton("🎫 Support",  callback_data="support_ticket"),
            InlineKeyboardButton("💬 Feedback", callback_data="feedback"),
        ],
        [InlineKeyboardButton("❓ Help & Docs",      callback_data="help")],
    ]
    # URL buttons sirf tab jab URL set ho — empty URL Telegram reject karta hai
    url_row = []
    if SUPPORT_GROUP:
        url_row.append(InlineKeyboardButton("💬 Support Group", url=SUPPORT_GROUP))
    if CHANNEL:
        url_row.append(InlineKeyboardButton("📢 Channel", url=CHANNEL))
    if url_row:
        rows.append(url_row)
    return InlineKeyboardMarkup(rows)


def key_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Renew (+30 days)",      callback_data="renew_key")],
        [InlineKeyboardButton("🔁 Revoke & New Key",      callback_data="revoke_key")],
        [InlineKeyboardButton("⬆️ Upgrade Plan",          callback_data="upgrade_request")],
        [InlineKeyboardButton("🔙 Main Menu",             callback_data="back_main")],
    ])


def back_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔙 Main Menu", callback_data="back_main")]
    ])


def plans_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⚡ Starter — ₹99 · 1k/day",  callback_data="buy_basic")],
        [InlineKeyboardButton("🚀 Pro — ₹199 · 3k/day", callback_data="buy_pro")],
        [InlineKeyboardButton("👑 Ultra — ₹490 · 9k/day", callback_data="buy_ultra")],
        [InlineKeyboardButton("🔙 Main Menu",            callback_data="back_main")],
    ])


def test_type_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🎵 Audio", callback_data="test_audio"),
            InlineKeyboardButton("🎬 Video", callback_data="test_video"),
        ],
        [InlineKeyboardButton("❌ Cancel", callback_data="back_main")],
    ])


def admin_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Stats",           callback_data="admin_stats")],
        [InlineKeyboardButton("💰 Pending Payments", callback_data="admin_payments")],
        [InlineKeyboardButton("🎫 Open Tickets",    callback_data="admin_tickets")],
        [InlineKeyboardButton("📢 Broadcast",       callback_data="admin_broadcast")],
        [InlineKeyboardButton("🔙 Main Menu",       callback_data="back_main")],
    ])


# ── Helpers ───────────────────────────────────────────────────────────────────

def is_admin(uid: int) -> bool:
    return uid in ADMIN_IDS


def fmt_dur(seconds) -> str:
    if not seconds:
        return "N/A"
    try:
        m, s = divmod(int(seconds), 60)
        h, m = divmod(m, 60)
        return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"
    except Exception:
        return "N/A"


def fmt_ist(dt_str: str) -> str:
    try:
        dt  = datetime.strptime(dt_str, "%Y-%m-%d %H:%M:%S")
        ist = pytz.utc.localize(dt).astimezone(IST)
        return ist.strftime("%d %b %Y, %I:%M %p IST")
    except Exception:
        return str(dt_str)[:16] if dt_str else "N/A"


def _ensure_key(user_id: int):
    row = db.get_active_key(user_id)
    if not row:
        db.create_key(user_id, "free")
        row = db.get_active_key(user_id)
    return row


async def safe_send(bot, chat_id: int, text: str, **kw):
    try:
        await bot.send_message(chat_id, text, **kw)
    except TelegramError as e:
        logger.warning(f"safe_send to {chat_id}: {e}")
    except Exception as e:
        logger.warning(f"safe_send unexpected {chat_id}: {e}")


async def safe_edit(query, text: str, **kw):
    try:
        await query.edit_message_text(text, **kw)
    except BadRequest as e:
        if "not modified" not in str(e).lower():
            logger.warning(f"safe_edit: {e}")
    except Exception as e:
        logger.warning(f"safe_edit: {e}")


APP_REF = None   # run_bot() me set hota hai — logger events ke liye


def _esc(s) -> str:
    import html as _html
    return _html.escape(str(s))


def _uname(user) -> str:
    if getattr(user, "username", None):
        return f"@{user.username}"
    return _esc(getattr(user, "first_name", None) or "—")


def _now_ist() -> str:
    return datetime.now(IST).strftime("%d %b %Y, %I:%M %p IST")


def log_event(text: str):
    """Fire-and-forget event → LOGGER_ID (logger chat). Main flow kabhi block nahi karta."""
    if not (LOGGER_ID and APP_REF):
        return

    async def _send():
        try:
            await APP_REF.bot.send_message(
                LOGGER_ID, text,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
        except Exception as e:
            logger.warning(f"log_event fail: {e}")

    try:
        asyncio.get_running_loop().create_task(_send())
    except RuntimeError:
        pass   # event-loop ke bahar se call hua — chhod do


def _key_card(key_row, user_id: int) -> str:
    if not key_row:
        return "❌ No active key."
    try:
        days     = db.days_remaining(user_id)
        stats    = db.get_usage_stats(user_id)
        plan     = key_row["plan"]
        cfg      = db.PLANS.get(plan, db.PLANS["free"])
        rpd      = cfg["rpd"]
        lim      = "Unlimited" if rpd == -1 else f"{rpd:,}"
        warn     = " ⚠️ Expiring soon!" if days <= 3 else ""

        return (
            f"🔑 *API Key Details*\n\n"
            f"Key: `{key_row['key']}`\n"
            f"Plan: *{plan.upper()}* — {cfg['price']}\n"
            f"Status: 🟢 Active{warn}\n"
            f"Expires: {fmt_ist(key_row['expires_at'])} ({days}d left)\n\n"
            f"*Today:*  {stats['today']} reqs  |  🎵 {stats['today_audio']}  🎬 {stats['today_video']}\n"
            f"*Total:*  {stats['total']} reqs  |  🎵 {stats['total_audio']}  🎬 {stats['total_video']}\n\n"
            f"Daily limit: {lim} req/day  •  {cfg['rpm']} req/min"
        )
    except Exception as e:
        logger.warning(f"_key_card: {e}")
        return f"🔑 Key: `{key_row['key']}`\n_Stats unavailable._"


def _plans_text() -> str:
    icons = {"free": "🆓", "basic": "⚡", "pro": "🚀", "ultra": "👑"}
    lines = [f"💎 *{BOT_NAME} — Plans & Pricing*\n"]
    for pid, p in db.PLANS.items():
        rpd = "Unlimited" if p["rpd"] == -1 else f"{p['rpd']:,}"
        feats = "  •  ".join(p.get("features", []))
        lines.append(
            f"{icons.get(pid,'•')} *{p['name']}*  —  {p['price']}\n"
            f"  {feats}\n"
        )
    lines += [
        "\n💳 *How to buy:*",
        "1️⃣ Tap a plan button below",
        "2️⃣ Send payment to our UPI",
        "3️⃣ Share UTR/screenshot",
        "4️⃣ Get upgraded instantly ✅",
    ]
    return "\n".join(lines)


def _upi_deep_link(plan: str) -> str:
    """upi:// deep link — tap karo → UPI app khul jayega amount pre-filled."""
    import re as _re
    cfg = db.PLANS.get(plan, db.PLANS["basic"])
    m   = _re.search(r"(\d+)", cfg["price"])   # "₹299/month" -> 299
    amt = m.group(1) if m else ""
    name = UPI_NAME.replace(" ", "%20")
    note = f"{BOT_NAME} {cfg['name']} plan".replace(" ", "%20")
    return (
        f"upi://pay?pa={UPI_ID}&pn={name}&am={amt}"
        f"&cu=INR&tn={note}"
    )


def _payment_info(plan: str) -> str:
    cfg    = db.PLANS.get(plan, db.PLANS["basic"])
    amount = cfg["price"]
    lines  = [
        f"💳 *Payment for {cfg['name']} Plan*\n",
        f"Amount: *{amount}*",
        f"Validity: *{cfg['validity']} days*\n",
        f"*Pay via UPI:*",
        f"`{UPI_ID}`",
        f"Name: {UPI_NAME}\n",
    ]
    if BANK_ACCOUNT:
        lines += [
            "*Bank Transfer:*",
            f"Account: `{BANK_ACCOUNT}`",
            f"IFSC: `{BANK_IFSC}`",
            f"Bank: {BANK_NAME}\n",
        ]
    lines += [
        "✅ *After payment:*",
        "Screenshot ya *UTR number* yahin bhej do.",
        "Admin verify karega — plan turant active.",
        "",
        "⚠️ _Payment ke baad ye chat band mat karna._",
    ]
    return "\n".join(lines)


async def _check_api_status() -> dict:
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            t0   = time.time()
            resp = await client.get(f"{API_BASE_URL}/")
            ms   = round((time.time() - t0) * 1000)
            if resp.status_code == 200:
                return {"ok": True, "ms": ms}
            return {"ok": False, "ms": ms, "code": resp.status_code}
    except Exception as e:
        return {"ok": False, "error": str(e)[:60]}


# ── /start ────────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    had_key = db.get_active_key(user.id) is not None
    is_new  = db.get_user(user.id) is None
    db.upsert_user(user.id, user.username, user.first_name)
    key_row  = _ensure_key(user.id)
    days     = db.days_remaining(user.id)
    plan     = key_row["plan"]
    plan_cfg = db.PLANS.get(plan, db.PLANS["free"])
    rpd      = plan_cfg["rpd"]
    lim      = "Unlimited" if rpd == -1 else f"{rpd:,}"
    api_url  = APP_URL or API_BASE_URL

    _wb = "━━━━━━━━━━━━━━━━━━━━"
    msg = (
        f"⚡ *Welcome to {BOT_NAME}* — Blazing-Fast YouTube API\n\n"
        f"{_wb}\n"
        f"🔑 *Your API Key:*\n"
        f"`{key_row['key']}`\n\n"
        f"🌐 *Endpoint:* `{api_url}`\n"
        f"📦 Plan: *{plan.upper()}* • 📊 {lim} req/day\n"
        f"⏳ Expires: {days} days\n"
        f"{_wb}\n\n"
        f"🚀 *Quick Start:*\n"
        f"1️⃣ /getfile — drop-in youtube.py lo\n"
        f"2️⃣ Bot me `HELLAPI_URL` + `HELLAPI_KEY` set karo\n"
        f"3️⃣ Done — 20ms cached streams! ⚡\n\n"
        f"🌍 Website: vip-saksham.github.io/api\n"
        f"📊 /usage — live usage stats"
    )
    _photo = os.getenv("START_IMG", "")
    _sent = False
    if _photo:
        try:
            await update.message.reply_photo(
                photo=_photo, caption=msg,
                parse_mode=ParseMode.MARKDOWN, reply_markup=main_menu_kb())
            _sent = True
        except Exception:
            _sent = False
    if not _sent:
        await update.message.reply_text(
            msg, parse_mode=ParseMode.MARKDOWN, reply_markup=main_menu_kb()
        )

    # ── Logger: kaun start kiya + nayi key bani? ──
    _l = [f"{'🆕 <b>NAYA USER</b>' if is_new else '👋 <b>User</b>'} ne bot start kiya",
          f"👤 {_uname(user)} (<code>{user.id}</code>)"]
    if not had_key:
        _l.append("🔑 <b>Nayi FREE key auto-generate hui</b>")
    _l.append(f"💎 Plan: <b>{plan.upper()}</b>")
    _l.append(f"🕐 {_now_ist()}")
    log_event("\n".join(_l))



# ── /getfile ─────────────────────────────────────────────────────────────────

async def cmd_getfile(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """
    Send youtube.py (drop-in replacement) + hellapi.py (direct client).
    Keys are NOT embedded in files — shown only in caption.
    """
    user    = update.effective_user
    db.upsert_user(user.id, user.username, user.first_name)
    key_row = _ensure_key(user.id)
    api_url = APP_URL or API_BASE_URL

    import io, pathlib
    base_dir = pathlib.Path(__file__).parent

    # ── 1. Send youtube.py — the drop-in replacement ──────────────────────
    yt_src = base_dir / "youtube.py"
    if yt_src.exists():
        yt_bytes = yt_src.read_bytes()
    else:
        yt_bytes = b"# youtube.py not found on server"

    yt_obj      = io.BytesIO(yt_bytes)
    yt_obj.name = "youtube.py"

    yt_caption = (
        f"🎵 *youtube.py — Drop-in Replacement*\n\n"
        f"📌 *Yeh file apne music bot ke folder mein daalo*\n"
        f"_(purani youtube.py replace kar do)_\n\n"
        f"*Your credentials:*\n"
        f"🔑 Key: `{key_row['key']}`\n"
        f"🌐 URL: `{api_url}`\n\n"
        f"*Setup (.env mein add karo):*\n"
        f"```\n"
        f"HELLAPI_URL={api_url}\n"
        f"HELLAPI_KEY={key_row['key']}\n"
        f"```\n\n"
        f"✅ Bas itna karo — koi aur change nahi\n"
        f"⚠️ `.env` ko `.gitignore` mein add karo!"
    )

    await update.message.reply_document(
        document=yt_obj,
        filename="youtube.py",
        caption=yt_caption,
        parse_mode=ParseMode.MARKDOWN,
    )
    log_event(f"📥 <b>youtube.py download kiya</b>\n👤 {_uname(user)} (<code>{user.id}</code>)\n🔑 Key: <code>{key_row['key'][:12]}…</code>\n🕐 {_now_ist()}")




# ── /revokekey — user apni key khud revoke kare (leak hone pe) ───────────────

async def cmd_revokekey(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """User apni leaked/compromised key instant revoke kar sakta hai."""
    user = update.effective_user
    db.upsert_user(user.id, user.username, user.first_name)
    old_row = db.get_active_key(user.id)
    if not old_row:
        await update.message.reply_text(
            "❌ No active key found.", reply_markup=back_kb()
        )
        return
    old_key = old_row["key"]
    new_key = db.revoke_key(user.id)     # deactivates old, creates new same plan
    # Log the revoke event
    db.log_key_audit(user.id, old_key, "user_revoke",
                     ip="telegram", note="User self-revoked via /revokekey")
    log_event(f"🔁 <b>Key revoked + nayi key bani (self)</b>\n👤 {_uname(user)} (<code>{user.id}</code>)\n❌ Old: <code>{old_key[:12]}…</code>\n🆕 New: <code>{new_key[:12]}…</code>\n🕐 {_now_ist()}")
    await update.message.reply_text(
        f"🔁 *Key Revoked & Replaced!*\n\n"
        f"Old key: `{old_key[:20]}...` ❌ (dead)\n\n"
        f"New key: `{new_key}`\n\n"
        f"⚠️ Update your `.env` file with the new key.\n"
        f"Old key will not work anymore.",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=back_kb(),
    )


# ── /revokeuser <uid> — admin kisi bhi user ki key revoke kare ───────────────

async def cmd_revokeuser(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Admin: force revoke any user's key (abuse/compromise)."""
    if not is_admin(update.effective_user.id):
        return
    args = ctx.args or []
    if not args:
        await update.message.reply_text(
            "Usage: `/revokeuser <user_id> [reason]`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    try:
        target_id = int(args[0])
        reason    = " ".join(args[1:]) if len(args) > 1 else "Admin revoked"
    except ValueError:
        await update.message.reply_text("❌ user_id must be a number.")
        return

    old_row = db.get_active_key(target_id)
    if not old_row:
        await update.message.reply_text(f"❌ No active key for user `{target_id}`.",
                                        parse_mode=ParseMode.MARKDOWN)
        return
    old_key = old_row["key"]
    new_key = db.revoke_key(target_id)
    db.log_key_audit(target_id, old_key, "admin_revoke",
                     ip="admin", note=reason)
    log_event(f"🛡 <b>ADMIN ne key revoke ki</b>\n🎯 User: <code>{target_id}</code>\n👮 Admin: {_uname(update.effective_user)} (<code>{update.effective_user.id}</code>)\n📝 Reason: {_esc(reason)}\n🆕 New: <code>{new_key[:12]}…</code>\n🕐 {_now_ist()}")
    await safe_send(
        ctx.bot, target_id,
        f"⚠️ *Security Notice*\n\n"
        f"Your API key was revoked by admin.\n"
        f"Reason: {reason}\n\n"
        f"New key issued:\n`{new_key}`\n\n"
        f"Update your `.env` file immediately.",
        parse_mode=ParseMode.MARKDOWN,
    )
    await update.message.reply_text(
        f"✅ User `{target_id}` key revoked.\n"
        f"Old: `{old_key[:20]}...`\nNew: `{new_key}`",
        parse_mode=ParseMode.MARKDOWN,
    )


# ── /mykey ────────────────────────────────────────────────────────────────────

async def cmd_mykey(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user    = update.effective_user
    db.upsert_user(user.id, user.username, user.first_name)
    key_row = _ensure_key(user.id)
    await update.message.reply_text(
        _key_card(key_row, user.id),
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=key_menu_kb(),
    )


# ── /status ───────────────────────────────────────────────────────────────────

async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    # 🔒 admin-only (users ko /usage milega)
    if not is_admin(update.effective_user.id):
        await update.message.reply_text(
            "🔒 *API status sirf admin ke liye.*\n\n"
            "Tum ye use karo:\n"
            "• /mykey — apni key\n"
            "• /usage — apni requests\n"
            "• /getfile — youtube.py\n"
            "• /upgrade — plan upgrade",
            parse_mode=ParseMode.MARKDOWN)
        return
    msg = await update.message.reply_text("⏳ Checking API status...")
    s   = await _check_api_status()
    if s.get("ok"):
        text = (
            f"✅ *{BOT_NAME} API — Online*\n\n"
            f"Response time: `{s['ms']} ms`\n"
            f"Endpoint: `{API_BASE_URL}`\n\n"
            f"_All systems operational._"
        )
    else:
        err = s.get("error") or f"HTTP {s.get('code', '?')}"
        text = (
            f"❌ *API Status — Issue Detected*\n\n"
            f"Error: `{err}`\n"
            f"Endpoint: `{API_BASE_URL}`\n\n"
            f"_Please contact support._"
        )
    await msg.edit_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=back_kb())


# ── /apiurl <url> — ADMIN: API base URL set/check ─────────────────────────

async def cmd_apiurl(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Admin: URL dekho, set karo (live-test ke saath), ya persist-check."""
    uid = update.effective_user.id
    if not is_admin(uid):
        await update.message.reply_text(
            "🔒 Ye command sirf *ADMIN* ke liye hai.\n\n"
            "Tumhare liye:\n"
            "• /mykey — API key nikaalo\n"
            "• /usage — apni requests dekho\n"
            "• /upgrade — plan upgrade\n"
            "• /getfile — youtube.py download",
            parse_mode=ParseMode.MARKDOWN)
        return

    args = ctx.args or []
    global API_BASE_URL
    if not args:
        s = await _check_api_status()
        cur = f"✅ online ({s.get('ms','?')} ms)" if s.get("ok") else f"❌ down ({s.get('error') or s.get('code')})"
        persisted = "✅" if os.path.exists(API_URL_FILE) else "— (env se)"
        await update.message.reply_text(
            f"🛠 *API URL (admin)*\n\n"
            f"Current: `{API_BASE_URL}`\n"
            f"Status: {cur}\n"
            f"Persisted: {persisted}\n\n"
            f"Change: `/apiurl https://naya-url.com`",
            parse_mode=ParseMode.MARKDOWN)
        return

    new_url = args[0].strip().rstrip("/")
    if not new_url.startswith(("http://", "https://")):
        await update.message.reply_text("❌ URL `http://` ya `https://` se shuru hona chahiye.", parse_mode=ParseMode.MARKDOWN)
        return

    old = API_BASE_URL
    API_BASE_URL = new_url
    s = await _check_api_status()
    if s.get("ok"):
        try:
            with open(API_URL_FILE, "w", encoding="utf-8") as f:
                f.write(new_url)
        except Exception:
            pass
        await update.message.reply_text(
            f"✅ *API URL set!*\n\n`{new_url}`\nPing: {s['ms']} ms\n\nRestart-proof saved ✓",
            parse_mode=ParseMode.MARKDOWN)
        logger.info(f"admin {uid} set API_URL -> {new_url}")
    else:
        API_BASE_URL = old  # dead URL kabhi set mat karo — rollback
        err = s.get("error") or f"HTTP {s.get('code', '?')}"
        await update.message.reply_text(
            f"❌ Naya URL dead ({err}) — purana rakha:\n`{old}`",
            parse_mode=ParseMode.MARKDOWN)


# ── /usage — NORMAL USERS: apni API usage ──────────────────────────────────

async def cmd_usage(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """User apni khud ki request-stats dekhta hai (admin nahi)."""
    user = update.effective_user
    uid  = user.id
    db.upsert_user(uid, user.username, user.first_name)
    key_row = _ensure_key(uid)
    stats   = db.get_usage_stats(uid)
    plan    = key_row["plan"] if key_row else "free"
    cfg     = db.PLANS.get(plan, db.PLANS["free"])
    days    = db.days_remaining(uid)

    def _bar(used, limit):
        if not limit or limit <= 0:
            return "∞"
        pct = min(100, int(used * 100 / limit))
        filled = pct // 10
        return "█" * filled + "░" * (10 - filled) + f" {pct}%"

    rpd = cfg.get("rpd", 0)
    plan_line = f"💎 Plan: <b>{cfg['name']}</b>" + (f" · {days}d bache" if days else "")
    key_line  = f"🔑 Key: <code>{(key_row['key'][:8] + '…') if key_row else '—'}</code>"
    today_line = f"<b>Aaj:</b> {stats['today']}" + (f"/{rpd}" if rpd else "") + " requests"
    text = (
        f"📊 <b>Aapki API Usage</b>\n\n"
        f"{plan_line}\n{key_line}\n\n"
        f"{today_line}\n"
        f"{_bar(stats['today'], rpd)}\n"
        f"  🎵 Audio: {stats['today_audio']} · 🎬 Video: {stats['today_video']}\n\n"
        f"<b>Total:</b> {stats['total']} requests\n"
        f"  🎵 Audio: {stats['total_audio']} · 🎬 Video: {stats['total_video']}\n"
        f"🕘 Last: {stats['last_used']}\n\n"
        f"➕ Zyada requests chahiye? /upgrade"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=key_menu_kb())


# ── /admin ────────────────────────────────────────────────────────────────────

async def cmd_admin(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ Access denied.")
        return
    stats = db.get_admin_stats()
    text  = (
        f"👑 *Admin Panel — {BOT_NAME}*\n\n"
        f"👥 Users: `{stats['total_users']}`\n"
        f"🔑 Active Keys: `{stats['active_keys']}`\n"
        f"📈 Requests Today: `{stats['today_requests']}`\n"
        f"💰 Pending Payments: `{stats['pending_payments']}`\n"
        f"🎫 Open Tickets: `{stats['open_tickets']}`"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=admin_kb())


# ── /upgrade <uid> <plan> ─────────────────────────────────────────────────────

async def cmd_upgrade(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    args = ctx.args or []
    if len(args) < 2:
        await update.message.reply_text(
            "Usage: `/upgrade <user_id> <plan>`\nPlans: `free` `basic` `pro` `ultra`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    try:
        tid      = int(args[0])
        new_plan = args[1].lower()
        new_key  = db.upgrade_plan(tid, new_plan)
        log_event(f"👑 <b>PLAN UPGRADED (admin)</b>\n🎯 User: <code>{tid}</code> → <b>{new_plan.upper()}</b>\n👮 Admin: {_uname(update.effective_user)} (<code>{update.effective_user.id}</code>)\n🆕 Key: <code>{new_key[:12]}…</code>\n🕐 {_now_ist()}")
        cfg      = db.PLANS[new_plan]
        rpd_str  = "Unlimited" if cfg["rpd"] == -1 else f"{cfg['rpd']:,}"

        await safe_send(
            ctx.bot, tid,
            f"🎉 *Plan Upgraded!*\n\n"
            f"Your plan is now: *{cfg['name']}* ({cfg['price']})\n\n"
            f"🔑 New key:\n`{new_key}`\n\n"
            f"📈 {rpd_str} requests/day  •  {cfg['rpm']} req/min\n"
            f"⏳ Valid for {cfg['validity']} days\n\n"
            f"_Old key has been revoked. Start using the new key now!_",
            parse_mode=ParseMode.MARKDOWN,
        )
        await update.message.reply_text(
            f"✅ User `{tid}` → *{new_plan}* plan.\nNew key: `{new_key}`",
            parse_mode=ParseMode.MARKDOWN,
        )
    except ValueError as e:
        await update.message.reply_text(f"❌ {e}")
    except Exception as e:
        logger.error(f"cmd_upgrade: {e}", exc_info=True)
        await update.message.reply_text(f"❌ Error: {e}")


# ── /approve <payment_id> ─────────────────────────────────────────────────────

async def cmd_approve(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    args = ctx.args or []
    if not args:
        await update.message.reply_text("Usage: `/approve <payment_id>`",
                                        parse_mode=ParseMode.MARKDOWN)
        return
    try:
        pay_id = int(args[0])
        with db.get_conn() as conn:
            pay = conn.execute(
                "SELECT * FROM payment_requests WHERE id=?", (pay_id,)
            ).fetchone()
        if not pay:
            await update.message.reply_text(f"❌ Payment #{pay_id} not found.")
            return

        new_key = db.upgrade_plan(pay["user_id"], pay["plan"])
        db.resolve_payment(pay_id, "approved")
        log_event(f"💰 <b>PAYMENT APPROVED #{pay_id}</b>\n👤 User: <code>{pay['user_id']}</code> → <b>{pay['plan'].upper()}</b>\n👮 Admin: {_uname(update.effective_user)} (<code>{update.effective_user.id}</code>)\n🕐 {_now_ist()}")
        cfg     = db.PLANS[pay["plan"]]
        rpd_str = "Unlimited" if cfg["rpd"] == -1 else f"{cfg['rpd']:,}"

        await safe_send(
            ctx.bot, pay["user_id"],
            f"🎉 *Payment Approved!*\n\n"
            f"Plan: *{cfg['name']}* ({cfg['price']})\n\n"
            f"🔑 Your new API key:\n`{new_key}`\n\n"
            f"📈 {rpd_str} req/day  •  {cfg['rpm']} req/min\n"
            f"⏳ Valid: {cfg['validity']} days\n\n"
            f"Thank you for choosing {BOT_NAME}! 🙏",
            parse_mode=ParseMode.MARKDOWN,
        )
        await update.message.reply_text(
            f"✅ Payment #{pay_id} approved. User `{pay['user_id']}` upgraded to *{pay['plan']}*.",
            parse_mode=ParseMode.MARKDOWN,
        )
    except Exception as e:
        logger.error(f"cmd_approve: {e}", exc_info=True)
        await update.message.reply_text(f"❌ Error: {e}")


# ── /reject <payment_id> [reason] ────────────────────────────────────────────

async def cmd_reject(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    args = ctx.args or []
    if not args:
        await update.message.reply_text("Usage: `/reject <payment_id> [reason]`",
                                        parse_mode=ParseMode.MARKDOWN)
        return
    try:
        pay_id = int(args[0])
        reason = " ".join(args[1:]) if len(args) > 1 else "Payment could not be verified."
        with db.get_conn() as conn:
            pay = conn.execute(
                "SELECT * FROM payment_requests WHERE id=?", (pay_id,)
            ).fetchone()
        if not pay:
            await update.message.reply_text(f"❌ Payment #{pay_id} not found.")
            return
        db.resolve_payment(pay_id, "rejected", reason)
        await safe_send(
            ctx.bot, pay["user_id"],
            f"❌ *Payment Not Approved*\n\n"
            f"Reason: {reason}\n\n"
            f"Please contact support: {SUPPORT_GROUP}",
            parse_mode=ParseMode.MARKDOWN,
        )
        await update.message.reply_text(f"✅ Payment #{pay_id} rejected.")
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")


# ── /payments ─────────────────────────────────────────────────────────────────

async def cmd_payments(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    pending = db.get_pending_payments()
    if not pending:
        await update.message.reply_text("✅ No pending payments.")
        return
    text = "💰 *Pending Payments*\n\n"
    for p in pending[:15]:
        uname = f"@{p['username']}" if p["username"] else p["first_name"]
        text += (
            f"*#{p['id']}*  {uname} (`{p['user_id']}`)\n"
            f"Plan: `{p['plan']}`  •  UTR: `{p['utr'] or 'N/A'}`\n"
            f"Date: {str(p['created_at'])[:16]}\n"
            f"`/approve {p['id']}`  or  `/reject {p['id']}`\n\n"
        )
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)


# ── /reply <tid> <msg> ────────────────────────────────────────────────────────

async def cmd_reply(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    args = ctx.args or []
    if len(args) < 2:
        await update.message.reply_text("Usage: `/reply <ticket_id> <message>`",
                                        parse_mode=ParseMode.MARKDOWN)
        return
    try:
        tid       = int(args[0])
        reply_msg = " ".join(args[1:])
    except ValueError:
        await update.message.reply_text("❌ ticket_id must be a number.")
        return

    db.close_ticket(tid, reply_msg)
    with db.get_conn() as conn:
        ticket = conn.execute("SELECT * FROM tickets WHERE id=?", (tid,)).fetchone()
    if ticket:
        await safe_send(
            ctx.bot, ticket["user_id"],
            f"🎫 *Support Reply — Ticket #{tid}*\n\n{reply_msg}",
            parse_mode=ParseMode.MARKDOWN,
        )
        await update.message.reply_text(f"✅ Replied to ticket #{tid}.")
    else:
        await update.message.reply_text(f"❌ Ticket #{tid} not found.")


# ── Callbacks ─────────────────────────────────────────────────────────────────

async def on_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    # 🔒 Groups/channels me buttons ignore (sirf DM)
    if update.effective_chat and update.effective_chat.type != ChatType.PRIVATE:
        return
    q       = update.callback_query
    await q.answer()
    data    = q.data or ""
    user    = q.from_user
    uid     = user.id

    db.upsert_user(uid, user.username, user.first_name)

    # ── back ──────────────────────────────────────────────────────────────
    if data == "back_main":
        ctx.user_data.pop("awaiting", None)
        key_row = _ensure_key(uid)
        await safe_edit(
            q,
            f"👋 *{BOT_NAME}* — Main Menu\n\n"
            f"🔑 Key: `{key_row['key']}`\n"
            f"Plan: *{key_row['plan'].upper()}*",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=main_menu_kb(),
        )

    # ── view key ──────────────────────────────────────────────────────────
    elif data == "view_key":
        key_row = _ensure_key(uid)
        await safe_edit(q, _key_card(key_row, uid),
                        parse_mode=ParseMode.MARKDOWN, reply_markup=key_menu_kb())

    # ── renew ─────────────────────────────────────────────────────────────
    elif data == "renew_key":
        result = db.renew_key(uid)
        if not result:
            db.create_key(uid, "free")
        key_row = db.get_active_key(uid)
        await safe_edit(
            q,
            f"✅ *Key Renewed!*\n\n" + _key_card(key_row, uid),
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=key_menu_kb(),
        )
        log_event(f"🔄 <b>Key renew ki</b>\n👤 {_uname(user)} (<code>{uid}</code>)\n🕐 {_now_ist()}")

    # ── revoke ────────────────────────────────────────────────────────────
    elif data == "revoke_key":
        db.revoke_key(uid)
        key_row = db.get_active_key(uid)
        await safe_edit(
            q,
            f"🔁 *New key issued!*\n\n" + _key_card(key_row, uid),
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=key_menu_kb(),
        )
        log_event(f"🔁 <b>Key revoked + nayi key (button)</b>\n👤 {_uname(user)} (<code>{uid}</code>)\n🆕 New: <code>{(key_row['key'][:12] + '…') if key_row else '—'}</code>\n🕐 {_now_ist()}")

    # ── plans ─────────────────────────────────────────────────────────────
    elif data == "plans":
        await safe_edit(q, _plans_text(),
                        parse_mode=ParseMode.MARKDOWN, reply_markup=plans_kb())

    # ── buy plan (commercial flow) ────────────────────────────────────────
    elif data in ("buy_basic", "buy_pro", "buy_ultra"):
        plan = data.replace("buy_", "")
        ctx.user_data["buying_plan"] = plan
        ctx.user_data["awaiting"]    = f"utr_{plan}"
        await safe_edit(
            q,
            _payment_info(plan),
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(
                    "📲 Pay via UPI App",
                    url=_upi_deep_link(plan))],
                [InlineKeyboardButton(
                    "❌ Cancel", callback_data="back_main")],
            ]),
        )

    # ── upgrade request (free → sends request to admin) ───────────────────
    elif data == "upgrade_request":
        key_row      = db.get_active_key(uid)
        current_plan = key_row["plan"] if key_row else "free"
        uname        = f"@{user.username}" if user.username else user.first_name
        for admin_id in ADMIN_IDS:
            await safe_send(
                ctx.bot, admin_id,
                f"⬆️ *Upgrade Request*\n\n"
                f"User: {uname}\nID: `{uid}`\n"
                f"Current: `{current_plan}`\n\n"
                f"`/upgrade {uid} basic`\n"
                f"`/upgrade {uid} pro`\n"
                f"`/upgrade {uid} ultra`",
                parse_mode=ParseMode.MARKDOWN,
            )
        log_event(f"⬆️ <b>UPGRADE REQUEST</b>\n👤 {_uname(user)} (<code>{uid}</code>)\n💎 Current plan: <b>{current_plan.upper()}</b>\n🕐 {_now_ist()}")
        await safe_edit(
            q,
            f"⬆️ *Request Sent!*\n\n"
            f"Admin will contact you shortly.\n"
            f"Or tap *Plans & Pricing* to pay directly.\n\n"
            f"Support: {SUPPORT_GROUP}",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=back_kb(),
        )

    # ── usage ─────────────────────────────────────────────────────────────
    elif data == "my_usage":
        key_row  = _ensure_key(uid)
        stats    = db.get_usage_stats(uid)
        days     = db.days_remaining(uid)
        plan     = key_row["plan"]
        cfg      = db.PLANS.get(plan, db.PLANS["free"])
        rpd      = cfg["rpd"]
        lim      = "Unlimited" if rpd == -1 else f"{rpd:,}"
        rem      = "Unlimited" if rpd == -1 else str(max(0, rpd - stats["today"]))

        await safe_edit(
            q,
            f"📊 *Usage Stats*\n\n"
            f"Plan: *{plan.upper()}* — {cfg['price']}\n"
            f"Key: `{key_row['key'][:20]}...`\n"
            f"Expires in: *{days} days*\n\n"
            f"*Today:*\n"
            f"  Used: {stats['today']} / {lim}\n"
            f"  Remaining: {rem}\n"
            f"  🎵 Audio: {stats['today_audio']}  🎬 Video: {stats['today_video']}\n\n"
            f"*All-Time:*\n"
            f"  Total: {stats['total']}\n"
            f"  🎵 Audio: {stats['total_audio']}  🎬 Video: {stats['total_video']}\n\n"
            f"Last used: {stats['last_used']}",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=back_kb(),
        )

    # ── help ──────────────────────────────────────────────────────────────
    elif data == "help":
        await safe_edit(
            q,
            f"❓ *{BOT_NAME} — Quick Guide*\n\n"
            f"*1. Get your API key*\n"
            f"   Tap 🔑 My API Key — auto-generated, valid 28 days.\n\n"
            f"*2. Use the API*\n"
            f"   `GET {API_BASE_URL}/stream?url=YT_URL&api_key=YOUR_KEY`\n\n"
            f"*3. Endpoints*\n"
            f"   `/stream` — Universal audio stream\n"
            f"   `/song` — AnonXMusic/Yukki style\n"
            f"   `/api/stream` — Audio or video\n"
             f"   `url` must be a YouTube link or video ID (song-name search is off)\n"
            f"   `/info` — Video metadata\n\n"
            f"*4. Full Docs*\n"
            f"   {APP_URL}/docs\n\n"
            f"*5. Renew / Replace key*\n"
            f"   🔄 Renew extends by 30 days.\n"
            f"   🔁 Revoke issues a brand-new key.\n\n"
            f"*6. Upgrade*\n"
            f"   Tap 💎 Plans & Pricing for paid options.\n\n"
            f"⚠️ Never share your API key publicly.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=back_kb(),
        )

    # ── support ticket ────────────────────────────────────────────────────
    elif data == "support_ticket":
        ctx.user_data["awaiting"] = "ticket"
        await safe_edit(
            q,
            "🎫 *Support Ticket*\n\nDescribe your issue in detail:",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("❌ Cancel", callback_data="back_main")]
            ]),
        )

    # ── feedback ──────────────────────────────────────────────────────────
    elif data == "feedback":
        ctx.user_data["awaiting"] = "feedback"
        await safe_edit(
            q,
            "💬 *Feedback*\n\nShare your thoughts or suggestions:",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("❌ Cancel", callback_data="back_main")]
            ]),
        )

    # ── admin: stats ──────────────────────────────────────────────────────
    elif data == "admin_stats":
        if not is_admin(uid):
            await q.answer("❌ Access denied.", show_alert=True)
            return
        s  = db.get_admin_stats()
        pc = s["plan_counts"]
        await safe_edit(
            q,
            f"📊 *Admin Stats — {BOT_NAME}*\n\n"
            f"👥 Total Users     : `{s['total_users']}`\n"
            f"🔑 Active Keys     : `{s['active_keys']}`\n"
            f"📈 Today Requests  : `{s['today_requests']}`\n"
            f"📊 Total Requests  : `{s['total_requests']}`\n"
            f"💰 Pending Payments: `{s['pending_payments']}`\n"
            f"🎫 Open Tickets    : `{s['open_tickets']}`\n\n"
            f"*Plan Distribution:*\n"
            f"🆓 Free  : `{pc.get('free',0)}`\n"
            f"⚡ Basic : `{pc.get('basic',0)}`\n"
            f"🚀 Pro   : `{pc.get('pro',0)}`\n"
            f"👑 Ultra : `{pc.get('ultra',0)}`",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=admin_kb(),
        )

    # ── admin: pending payments ───────────────────────────────────────────
    elif data == "admin_payments":
        if not is_admin(uid):
            await q.answer("❌ Access denied.", show_alert=True)
            return
        pending = db.get_pending_payments()
        if not pending:
            await safe_edit(q, "✅ No pending payments.", reply_markup=admin_kb())
            return
        text = "💰 *Pending Payments*\n\n"
        for p in pending[:10]:
            uname = f"@{p['username']}" if p["username"] else p["first_name"]
            text += (
                f"*#{p['id']}*  {uname} (`{p['user_id']}`)\n"
                f"Plan: `{p['plan']}`  UTR: `{p['utr'] or 'N/A'}`\n"
                f"`/approve {p['id']}`  `/reject {p['id']}`\n\n"
            )
        await safe_edit(q, text, parse_mode=ParseMode.MARKDOWN, reply_markup=admin_kb())

    # ── admin: tickets ────────────────────────────────────────────────────
    elif data == "admin_tickets":
        if not is_admin(uid):
            await q.answer("❌ Access denied.", show_alert=True)
            return
        tickets = db.get_open_tickets()
        if not tickets:
            await safe_edit(q, "✅ No open tickets.", reply_markup=admin_kb())
            return
        text = "🎫 *Open Tickets*\n\n"
        for t in tickets[:10]:
            uname = t["username"] or t["first_name"] or str(t["user_id"])
            text += (
                f"*#{t['id']}*  @{uname}\n"
                f"`{str(t['message'])[:100]}`\n"
                f"`/reply {t['id']} <message>`\n\n"
            )
        await safe_edit(q, text, parse_mode=ParseMode.MARKDOWN, reply_markup=admin_kb())

    # ── admin: broadcast ──────────────────────────────────────────────────
    elif data == "admin_broadcast":
        if not is_admin(uid):
            await q.answer("❌ Access denied.", show_alert=True)
            return
        ctx.user_data["awaiting"] = "broadcast"
        await safe_edit(
            q,
            "📢 *Broadcast*\n\nType your message (supports Markdown):",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("❌ Cancel", callback_data="back_main")]
            ]),
        )

    # ── test stream ───────────────────────────────────────────────────────
    elif data in ("test_audio", "test_video"):
        media_type = "audio" if data == "test_audio" else "video"
        test_url   = ctx.user_data.get("test_url", "")
        if not test_url:
            await safe_edit(q, "❌ No URL. Send a YouTube link first.", reply_markup=back_kb())
            return

        key_row = db.get_active_key(uid)
        if not key_row:
            await safe_edit(q, "❌ No active key.", reply_markup=back_kb())
            return

        await safe_edit(q, f"⏳ Fetching {media_type} stream...")

        try:
            params  = {"url": test_url, "type": media_type}
            headers = {"x-api-key": key_row["key"]}
            t0      = time.time()
            async with httpx.AsyncClient(timeout=90.0) as client:
                resp = await client.get(f"{API_BASE_URL}/api/stream",
                                        params=params, headers=headers)
                resp.raise_for_status()
                result = resp.json()
            elapsed = round(time.time() - t0, 2)

            stream_url = result.get("stream_url") or result.get("direct_url", "")
            text = (
                f"✅ *Stream Ready* `({elapsed}s)`\n\n"
                f"*{result.get('title','Unknown')}*\n"
                f"👤 {result.get('uploader','')}\n"
                f"⏱ {fmt_dur(result.get('duration',0))}\n\n"
                f"📦 Format: `{result.get('ext','')}`\n"
                f"💎 Plan: `{result.get('plan','free')}`\n\n"
                f"🔗 Stream URL:\n`{stream_url[:180]}...`"
            )
            thumb = result.get("thumbnail", "")
            if thumb:
                try:
                    await q.message.reply_photo(photo=thumb, caption=text,
                                                parse_mode=ParseMode.MARKDOWN,
                                                reply_markup=back_kb())
                    await q.message.delete()
                    return
                except Exception:
                    pass
            await safe_edit(q, text, parse_mode=ParseMode.MARKDOWN, reply_markup=back_kb())

        except httpx.HTTPStatusError as e:
            err = str(e)
            try:
                d = e.response.json().get("detail", {})
                err = d.get("message", err) if isinstance(d, dict) else str(d)
            except Exception:
                pass
            await safe_edit(q, f"❌ *API Error*\n\n`{err[:300]}`",
                            parse_mode=ParseMode.MARKDOWN, reply_markup=back_kb())
        except httpx.TimeoutException:
            await safe_edit(q, "❌ *Timeout.* Extraction took too long. Try again.",
                            parse_mode=ParseMode.MARKDOWN, reply_markup=back_kb())
        except Exception as e:
            logger.error(f"test_stream: {e}", exc_info=True)
            await safe_edit(q, f"❌ Error: `{str(e)[:200]}`",
                            parse_mode=ParseMode.MARKDOWN, reply_markup=back_kb())


# ── Message handler ───────────────────────────────────────────────────────────

async def on_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return

    user     = update.effective_user
    uid      = user.id
    text     = (update.message.text or "").strip()
    awaiting = ctx.user_data.get("awaiting", "")

    db.upsert_user(uid, user.username, user.first_name)

    # ── Photo bheja (screenshot flow) — text ke bina bhi handle hoga ────
    if update.message.photo:
        if awaiting.startswith("utr_"):
            plan = awaiting.replace("utr_", "")
            ctx.user_data.pop("awaiting", None)
            cfg  = db.PLANS.get(plan, db.PLANS["basic"])
            pay_id = db.create_payment_request(uid, plan, utr="screenshot")
            uname  = f"@{user.username}" if user.username else user.first_name
            caption = (update.message.caption or "").strip()
            for admin_id in ADMIN_IDS:
                try:
                    await ctx.bot.send_photo(
                        admin_id, update.message.photo[-1].file_id,
                        caption=(
                            f"💰 *Payment Screenshot #{pay_id}*\n\n"
                            f"User: {uname} (`{uid}`)\n"
                            f"Plan: *{cfg['name']}* — {cfg['price']}\n"
                            f"Note: `{caption[:150] or '—'}`\n\n"
                            f"`/approve {pay_id}`  or  `/reject {pay_id} <reason>`"
                        ),
                        parse_mode=ParseMode.MARKDOWN,
                    )
                except Exception as e:
                    logger.warning(f"screenshot forward fail: {e}")
            await update.message.reply_text(
                f"✅ *Screenshot mil gaya!*\n\n"
                f"Plan: *{cfg['name']}*  |  Request `#{pay_id}`\n"
                f"Admin verify karega — plan jaldi active hoga.",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=back_kb(),
            )
            return
        # Silent — bina flow ke koi reply nahi (user request)
        return

    if not text:
        return

    # ── UTR / payment screenshot ──────────────────────────────────────────
    if awaiting and awaiting.startswith("utr_"):
        plan = awaiting.replace("utr_", "")
        cfg  = db.PLANS.get(plan, db.PLANS["basic"])
        # Duplicate check — isi user ka isi plan ka PENDING request already?
        dup = db.get_pending_payments()
        dup = next((p for p in dup
                    if p["user_id"] == uid and p["plan"] == plan), None)
        if dup:
            await update.message.reply_text(
                f"⏳ Tumhara *{cfg['name']}* request #{dup['id']} pehle se "
                f"pending hai. Admin verify kar raha hai — thoda ruko.",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=back_kb(),
            )
            return
        ctx.user_data.pop("awaiting", None)
        pay_id = db.create_payment_request(uid, plan, utr=text[:100])
        uname  = f"@{user.username}" if user.username else user.first_name
        log_event(f"💰 <b>NAYA PAYMENT REQUEST #{pay_id}</b>\n👤 {_uname(user)} (<code>{uid}</code>)\n💎 Plan: <b>{plan.upper()}</b>\n📝 UTR: <code>{_esc(text[:60])}</code>\n🕐 {_now_ist()}")

        for admin_id in ADMIN_IDS:
            await safe_send(
                ctx.bot, admin_id,
                f"💰 *New Payment Request #{pay_id}*\n\n"
                f"User: {uname} (`{uid}`)\n"
                f"Plan: *{cfg['name']}* — {cfg['price']}\n"
                f"UTR/Note: `{text[:200]}`\n\n"
                f"`/approve {pay_id}`  or  `/reject {pay_id} <reason>`",
                parse_mode=ParseMode.MARKDOWN,
            )
        await update.message.reply_text(
            f"✅ *Payment Request Submitted!*\n\n"
            f"Plan: *{cfg['name']}*\n"
            f"Reference ID: `#{pay_id}`\n\n"
            f"Admin verify karke plan activate kar dega.\n"
            f"For instant support: {SUPPORT_GROUP}",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=back_kb(),
        )
        return

    # ── Support ticket ────────────────────────────────────────────────────
    if awaiting == "ticket":
        ctx.user_data.pop("awaiting", None)
        tid   = db.create_ticket(uid, text)
        uname = f"@{user.username}" if user.username else user.first_name
        for admin_id in ADMIN_IDS:
            await safe_send(
                ctx.bot, admin_id,
                f"🎫 *Ticket #{tid}*\n\nFrom: {uname} (`{uid}`)\n\n`{text[:500]}`\n\n"
                f"`/reply {tid} <message>`",
                parse_mode=ParseMode.MARKDOWN,
            )
        await update.message.reply_text(
            f"✅ *Ticket #{tid} submitted!*\n\nWe'll reply here directly.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=back_kb(),
        )
        return

    # ── Feedback ──────────────────────────────────────────────────────────
    if awaiting == "feedback":
        ctx.user_data.pop("awaiting", None)
        db.save_feedback(uid, text)
        uname = f"@{user.username}" if user.username else user.first_name
        for admin_id in ADMIN_IDS:
            await safe_send(
                ctx.bot, admin_id,
                f"💬 *Feedback*\nFrom: {uname} (`{uid}`)\n\n`{text[:500]}`",
                parse_mode=ParseMode.MARKDOWN,
            )
        await update.message.reply_text(
            "💬 *Thank you for the feedback!*",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=back_kb(),
        )
        return

    # ── Admin broadcast ───────────────────────────────────────────────────
    if awaiting == "broadcast" and is_admin(uid):
        ctx.user_data.pop("awaiting", None)
        users  = db.get_all_users()
        sent = failed = 0
        for u in users:
            try:
                await ctx.bot.send_message(
                    u["user_id"], text, parse_mode=ParseMode.MARKDOWN
                )
                sent += 1
                await asyncio.sleep(0.04)
            except Exception:
                failed += 1
        await update.message.reply_text(
            f"📢 Broadcast done.\n✅ Sent: {sent}  ❌ Failed: {failed}",
            reply_markup=admin_kb(),
        )
        return

    # ── YouTube URL / ID only (song-name search is intentionally disabled) ──
    is_yt_url = "youtube.com" in text or "youtu.be" in text
    is_yt_id  = len(text) == 11 and text.replace("-", "").replace("_", "").isalnum()

    if is_yt_url or is_yt_id:
        return   # Silent — bina command ke koi reply nahi (user request)

    if len(text) >= 3 and not text.startswith("/"):
        return   # Silent — bina command ke koi reply nahi (user request)

    # ── Default: silent — bina command ke koi reply nahi (user request) ───
    return


# ── Bot commands menu ─────────────────────────────────────────────────────────

BOT_COMMANDS = [
    BotCommand("start",      "Start & get your API key"),
    BotCommand("mykey",      "View your current API key"),
    BotCommand("getfile",    "Download youtube.py for your music bot"),
    BotCommand("revokekey",  "🔒 Key leaked? Revoke & get new one instantly"),
    BotCommand("status",     "Check API server status"),
    BotCommand("usage",      "📊 Aapki API usage dekho"),
    BotCommand("help",       "How to use the API"),
]


# ── Key expiry reminder job ─────────────────────────────────────────────

REMINDER_CHECK_SEC   = int(os.getenv("REMINDER_CHECK_SEC", "3600"))   # 1 hour
REMINDER_BEFORE_DAYS = int(os.getenv("REMINDER_BEFORE_DAYS", "3"))

async def renewal_reminder_loop(app):
    """
    Har REMINDER_CHECK_SEC me: agle N din me expire hone wali active keys
    dhundo → user ko ek baar reminder bhejo (renewal_reminders table me
    (key, expires_at) mark hota hai — duplicate kabhi nahi jayega).
    """
    logger.info(f"Reminder job started: every {REMINDER_CHECK_SEC}s, "
                f"{REMINDER_BEFORE_DAYS}d before expiry")
    while True:
        try:
            due = db.get_keys_needing_renewal_reminder(REMINDER_BEFORE_DAYS)
            for r in due:
                uid       = r["user_id"]
                plan      = r["plan"]
                exp_str   = r["expires_at"]
                days_left = db.days_remaining(uid)
                cfg       = db.PLANS.get(plan, db.PLANS["free"])
                try:
                    await app.bot.send_message(
                        uid,
                        f"⏳ *Key Expiry Reminder*\n\n"
                        f"Tumhari *{cfg['name']}* API key *{days_left} din* me "
                        f"expire ho jayegi ({fmt_ist(exp_str)}).\n\n"
                        f"Renew karo to plan aur limits waise hi rahenge — "
                        f"naya key bhi same rahega:\n"
                        f"`{r['key']}`\n\n"
                        f"Key expire hone ke baad API requests fail hone lagengi.",
                        parse_mode=ParseMode.MARKDOWN,
                        reply_markup=InlineKeyboardMarkup([[
                            InlineKeyboardButton("🔄 Renew Now",
                                                 callback_data="renew_key"),
                        ]]),
                    )
                    db.mark_renewal_reminded(r["key"], exp_str)
                    logger.info(f"Renewal reminder sent: user={uid} "
                                f"plan={plan} expires={exp_str}")
                    await asyncio.sleep(0.5)   # Telegram flood-safe
                except Exception as e:
                    logger.warning(f"reminder send fail user={uid}: {e}")
                    # send fail hua to mark nahi kiya — agli baar retry hoga
        except Exception as e:
            logger.warning(f"reminder loop error: {e}")
        await asyncio.sleep(REMINDER_CHECK_SEC)


# ── Auto-archiver job — library songs ki MP3 channel me save ────────────

ARCHIVE_ENABLED    = os.getenv("ARCHIVE_ENABLED", "true").lower() in {"1", "true", "yes", "on"}
ARCHIVE_BATCH      = int(os.getenv("ARCHIVE_BATCH", "3"))
ARCHIVE_EVERY_SEC  = int(os.getenv("ARCHIVE_EVERY_SEC", "600"))

async def archive_loop(app):
    """
    Har ARCHIVE_EVERY_SEC me: library ke top songs me se jinki MP3 abhi
    store nahi hai — ARCHIVE_BATCH tak download-convert-upload karke channel
    me save karta hai. Isse database kabhi khatam nahi hota — har song ki
    file permanent backup ban jati hai. Slow & steady (flood-safe).
    """
    if not ARCHIVE_ENABLED:
        logger.info("Archive job disabled (ARCHIVE_ENABLED=false)")
        return
    logger.info(f"Archive job started: every {ARCHIVE_EVERY_SEC}s, batch={ARCHIVE_BATCH}")
    await asyncio.sleep(90)   # startup settle hone do
    while True:
        try:
            import tgstore
            if tgstore.is_enabled():
                top  = db.library_top(50)
                todo = [s for s in top
                        if not (db.tg_file_get(s["video_id"]) or (None,))[0]
                       ][:ARCHIVE_BATCH]
                if todo:
                    logger.info(f"Archiving {len(todo)} songs...")
                for s in todo:
                    try:
                        from ytdlp import get_stream_data_with_fallback
                        info, result = await get_stream_data_with_fallback(
                            f"https://www.youtube.com/watch?v={s['video_id']}",
                            "bestaudio/best")
                        durl = result.get("direct_url") or ""
                        if durl and not result.get("is_live"):
                            ok = await tgstore.tg_store_file(
                                s["video_id"],
                                info.get("title") or s.get("title", ""),
                                durl, result.get("ext") or "")
                            logger.info(f"archive {s['video_id']}: "
                                        f"{'OK' if ok else 'FAIL'}")
                        await asyncio.sleep(5)   # YouTube + TG dono flood-safe
                    except Exception as e:
                        logger.warning(f"archive {s['video_id']} fail: {e}")
        except Exception as e:
            logger.warning(f"archive loop error: {e}")
        await asyncio.sleep(ARCHIVE_EVERY_SEC)


# ── Runner ────────────────────────────────────────────────────────────────────

async def run_bot():
    global APP_REF
    logger.info(f"Starting {BOT_NAME} Bot | API: {API_BASE_URL}")

    app = Application.builder().token(BOT_TOKEN).build()
    APP_REF = app

    # 🔒 Sirf PRIVATE (DM) me reply — groups/channels me bilkul silent
    PRIVATE = filters.ChatType.PRIVATE
    app.add_handler(CommandHandler("start",      cmd_start,  filters=PRIVATE))
    app.add_handler(CommandHandler("mykey",      cmd_mykey,  filters=PRIVATE))
    app.add_handler(CommandHandler("getfile",    cmd_getfile, filters=PRIVATE))
    app.add_handler(CommandHandler("revokekey",  cmd_revokekey, filters=PRIVATE))
    app.add_handler(CommandHandler("revokeuser", cmd_revokeuser, filters=PRIVATE))
    app.add_handler(CommandHandler("status",     cmd_status, filters=PRIVATE))
    app.add_handler(CommandHandler("apiurl",     cmd_apiurl, filters=PRIVATE))
    app.add_handler(CommandHandler("usage",      cmd_usage,  filters=PRIVATE))
    app.add_handler(CommandHandler("help",     cmd_start,  filters=PRIVATE))
    app.add_handler(CommandHandler("admin",    cmd_admin,   filters=PRIVATE))
    app.add_handler(CommandHandler("upgrade",  cmd_upgrade, filters=PRIVATE))
    app.add_handler(CommandHandler("approve",  cmd_approve, filters=PRIVATE))
    app.add_handler(CommandHandler("reject",   cmd_reject,  filters=PRIVATE))
    app.add_handler(CommandHandler("payments", cmd_payments, filters=PRIVATE))
    app.add_handler(CommandHandler("reply",    cmd_reply,   filters=PRIVATE))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(
        PRIVATE & (filters.TEXT | filters.PHOTO) & ~filters.COMMAND, on_message))

    async with app:
        await app.bot.set_my_commands(BOT_COMMANDS)
        await app.initialize()
        await app.start()
        await app.updater.start_polling(
            drop_pending_updates=True,
            allowed_updates=["message", "callback_query"],
        )
        logger.info(f"{BOT_NAME} Bot running.")
        log_event(f"🚀 <b>{BOT_NAME} Bot Started</b>\n🌐 API: <code>{API_BASE_URL}</code>\n👑 Owner: <code>{OWNER_ID}</code>\n📢 Logger: <code>{LOGGER_ID}</code>\n🕐 {_now_ist()}")
        # yt-dlp auto-update + IP-watchdog daemon (API down ho to bhi bot active rahe)
        try:
            import autoupdate
            autoupdate.start_daemon()
        except Exception as e:
            logger.warning(f"autoupdate daemon start fail: {e}")
        reminder_task = asyncio.create_task(renewal_reminder_loop(app))
        archive_task  = asyncio.create_task(archive_loop(app))
        try:
            await asyncio.Event().wait()
        finally:
            reminder_task.cancel()
            archive_task.cancel()
        await app.updater.stop()
        await app.stop()
        await app.updater.stop()
        await app.stop()


def main():
    asyncio.run(run_bot())


if __name__ == "__main__":
    main()

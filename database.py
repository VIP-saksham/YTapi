"""
database.py
-----------
SQLite manager for HellAPI — commercial grade.

Tables:
  users            - registered Telegram users
  api_keys         - API keys with plan + expiry
  usage_logs       - per-request tracking (rate-limit source of truth)
  tickets          - support tickets
  feedback         - user feedback
  payment_requests - commercial plan purchase requests

Thread-safety: WAL journal + check_same_thread=False.
"""

import sqlite3
import os
import secrets
import string
import logging
from datetime import datetime, timedelta
from typing import Optional
from contextlib import contextmanager

logger = logging.getLogger(__name__)

DB_PATH           = "bot_data.db"
KEY_VALIDITY_DAYS = 30          # 30-day keys for paid plans, 28 for free


# ---------------------------------------------------------------------------
# Plans  — commercial pricing
# ---------------------------------------------------------------------------

PLANS = {
    "free": {
        "name":     "Free",
        "rpd":      500,
        "rpm":      5,
        "price":    "Free",
        "validity": 28,     # days
        "features": ["500 requests/day", "5 req/min", "Audio only"],
    },
    "basic": {
        "name":     "Basic",
        "rpd":      500,
        "rpm":      20,
        "price":    "₹99/month",
        "validity": 30,
        "features": ["500 requests/day", "20 req/min", "Audio + Video"],
    },
    "pro": {
        "name":     "Pro",
        "rpd":      5000,
        "rpm":      60,
        "price":    "₹299/month",
        "validity": 30,
        "features": ["5000 requests/day", "60 req/min", "Audio + Video", "Priority support"],
    },
    "ultra": {
        "name":     "Ultra",
        "rpd":      -1,
        "rpm":      200,
        "price":    "₹699/month",
        "validity": 30,
        "features": ["Unlimited requests/day", "200 req/min", "Audio + Video",
                     "Priority support", "Dedicated assistance"],
    },
}


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------

@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.execute("PRAGMA cache_size=-8000;")   # 8 MB page cache
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

def init_db():
    with get_conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                user_id     INTEGER PRIMARY KEY,
                username    TEXT    DEFAULT '',
                first_name  TEXT    DEFAULT '',
                joined_at   TEXT    DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS api_keys (
                key         TEXT    PRIMARY KEY,
                user_id     INTEGER NOT NULL,
                plan        TEXT    DEFAULT 'free',
                created_at  TEXT    DEFAULT (datetime('now')),
                expires_at  TEXT    NOT NULL,
                is_active   INTEGER DEFAULT 1,
                FOREIGN KEY (user_id) REFERENCES users(user_id)
            );

            CREATE TABLE IF NOT EXISTS usage_logs (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     INTEGER NOT NULL,
                api_key     TEXT    NOT NULL,
                endpoint    TEXT    NOT NULL,
                query       TEXT    DEFAULT '',
                media_type  TEXT    DEFAULT 'audio',
                timestamp   TEXT    DEFAULT (datetime('now')),
                success     INTEGER DEFAULT 1
            );

            CREATE TABLE IF NOT EXISTS tickets (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     INTEGER NOT NULL,
                message     TEXT    NOT NULL,
                status      TEXT    DEFAULT 'open',
                created_at  TEXT    DEFAULT (datetime('now')),
                reply       TEXT
            );

            CREATE TABLE IF NOT EXISTS feedback (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     INTEGER NOT NULL,
                message     TEXT    NOT NULL,
                created_at  TEXT    DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS payment_requests (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     INTEGER NOT NULL,
                plan        TEXT    NOT NULL,
                status      TEXT    DEFAULT 'pending',
                utr         TEXT    DEFAULT '',
                amount      TEXT    DEFAULT '',
                created_at  TEXT    DEFAULT (datetime('now')),
                resolved_at TEXT,
                note        TEXT    DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS key_audit_log (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     INTEGER NOT NULL,
                api_key     TEXT    NOT NULL,
                event       TEXT    NOT NULL,
                ip          TEXT    DEFAULT '',
                note        TEXT    DEFAULT '',
                timestamp   TEXT    DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS video_cache (
                video_id     TEXT    PRIMARY KEY,
                url          TEXT    NOT NULL,
                title        TEXT    DEFAULT '',
                uploader     TEXT    DEFAULT '',
                duration_sec INTEGER DEFAULT 0,
                thumbnail    TEXT    DEFAULT '',
                upload_date  TEXT    DEFAULT '',
                view_count   INTEGER DEFAULT 0,
                like_count   INTEGER DEFAULT 0,
                description  TEXT    DEFAULT '',
                is_live      INTEGER DEFAULT 0,
                cached_at    TEXT    DEFAULT (datetime('now')),
                hit_count    INTEGER DEFAULT 0
            );

            CREATE INDEX IF NOT EXISTS idx_usage_key_time  ON usage_logs    (api_key, timestamp);
            CREATE INDEX IF NOT EXISTS idx_usage_user_time ON usage_logs    (user_id, timestamp);
            CREATE INDEX IF NOT EXISTS idx_keys_user       ON api_keys      (user_id, is_active);
            CREATE INDEX IF NOT EXISTS idx_pay_user        ON payment_requests (user_id, status);
            CREATE INDEX IF NOT EXISTS idx_audit_key       ON key_audit_log (api_key, timestamp);
            CREATE INDEX IF NOT EXISTS idx_audit_ip        ON key_audit_log (ip, timestamp);

            CREATE TABLE IF NOT EXISTS search_cache (
                query_key  TEXT PRIMARY KEY,
                results    TEXT NOT NULL,
                cached_at  TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS tg_msg_index (
                video_id     TEXT PRIMARY KEY,
                message_id   INTEGER NOT NULL,
                expires_at   REAL    DEFAULT 0,
                fmt          TEXT    DEFAULT '',
                stored_at    TEXT    DEFAULT (datetime('now'))
            );
            CREATE INDEX IF NOT EXISTS idx_tgmsg_exp ON tg_msg_index (expires_at);

            CREATE TABLE IF NOT EXISTS song_library (
                video_id     TEXT PRIMARY KEY,
                title        TEXT    DEFAULT '',
                uploader     TEXT    DEFAULT '',
                duration_sec INTEGER DEFAULT 0,
                thumbnail    TEXT    DEFAULT '',
                play_count   INTEGER DEFAULT 0,
                first_played TEXT    DEFAULT (datetime('now')),
                last_played  TEXT    DEFAULT (datetime('now'))
            );
            CREATE INDEX IF NOT EXISTS idx_lib_plays ON song_library (play_count DESC);
            CREATE INDEX IF NOT EXISTS idx_lib_last  ON song_library (last_played DESC);

            CREATE TABLE IF NOT EXISTS renewal_reminders (
                key         TEXT NOT NULL,
                expires_at  TEXT NOT NULL,
                reminded_at TEXT DEFAULT (datetime('now')),
                PRIMARY KEY (key, expires_at)
            );

            CREATE TABLE IF NOT EXISTS tg_file_index (
                video_id    TEXT NOT NULL,
                message_id  INTEGER NOT NULL,
                file_id     TEXT    DEFAULT '',
                title       TEXT    DEFAULT '',
                size_mb     REAL    DEFAULT 0,
                kind        TEXT    DEFAULT 'audio',
                stored_at   TEXT    DEFAULT (datetime('now')),
                PRIMARY KEY (video_id, kind)
            );
        """)
        # video_cache me stream-URL columns (purane DB pe migration — idempotent)
        for _col in ("stream_url", "stream_expires_at", "stream_format",
                     "format_id", "ext", "resolution"):
            try:
                conn.execute(
                    f"ALTER TABLE video_cache ADD COLUMN {_col} TEXT DEFAULT ''")
            except sqlite3.OperationalError:
                pass   # column already exists
        # tg_file_index migration: audio + video dono store ho sakein
        try:
            _cols = [r[1] for r in conn.execute(
                "PRAGMA table_info(tg_file_index)").fetchall()]
            if _cols and "kind" not in _cols:
                conn.execute("ALTER TABLE tg_file_index RENAME TO tg_file_index_old")
                conn.execute("""
                    CREATE TABLE tg_file_index (
                        video_id    TEXT NOT NULL,
                        message_id  INTEGER NOT NULL,
                        file_id     TEXT    DEFAULT '',
                        title       TEXT    DEFAULT '',
                        size_mb     REAL    DEFAULT 0,
                        kind        TEXT    DEFAULT 'audio',
                        stored_at   TEXT    DEFAULT (datetime('now')),
                        PRIMARY KEY (video_id, kind)
                    );
                """)
                conn.execute("""
                    INSERT OR REPLACE INTO tg_file_index
                        (video_id, message_id, file_id, title, size_mb, kind, stored_at)
                    SELECT video_id, message_id, file_id, title, size_mb,
                           'audio', stored_at
                    FROM tg_file_index_old
                """)
                conn.execute("DROP TABLE tg_file_index_old")
                logger.info("tg_file_index migrated: (video_id, kind) PK")
        except sqlite3.OperationalError as _e:
            logger.warning(f"tg_file_index migration skip: {_e}")
    logger.info("DB init OK.")


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------

def upsert_user(user_id: int, username: str, first_name: str):
    with get_conn() as conn:
        conn.execute("""
            INSERT INTO users (user_id, username, first_name)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                username   = excluded.username,
                first_name = excluded.first_name
        """, (user_id, username or "", first_name or ""))


def get_user(user_id: int) -> Optional[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM users WHERE user_id = ?", (user_id,)
        ).fetchone()


def get_all_users() -> list:
    with get_conn() as conn:
        return conn.execute("SELECT * FROM users ORDER BY joined_at DESC").fetchall()


def count_users() -> int:
    with get_conn() as conn:
        return conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"]


# ---------------------------------------------------------------------------
# API Keys
# ---------------------------------------------------------------------------

def _gen_key() -> str:
    chars = string.ascii_letters + string.digits
    return "HellAPI" + "".join(secrets.choice(chars) for _ in range(32))


def _validity_days(plan: str) -> int:
    return PLANS.get(plan, PLANS["free"])["validity"]


def get_active_key(user_id: int) -> Optional[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute("""
            SELECT * FROM api_keys
            WHERE user_id = ?
              AND is_active = 1
              AND datetime(expires_at) > datetime('now')
            ORDER BY created_at DESC LIMIT 1
        """, (user_id,)).fetchone()


def create_key(user_id: int, plan: str = "free") -> str:
    if plan not in PLANS:
        plan = "free"
    validity   = _validity_days(plan)
    key        = _gen_key()
    expires_at = (datetime.utcnow() + timedelta(days=validity)).strftime("%Y-%m-%d %H:%M:%S")
    with get_conn() as conn:
        conn.execute("UPDATE api_keys SET is_active = 0 WHERE user_id = ?", (user_id,))
        conn.execute("""
            INSERT INTO api_keys (key, user_id, plan, expires_at)
            VALUES (?, ?, ?, ?)
        """, (key, user_id, plan, expires_at))
    return key


def renew_key(user_id: int) -> Optional[str]:
    row = get_active_key(user_id)
    if not row:
        return None
    validity    = _validity_days(row["plan"])
    new_expires = (datetime.utcnow() + timedelta(days=validity)).strftime("%Y-%m-%d %H:%M:%S")
    with get_conn() as conn:
        conn.execute("UPDATE api_keys SET expires_at = ? WHERE key = ?",
                     (new_expires, row["key"]))
    return row["key"]


def revoke_key(user_id: int) -> str:
    row  = get_active_key(user_id)
    plan = row["plan"] if row else "free"
    with get_conn() as conn:
        conn.execute("UPDATE api_keys SET is_active = 0 WHERE user_id = ?", (user_id,))
    return create_key(user_id, plan)


def upgrade_plan(user_id: int, new_plan: str) -> str:
    if new_plan not in PLANS:
        raise ValueError(f"Invalid plan '{new_plan}'. Valid: {', '.join(PLANS)}")
    with get_conn() as conn:
        conn.execute("UPDATE api_keys SET is_active = 0 WHERE user_id = ?", (user_id,))
    return create_key(user_id, new_plan)


def validate_key(api_key: str) -> Optional[sqlite3.Row]:
    if not api_key:
        return None
    with get_conn() as conn:
        return conn.execute("""
            SELECT * FROM api_keys
            WHERE key = ?
              AND is_active = 1
              AND datetime(expires_at) > datetime('now')
        """, (api_key,)).fetchone()


def days_remaining(user_id: int) -> int:
    row = get_active_key(user_id)
    if not row:
        return 0
    try:
        exp   = datetime.strptime(row["expires_at"], "%Y-%m-%d %H:%M:%S")
        delta = exp - datetime.utcnow()
        return max(0, delta.days)
    except Exception:
        return 0


def days_remaining_by_key(api_key: str) -> int:
    row = validate_key(api_key)
    if not row:
        return 0
    try:
        exp   = datetime.strptime(row["expires_at"], "%Y-%m-%d %H:%M:%S")
        delta = exp - datetime.utcnow()
        return max(0, delta.days)
    except Exception:
        return 0


def get_expiring_keys(within_days: int = 3) -> list:
    """Return all active keys expiring within N days (for warning notifications)."""
    cutoff = (datetime.utcnow() + timedelta(days=within_days)).strftime("%Y-%m-%d %H:%M:%S")
    with get_conn() as conn:
        return conn.execute("""
            SELECT k.*, u.first_name, u.username
            FROM api_keys k
            JOIN users u ON k.user_id = u.user_id
            WHERE k.is_active = 1
              AND datetime(k.expires_at) > datetime('now')
              AND datetime(k.expires_at) <= datetime(?)
        """, (cutoff,)).fetchall()


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------

def check_rate_limit(api_key: str) -> dict:
    key_row = validate_key(api_key)
    if not key_row:
        return {"allowed": False, "reason": "invalid_key", "used": 0, "limit": 0, "plan": "free"}

    plan     = key_row["plan"]
    plan_cfg = PLANS.get(plan, PLANS["free"])
    rpd      = plan_cfg["rpd"]
    rpm      = plan_cfg["rpm"]

    with get_conn() as conn:
        used_today = conn.execute("""
            SELECT COUNT(*) AS cnt FROM usage_logs
            WHERE api_key = ? AND date(timestamp) = date('now') AND success = 1
        """, (api_key,)).fetchone()["cnt"]

        used_min = conn.execute("""
            SELECT COUNT(*) AS cnt FROM usage_logs
            WHERE api_key = ?
              AND timestamp >= datetime('now', '-1 minute')
              AND success = 1
        """, (api_key,)).fetchone()["cnt"]

    if rpm != -1 and used_min >= rpm:
        return {"allowed": False, "reason": "rpm_exceeded",
                "used": used_today, "limit": rpd, "plan": plan}

    if rpd != -1 and used_today >= rpd:
        return {"allowed": False, "reason": "rpd_exceeded",
                "used": used_today, "limit": rpd, "plan": plan}

    return {"allowed": True, "used": used_today, "limit": rpd, "plan": plan}


# ---------------------------------------------------------------------------
# Usage logging
# ---------------------------------------------------------------------------

def log_usage(user_id: int, api_key: str, endpoint: str, query: str,
              success: bool = True, media_type: str = "audio"):
    try:
        with get_conn() as conn:
            conn.execute("""
                INSERT INTO usage_logs (user_id, api_key, endpoint, query, success, media_type)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (user_id, api_key, endpoint, query or "", int(success), media_type))
    except Exception as e:
        logger.warning(f"log_usage: {e}")


def get_usage_stats(user_id: int) -> dict:
    with get_conn() as conn:
        def _c(sql, p=()):
            return conn.execute(sql, p).fetchone()["cnt"]

        total       = _c("SELECT COUNT(*) AS cnt FROM usage_logs WHERE user_id=?", (user_id,))
        today       = _c("SELECT COUNT(*) AS cnt FROM usage_logs WHERE user_id=? AND date(timestamp)=date('now')", (user_id,))
        today_audio = _c("SELECT COUNT(*) AS cnt FROM usage_logs WHERE user_id=? AND date(timestamp)=date('now') AND media_type='audio'", (user_id,))
        today_video = _c("SELECT COUNT(*) AS cnt FROM usage_logs WHERE user_id=? AND date(timestamp)=date('now') AND media_type='video'", (user_id,))
        total_audio = _c("SELECT COUNT(*) AS cnt FROM usage_logs WHERE user_id=? AND media_type='audio'", (user_id,))
        total_video = _c("SELECT COUNT(*) AS cnt FROM usage_logs WHERE user_id=? AND media_type='video'", (user_id,))
        last        = conn.execute("SELECT timestamp FROM usage_logs WHERE user_id=? ORDER BY timestamp DESC LIMIT 1", (user_id,)).fetchone()

    return {
        "total": total, "today": today,
        "today_audio": today_audio, "today_video": today_video,
        "total_audio": total_audio, "total_video": total_video,
        "last_used": last["timestamp"] if last else "Never",
    }


def get_admin_stats() -> dict:
    with get_conn() as conn:
        total_users    = conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"]
        active_keys    = conn.execute("SELECT COUNT(*) AS c FROM api_keys WHERE is_active=1 AND datetime(expires_at)>datetime('now')").fetchone()["c"]
        total_requests = conn.execute("SELECT COUNT(*) AS c FROM usage_logs").fetchone()["c"]
        today_requests = conn.execute("SELECT COUNT(*) AS c FROM usage_logs WHERE date(timestamp)=date('now')").fetchone()["c"]
        open_tickets   = conn.execute("SELECT COUNT(*) AS c FROM tickets WHERE status='open'").fetchone()["c"]
        pending_pay    = conn.execute("SELECT COUNT(*) AS c FROM payment_requests WHERE status='pending'").fetchone()["c"]

        plan_counts = {p: conn.execute("""
            SELECT COUNT(*) AS c FROM api_keys
            WHERE plan=? AND is_active=1 AND datetime(expires_at)>datetime('now')
        """, (p,)).fetchone()["c"] for p in PLANS}

    return {
        "total_users":    total_users,
        "active_keys":    active_keys,
        "total_requests": total_requests,
        "today_requests": today_requests,
        "open_tickets":   open_tickets,
        "pending_payments": pending_pay,
        "plan_counts":    plan_counts,
    }


# ---------------------------------------------------------------------------
# Tickets
# ---------------------------------------------------------------------------

def create_ticket(user_id: int, message: str) -> int:
    with get_conn() as conn:
        return conn.execute(
            "INSERT INTO tickets (user_id, message) VALUES (?, ?)", (user_id, message)
        ).lastrowid


def get_open_tickets() -> list:
    with get_conn() as conn:
        return conn.execute("""
            SELECT t.*, u.first_name, u.username
            FROM tickets t JOIN users u ON t.user_id = u.user_id
            WHERE t.status = 'open' ORDER BY t.created_at ASC
        """).fetchall()


def close_ticket(ticket_id: int, reply: str):
    with get_conn() as conn:
        conn.execute("UPDATE tickets SET status='closed', reply=? WHERE id=?",
                     (reply, ticket_id))


def get_user_tickets(user_id: int) -> list:
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM tickets WHERE user_id=? ORDER BY created_at DESC LIMIT 5",
            (user_id,)
        ).fetchall()


# ---------------------------------------------------------------------------
# Feedback
# ---------------------------------------------------------------------------

def save_feedback(user_id: int, message: str):
    with get_conn() as conn:
        conn.execute("INSERT INTO feedback (user_id, message) VALUES (?, ?)",
                     (user_id, message))


# ---------------------------------------------------------------------------
# Payment requests
# ---------------------------------------------------------------------------

def create_payment_request(user_id: int, plan: str, utr: str = "", amount: str = "") -> int:
    with get_conn() as conn:
        return conn.execute("""
            INSERT INTO payment_requests (user_id, plan, utr, amount)
            VALUES (?, ?, ?, ?)
        """, (user_id, plan, utr, amount)).lastrowid


def get_pending_payments() -> list:
    with get_conn() as conn:
        return conn.execute("""
            SELECT p.*, u.first_name, u.username
            FROM payment_requests p JOIN users u ON p.user_id = u.user_id
            WHERE p.status = 'pending'
            ORDER BY p.created_at ASC
        """).fetchall()


def resolve_payment(payment_id: int, status: str = "approved", note: str = ""):
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    with get_conn() as conn:
        conn.execute("""
            UPDATE payment_requests
            SET status=?, resolved_at=?, note=?
            WHERE id=?
        """, (status, now, note, payment_id))


def get_user_payments(user_id: int) -> list:
    with get_conn() as conn:
        return conn.execute("""
            SELECT * FROM payment_requests
            WHERE user_id=? ORDER BY created_at DESC LIMIT 5
        """, (user_id,)).fetchall()


# ---------------------------------------------------------------------------
# Auto init
# ---------------------------------------------------------------------------
init_db()


# ---------------------------------------------------------------------------
# Key audit log
# ---------------------------------------------------------------------------

def log_key_audit(user_id: int, api_key: str, event: str,
                  ip: str = "", note: str = ""):
    """
    Log key lifecycle events:
      events: created | user_revoke | admin_revoke | expired | invalid_attempt
    """
    try:
        with get_conn() as conn:
            conn.execute("""
                INSERT INTO key_audit_log (user_id, api_key, event, ip, note)
                VALUES (?, ?, ?, ?, ?)
            """, (user_id, api_key, event, ip[:100], note[:300]))
    except Exception as e:
        logger.warning(f"log_key_audit failed: {e}")


def get_key_audit(user_id: int, limit: int = 20) -> list:
    """Last N audit events for a user."""
    with get_conn() as conn:
        return conn.execute("""
            SELECT * FROM key_audit_log
            WHERE user_id = ?
            ORDER BY timestamp DESC LIMIT ?
        """, (user_id, limit)).fetchall()


# ---------------------------------------------------------------------------
# Brute-force tracking (in-memory — fast, no DB hit per request)
# ---------------------------------------------------------------------------

import threading
import time as _time
from collections import defaultdict

_bf_lock    = threading.Lock()
# ip → {"count": int, "window_start": float}
_bf_tracker: dict = defaultdict(lambda: {"count": 0, "window_start": 0.0})

BF_MAX_ATTEMPTS = int(os.getenv("BF_MAX_ATTEMPTS", "10"))   # fails before block
BF_WINDOW_SEC   = int(os.getenv("BF_WINDOW_SEC",   "60"))   # rolling window
BF_BLOCK_SEC    = int(os.getenv("BF_BLOCK_SEC",    "300"))  # block duration (5 min)

# ip → block_until timestamp
_bf_blocked: dict = {}


def is_ip_blocked(ip: str) -> bool:
    """True if IP is currently in block period."""
    with _bf_lock:
        until = _bf_blocked.get(ip, 0)
        if _time.time() < until:
            return True
        if until:                           # block expired — clean up
            del _bf_blocked[ip]
            _bf_tracker.pop(ip, None)
        return False


def record_invalid_attempt(ip: str) -> dict:
    """
    Record one failed auth attempt for this IP.
    Returns {"blocked": bool, "attempts": int, "block_until": float|None}
    """
    now = _time.time()
    with _bf_lock:
        rec = _bf_tracker[ip]
        # Reset window if it has expired
        if now - rec["window_start"] > BF_WINDOW_SEC:
            rec["count"]        = 0
            rec["window_start"] = now
        rec["count"] += 1

        if rec["count"] >= BF_MAX_ATTEMPTS:
            block_until          = now + BF_BLOCK_SEC
            _bf_blocked[ip]      = block_until
            _bf_tracker.pop(ip, None)   # reset counter after blocking
            return {"blocked": True, "attempts": rec["count"],
                    "block_until": block_until}

    return {"blocked": False, "attempts": rec["count"], "block_until": None}


def reset_attempts(ip: str):
    """Call after a successful auth to clear failed counter."""
    with _bf_lock:
        _bf_tracker.pop(ip, None)
        _bf_blocked.pop(ip, None)


def get_bf_stats() -> dict:
    """Admin: current brute-force state."""
    now = _time.time()
    with _bf_lock:
        active_blocks = {
            ip: round(until - now, 1)
            for ip, until in _bf_blocked.items()
            if until > now
        }
        pending = {
            ip: rec["count"]
            for ip, rec in _bf_tracker.items()
        }
    return {"active_blocks": active_blocks, "pending_attempts": pending}


# ---------------------------------------------------------------------------
# Video metadata cache (persistent DB)
# ---------------------------------------------------------------------------
# Stream URLs expire quickly (1-6 hrs) — we only cache metadata.
# Stream URL is always fetched fresh from yt-dlp.
# ---------------------------------------------------------------------------

import hashlib

# How long to keep metadata in DB (default 7 days)
VIDEO_CACHE_TTL_DAYS = int(os.getenv("VIDEO_CACHE_TTL_DAYS", "7"))


def _url_to_video_id(url: str) -> str:
    """Extract YouTube video ID from URL, or hash the string if not a URL."""
    # Standard YouTube URL
    if "v=" in url:
        return url.split("v=")[-1].split("&")[0].strip()
    # Short URL youtu.be/VIDEO_ID
    if "youtu.be/" in url:
        return url.split("youtu.be/")[-1].split("?")[0].strip()
    # If already an 11-char ID
    if len(url) == 11 and url.replace("-", "").replace("_", "").isalnum():
        return url
    # For search queries — use MD5 hash as key
    return "q_" + hashlib.md5(url.lower().strip().encode()).hexdigest()[:16]


def cache_video_metadata(url: str, info: dict):
    """
    Save video metadata to DB.
    Called after every successful yt-dlp extraction.
    info dict = what get_video_info() returns.
    """
    video_id = _url_to_video_id(url)
    try:
        with get_conn() as conn:
            conn.execute("""
                INSERT INTO video_cache
                    (video_id, url, title, uploader, duration_sec,
                     thumbnail, upload_date, view_count, like_count,
                     description, is_live, cached_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
                ON CONFLICT(video_id) DO UPDATE SET
                    title        = excluded.title,
                    uploader     = excluded.uploader,
                    duration_sec = excluded.duration_sec,
                    thumbnail    = excluded.thumbnail,
                    upload_date  = excluded.upload_date,
                    view_count   = excluded.view_count,
                    like_count   = excluded.like_count,
                    description  = excluded.description,
                    is_live      = excluded.is_live,
                    cached_at    = datetime('now')
            """, (
                video_id,
                url,
                info.get("title", ""),
                info.get("uploader", ""),
                int(info.get("duration_seconds") or 0),
                info.get("thumbnail", ""),
                info.get("upload_date", ""),
                int(info.get("view_count") or 0),
                int(info.get("like_count") or 0),
                (info.get("description") or "")[:500],
                int(info.get("is_live", False)),
            ))
    except Exception as e:
        logger.warning(f"cache_video_metadata failed: {e}")


def get_cached_video_metadata(url: str) -> Optional[dict]:
    """
    Get video metadata from DB cache.
    Returns dict (same format as get_video_info) or None if not cached / expired.
    """
    video_id = _url_to_video_id(url)
    try:
        with get_conn() as conn:
            row = conn.execute("""
                SELECT * FROM video_cache
                WHERE video_id = ?
                  AND datetime(cached_at) > datetime('now', ?)
            """, (video_id, f"-{VIDEO_CACHE_TTL_DAYS} days")).fetchone()

            if not row:
                return None

            # Increment hit counter
            conn.execute(
                "UPDATE video_cache SET hit_count = hit_count + 1 WHERE video_id = ?",
                (video_id,)
            )

        return {
            "id":               row["video_id"],
            "title":            row["title"],
            "uploader":         row["uploader"],
            "duration_seconds": row["duration_sec"],
            "thumbnail":        row["thumbnail"],
            "upload_date":      row["upload_date"],
            "view_count":       row["view_count"],
            "like_count":       row["like_count"],
            "description":      row["description"],
            "is_live":          bool(row["is_live"]),
            "webpage_url":      row["url"],
            "channel_url":      None,
            "tags":             [],
            "_from_db_cache":   True,   # helpful for debugging
        }
    except Exception as e:
        logger.warning(f"get_cached_video_metadata failed: {e}")
        return None


# ---------------------------------------------------------------------------
# Song Library — CENTRAL DATABASE (har user ka search/play yahan save hota hai)
# Jo bhi API user search ya play kare, song + play_count permanent yahan.
# TG channel me stream URL hota hai; yahan permanent metadata + stats.
# ---------------------------------------------------------------------------

def library_put(video_id: str, title: str, uploader: str = "",
                duration_sec: int = 0, thumbnail: str = "",
                source: str = "play"):
    """Song library me add/update — play_count auto-increment."""
    if not video_id:
        return
    try:
        with get_conn() as conn:
            conn.execute("""
                INSERT INTO song_library
                    (video_id, title, uploader, duration_sec, thumbnail,
                     play_count, first_played, last_played)
                VALUES (?, ?, ?, ?, ?, 1, datetime('now'), datetime('now'))
                ON CONFLICT(video_id) DO UPDATE SET
                    play_count  = play_count + 1,
                    title       = CASE WHEN excluded.title != ''
                                       THEN excluded.title ELSE title END,
                    uploader    = CASE WHEN excluded.uploader != ''
                                       THEN excluded.uploader ELSE uploader END,
                    thumbnail   = CASE WHEN excluded.thumbnail != ''
                                       THEN excluded.thumbnail ELSE thumbnail END,
                    last_played = datetime('now')
            """, (video_id, title or "", uploader or "",
                  int(duration_sec or 0), thumbnail or ""))
    except Exception as e:
        logger.warning(f"library_put failed: {e}")


def library_top(limit: int = 10) -> list:
    """Sabse zyada played songs (sab users mila ke)."""
    try:
        with get_conn() as conn:
            rows = conn.execute("""
                SELECT video_id, title, uploader, duration_sec, thumbnail,
                       play_count, first_played, last_played
                FROM song_library ORDER BY play_count DESC, last_played DESC
                LIMIT ?
            """, (int(limit),)).fetchall()
        return [dict(r) for r in rows]
    except Exception as e:
        logger.warning(f"library_top failed: {e}")
        return []


def library_recent(limit: int = 10) -> list:
    """Abhi-abhi kya play hua."""
    try:
        with get_conn() as conn:
            rows = conn.execute("""
                SELECT video_id, title, uploader, duration_sec, thumbnail,
                       play_count, last_played
                FROM song_library ORDER BY last_played DESC LIMIT ?
            """, (int(limit),)).fetchall()
        return [dict(r) for r in rows]
    except Exception as e:
        logger.warning(f"library_recent failed: {e}")
        return []


def library_search(query: str, limit: int = 10) -> list:
    """Library me naam se dhundo (title/uploader LIKE)."""
    q = f"%{query.strip()}%"
    try:
        with get_conn() as conn:
            rows = conn.execute("""
                SELECT video_id, title, uploader, duration_sec, thumbnail,
                       play_count, last_played
                FROM song_library
                WHERE title LIKE ? OR uploader LIKE ?
                ORDER BY play_count DESC LIMIT ?
            """, (q, q, int(limit))).fetchall()
        return [dict(r) for r in rows]
    except Exception as e:
        logger.warning(f"library_search failed: {e}")
        return []


def library_get(video_id: str) -> Optional[dict]:
    """Ek song ka pura record."""
    try:
        with get_conn() as conn:
            row = conn.execute(
                "SELECT * FROM song_library WHERE video_id = ?", (video_id,)
            ).fetchone()
        return dict(row) if row else None
    except Exception as e:
        logger.warning(f"library_get failed: {e}")
        return None


def library_stats() -> dict:
    try:
        with get_conn() as conn:
            total   = conn.execute(
                "SELECT COUNT(*) AS c FROM song_library").fetchone()["c"]
            plays   = conn.execute(
                "SELECT SUM(play_count) AS s FROM song_library").fetchone()["s"] or 0
            today   = conn.execute("""
                SELECT COUNT(*) AS c FROM song_library
                WHERE date(last_played) = date('now')
            """).fetchone()["c"]
            unique7 = conn.execute("""
                SELECT COUNT(*) AS c FROM song_library
                WHERE last_played > datetime('now', '-7 days')
            """).fetchone()["c"]
        return {"total_songs": total, "total_plays": plays,
                "played_today": today, "active_7days": unique7}
    except Exception as e:
        return {"error": str(e)[:80]}


def library_delete(video_id: str):
    try:
        with get_conn() as conn:
            conn.execute("DELETE FROM song_library WHERE video_id = ?", (video_id,))
    except Exception as e:
        logger.warning(f"library_delete failed: {e}")


# ---------------------------------------------------------------------------
# Telegram-channel-as-database index
# Channel me asli data rehta hai (messages); DB me sirf msg_id ka index.
# VPS khatam → naya VPS → recover_from_channel() → pura cache wapas.
# ---------------------------------------------------------------------------

def tg_index_put(video_id: str, message_id: int, expires_at: float, fmt: str = ""):
    try:
        with get_conn() as conn:
            conn.execute("""
                INSERT INTO tg_msg_index (video_id, message_id, expires_at, fmt)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(video_id) DO UPDATE SET
                    message_id = excluded.message_id,
                    expires_at = excluded.expires_at,
                    fmt        = excluded.fmt,
                    stored_at  = datetime('now')
            """, (video_id, int(message_id), float(expires_at), fmt or ""))
    except Exception as e:
        logger.warning(f"tg_index_put failed: {e}")


def tg_index_get(video_id: str):
    """Returns (message_id, expires_at) ya None."""
    try:
        with get_conn() as conn:
            row = conn.execute(
                "SELECT message_id, expires_at FROM tg_msg_index WHERE video_id = ?",
                (video_id,)
            ).fetchone()
        return (row[0], row[1]) if row else None
    except Exception as e:
        logger.warning(f"tg_index_get failed: {e}")
        return None


def tg_index_del(video_id: str):
    try:
        with get_conn() as conn:
            conn.execute("DELETE FROM tg_msg_index WHERE video_id = ?", (video_id,))
    except Exception as e:
        logger.warning(f"tg_index_del failed: {e}")


def tg_index_all() -> list:
    """Recovery/status ke liye — sirf live (non-expired) entries."""
    try:
        with get_conn() as conn:
            rows = conn.execute("""
                SELECT video_id, message_id, expires_at FROM tg_msg_index
                WHERE expires_at > ?
                ORDER BY stored_at DESC
            """, (_time.time(),)).fetchall()
        return [(r[0], r[1], r[2]) for r in rows]
    except Exception as e:
        logger.warning(f"tg_index_all failed: {e}")
        return []


def tg_stats() -> dict:
    try:
        with get_conn() as conn:
            total = conn.execute("SELECT COUNT(*) AS c FROM tg_msg_index").fetchone()["c"]
            live  = conn.execute(
                "SELECT COUNT(*) AS c FROM tg_msg_index WHERE expires_at > ?",
                (_time.time(),)
            ).fetchone()["c"]
        return {"total_indexed": total, "live_entries": live}
    except Exception as e:
        return {"error": str(e)[:80]}


# ---------------------------------------------------------------------------
# Key renewal reminders — expiry se N din pehle user ko Telegram reminder
# Ek (key, expires_at) pair pe sirf EK baar reminder — renew ke baad naya
# expiry aata hai, to agle expiry pe phir reminder ja sakta hai.
# ---------------------------------------------------------------------------

def get_keys_needing_renewal_reminder(days: int = 3) -> list:
    """Active keys jo agle `days` din me expire hongi + reminder abhi nahi gaya."""
    try:
        with get_conn() as conn:
            rows = conn.execute("""
                SELECT k.user_id, k.key, k.plan, k.expires_at
                FROM api_keys k
                WHERE k.is_active = 1
                  AND datetime(k.expires_at) > datetime('now')
                  AND datetime(k.expires_at) <= datetime('now', ?)
                  AND NOT EXISTS (
                      SELECT 1 FROM renewal_reminders r
                      WHERE r.key = k.key AND r.expires_at = k.expires_at
                  )
            """, (f"+{int(days)} days",)).fetchall()
        return [dict(r) for r in rows]
    except Exception as e:
        logger.warning(f"get_keys_needing_renewal_reminder: {e}")
        return []


def mark_renewal_reminded(key: str, expires_at: str):
    try:
        with get_conn() as conn:
            conn.execute("""
                INSERT OR IGNORE INTO renewal_reminders (key, expires_at)
                VALUES (?, ?)
            """, (key, expires_at))
    except Exception as e:
        logger.warning(f"mark_renewal_reminded: {e}")


# ---------------------------------------------------------------------------
# TG file index — MP3/MP4 files channel me store hote hain, msg_id + file_id
# yahan. File kabhi expire nahi hoti (stream URL se different).
# ---------------------------------------------------------------------------

def tg_file_put(video_id: str, message_id: int, file_id: str,
                title: str = "", size_mb: float = 0.0,
                kind: str = "audio"):
    try:
        with get_conn() as conn:
            cols = [r[1] for r in conn.execute(
                "PRAGMA table_info(tg_file_index)").fetchall()]
            if "kind" not in cols:
                conn.execute("ALTER TABLE tg_file_index ADD COLUMN kind TEXT DEFAULT 'audio'")
            conn.execute("""
                INSERT INTO tg_file_index (video_id, message_id, file_id, title, size_mb, kind)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(video_id, kind) DO UPDATE SET
                    message_id = excluded.message_id,
                    file_id    = excluded.file_id,
                    title      = excluded.title,
                    size_mb    = excluded.size_mb,
                    stored_at  = datetime('now')
            """, (video_id, int(message_id), file_id or "",
                  title or "", float(size_mb or 0), kind or "audio"))
    except Exception as e:
        logger.warning(f"tg_file_put failed: {e}")


def tg_file_get(video_id: str, kind: str = "audio"):
    """Returns (file_id, title, size_mb) ya None. kind='video' bhi supported."""
    try:
        with get_conn() as conn:
            cols = [r[1] for r in conn.execute(
                "PRAGMA table_info(tg_file_index)").fetchall()]
            if "kind" not in cols:
                return None
            row = conn.execute(
                "SELECT file_id, title, size_mb FROM tg_file_index "
                "WHERE video_id = ? AND kind = ?",
                (video_id, kind or "audio")
            ).fetchone()
        return (row[0], row[1], row[2]) if row else None
    except Exception as e:
        logger.warning(f"tg_file_get failed: {e}")
        return None
    except Exception as e:
        logger.warning(f"tg_file_get failed: {e}")
        return None


def tg_file_stats() -> dict:
    try:
        with get_conn() as conn:
            total = conn.execute(
                "SELECT COUNT(*) AS c FROM tg_file_index").fetchone()["c"]
            size  = conn.execute(
                "SELECT SUM(size_mb) AS s FROM tg_file_index").fetchone()["s"] or 0
            recent = conn.execute("""
                SELECT video_id, title, size_mb FROM tg_file_index
                ORDER BY stored_at DESC LIMIT 5
            """).fetchall()
        return {"total_files": total, "total_mb": round(size, 1),
                "recent": [{"id": r["video_id"], "title": r["title"][:40],
                            "mb": r["size_mb"]} for r in recent]}
    except Exception as e:
        return {"error": str(e)[:80]}


def get_video_cache_stats() -> dict:
    """Admin: how many videos are cached and total cache hits."""
    try:
        with get_conn() as conn:
            total  = conn.execute("SELECT COUNT(*) AS c FROM video_cache").fetchone()["c"]
            hits   = conn.execute("SELECT SUM(hit_count) AS s FROM video_cache").fetchone()["s"] or 0
            fresh  = conn.execute(f"""
                SELECT COUNT(*) AS c FROM video_cache
                WHERE datetime(cached_at) > datetime('now', '-{VIDEO_CACHE_TTL_DAYS} days')
            """).fetchone()["c"]
            top5   = conn.execute("""
                SELECT video_id, title, hit_count FROM video_cache
                ORDER BY hit_count DESC LIMIT 5
            """).fetchall()
        return {
            "total_cached":  total,
            "fresh_entries": fresh,
            "total_hits":    hits,
            "top_videos":    [{"id": r["video_id"], "title": r["title"],
                               "hits": r["hit_count"]} for r in top5],
        }
    except Exception as e:
        logger.warning(f"get_video_cache_stats: {e}")
        return {}


def clear_video_cache(older_than_days: int = None):
    """
    Clear video cache.
    older_than_days=None → clear everything
    older_than_days=7    → clear entries older than 7 days
    """
    try:
        with get_conn() as conn:
            if older_than_days is None:
                conn.execute("DELETE FROM video_cache")
            else:
                conn.execute(
                    "DELETE FROM video_cache WHERE datetime(cached_at) <= datetime('now', ?)",
                    (f"-{older_than_days} days",)
                )
    except Exception as e:
        logger.warning(f"clear_video_cache: {e}")


# ---------------------------------------------------------------------------
# Stream-URL DB cache — same song dobara = DB se (restart-proof)
# googlevideo URL ~6h valid hota hai; uski apni expiry tak DB se serve.
# ---------------------------------------------------------------------------

def get_cached_stream(url: str, fmt: str = "") -> Optional[dict]:
    """
    DB se stream URL (info+result) do — agar expiry me 30s+ bacha ho.
    Return format get_stream_data jaisa: {"info": ..., "result": ...} ya None.
    """
    video_id = _url_to_video_id(url)
    try:
        with get_conn() as conn:
            row = conn.execute("""
                SELECT stream_url, stream_expires_at, stream_format,
                       title, uploader, duration_sec, thumbnail,
                       upload_date, view_count, description, is_live,
                       format_id, ext, resolution
                FROM video_cache WHERE video_id = ?
            """, (video_id,)).fetchone()
        if not row or not row["stream_url"] or not row["stream_expires_at"]:
            return None
        try:
            exp = datetime.strptime(row["stream_expires_at"], "%Y-%m-%d %H:%M:%S")
        except (ValueError, TypeError):
            return None
        if exp <= datetime.utcnow() + timedelta(seconds=30):
            return None                      # expire ho chuka / ho raha hai

        result = {
            "direct_url": row["stream_url"],
            "format_id":  row["format_id"] or "",
            "ext":        row["ext"] or "",
            "resolution": row["resolution"] or "",
            "is_live":    bool(row["is_live"]),
            "title":      row["title"] or "",
        }
        info = {
            "id":               video_id,
            "title":            row["title"] or "",
            "uploader":         row["uploader"] or "",
            "duration_seconds": row["duration_sec"] or 0,
            "thumbnail":        row["thumbnail"] or "",
            "upload_date":      row["upload_date"] or "",
            "view_count":       row["view_count"] or 0,
            "description":      row["description"] or "",
            "is_live":          bool(row["is_live"]),
            "tags":             [],
            "webpage_url":      url,
            "_from_db_stream":  True,
        }
        return {"info": info, "result": result}
    except Exception as e:
        logger.warning(f"get_cached_stream: {e}")
        return None


def cache_stream(url: str, fmt: str, info: dict, result: dict,
                 expires_at_ts: float):
    """Stream URL ko video_cache row me upsert karo (expiry ke saath)."""
    video_id = _url_to_video_id(url)
    exp = datetime.utcfromtimestamp(expires_at_ts).strftime("%Y-%m-%d %H:%M:%S")
    try:
        with get_conn() as conn:
            conn.execute("""
                INSERT INTO video_cache
                    (video_id, url, title, uploader, duration_sec, thumbnail,
                     is_live, format_id, ext, resolution,
                     stream_url, stream_expires_at, stream_format, cached_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
                ON CONFLICT(video_id) DO UPDATE SET
                    stream_url        = excluded.stream_url,
                    stream_expires_at = excluded.stream_expires_at,
                    stream_format     = excluded.stream_format,
                    format_id         = COALESCE(NULLIF(excluded.format_id,''), video_cache.format_id),
                    ext               = COALESCE(NULLIF(excluded.ext,''),       video_cache.ext),
                    resolution        = COALESCE(NULLIF(excluded.resolution,''),video_cache.resolution),
                    cached_at         = datetime('now')
            """, (
                video_id, url,
                info.get("title") or "",
                info.get("uploader") or "",
                int(info.get("duration_seconds") or 0),
                info.get("thumbnail") or "",
                int(info.get("is_live", False)),
                str(result.get("format_id") or ""),
                str(result.get("ext") or ""),
                str(result.get("resolution") or ""),
                result.get("direct_url") or "",
                exp,
                fmt or "",
            ))
    except Exception as e:
        logger.warning(f"cache_stream: {e}")


def get_stream_cache_stats() -> dict:
    """Admin: kitne stream URLs DB me live hain."""
    try:
        with get_conn() as conn:
            total = conn.execute(
                "SELECT COUNT(*) AS c FROM video_cache WHERE stream_url != ''"
            ).fetchone()["c"]
            live = conn.execute("""
                SELECT COUNT(*) AS c FROM video_cache
                WHERE stream_url != ''
                  AND datetime(stream_expires_at) > datetime('now', '+30 seconds')
            """).fetchone()["c"]
        return {"total_cached": total, "live_now": live}
    except Exception as e:
        logger.warning(f"get_stream_cache_stats: {e}")
        return {}


# ---------------------------------------------------------------------------
# Search results DB cache — same query dobara = DB se (restart-proof)
# ---------------------------------------------------------------------------

import json as _json

SEARCH_CACHE_TTL_HOURS = int(os.getenv("SEARCH_CACHE_TTL_HOURS", "24"))


def _search_key(query: str, limit: int) -> str:
    # lower + saare extra spaces collapse — "A  B" aur "a b" same key
    return f"{' '.join(query.split()).lower()}::{limit}"


def get_cached_search(query: str, limit: int) -> Optional[list]:
    key = _search_key(query, limit)
    try:
        with get_conn() as conn:
            row = conn.execute("""
                SELECT results FROM search_cache
                WHERE query_key = ?
                  AND datetime(cached_at) > datetime('now', ?)
            """, (key, f"-{SEARCH_CACHE_TTL_HOURS} hours")).fetchone()
        if not row:
            return None
        return _json.loads(row["results"])
    except Exception as e:
        logger.warning(f"get_cached_search: {e}")
        return None


def cache_search(query: str, limit: int, results: list):
    key = _search_key(query, limit)
    try:
        with get_conn() as conn:
            conn.execute("""
                INSERT INTO search_cache (query_key, results, cached_at)
                VALUES (?, ?, datetime('now'))
                ON CONFLICT(query_key) DO UPDATE SET
                    results = excluded.results,
                    cached_at = datetime('now')
            """, (key, _json.dumps(results)))
    except Exception as e:
        logger.warning(f"cache_search: {e}")

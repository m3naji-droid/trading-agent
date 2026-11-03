"""
الذاكرة السياقية (SQLite):
- تحفظ كل صفقة مع "بصمة" ظروفها وأسباب الدخول والخروج.
- قبل أي صفقة جديدة تبحث عن صفقات سابقة مشابهة؛ إذا كان النمط خاسراً بشكل متكرر يُرفض.
"""
import json
import math
import os
import sqlite3
import threading
import time

from .config import settings

DB = os.path.join(settings.data_dir, "agent.db")
_lock = threading.Lock()


def _conn():
    c = sqlite3.connect(DB, check_same_thread=False)
    c.row_factory = sqlite3.Row
    return c


def init():
    with _lock, _conn() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS trades(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT, market TEXT, strategy TEXT, side TEXT, regime TEXT,
            features TEXT, reasons TEXT, entry REAL, stop REAL, target REAL,
            qty REAL, leverage INTEGER, exit REAL, pnl REAL, r_mult REAL,
            exit_reason TEXT, opened REAL, closed REAL, mode TEXT, shadow INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS events(
            id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, level TEXT, msg TEXT
        );
        CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT);
        CREATE TABLE IF NOT EXISTS equity(ts REAL, value REAL);
        CREATE INDEX IF NOT EXISTS ix_tr ON trades(strategy, side, closed);
        """)


# ------------------------------------------------------------------ أدوات عامة
def log_event(msg, level="info"):
    with _lock, _conn() as c:
        c.execute("INSERT INTO events(ts,level,msg) VALUES(?,?,?)", (time.time(), level, msg))
        c.execute("DELETE FROM events WHERE id < (SELECT MAX(id) - 2000 FROM events)")


def events(limit=100):
    with _conn() as c:
        return [dict(r) for r in c.execute("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,))]


def kv_get(k, default=None):
    with _conn() as c:
        r = c.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
        return json.loads(r["v"]) if r else default


def kv_set(k, v):
    with _lock, _conn() as c:
        c.execute("INSERT OR REPLACE INTO kv(k,v) VALUES(?,?)", (k, json.dumps(v)))


def record_equity(value):
    with _lock, _conn() as c:
        c.execute("INSERT INTO equity VALUES(?,?)", (time.time(), value))


def equity_curve(since_sec=7 * 86400):
    with _conn() as c:
        rows = c.execute("SELECT ts,value FROM equity WHERE ts>? ORDER BY ts", (time.time() - since_sec,)).fetchall()
    step = max(1, len(rows) // 400)
    return [dict(r) for r in rows[::step]]


# ------------------------------------------------------------------ الصفقات
def save_trade(t: dict) -> int:
    cols = ["symbol", "market", "strategy", "side", "regime", "features", "reasons", "entry", "stop",
            "target", "qty", "leverage", "exit", "pnl", "r_mult", "exit_reason", "opened", "closed",
            "mode", "shadow"]
    row = {k: t.get(k) for k in cols}
    row["features"] = json.dumps(row["features"] or [])
    row["reasons"] = json.dumps(row["reasons"] or [], ensure_ascii=False)
    with _lock, _conn() as c:
        cur = c.execute(f"INSERT INTO trades({','.join(cols)}) VALUES({','.join('?' * len(cols))})",
                        [row[k] for k in cols])
        return cur.lastrowid


def recent_trades(limit=50, shadow=0):
    with _conn() as c:
        rows = c.execute("SELECT * FROM trades WHERE shadow=? AND closed IS NOT NULL ORDER BY closed DESC LIMIT ?",
                         (shadow, limit)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["reasons"] = json.loads(d["reasons"] or "[]")
        d.pop("features", None)
        out.append(d)
    return out


def _cos(a, b):
    n = min(len(a), len(b))
    dot = sum(a[i] * b[i] for i in range(n))
    na = math.sqrt(sum(x * x for x in a[:n])) or 1e-9
    nb = math.sqrt(sum(x * x for x in b[:n])) or 1e-9
    return dot / (na * nb)


def check_pattern(strategy, side, feats, k=12):
    """
    يرجع (مسموح؟, شرح). يرفض إذا كانت أقرب الصفقات المشابهة جداً خاسرة في الغالب.
    يشمل الصفقات الحقيقية والظلّية (التجريبية الموازية) والتدريب التاريخي.
    """
    with _conn() as c:
        rows = c.execute(
            "SELECT features, r_mult, exit_reason FROM trades WHERE strategy=? AND side=? AND closed IS NOT NULL "
            "ORDER BY closed DESC LIMIT 3000", (strategy, side)).fetchall()
    sims = []
    for r in rows:
        f = json.loads(r["features"] or "[]")
        if f:
            s = _cos(feats, f)
            if s >= settings.memory_similarity:
                sims.append((s, r["r_mult"] or 0))
    sims.sort(reverse=True)
    near = sims[:k]
    if len(near) < 4:
        return True, f"نمط جديد ({len(near)} حالة مشابهة)"
    losses = sum(1 for _, r in near if r <= 0)
    avg_r = sum(r for _, r in near) / len(near)
    rate = losses / len(near)
    if rate >= 0.7 and avg_r < 0:
        return False, f"نمط خاسر سابقاً: {losses}/{len(near)} خسارة، متوسط {avg_r:+.2f}R"
    return True, f"نمط معروف: {len(near) - losses}/{len(near)} ربح، متوسط {avg_r:+.2f}R"


def stats(shadow=0):
    with _conn() as c:
        rows = c.execute("SELECT strategy, r_mult, pnl FROM trades WHERE shadow=? AND closed IS NOT NULL",
                         (shadow,)).fetchall()
    out = {}
    for r in rows:
        s = out.setdefault(r["strategy"], {"n": 0, "wins": 0, "r": 0.0, "pnl": 0.0})
        s["n"] += 1
        s["wins"] += 1 if (r["r_mult"] or 0) > 0 else 0
        s["r"] += r["r_mult"] or 0
        s["pnl"] += r["pnl"] or 0
    return out


def lessons(limit=8):
    """أحدث الدروس من الصفقات الخاسرة الحقيقية."""
    with _conn() as c:
        rows = c.execute("SELECT symbol, strategy, side, regime, reasons, exit_reason, r_mult, closed FROM trades "
                         "WHERE shadow=0 AND r_mult<0 ORDER BY closed DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) | {"reasons": json.loads(r["reasons"] or "[]")} for r in rows]

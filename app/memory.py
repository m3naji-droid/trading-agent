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
        for col in ("btc REAL", "mode TEXT"):  # ترقية قاعدة البيانات القديمة
            try:
                c.execute(f"ALTER TABLE equity ADD COLUMN {col}")
            except sqlite3.OperationalError:
                pass
        try:
            c.execute("ALTER TABLE trades ADD COLUMN council TEXT")
        except sqlite3.OperationalError:
            pass


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


def _jsonable(o):
    """أنواع numpy وغيرها => أنواع JSON عادية (حتى لا يفشل الحفظ أبداً)."""
    if hasattr(o, "item"):
        return o.item()
    if isinstance(o, (set, tuple)):
        return list(o)
    return str(o)


def kv_set(k, v):
    with _lock, _conn() as c:
        c.execute("INSERT OR REPLACE INTO kv(k,v) VALUES(?,?)", (k, json.dumps(v, default=_jsonable)))


def record_equity(value, btc=None, mode=None):
    with _lock, _conn() as c:
        c.execute("INSERT INTO equity(ts,value,btc,mode) VALUES(?,?,?,?)", (time.time(), value, btc, mode))


def _mode_clause(mode):
    if mode is None:
        return "1=1", ()
    if mode == "paper":
        return "(mode=? OR mode IS NULL)", (mode,)
    return "mode=?", (mode,)


def equity_rows(mode=None, since_sec=None):
    cl, args = _mode_clause(mode)
    q = f"SELECT ts,value,btc FROM equity WHERE {cl}"
    if since_sec:
        q += " AND ts>?"
        args = args + (time.time() - since_sec,)
    with _conn() as c:
        return [dict(r) for r in c.execute(q + " ORDER BY ts", args)]


def equity_curve(since_sec=30 * 86400, mode=None):
    rows = equity_rows(mode, since_sec)
    step = max(1, len(rows) // 400)
    out = rows[::step]
    if rows and out[-1] is not rows[-1]:
        out.append(rows[-1])
    return out


# ------------------------------------------------------------------ الصفقات
def save_trade(t: dict) -> int:
    cols = ["symbol", "market", "strategy", "side", "regime", "features", "reasons", "entry", "stop",
            "target", "qty", "leverage", "exit", "pnl", "r_mult", "exit_reason", "opened", "closed",
            "mode", "shadow", "council"]
    row = {k: t.get(k) for k in cols}
    row["features"] = json.dumps(row["features"] or [])
    row["reasons"] = json.dumps(row["reasons"] or [], ensure_ascii=False)
    row["council"] = json.dumps(row["council"], ensure_ascii=False) if row["council"] else None
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
        d["council"] = json.loads(d["council"]) if d.get("council") else None
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
    يرجع (مسموح؟, شرح, إحصاءات). يرفض إذا كانت أقرب الصفقات المشابهة جداً خاسرة في الغالب.
    يشمل الصفقات الحقيقية والظلّية (التجريبية الموازية) والتدريب التاريخي.
    """
    with _conn() as c:
        rows = c.execute(
            "SELECT features, r_mult FROM trades WHERE strategy=? AND side=? AND closed IS NOT NULL "
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
        return True, f"نمط جديد ({len(near)} حالة مشابهة)", {"n": len(near), "winrate": 0.5, "avg_r": 0.0}
    losses = sum(1 for _, r in near if r <= 0)
    avg_r = sum(r for _, r in near) / len(near)
    st = {"n": len(near), "winrate": 1 - losses / len(near), "avg_r": avg_r}
    if losses / len(near) >= 0.7 and avg_r < 0:
        return False, f"نمط خاسر سابقاً: {losses}/{len(near)} خسارة، متوسط {avg_r:+.2f}R", st
    return True, f"نمط معروف: {len(near) - losses}/{len(near)} ربح، متوسط {avg_r:+.2f}R", st


def council_stats(mode, days=30):
    """هل المجلس محق؟ مقارنة نتائج الفرص التي وافق عليها بالتي رفضها (من الصفقات الظلّية)."""
    with _conn() as c:
        rows = c.execute("SELECT council, r_mult FROM trades WHERE shadow=1 AND market='shadow' AND mode=? "
                         "AND council IS NOT NULL AND closed>?", (mode, time.time() - days * 86400)).fetchall()
    out = {"approved": {"n": 0, "r": 0.0, "wins": 0}, "rejected": {"n": 0, "r": 0.0, "wins": 0},
           "members": {}}
    for row in rows:
        cn = json.loads(row["council"])
        r = row["r_mult"] or 0
        b = out["approved" if cn.get("approved") else "rejected"]
        b["n"] += 1; b["r"] += r; b["wins"] += 1 if r > 0 else 0
        for v in cn.get("votes", []):
            m = out["members"].setdefault(v["key"], {"right": 0, "n": 0})
            m["n"] += 1
            m["right"] += 1 if (v["vote"] and r > 0) or (not v["vote"] and r <= 0) else 0
    for k in ("approved", "rejected"):
        b = out[k]
        b["avg_r"] = b["r"] / b["n"] if b["n"] else 0.0
        b["winrate"] = b["wins"] / b["n"] if b["n"] else 0.0
    return out


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


def get_trade(tid):
    with _conn() as c:
        r = c.execute("SELECT * FROM trades WHERE id=?", (tid,)).fetchone()
    if not r:
        return None
    d = dict(r)
    d["reasons"] = json.loads(d["reasons"] or "[]")
    d["council"] = json.loads(d["council"]) if d.get("council") else None
    d.pop("features", None)
    return d


def real_trades(mode):
    """كل الصفقات المغلقة غير التجريبية في وضع معيّن، من الأقدم للأحدث."""
    with _conn() as c:
        rows = c.execute("SELECT id,symbol,strategy,side,regime,pnl,r_mult,exit_reason,opened,closed FROM trades "
                         "WHERE shadow=0 AND mode=? AND closed IS NOT NULL ORDER BY closed", (mode,)).fetchall()
    return [dict(r) for r in rows]

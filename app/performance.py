"""
مقاييس الأداء + مؤشر جاهزية الوكيل للمال الحقيقي + المقارنة بالاحتفاظ بالبيتكوين.
"""
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from . import memory

LOCAL_TZ = timezone(timedelta(hours=3))  # توقيت البحرين

# شروط الجاهزية: (المفتاح, الوصف, الهدف المعروض)
CRITERIA = [
    ("days", "مدة التجربة", "14 يوماً على الأقل"),
    ("trades", "عدد الصفقات", "30 صفقة على الأقل"),
    ("profit_factor", "معامل الربح", "1.3 أو أعلى"),
    ("avg_r", "متوسط ربح الصفقة", "أعلى من +0.10R"),
    ("max_dd", "أقصى تراجع للمحفظة", "15% أو أقل"),
    ("beat_btc", "التفوق على الاحتفاظ بالبيتكوين", "عائد أعلى من BTC"),
]

_cache = {"t": 0, "mode": None, "v": None}


def _max_drawdown(values):
    peak, dd = 0.0, 0.0
    for v in values:
        peak = max(peak, v)
        if peak > 0:
            dd = max(dd, 1 - v / peak)
    return dd


def summary(mode: str) -> dict:
    if _cache["mode"] == mode and time.time() - _cache["t"] < 30:
        return _cache["v"]
    trades = memory.real_trades(mode)
    eq = memory.equity_rows(mode)

    n = len(trades)
    wins = [t for t in trades if (t["pnl"] or 0) > 0]
    gross_win = sum(t["pnl"] for t in wins)
    gross_loss = -sum(t["pnl"] for t in trades if (t["pnl"] or 0) <= 0)
    pf = gross_win / gross_loss if gross_loss > 0 else (float("inf") if gross_win > 0 else 0.0)
    avg_r = sum(t["r_mult"] or 0 for t in trades) / n if n else 0.0
    total_pnl = sum(t["pnl"] or 0 for t in trades)

    values = [r["value"] for r in eq if r["value"]]
    max_dd = _max_drawdown(values)
    days = (time.time() - eq[0]["ts"]) / 86400 if eq else 0.0
    agent_ret = values[-1] / values[0] - 1 if len(values) > 1 and values[0] else 0.0
    btc_rows = [r for r in eq if r.get("btc")]
    btc_ret = btc_rows[-1]["btc"] / btc_rows[0]["btc"] - 1 if len(btc_rows) > 1 else 0.0

    # حسب العملة
    by_sym = defaultdict(lambda: {"n": 0, "pnl": 0.0, "wins": 0})
    for t in trades:
        s = by_sym[t["symbol"]]
        s["n"] += 1
        s["pnl"] += t["pnl"] or 0
        s["wins"] += 1 if (t["pnl"] or 0) > 0 else 0
    syms = sorted(({"symbol": k, **v} for k, v in by_sym.items()), key=lambda x: -x["pnl"])

    # حسب ساعة الدخول (بتوقيتك)
    by_hour = defaultdict(lambda: {"n": 0, "pnl": 0.0, "wins": 0})
    for t in trades:
        h = datetime.fromtimestamp(t["opened"] or t["closed"], LOCAL_TZ).hour
        b = by_hour[h]
        b["n"] += 1
        b["pnl"] += t["pnl"] or 0
        b["wins"] += 1 if (t["pnl"] or 0) > 0 else 0
    hours = [{"hour": h, **by_hour[h]} for h in range(24)]

    # أسبوعي وشهري
    weekly, monthly = defaultdict(float), defaultdict(float)
    for t in trades:
        d = datetime.fromtimestamp(t["closed"], LOCAL_TZ)
        y, w, _ = d.isocalendar()
        weekly[f"{y}-W{w:02d}"] += t["pnl"] or 0
        monthly[d.strftime("%Y-%m")] += t["pnl"] or 0

    checks = {
        "days": (days >= 14, f"{days:.1f}", "يوم"),
        "trades": (n >= 30, f"{n}", "صفقة"),
        "profit_factor": (pf >= 1.3, "∞" if pf == float("inf") else f"{pf:.2f}", ""),
        "avg_r": (avg_r > 0.10, f"{avg_r:+.2f}R", ""),
        "max_dd": (max_dd <= 0.15 and len(values) > 1, f"{max_dd:.1%}", ""),
        "beat_btc": (len(btc_rows) > 1 and agent_ret > btc_ret, f"{agent_ret:+.1%} / {btc_ret:+.1%}", "الوكيل / BTC"),
    }
    items = [{"key": k, "label": lbl, "target": tgt, "ok": checks[k][0], "value": checks[k][1], "unit": checks[k][2]}
             for k, lbl, tgt in CRITERIA]
    passed = sum(i["ok"] for i in items)
    status = "ready" if passed == len(items) else "close" if passed >= len(items) - 2 else "not_ready"

    v = {
        "trades": n, "wins": len(wins), "winrate": len(wins) / n if n else 0.0,
        "profit_factor": None if pf == float("inf") else pf, "avg_r": avg_r, "total_pnl": total_pnl,
        "max_dd": max_dd, "days": days, "agent_return": agent_ret, "btc_return": btc_ret,
        "best": syms[:3], "worst": [s for s in reversed(syms[-3:]) if s["pnl"] < 0],
        "hours": hours,
        "weekly": [{"k": k, "pnl": v} for k, v in sorted(weekly.items())][-8:],
        "monthly": [{"k": k, "pnl": v} for k, v in sorted(monthly.items())][-6:],
        "readiness": {"status": status, "passed": passed, "total": len(items), "items": items},
        "monte_carlo": monte_carlo(mode),
        "daily": daily_pnl(mode),
    }
    _cache.update(t=time.time(), mode=mode, v=v)
    return v


def monte_carlo(mode: str, n_paths=2000, n_trades=100):
    """
    محاكاة مونت كارلو: نعيد خلط نتائج الصفقات الفعلية آلاف المرات لنقدّر
    "أسوأ سيناريو معقول" للـ100 صفقة القادمة (ليس توقعاً — بل نطاق احتمالات).
    """
    import numpy as np
    from .config import settings
    rs = [t["r_mult"] for t in memory.real_trades(mode) if t["r_mult"] is not None]
    src = "صفقات الوكيل الفعلية"
    if len(rs) < 20:
        with memory._conn() as c:
            rows = c.execute("SELECT r_mult FROM trades WHERE market='explore' AND mode=? AND closed IS NOT NULL "
                             "ORDER BY closed DESC LIMIT 2000", (mode,)).fetchall()
        rs = [r[0] for r in rows if r[0] is not None]
        src = "صفقات المستكشف (لا توجد صفقات فعلية كافية بعد)"
    if len(rs) < 20:
        return {"ready": False, "n": len(rs), "need": 20}
    r = np.clip(np.array(rs, dtype=float), -3, 10)
    risk = (settings.risk_min + settings.risk_max) / 2
    rng = np.random.default_rng(7)
    sample = rng.choice(r, size=(n_paths, n_trades), replace=True)
    eq = np.cumprod(1 + sample * risk, axis=1)
    peak = np.maximum.accumulate(np.concatenate([np.ones((n_paths, 1)), eq], axis=1), axis=1)[:, 1:]
    dd = (1 - eq / peak).max(axis=1)
    fin = eq[:, -1] - 1
    return {"ready": True, "n": len(rs), "source": src, "trades": n_trades, "risk": risk,
            "median": float(np.median(fin)), "p5": float(np.percentile(fin, 5)), "p95": float(np.percentile(fin, 95)),
            "p_loss": float((fin < 0).mean()), "dd_median": float(np.median(dd)), "dd_p95": float(np.percentile(dd, 95)),
            "p_dd10": float((dd > 0.10).mean()), "p_dd20": float((dd > 0.20).mean()), "p_dd30": float((dd > 0.30).mean())}


def weekly_report(mode, equity, darwin_history, gate, explorer):
    since = time.time() - 7 * 86400
    week = [t for t in memory.real_trades(mode) if t["closed"] >= since]
    pnl = sum(t["pnl"] or 0 for t in week)
    wins = sum(1 for t in week if (t["pnl"] or 0) > 0)
    eq = memory.equity_rows(mode, since_sec=7 * 86400)
    start = eq[0]["value"] if eq else equity
    btc = [r for r in eq if r.get("btc")]
    btc_ret = btc[-1]["btc"] / btc[0]["btc"] - 1 if len(btc) > 1 else 0.0
    ag_ret = equity / start - 1 if start else 0.0
    promos = [h for h in darwin_history if h["ts"] >= since]
    approved = [k for k, g in gate.items() if g.get("status") == "approved"]
    suspended = [k for k, g in gate.items() if g.get("status") == "suspended"]
    from .strategies import STRATEGY_AR
    s = summary(mode)
    lines = [
        f"💼 المحفظة: ${equity:,.2f} — الأسبوع {ag_ret:+.2%} مقابل البيتكوين {btc_ret:+.2%}"
        + (" ✅ تفوّق" if ag_ret > btc_ret else ""),
        f"📊 {len(week)} صفقة (رابحة {wins}) — صافي {pnl:+.2f}$",
        f"🛡️ معتمدة للتداول: {len(approved)} استراتيجية" + (f" · معلّقة: {'، '.join(STRATEGY_AR[k] for k in suspended)}" if suspended else ""),
        f"🧬 ترقيات داروين هذا الأسبوع: {len(promos)}" + (" (" + "، ".join(STRATEGY_AR.get(h['strategy'], h['strategy']) for h in promos[:3]) + ")" if promos else ""),
        f"🧭 المستكشف: {explorer.get('trades', 0)} صفقة تعليمية إجمالاً، متوسط {explorer.get('avg_r', 0):+.2f}R",
        f"🎯 الجاهزية للحقيقي: {s['readiness']['passed']}/{s['readiness']['total']}",
    ]
    return f"التقرير الأسبوعي — {datetime.now(LOCAL_TZ).strftime('%Y-%m-%d')}", "\n".join(lines)


def daily_pnl(mode, days=120):
    """ربح كل يوم (بتوقيتك) — لتقويم الأرباح."""
    out = defaultdict(float)
    cnt = defaultdict(int)
    cut = time.time() - days * 86400
    for t in memory.real_trades(mode):
        if t["closed"] >= cut:
            d = datetime.fromtimestamp(t["closed"], LOCAL_TZ).strftime("%Y-%m-%d")
            out[d] += t["pnl"] or 0
            cnt[d] += 1
    return [{"d": d, "pnl": out[d], "n": cnt[d]} for d in sorted(out)]


def daily_report(mode: str, equity: float, extra: str = "") -> tuple[str, str]:
    """تقرير آخر 24 ساعة. يرجع (عنوان, نص)."""
    since = time.time() - 86400
    day = [t for t in memory.real_trades(mode) if t["closed"] >= since]
    pnl = sum(t["pnl"] or 0 for t in day)
    wins = sum(1 for t in day if (t["pnl"] or 0) > 0)
    eq = memory.equity_rows(mode, since_sec=86400)
    start = eq[0]["value"] if eq else equity
    chg = equity / start - 1 if start else 0.0
    s = summary(mode)
    rd = s["readiness"]
    lines = [
        f"💼 المحفظة: ${equity:,.2f} ({chg:+.2%} خلال 24 ساعة)",
        f"📊 الصفقات المغلقة: {len(day)} (رابحة {wins}) — صافي {pnl:+.2f}$",
    ]
    if day:
        best = max(day, key=lambda t: t["pnl"] or 0)
        worst = min(day, key=lambda t: t["pnl"] or 0)
        lines.append(f"🏆 أفضل صفقة: {best['symbol']} {best['pnl']:+.2f}$")
        if (worst["pnl"] or 0) < 0:
            lines.append(f"🔻 أسوأ صفقة: {worst['symbol']} {worst['pnl']:+.2f}$")
    lines.append(f"🎯 الجاهزية للحقيقي: {rd['passed']}/{rd['total']} شروط")
    if extra:
        lines.append(extra)
    title = f"التقرير اليومي — {datetime.now(LOCAL_TZ).strftime('%Y-%m-%d')}"
    return title, "\n".join(lines)

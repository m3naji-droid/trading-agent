"""
التعلم السريع والصادق:
  📚 دراسة سنتين من تاريخ كل عملة (بدلاً من 6 أسابيع) — تُحفظ الشموع على القرص وتُحدَّث تدريجياً.
  🛡️ الاختبار على المستقبل المخفي: أول 70% من التاريخ للتعلم، وآخر 30% لم "يره" الوكيل للحكم.
     لا يتداول الوكيل فعلياً باستراتيجية إلا إذا نجحت في الفترة المخفية (أو أثبتت نفسها حياً).
  🌙 حلم الوكيل: كل ليلة يعيد محاكاة آخر 30 يوماً بكل الاستراتيجيات وجينات داروين،
     ويعلّق مؤقتاً أي استراتيجية معتمدة بدأت تخسر بوضوح في السوق الحالي.

ملاحظة صدق: الشموع ذات الفريم الأكبر (4 ساعات / يومي) تُستخدم فقط بعد إغلاقها فعلاً
لتجنّب "رؤية المستقبل" في المحاكاة.
"""
import logging
import os
import time

import numpy as np
import pandas as pd

from . import memory
from . import smc as smc_mod
from .config import settings
from .cross import add_btc_link, add_funding, add_rs, rs_panel
from .darwin import accepts
from .indicators import enrich
from .learner import simulate_trade
from .regime import detect_regime
from .strategies import STRATEGIES, features, metrics, run_all

log = logging.getLogger("study")
HIST_DIR = os.path.join(settings.data_dir, "hist")
os.makedirs(HIST_DIR, exist_ok=True)
H = 3_600_000
TF_MS = {"1h": H, "4h": 4 * H, "1d": 24 * H}
COLS = ["ts", "open", "high", "low", "close", "volume"]
TEST_SHARE = 0.30          # آخر 30% من التاريخ = المستقبل المخفي
SAVE_RECENT_BARS = 6000    # تُحفظ في ذاكرة الأنماط إشارات آخر ~8 أشهر فقط (توفير المساحة)
MAX_BARS = 48              # أقصى مدة صفقة في المحاكاة (48 ساعة)

# شروط اعتماد الاستراتيجية
GATE_MIN_N, GATE_MIN_AVG, GATE_MIN_PF = 20, 0.05, 1.1
LIVE_MIN_N, LIVE_MIN_AVG = 40, 0.10
SUSPEND_N, SUSPEND_AVG = 15, -0.15


# ------------------------------------------------------------------ تخزين التاريخ على القرص
def _path(base, tf):
    return os.path.join(HIST_DIR, f"{base}_{tf}.npy")


def load_cached(base, tf):
    p = _path(base, tf)
    if os.path.exists(p):
        try:
            d = pd.DataFrame(np.load(p), columns=COLS)
            d["ts"] = d["ts"].astype("int64")
            return d
        except Exception:
            pass
    return None


async def update_history(ex, sym, tf, bars):
    """يحمّل الشموع من القرص ويكمل الناقص فقط من Binance."""
    base = sym.split("/")[0]
    df = load_cached(base, tf)
    ms = TF_MS[tf]
    now = ex.milliseconds()
    since = int(df["ts"].iloc[-1]) + 1 if df is not None and len(df) else now - bars * ms
    rows = []
    for _ in range(40):
        batch = await ex.fetch_ohlcv(sym, tf, since=since, limit=1000)
        if not batch:
            break
        rows += batch
        since = batch[-1][0] + 1
        if len(batch) < 1000:
            break
    if rows:
        new = pd.DataFrame(rows, columns=COLS)
        df = new if df is None else pd.concat([df, new])
    if df is None:
        return pd.DataFrame(columns=COLS)
    df["ts"] = df["ts"].astype("int64")
    df = df.drop_duplicates("ts").sort_values("ts")
    df = df[df["ts"] <= now - ms]  # الشموع المغلقة فقط
    df = df.iloc[-bars:].reset_index(drop=True)
    np.save(_path(base, tf), df[COLS].to_numpy(dtype=np.float64))
    return df


async def update_funding(fut, sym, days):
    base = sym.split("/")[0]
    p = _path(base, "fund")
    df = pd.DataFrame(np.load(p), columns=["ts", "rate"]) if os.path.exists(p) else None
    if df is not None:
        df["ts"] = df["ts"].astype("int64")
    since = int(df["ts"].iloc[-1]) + 1 if df is not None and len(df) else fut.milliseconds() - days * 86_400_000
    rows = []
    try:
        for _ in range(20):
            batch = await fut.fetch_funding_rate_history(sym + ":USDT", since=since, limit=1000)
            if not batch:
                break
            rows += [(b["timestamp"], float(b.get("fundingRate") or 0)) for b in batch]
            since = batch[-1]["timestamp"] + 1
            if len(batch) < 1000:
                break
    except Exception as e:
        log.warning(f"funding history {sym}: {e}")
    if rows:
        new = pd.DataFrame(rows, columns=["ts", "rate"])
        df = new if df is None else pd.concat([df, new])
    if df is None or not len(df):
        return None
    df["ts"] = df["ts"].astype("int64")
    df = df.drop_duplicates("ts").sort_values("ts").reset_index(drop=True)
    np.save(p, df.to_numpy(dtype=np.float64))
    return df


def daily_from_4h(h4):
    d = h4.copy()
    d["day"] = (d["ts"] // TF_MS["1d"]) * TF_MS["1d"]
    g = d.groupby("day").agg(open=("open", "first"), high=("high", "max"), low=("low", "min"),
                              close=("close", "last"), volume=("volume", "sum"), n=("ts", "count"))
    g = g[g["n"] == 6].drop(columns="n").reset_index().rename(columns={"day": "ts"})
    return g


# ------------------------------------------------------------------ المحاكاة
def _new_bucket():
    return {"n": 0, "sum": 0.0, "wins": 0, "gw": 0.0, "gl": 0.0}


def _add(b, r):
    b["n"] += 1
    b["sum"] += r
    if r > 0:
        b["wins"] += 1
        b["gw"] += r
    else:
        b["gl"] += -r


def simulate_symbol(base, raw1h, raw4h, btc_e, rs_series, funding, split_ts, darwin, learner, *,
                    start_ts=None, learn=True, save=True, step=1, lw=0.15, dw=0.5, recent_from=None,
                    progress=None):
    """
    يمر على التاريخ شمعة شمعة كأنه يتداول لحظياً.
    يرجع إحصاءات لكل استراتيجية: train / test (المخفي) / recent (آخر 30 يوماً).
    """
    e = enrich(raw1h).dropna().reset_index(drop=True)
    if base != "BTC" and btc_e is not None:
        e = add_btc_link(e, btc_e)
    e = add_rs(e, rs_series)
    e = add_funding(e, funding).reset_index(drop=True)
    h4 = enrich(raw4h).dropna().reset_index(drop=True)
    d1 = enrich(daily_from_4h(raw4h)).dropna().reset_index(drop=True)
    h4_close = h4["ts"].values + TF_MS["4h"]
    d1_close = d1["ts"].values + TF_MS["1d"]
    ts = e["ts"].values
    stats = {s: {"train": _new_bucket(), "test": _new_bucket(), "recent": _new_bucket()} for s in STRATEGIES}
    if len(e) < 320 or len(h4) < 60:
        return stats, 0
    save_from = ts[max(0, len(ts) - SAVE_RECENT_BARS)]
    start_i = 260 if start_ts is None else max(260, int(np.searchsorted(ts, start_ts)))
    cooldown = {s: -1 for s in STRATEGIES}
    n = 0
    total = max(1, len(e) - 2 - start_i)
    for i in range(start_i, len(e) - 2, step):
        t = ts[i]
        now_close = t + H  # لحظة إغلاق الشمعة الحالية
        j = int(np.searchsorted(h4_close, now_close, side="right"))
        if j < 50:
            continue
        hwin = h4.iloc[max(0, j - 120):j]
        k = int(np.searchsorted(d1_close, now_close, side="right"))
        dwin = d1.iloc[max(0, k - 120):k] if k >= 30 else None
        win = e.iloc[i - 250:i + 1]
        ctx = {"df": win, "htf": hwin, "ttf": dwin, "regime": detect_regime(win), "smc": smc_mod.analyze(win),
               "flow": None, "base": base, "liq": None}
        reg = ctx["regime"]["regime"]
        for sig in run_all(ctx):
            if i <= cooldown[sig.strategy]:
                continue
            cooldown[sig.strategy] = i + 6
            fut = e.iloc[i + 1:i + 1 + MAX_BARS]
            atr = float(win["atr"].iloc[-1])
            m = metrics(ctx, sig)
            r_def = simulate_trade(sig, fut, atr)[0]  # الجينات الافتراضية => حكم نظيف على الاستراتيجية نفسها
            _add(stats[sig.strategy]["test" if t >= split_ts else "train"], r_def)
            if recent_from is not None and t >= recent_from:
                _add(stats[sig.strategy]["recent"], r_def)
            res = darwin.evaluate(sig.strategy, sig, m, fut, atr, simulate_trade) if darwin else {}
            if darwin and dw:
                darwin.record(sig.strategy, res, weight=dw)
            n += 1
            champ = darwin.champion(sig.strategy) if darwin else None
            if champ and not accepts(champ, m):
                continue
            r_ch, why, px, bars = simulate_trade(sig, fut, atr, genome=champ)
            if learn and learner is not None:
                learner.update(sig.strategy, reg, r_ch, weight=lw, save=False, decay=False)
            if save and t >= save_from:
                memory.save_trade({
                    "symbol": f"{base}/USDT", "market": "history", "strategy": sig.strategy, "side": sig.side,
                    "regime": reg, "features": features(ctx, sig), "reasons": sig.reasons,
                    "entry": sig.entry, "stop": sig.stop, "target": sig.target, "qty": 0, "leverage": 1,
                    "exit": px, "pnl": 0, "r_mult": r_ch, "exit_reason": why,
                    "opened": float(t) / 1000, "closed": float(t) / 1000 + bars * 3600,
                    "mode": "pretrain", "shadow": 1})
        if progress and (i - start_i) % 500 == 0:
            progress((i - start_i) / total)
    if learner is not None and learn:
        learner.save()
    if darwin:
        darwin.save()
    return stats, n


# ------------------------------------------------------------------ بوابة الاعتماد
def merge_stats(agg, stats, keys=("train", "test", "recent")):
    for s, st in stats.items():
        a = agg.setdefault(s, {k: _new_bucket() for k in ("train", "test", "recent")})
        for k in keys:
            for f in ("n", "sum", "wins", "gw", "gl"):
                a[k][f] += st[k][f]
    return agg


def _summ(b):
    n = b["n"]
    return {"n": n, "avg_r": b["sum"] / n if n else 0.0, "winrate": b["wins"] / n if n else 0.0,
            "pf": (b["gw"] / b["gl"]) if b["gl"] > 0 else (None if b["gw"] == 0 else 99.0)}


def compute_gate(agg, old=None):
    old = old or {}
    gate = {}
    for s in STRATEGIES:
        a = agg.get(s)
        prev = old.get(s, {})
        if not a:
            gate[s] = prev or {"status": "testing", "reason": "لا توجد بيانات تاريخية — تُختبر حياً",
                               "train": _summ(_new_bucket()), "test": _summ(_new_bucket())}
            continue
        tr, te = _summ(a["train"]), _summ(a["test"])
        if te["n"] >= GATE_MIN_N and te["avg_r"] >= GATE_MIN_AVG and (te["pf"] or 0) >= GATE_MIN_PF:
            st, why = "approved", f"نجحت في المستقبل المخفي ({te['avg_r']:+.2f}R على {te['n']} صفقة)"
        elif te["n"] < GATE_MIN_N:
            st, why = "testing", f"صفقات قليلة في الفترة المخفية ({te['n']}) — تُختبر حياً"
        else:
            st, why = "rejected", f"فشلت في المستقبل المخفي ({te['avg_r']:+.2f}R) — تبقى للتعلم فقط"
        if prev.get("status") == "approved" and prev.get("source") == "live" and st != "approved":
            st, why = "approved", prev["reason"]  # من أثبت نفسه حياً يبقى
        gate[s] = {"status": st, "reason": why, "train": tr, "test": te, "source": "history", "ts": time.time()}
    return gate


def apply_recent(gate, agg_recent):
    """حلم الليل: تعليق المعتمدة التي تخسر بوضوح مؤخراً، وإعادة المعلّقة إذا تعافت."""
    changes = []
    for s, g in gate.items():
        r = _summ((agg_recent.get(s) or {}).get("recent", _new_bucket()))
        g["recent"] = r
        if g["status"] == "approved" and r["n"] >= SUSPEND_N and r["avg_r"] < SUSPEND_AVG:
            g["status"], g["reason"] = "suspended", f"تخسر في آخر 30 يوماً ({r['avg_r']:+.2f}R على {r['n']}) — معلّقة مؤقتاً"
            changes.append((s, "suspended"))
        elif g["status"] == "suspended" and r["n"] >= 5 and r["avg_r"] >= 0:
            g["status"], g["reason"] = "approved", f"تعافت في آخر 30 يوماً ({r['avg_r']:+.2f}R)"
            changes.append((s, "approved"))
    return changes


def live_redemption(gate, mode):
    """استراتيجية غير معتمدة تثبت نفسها في الصفقات التجريبية الحية => تُعتمد."""
    changes = []
    with memory._conn() as c:
        rows = c.execute("SELECT strategy, COUNT(*) n, AVG(r_mult) a FROM trades WHERE shadow=1 AND market='shadow' "
                         "AND mode=? AND closed>? GROUP BY strategy", (mode, time.time() - 30 * 86400)).fetchall()
    for r in rows:
        g = gate.get(r["strategy"])
        if g and g["status"] in ("testing", "rejected") and r["n"] >= LIVE_MIN_N and (r["a"] or 0) >= LIVE_MIN_AVG:
            g.update(status="approved", source="live",
                     reason=f"أثبتت نفسها في التداول التجريبي الحي ({r['a']:+.2f}R على {r['n']} فرصة)")
            changes.append(r["strategy"])
    return changes

"""
التعلم التعزيزي (Multi-Armed Bandit بأخذ عينات Thompson):
- كل "ذراع" = استراتيجية × حالة سوق.
- يتعلم من: (1) التدريب على تاريخ العملات، (2) الصفقات الظلّية الموازية، (3) الصفقات الفعلية.
- عامل نسيان (decay) يجعله يتكيف بسرعة مع تغير السوق.
"""
import random
import threading
import time
import logging

import pandas as pd

from . import memory
from .indicators import enrich
from .regime import detect_regime
from . import smc as smc_mod
from .strategies import run_all, features, STRATEGIES
from .config import settings
from . import exits

log = logging.getLogger("learner")
DECAY = 0.997       # نسيان تدريجي => تعلم سريع من الأحدث
PRIOR = 3.0


class Learner:
    def __init__(self):
        self.arms = memory.kv_get("arms", {})
        self.lock = threading.RLock()

    def _arm(self, strategy, regime):
        return self.arms.setdefault(f"{strategy}|{regime}", {"w": 0.0, "l": 0.0, "r": 0.0, "n": 0.0})

    def update(self, strategy, regime, r_mult, weight=1.0, save=True, decay=True):
        with self.lock:
            self._update(strategy, regime, r_mult, weight, save, decay)

    def reset(self):
        with self.lock:
            self.arms = {}
            memory.kv_set("arms", self.arms)

    def _update(self, strategy, regime, r_mult, weight, save, decay=True):
        if decay:
            for a in self.arms.values():
                for k in a:
                    a[k] *= DECAY
        a = self._arm(strategy, regime)
        a["n"] += weight
        a["r"] += r_mult * weight
        if r_mult > 0:
            a["w"] += weight
        else:
            a["l"] += weight
        if save:
            memory.kv_set("arms", self.arms)

    def score(self, strategy, regime, sample=True):
        """درجة 0..1 لمدى جدارة الاستراتيجية في هذه الحالة."""
        with self.lock:
            a = dict(self._arm(strategy, regime))
        alpha, beta = a["w"] + PRIOR, a["l"] + PRIOR
        p = random.betavariate(alpha, beta) if sample else alpha / (alpha + beta)
        expectancy = a["r"] / (a["n"] + 8)  # تقليص نحو الصفر عند قلة البيانات
        exp_score = 1 / (1 + pow(2.718, -3 * expectancy))
        return 0.5 * p + 0.5 * exp_score

    def blended_confidence(self, sig, regime):
        return 0.5 * sig.confidence + 0.5 * self.score(sig.strategy, regime)

    def leaderboard(self):
        rows = []
        with self.lock:
            items = [(k, dict(v)) for k, v in self.arms.items()]
        for key, a in items:
            s, reg = key.split("|")
            n = a["n"]
            rows.append({"strategy": s, "regime": reg, "n": round(n, 1),
                         "winrate": a["w"] / n if n else 0, "avg_r": a["r"] / n if n else 0,
                         "score": self.score(s, reg, sample=False)})
        return sorted(rows, key=lambda x: -x["score"])

    def save(self):
        with self.lock:
            memory.kv_set("arms", self.arms)


# ------------------------------------------------------------------ محاكاة صفقة
DEFAULT_GENOME = {"stop": 1.0, "rr": 1.0, "trail": 1.5, "be": 1.0, "pg": 2.0, "pg_sig": 2.0, "tp1": 2.0, "gb": 1.0}


def simulate_trade(sig, future: pd.DataFrame, atr: float, max_bars=48, fee=None, genome=None):
    """
    يحاكي صفقة على الشموع اللاحقة وفق "جينات" الإدارة (داروين):
    مسافة الوقف × stop، الهدف × rr، يبدأ التتبع عند be×R بمسافة trail×ATR (تضيق بعد +2R)،
    + أدوات حماية الأرباح: حارس الأرباح (pg/pg_sig)، ربح جزئي (tp1)، قاعدة إرجاع الربح (gb).
    يرجع (R-multiple صافي, سبب الخروج, سعر الخروج, عدد الشموع).
    """
    g = dict(DEFAULT_GENOME, **(genome or {}))
    fee = settings.fee_rate if fee is None else fee
    e = sig.entry
    risk0 = abs(e - sig.stop)
    if risk0 <= 0 or len(future) == 0:
        return 0.0, "none", e, 0
    rr0 = abs(sig.target - e) / risk0
    risk = risk0 * g["stop"]
    long = sig.side == "long"
    d = 1 if long else -1
    stop0 = e - d * risk
    stop = stop0
    target = e + d * risk * rr0 * g["rr"]
    fut = future.iloc[:max_bars]
    H, L, C = fut["high"].values, fut["low"].values, fut["close"].values
    use_pg = exits.pg_on(g) and all(c in fut.columns for c in ("open", "ema20", "rsi", "vol_z", "atr"))
    use_tp1, use_gb = exits.tp1_on(g), exits.gb_on(g)
    if use_pg:
        O, E20, RS, VZ, AT = (fut[c].values for c in ("open", "ema20", "rsi", "vol_z", "atr"))
    need_sig = int(round(g["pg_sig"]))
    best = e
    frac, realized, partial = 1.0, 0.0, False
    peak_r = 0.0
    why = None
    for i in range(len(H)):
        h, l = H[i], L[i]
        if long:
            if l <= stop:
                px, why = stop, "stop"
                break
            if h >= target:
                px, why = target, "target"
                break
        else:
            if h >= stop:
                px, why = stop, "stop"
                break
            if l <= target:
                px, why = target, "target"
                break
        if use_tp1 and not partial and (h - e if long else e - l) >= g["tp1"] * risk:
            realized += exits.PARTIAL * g["tp1"] * risk  # نصف الصفقة أُغلق عند +tp1 R
            frac -= exits.PARTIAL
            partial = True
            stop = max(stop, e) if long else min(stop, e)  # الباقي بلا خسارة
        best = max(best, h) if long else min(best, l)
        gain = (best - e) * d
        if gain >= g["be"] * risk:
            trail = g["trail"] * atr * (0.67 if gain >= 2 * risk else 1.0)
            stop = max(stop, e, best - trail) if long else min(stop, e, best + trail)
        c = C[i]
        r_close = (c - e) * d / risk
        peak_r = max(peak_r, gain / risk)
        if use_gb and peak_r >= exits.GB_ARM_R and r_close < peak_r * (1 - g["gb"]):
            px, why = c, "giveback"
            break
        if use_pg and peak_r >= g["pg"] and r_close > exits.PG_FLOOR and i >= 1:
            bar = {"open": O[i], "close": c, "high": h, "low": l, "ema20": E20[i], "rsi": RS[i], "vol_z": VZ[i],
                   "atr": AT[i]}
            n, _ = exits.warnings(bar, {"rsi": RS[i - 1]}, sig.side)
            if n >= need_sig:
                px, why = c, "guard"
                break
    if why is None:
        i = len(H) - 1
        px, why = C[i], "timeout"
    gross = realized + frac * d * (px - e)
    if why == "stop" and abs(stop - stop0) > 1e-12:
        why = "breakeven" if partial and abs(stop - e) < 1e-12 else "trailing"
    return float((gross - 2 * fee * e) / risk), why, float(px), i + 1


def build_ctx(df: pd.DataFrame, htf: pd.DataFrame, flow=None):
    return {"df": df, "htf": htf, "regime": detect_regime(df), "smc": smc_mod.analyze(df), "flow": flow}


def pretrain(learner: Learner, symbol: str, h1: pd.DataFrame, h4: pd.DataFrame, step=1, darwin=None, learn=True):
    """
    دراسة تاريخ العملة: يمرّ على الشموع التاريخية كأنه يتداول لحظياً.
    learn=True : يسجّل النتائج في الذاكرة ويحدّث المتعلّم.
    darwin     : يختبر كل جينات داروين على نفس الإشارات التاريخية.
    """
    df = enrich(h1).dropna().reset_index(drop=True)
    hdf = enrich(h4).dropna().reset_index(drop=True)
    if len(df) < 320 or len(hdf) < 60:
        return 0
    cooldown = {s: -1 for s in STRATEGIES}
    n = 0
    for i in range(260, len(df) - 2, step):
        win = df.iloc[i - 250:i + 1]
        t = win["ts"].iloc[-1]
        hwin = hdf[hdf["ts"] <= t].iloc[-120:]
        if len(hwin) < 50:
            continue
        ctx = build_ctx(win, hwin)
        reg = ctx["regime"]["regime"]
        for sig in run_all(ctx):
            if i <= cooldown[sig.strategy]:
                continue
            cooldown[sig.strategy] = i + 6
            fut = df.iloc[i + 1:]
            atr = float(win["atr"].iloc[-1])
            genome = darwin.champion(sig.strategy) if darwin else None
            r, why, px, bars = simulate_trade(sig, fut, atr, genome=genome)
            if darwin:
                darwin.record(sig.strategy, {gid: simulate_trade(sig, fut, atr, genome=g)[0]
                                             for gid, g in darwin.genomes(sig.strategy)}, weight=0.5)
            n += 1
            if not learn:
                continue
            learner.update(sig.strategy, reg, r, weight=0.5, save=False)  # وزن أقل للتاريخ
            memory.save_trade({
                "symbol": symbol, "market": "history", "strategy": sig.strategy, "side": sig.side,
                "regime": reg, "features": features(ctx, sig), "reasons": sig.reasons,
                "entry": sig.entry, "stop": sig.stop, "target": sig.target, "qty": 0, "leverage": 1,
                "exit": px, "pnl": 0, "r_mult": r, "exit_reason": why,
                "opened": float(t) / 1000, "closed": float(t) / 1000 + bars * 3600,
                "mode": "pretrain", "shadow": 1})
    if learn:
        learner.save()
    if darwin:
        darwin.save()
    return n

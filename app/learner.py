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

log = logging.getLogger("learner")
DECAY = 0.997       # نسيان تدريجي => تعلم سريع من الأحدث
PRIOR = 3.0


class Learner:
    def __init__(self):
        self.arms = memory.kv_get("arms", {})
        self.lock = threading.RLock()

    def _arm(self, strategy, regime):
        return self.arms.setdefault(f"{strategy}|{regime}", {"w": 0.0, "l": 0.0, "r": 0.0, "n": 0.0})

    def update(self, strategy, regime, r_mult, weight=1.0, save=True):
        with self.lock:
            self._update(strategy, regime, r_mult, weight, save)

    def _update(self, strategy, regime, r_mult, weight, save):
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
def simulate_trade(sig, future: pd.DataFrame, atr: float, max_bars=48, fee=None):
    """
    يحاكي صفقة على الشموع اللاحقة مع وقف خسارة متحرك:
    عند +1R ينتقل الوقف للتعادل، ثم يتبع السعر بمسافة 1.5×ATR.
    يرجع (R-multiple صافي, سبب الخروج, سعر الخروج, عدد الشموع).
    """
    fee = settings.fee_rate if fee is None else fee
    risk = abs(sig.entry - sig.stop)
    if risk <= 0 or future.empty:
        return 0.0, "none", sig.entry, 0
    stop, long = sig.stop, sig.side == "long"
    best = sig.entry
    for i, (_, b) in enumerate(future.iloc[:max_bars].iterrows()):
        if long:
            if b["low"] <= stop:
                px, why = stop, "stop"
                break
            if b["high"] >= sig.target:
                px, why = sig.target, "target"
                break
            best = max(best, b["high"])
            if best - sig.entry >= risk:
                stop = max(stop, sig.entry, best - 1.5 * atr)
        else:
            if b["high"] >= stop:
                px, why = stop, "stop"
                break
            if b["low"] <= sig.target:
                px, why = sig.target, "target"
                break
            best = min(best, b["low"])
            if sig.entry - best >= risk:
                stop = min(stop, sig.entry, best + 1.5 * atr)
    else:
        px, why = future["close"].iloc[min(max_bars, len(future)) - 1], "timeout"
        i = min(max_bars, len(future)) - 1
    gross = (px - sig.entry) if long else (sig.entry - px)
    fees = 2 * fee * sig.entry
    if why == "stop" and abs(stop - sig.stop) > 1e-12:
        why = "trailing"
    return (gross - fees) / risk, why, float(px), i + 1


def build_ctx(df: pd.DataFrame, htf: pd.DataFrame, flow=None):
    return {"df": df, "htf": htf, "regime": detect_regime(df), "smc": smc_mod.analyze(df), "flow": flow}


def pretrain(learner: Learner, symbol: str, h1: pd.DataFrame, h4: pd.DataFrame, step=1):
    """
    دراسة تاريخ العملة: يمرّ على الشموع التاريخية كأنه يتداول لحظياً،
    ويسجّل نتيجة كل إشارة في الذاكرة ويحدّث المتعلّم.
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
            r, why, px, bars = simulate_trade(sig, df.iloc[i + 1:], float(win["atr"].iloc[-1]))
            learner.update(sig.strategy, reg, r, weight=0.5, save=False)  # وزن أقل للتاريخ
            memory.save_trade({
                "symbol": symbol, "market": "history", "strategy": sig.strategy, "side": sig.side,
                "regime": reg, "features": features(ctx, sig), "reasons": sig.reasons,
                "entry": sig.entry, "stop": sig.stop, "target": sig.target, "qty": 0, "leverage": 1,
                "exit": px, "pnl": 0, "r_mult": r, "exit_reason": why,
                "opened": float(t) / 1000, "closed": float(t) / 1000 + bars * 3600,
                "mode": "pretrain", "shadow": 1})
            n += 1
    learner.save()
    return n

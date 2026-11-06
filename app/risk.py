"""
إدارة المخاطر الديناميكية + مفتاح الطوارئ.
"""
import math
import time
from datetime import datetime, timezone

from .config import settings
from . import memory


class RiskManager:
    def __init__(self):
        st = memory.kv_get("risk", {})
        self.loss_streak = st.get("loss_streak", 0)
        self.day = st.get("day", "")
        self.day_start_equity = st.get("day_start_equity", 0.0)
        self.frozen = st.get("frozen", False)
        self.frozen_reason = st.get("frozen_reason", "")
        self.manual_freeze = st.get("manual_freeze", False)
        self.peak_equity = st.get("peak_equity", 0.0)
        self.daily_dd_now = 0.0

    def save(self):
        memory.kv_set("risk", self.__dict__)

    # --------------------------------------------------------------- حجم الصفقة
    def risk_pct(self, confidence: float) -> float:
        if self.loss_streak >= settings.loss_streak_trigger:
            return settings.risk_reduced
        lo, hi = settings.min_confidence, 0.85
        x = max(0.0, min(1.0, (confidence - lo) / (hi - lo)))
        return settings.risk_min + x * (settings.risk_max - settings.risk_min)

    def size(self, equity, entry, stop, confidence, atr_pct, open_notional, slots_left, meme=False, mult=1.0):
        """mult: مضاعف الحجم من الأدلة (أداء الاستراتيجية) والارتباط مع الصفقات المفتوحة."""
        """
        يحدد الكمية والرافعة والسوق (spot/futures).
        الرافعة يقررها الوكيل حسب الثقة والتذبذب، بسقف صارم MAX_LEVERAGE.
        """
        meme_k = settings.meme_risk_mult if meme else 1.0
        base = self.risk_pct(confidence)
        if self.loss_streak >= settings.loss_streak_trigger:
            mult = min(mult, 1.0)  # بعد سلسلة خسائر: لا تكبير مهما كانت الأدلة
        rp = min(base * mult, settings.risk_max) * meme_k
        risk_usd = equity * rp
        dist = abs(entry - stop)
        if dist <= 0 or equity <= 0:
            return None
        qty = risk_usd / dist
        notional = qty * entry

        # سقف الرافعة حسب الثقة والتذبذب: تذبذب أعلى => رافعة أقل
        conf_cap = 1 + (settings.max_leverage - 1) * max(0, (confidence - settings.min_confidence) / 0.3)
        vol_cap = max(1.0, 0.02 / max(atr_pct, 1e-4) * 2)
        lev_cap = int(max(1, min(settings.max_leverage, conf_cap, vol_cap)))
        if meme:
            lev_cap = min(lev_cap, settings.meme_max_leverage)

        margin_budget = equity / max(1, slots_left + 0) * 0.95
        lev = max(1, math.ceil(notional / max(margin_budget, 1e-9)))
        if lev > lev_cap:  # لا نتجاوز السقف: نصغّر الكمية
            lev = lev_cap
            notional = margin_budget * lev
            qty = notional / entry

        # سقف التعرض الكلي
        room = settings.max_total_exposure * equity - open_notional
        if room <= 0:
            return None
        if notional > room:
            notional = room
            qty = notional / entry
        return {"qty": qty, "notional": notional, "leverage": int(lev), "risk_pct": rp,
                "risk_usd": qty * dist, "margin": notional / lev}

    # --------------------------------------------------------------- الطوارئ
    def on_equity(self, equity) -> str | None:
        """يُستدعى كل دورة. يرجع سبب التجميد إذا لزم."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if today != self.day:
            self.day, self.day_start_equity = today, equity
            if self.frozen and not self.manual_freeze:  # يوم جديد => استئناف تلقائي
                self.frozen, self.frozen_reason = False, ""
                memory.log_event("☀️ يوم جديد: رفع التجميد التلقائي واستئناف التداول")
        self.peak_equity = max(self.peak_equity, equity)
        dd = 1 - equity / self.day_start_equity if self.day_start_equity else 0
        self.daily_dd_now = dd
        self.save()
        if not self.frozen and dd >= settings.max_daily_drawdown:
            return f"التراجع اليومي {dd:.1%} تجاوز الحد {settings.max_daily_drawdown:.0%}"
        return None

    def daily_dd(self, equity):
        return 1 - equity / self.day_start_equity if self.day_start_equity else 0.0

    def freeze(self, reason, manual=False):
        self.frozen, self.frozen_reason, self.manual_freeze = True, reason, manual
        self.save()

    def unfreeze(self):
        self.frozen, self.frozen_reason, self.manual_freeze = False, "", False
        self.save()

    def on_close(self, r_mult):
        self.loss_streak = self.loss_streak + 1 if r_mult <= 0 else 0
        self.save()

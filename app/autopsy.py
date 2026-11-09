"""
🔬 تشريح الخسائر: بعد كل صفقة خاسرة ينتظر الوكيل 8 ساعات، ثم ينظر ماذا فعل السعر بعدها ويصنّف السبب:

  📉 أرجع الربح        وصلت +1R أو أكثر ثم انتهت خاسرة.
  📏 وقف ضيق جداً      ضرب الوقف ثم ذهب السعر للهدف دون أن يبتعد كثيراً عن الوقف => الفكرة صحيحة، الوقف قريب.
  ⏱️ دخول مبكر         ضرب الوقف، نزل أكثر، ثم ذهب للهدف => الفكرة صحيحة، التوقيت مبكر.
  🧭 عكس الاتجاه الأكبر  الصفقة عاكست اتجاه إطار 4 ساعات.
  💥 اختراق كاذب       استراتيجية اختراق خسرت خلال ساعة.
  🎲 خسارة طبيعية      لا خطأ واضح — جزء طبيعي من التداول.

العلاج التلقائي (لكل استراتيجية، عند تكرار نفس الخطأ ≥40% من 12 خسارة على الأقل خلال 30 يوماً):
  وقف ضيق      => حد أدنى لمسافة الوقف 1.2×ATR (الحجم يصغر تلقائياً فتبقى المخاطرة نفسها).
  عكس الاتجاه  => لا دخول لهذه الاستراتيجية عكس اتجاه 4 ساعات.
  اختراق كاذب  => يشترط حجم تداول أعلى من المعتاد للدخول.
  كل علاج يُراجع بعد 21 يوماً ويُلغى إن زال سببه.
"""
import time

from . import memory

CATS = {"gave_back": "📉 أرجع الربح", "tight_stop": "📏 وقف ضيق جداً", "early_entry": "⏱️ دخول مبكر",
        "against_trend": "🧭 عكس الاتجاه الأكبر", "fake_breakout": "💥 اختراق كاذب", "normal": "🎲 خسارة طبيعية"}
FIXES = {"tight_stop": "📏 وقف أوسع (1.2×ATR كحد أدنى) بنفس المخاطرة",
         "against_trend": "🧭 لا دخول عكس اتجاه 4 ساعات",
         "fake_breakout": "💥 يشترط حجم تداول قوي للاختراق"}
BREAKOUTS = {"momentum_breakout", "squeeze_breakout", "session_breakout", "donchian_turtle", "breakout_retest"}
WAIT = 8 * 3600
FIX_DAYS = 21


def classify(t, bars):
    """t: صفقة خاسرة (entry, stop=الوقف الأصلي, target, side, meta, opened, closed, exit_reason, strategy).
    bars: شموع 15د بعد الإغلاق (high, low) — يمكن أن تكون فارغة."""
    d = 1 if t["side"] == "long" else -1
    risk = abs(t["entry"] - t["stop"]) or 1e-12
    meta = t.get("meta") or {}
    if (meta.get("mfe") or 0) >= 1.0:
        return "gave_back"
    if t.get("exit_reason") in ("stop", "exchange_stop") and bars is not None and len(bars):
        tgt = t["entry"] + d * risk * 1.0  # يكفي بلوغ +1R لإثبات أن الفكرة كانت صحيحة
        worst = 0.0
        for _, b in bars.iterrows():
            adverse = ((t["stop"] - b["low"]) if d == 1 else (b["high"] - t["stop"])) / risk
            worst = max(worst, adverse)
            if worst >= 1.5:
                break
            if (b["high"] >= tgt) if d == 1 else (b["low"] <= tgt):
                return "tight_stop" if worst < 0.5 else "early_entry"
    if meta.get("mtf") == -1:
        return "against_trend"
    if t["strategy"] in BREAKOUTS and t.get("closed", 0) - t.get("opened", 0) <= 3600:
        return "fake_breakout"
    return "normal"


class Autopsy:
    def __init__(self):
        st = memory.kv_get("autopsy", {}) or {}
        self.queue = st.get("queue", [])
        self.done = st.get("done", [])
        self.fixes = st.get("fixes", {})   # strategy -> {kind: until_ts}

    def save(self):
        memory.kv_set("autopsy", {"queue": self.queue[-300:], "done": self.done[-600:], "fixes": self.fixes})

    def add(self, t, src):
        if (t.get("r") or 0) > 0:
            return
        keep = ("symbol", "side", "entry", "stop", "target", "strategy", "opened", "closed", "exit_reason", "r", "meta")
        self.queue.append({**{k: t.get(k) for k in keep}, "src": src})
        if len(self.queue) > 300:  # المستكشف يخسر كثيراً: نحتفظ بالأحدث
            self.queue = self.queue[-300:]

    def due(self, n=3):
        now = time.time()
        return [q for q in self.queue if now - (q.get("closed") or now) >= WAIT][:n]

    def resolve(self, q, bars):
        cat = classify(q, bars)
        self.queue.remove(q)
        self.done.append({"ts": q["closed"], "symbol": q["symbol"], "strategy": q["strategy"], "src": q["src"],
                          "cat": cat, "r": q.get("r")})
        return cat

    def review(self):
        """يراجع العلاجات: يفعّل ما تكرر سببه، ويلغي ما انتهت مدته وزال سببه. يرجع رسائل للسجل."""
        now = time.time()
        msgs = []
        recent = [x for x in self.done if x["ts"] >= now - 30 * 86400]
        by = {}
        for x in recent:
            by.setdefault(x["strategy"], []).append(x["cat"])
        for strat, cats in by.items():
            if len(cats) < 12:
                continue
            for kind in FIXES:
                share = cats.count(kind) / len(cats)
                cur = self.fixes.setdefault(strat, {})
                if share >= 0.4 and cur.get(kind, 0) < now:
                    cur[kind] = now + FIX_DAYS * 86400
                    msgs.append((strat, kind, share, True))
        for strat, kinds in list(self.fixes.items()):
            for kind, until in list(kinds.items()):
                if until < now:
                    kinds.pop(kind)
                    msgs.append((strat, kind, 0, False))
        return msgs

    def has_fix(self, strategy, kind):
        return self.fixes.get(strategy, {}).get(kind, 0) > time.time()

    def summary(self):
        now = time.time()
        week = [x for x in self.done if x["ts"] >= now - 7 * 86400]
        cnt = {}
        for x in week:
            cnt[x["cat"]] = cnt.get(x["cat"], 0) + 1
        fixable = {k: v for k, v in cnt.items() if k != "normal"}
        top = max(fixable, key=fixable.get) if fixable else None
        return {"week": len(week), "counts": [{"cat": k, "name": CATS[k], "n": v} for k, v in
                                               sorted(cnt.items(), key=lambda x: -x[1])],
                "top": CATS[top] if top else None, "pending": len(self.queue),
                "fixes": [{"strategy": s, "kind": k, "name": FIXES[k], "until": u}
                          for s, ks in self.fixes.items() for k, u in ks.items() if u > now]}

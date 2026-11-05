"""
داروين: تطوّر الاستراتيجيات ذاتياً.
لكل استراتيجية "بطل" (الجينات المستخدمة فعلياً) + 5 "منافسين" (نسخ معدّلة).
كل إشارة تُختبر بجميع الجينات على نفس الشموع اللاحقة (مقارنة عادلة)، والأقوى يُرقّى.

الجينات (تتحكم بإدارة الصفقة، وليس بقرار الدخول):
  stop  : مضاعف مسافة وقف الخسارة        (0.7 – 1.6)
  rr    : مضاعف بُعد الهدف                (0.6 – 1.8)
  trail : مسافة الوقف المتحرك × ATR       (0.8 – 3.0)
  be    : الربح (بالـR) الذي يبدأ عنده التتبع (0.5 – 1.8)

الحماية من "حفظ الماضي": الترقية تتطلب 40 مقارنة على الأقل، وتفوقاً واضحاً (+0.08R)،
وترقية واحدة كحد أقصى لكل استراتيجية كل 24 ساعة.
"""
import math
import random
import threading
import time

from . import memory

DEFAULT = {"stop": 1.0, "rr": 1.0, "trail": 1.5, "be": 1.0}
BOUNDS = {"stop": (0.7, 1.6), "rr": (0.6, 1.8), "trail": (0.8, 3.0), "be": (0.5, 1.8)}
GENE_AR = {"stop": "مسافة الوقف", "rr": "بُعد الهدف", "trail": "الوقف المتحرك", "be": "بدء التتبع"}
N_CHALLENGERS = 5
MIN_SAMPLES = 40
MARGIN = 0.08
COOLDOWN = 24 * 3600
DECAY = 0.995


def mutate(genes, scale=0.15):
    out = {}
    for k, v in genes.items():
        lo, hi = BOUNDS[k]
        out[k] = round(min(hi, max(lo, v * math.exp(random.gauss(0, scale)))), 2)
    return out


def _new(genes, gen):
    return {"id": f"g{random.randint(10**5, 10**6 - 1)}", "genes": genes, "n": 0.0, "w": 0.0, "r": 0.0,
            "raw_n": 0, "gen": gen, "born": time.time()}


class Darwin:
    def __init__(self, strategies):
        self.lock = threading.RLock()
        self.state = memory.kv_get("darwin", {}) or {}
        self.history = memory.kv_get("darwin_history", []) or []
        for s in strategies:
            if s not in self.state:
                champ = _new(dict(DEFAULT), 0)
                self.state[s] = {"champion": champ, "generation": 0, "last_promo": 0,
                                 "challengers": [_new(mutate(DEFAULT, 0.25), 1) for _ in range(N_CHALLENGERS)]}
        self.events = []  # رسائل للوكيل (ترقيات)
        self._dirty = 0

    def champion(self, strategy):
        with self.lock:
            st = self.state.get(strategy)
            return dict(st["champion"]["genes"]) if st else dict(DEFAULT)

    def genomes(self, strategy):
        """[(id, genes)] البطل أولاً."""
        with self.lock:
            st = self.state[strategy]
            return [(g["id"], dict(g["genes"])) for g in [st["champion"]] + st["challengers"]]

    @staticmethod
    def _avg(g):
        return g["r"] / g["n"] if g["n"] > 0 else 0.0

    def record(self, strategy, results: dict, weight=1.0):
        """results: {genome_id: R} لنفس الإشارة."""
        with self.lock:
            st = self.state.get(strategy)
            if not st:
                return
            for g in [st["champion"]] + st["challengers"]:
                if g["id"] in results:
                    g["n"] = g["n"] * DECAY + weight
                    g["r"] = g["r"] * DECAY + results[g["id"]] * weight
                    g["w"] = g["w"] * DECAY + (weight if results[g["id"]] > 0 else 0)
                    g["raw_n"] += 1
            self._evolve(strategy)
            self._dirty += 1
            if self._dirty >= 20:
                self.save()

    def _evolve(self, strategy):
        st = self.state[strategy]
        ch = st["champion"]
        ready = [c for c in st["challengers"] if c["raw_n"] >= MIN_SAMPLES]
        if not ready:
            return
        best = max(ready, key=self._avg)
        now = time.time()
        if (ch["raw_n"] >= MIN_SAMPLES and self._avg(best) > self._avg(ch) + MARGIN
                and now - st["last_promo"] > COOLDOWN):
            st["generation"] += 1
            st["challengers"].remove(best)
            st["challengers"].append(ch)  # البطل السابق يبقى منافساً
            st["champion"] = best
            st["last_promo"] = now
            msg = {"ts": now, "strategy": strategy, "generation": st["generation"],
                   "old": ch["genes"], "new": best["genes"],
                   "old_r": round(self._avg(ch), 3), "new_r": round(self._avg(best), 3)}
            self.history.insert(0, msg)
            self.history = self.history[:30]
            self.events.append(msg)
        # استبعاد الضعفاء واستبدالهم بطفرات من البطل الحالي
        champ_avg = self._avg(st["champion"])
        for c in list(st["challengers"]):
            if c["raw_n"] >= MIN_SAMPLES and self._avg(c) < champ_avg - 0.15:
                st["challengers"].remove(c)
                st["challengers"].append(_new(mutate(st["champion"]["genes"]), st["generation"] + 1))
        while len(st["challengers"]) > N_CHALLENGERS:
            worst = min(st["challengers"], key=self._avg)
            st["challengers"].remove(worst)

    def save(self):
        with self.lock:
            memory.kv_set("darwin", self.state)
            memory.kv_set("darwin_history", self.history)
            self._dirty = 0

    def pop_events(self):
        with self.lock:
            ev, self.events = self.events, []
            return ev

    def summary(self):
        with self.lock:
            out = {}
            for s, st in self.state.items():
                ch = st["champion"]
                out[s] = {
                    "generation": st["generation"],
                    "champion": {"genes": ch["genes"], "avg_r": round(self._avg(ch), 3), "samples": ch["raw_n"],
                                 "winrate": ch["w"] / ch["n"] if ch["n"] else 0},
                    "challengers": sorted([{"genes": c["genes"], "avg_r": round(self._avg(c), 3),
                                            "samples": c["raw_n"], "gen": c["gen"]} for c in st["challengers"]],
                                          key=lambda x: -x["avg_r"]),
                }
            return {"strategies": out, "history": self.history[:10], "default": DEFAULT, "bounds": BOUNDS,
                    "min_samples": MIN_SAMPLES}

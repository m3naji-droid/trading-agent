"""
داروين: تطوّر الاستراتيجيات ذاتياً.
لكل استراتيجية "بطل" (الجينات المستخدمة فعلياً) + 5 "منافسين" (نسخ معدّلة).
كل إشارة تُختبر بجميع الجينات على نفس الشموع اللاحقة (مقارنة عادلة)، والأقوى يُرقّى.

الجينات (تتحكم بإدارة الصفقة، وليس بقرار الدخول):
  stop  : مضاعف مسافة وقف الخسارة        (0.7 – 1.6)
  rr    : مضاعف بُعد الهدف                (0.6 – 1.8)
  trail : مسافة الوقف المتحرك × ATR       (0.8 – 3.0)
  be    : الربح (بالـR) الذي يبدأ عنده التتبع (0.5 – 1.8)

جينات شروط الدخول (v6) — تقرر أي الإشارات تستحق الدخول أصلاً:
  vz    : أقل قوة حجم مطلوبة (z-score)
  adx   : أقل قوة اتجاه مطلوبة
  rsi   : أقصى تمدد RSI في اتجاه الصفقة (يمنع مطاردة حركة ممتدة)
  minrr : أقل عائد/مخاطرة مقبول

الحماية من "حفظ الماضي": الترقية تتطلب 40 مقارنة على الأقل، وتفوقاً واضحاً (+0.08R)،
وترقية واحدة كحد أقصى لكل استراتيجية كل 24 ساعة، ويجب أن يقبل الجينوم 30% من الفرص على الأقل
(حتى لا "يربح" بالامتناع عن التداول كلياً).
"""
import math
import random
import threading
import time

from . import memory

DEFAULT = {"stop": 1.0, "rr": 1.0, "trail": 1.5, "be": 1.0, "vz": -3.0, "adx": 0.0, "rsi": 100.0, "minrr": 1.2}
BOUNDS = {"stop": (0.7, 1.6), "rr": (0.6, 1.8), "trail": (0.8, 3.0), "be": (0.5, 1.8),
          "vz": (-3.0, 2.0), "adx": (0.0, 35.0), "rsi": (55.0, 100.0), "minrr": (1.2, 3.0)}
GENE_AR = {"stop": "مسافة الوقف", "rr": "بُعد الهدف", "trail": "الوقف المتحرك", "be": "بدء التتبع",
           "vz": "أقل حجم", "adx": "أقل قوة اتجاه", "rsi": "أقصى تمدد RSI", "minrr": "أقل عائد/مخاطرة"}
EXIT_GENES = ("stop", "rr", "trail", "be")
ENTRY_GENES = ("vz", "adx", "rsi", "minrr")
MIN_TAKE = 0.3


def accepts(genes, m) -> bool:
    """هل يقبل هذا الجينوم الإشارة؟ m من strategies.metrics."""
    if not m:
        return True
    return (m["vz"] >= genes.get("vz", -3) and m["adx"] >= genes.get("adx", 0)
            and m["rsi_dir"] <= genes.get("rsi", 100) and m["rr"] >= genes.get("minrr", 1.2))
N_CHALLENGERS = 5
MIN_SAMPLES = 40
MARGIN = 0.08
COOLDOWN = 24 * 3600
DECAY = 0.995


def mutate(genes, scale=0.15):
    out = {}
    for k in DEFAULT:
        lo, hi = BOUNDS[k]
        v = genes.get(k, DEFAULT[k])
        if random.random() < 0.7:  # ليس كل جين يتغير في كل طفرة
            v = v + random.gauss(0, scale * (hi - lo) * 0.5)
        out[k] = round(min(hi, max(lo, v)), 2)
    return out


def _new(genes, gen):
    return {"id": f"g{random.randint(10**5, 10**6 - 1)}", "genes": genes, "n": 0.0, "w": 0.0, "r": 0.0,
            "t": 0.0, "raw_n": 0, "gen": gen, "born": time.time()}


class Darwin:
    def __init__(self, strategies):
        self.lock = threading.RLock()
        self.state = memory.kv_get("darwin", {}) or {}
        self.history = memory.kv_get("darwin_history", []) or []
        for st in self.state.values():  # ترقية الحالة القديمة: إضافة جينات الدخول
            for g in [st["champion"]] + st["challengers"]:
                for k, v in DEFAULT.items():
                    g["genes"].setdefault(k, v)
                g.setdefault("t", g.get("n", 0.0))
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
    def _take(g):
        return g.get("t", g["n"]) / g["n"] if g["n"] > 0 else 1.0

    @staticmethod
    def _avg(g):
        return g["r"] / g["n"] if g["n"] > 0 else 0.0

    def evaluate(self, strategy, sig, m, fut, atr, sim):
        """يختبر كل الجينات على نفس الإشارة. يرجع {id: (R, قَبِلها؟)}. الرفض = 0R."""
        out = {}
        for gid, g in self.genomes(strategy):
            if accepts(g, m):
                out[gid] = (sim(sig, fut, atr, genome=g)[0], True)
            else:
                out[gid] = (0.0, False)
        return out

    def record(self, strategy, results: dict, weight=1.0):
        """results: {genome_id: R أو (R, taken)} لنفس الإشارة."""
        with self.lock:
            st = self.state.get(strategy)
            if not st:
                return
            for g in [st["champion"]] + st["challengers"]:
                if g["id"] in results:
                    v = results[g["id"]]
                    r, taken = v if isinstance(v, tuple) else (v, True)
                    g["n"] = g["n"] * DECAY + weight
                    g["r"] = g["r"] * DECAY + r * weight
                    g["t"] = g.get("t", 0.0) * DECAY + (weight if taken else 0)
                    g["w"] = g["w"] * DECAY + (weight if (taken and r > 0) else 0)
                    g["raw_n"] += 1
            self._evolve(strategy)
            self._dirty += 1
            if self._dirty >= 20:
                self.save()

    def _evolve(self, strategy):
        st = self.state[strategy]
        ch = st["champion"]
        ready = [c for c in st["challengers"] if c["raw_n"] >= MIN_SAMPLES and self._take(c) >= MIN_TAKE]
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
                                 "winrate": ch["w"] / ch["t"] if ch.get("t") else 0, "take": round(self._take(ch), 2)},
                    "challengers": sorted([{"genes": c["genes"], "avg_r": round(self._avg(c), 3),
                                            "samples": c["raw_n"], "gen": c["gen"]} for c in st["challengers"]],
                                          key=lambda x: -x["avg_r"]),
                }
            return {"strategies": out, "history": self.history[:10], "default": DEFAULT, "bounds": BOUNDS,
                    "exit_genes": EXIT_GENES, "entry_genes": ENTRY_GENES,
                    "min_samples": MIN_SAMPLES}

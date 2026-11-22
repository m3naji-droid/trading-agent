"""
🎯 أدوات الدقة — كلها تتعلم من نتائج الوكيل الفعلية، ولا تؤثر إلا بعد أن تثبت فائدتها:

  🧠 الحَكَم الذكي     نموذج تعلّم آلي (أشجار معززة، مكتوب بـ numpy ليناسب السيرفر الصغير)
                       يدرس آلاف الصفقات (التاريخية + المستكشف + الظل + الحقيقية) ويحكم على كل فرصة جديدة.
                       لا يُستخدم إلا إذا نجح في اختبار مخفي على صفقات لم يرها (AUC ≥ 0.55).
  🎯 فحص صدق الثقة     هل الثقة الأعلى تعني فعلاً ربحاً أكثر؟ إن لم تكن، يعيد الوكيل ترتيب ثقته.
  🤝 قوة التوافق       اتفاق عدة استراتيجيات على نفس العملة والاتجاه => حجم أكبر قليلاً (بالأدلة).
  🕐 أفضل الأوقات      أداء كل جلسة (آسيا/أوروبا/أمريكا/آخر الليل/عطلة الأسبوع).
  🚨 الإنذار المبكر    CUSUM: يكتشف تدهور استراتيجية بعد خسائر قليلة ويوقفها 48 ساعة للمال الحقيقي.
"""
import json
import logging
import math
import threading
import time
from datetime import datetime, timezone

import numpy as np

from . import memory
from .strategies import STRATEGY_AR

log = logging.getLogger("precision")

SESSIONS = {"asia": "🌏 آسيا", "europe": "🇪🇺 أوروبا", "us": "🇺🇸 أمريكا", "late": "🌙 آخر الليل", "weekend": "🏖️ عطلة الأسبوع"}
STRATS = ['momentum_breakout', 'trend_pullback', 'mean_reversion', 'smc_reversal', 'liquidation_hunt', 'funding_extreme',
          'btc_shadow', 'rs_rotation', 'squeeze_breakout', 'mtf_confluence', 'divergence', 'session_breakout',
          'donchian_turtle', 'rsi2_pullback', 'vwap_reclaim', 'supertrend_adx', 'level_retest', 'breakout_retest']
META_KEYS = ["cons", "conflict", "mtf", "top", "crowd", "top_chg", "magnet", "macro", "rot", "smart", "fng"]
N_FEATS = 21


def session_of(ts):
    d = datetime.fromtimestamp(ts, timezone.utc)
    if d.weekday() >= 5:
        return "weekend"
    h = d.hour
    return "asia" if h < 7 else "europe" if h < 13 else "us" if h < 21 else "late"


def row_vector(feats, meta, strategy, side, rr, ts, source):
    f = list(feats or [])[:N_FEATS]
    f += [0.0] * (N_FEATS - len(f))
    meta = meta or {}
    h = datetime.fromtimestamp(ts, timezone.utc).hour if ts else 0
    v = f + [min(6.0, max(0.0, rr or 0.0)), 1.0 if side == "long" else -1.0,
             math.sin(2 * math.pi * h / 24), math.cos(2 * math.pi * h / 24),
             1.0 if ts and datetime.fromtimestamp(ts, timezone.utc).weekday() >= 5 else 0.0,
             1.0 if source == "history" else 0.0, 1.0 if meta else 0.0]
    v += [1.0 if strategy == s else 0.0 for s in STRATS]
    for k in META_KEYS:
        x = meta.get(k)
        v.append(float(x) if isinstance(x, (int, float)) and x == x else 0.0)
    return v


FEATURE_AR = (["اتجاه صاعد", "اتجاه هابط", "سوق عرضي", "خطورة", "اختراق", "ارتداد", "عودة للمتوسط", "SMC", "الاتجاه شراء/بيع",
               "قوة الاتجاه ADX", "RSI", "التذبذب ATR", "عرض البولنجر", "حجم التداول", "البعد عن المتوسط", "دفتر الأوامر",
               "الحيتان", "التمويل", "كسر الهيكل", "مرحلة السوق", "—"][:N_FEATS]
              + ["العائد/المخاطرة", "الاتجاه", "الساعة", "الساعة", "عطلة الأسبوع", "بيانات تاريخية", "بيانات حديثة"]
              + [f"استراتيجية {STRATEGY_AR.get(s, s)}" for s in STRATS]
              + ["توافق الاستراتيجيات", "تعارض الإشارات", "اتجاه 4 ساعات", "مراكز الكبار", "مراكز الجمهور",
                 "تغير مراكز الكبار", "مغناطيس التصفية", "الاقتصاد العالمي", "دورة السيولة", "المال الكبير", "الخوف والطمع"])


# ===================================================================== أشجار معززة (numpy)
class GBM:
    """أشجار بعمق 2، تعزيز تدريجي بخسارة لوجستية. خفيف جداً على الذاكرة."""

    def __init__(self, rounds=120, lr=0.08, bins=16, lam=5.0, min_leaf=40):
        self.rounds, self.lr, self.bins, self.lam, self.min_leaf = rounds, lr, bins, lam, min_leaf
        self.trees, self.edges, self.base = [], [], 0.0
        self.gain = None

    def _bin(self, X):
        return np.stack([np.searchsorted(self.edges[j], X[:, j], side="right") for j in range(X.shape[1])], 1).astype(np.int16)

    def _best(self, B, g, h, idx):
        best = (0.0, None, None)
        G, H = g[idx].sum(), h[idx].sum()
        parent = G * G / (H + self.lam)
        for j in range(B.shape[1]):
            nb = len(self.edges[j]) + 1
            if nb < 2:
                continue
            b = B[idx, j]
            gs = np.bincount(b, g[idx], nb).cumsum()[:-1]
            hs = np.bincount(b, h[idx], nb).cumsum()[:-1]
            cs = np.bincount(b, None, nb).cumsum()[:-1]
            ok = (cs >= self.min_leaf) & (len(idx) - cs >= self.min_leaf)
            if not ok.any():
                continue
            gain = gs ** 2 / (hs + self.lam) + (G - gs) ** 2 / (H - hs + self.lam) - parent
            gain[~ok] = -1
            k = int(np.argmax(gain))
            if gain[k] > best[0]:
                best = (float(gain[k]), j, k)
        return best

    def fit(self, X, y, w=None):
        n, d = X.shape
        w = np.ones(n) if w is None else w
        self.edges = [np.unique(np.quantile(X[:, j], np.linspace(0, 1, self.bins + 1)[1:-1])) for j in range(d)]
        B = self._bin(X)
        p0 = np.clip(np.average(y, weights=w), 0.02, 0.98)
        self.base = math.log(p0 / (1 - p0))
        F = np.full(n, self.base)
        self.gain = np.zeros(d)
        allidx = np.arange(n)
        for _ in range(self.rounds):
            p = 1 / (1 + np.exp(-F))
            g, h = (p - y) * w, np.maximum(p * (1 - p), 1e-6) * w
            gain, j, k = self._best(B, g, h, allidx)
            if j is None:
                break
            self.gain[j] += gain
            left = allidx[B[:, j] <= k]
            right = allidx[B[:, j] > k]
            node = {"j": j, "k": k, "kids": []}
            for side in (left, right):
                g2, j2, k2 = self._best(B, g, h, side)
                if j2 is not None:
                    self.gain[j2] += g2
                    a, b_ = side[B[side, j2] <= k2], side[B[side, j2] > k2]
                    vals = [-g[s].sum() / (h[s].sum() + self.lam) for s in (a, b_)]
                    node["kids"].append({"j": j2, "k": k2, "v": vals})
                    F[a] += self.lr * vals[0]
                    F[b_] += self.lr * vals[1]
                else:
                    v = -g[side].sum() / (h[side].sum() + self.lam)
                    node["kids"].append({"j": None, "v": [v, v]})
                    F[side] += self.lr * v
            self.trees.append(node)
        return self

    def raw(self, X):
        B = self._bin(X)
        F = np.full(len(X), self.base)
        for t in self.trees:
            go_r = B[:, t["j"]] > t["k"]
            for side, mask in ((0, ~go_r), (1, go_r)):
                kid = t["kids"][side]
                if kid["j"] is None:
                    F[mask] += self.lr * kid["v"][0]
                else:
                    r2 = B[:, kid["j"]] > kid["k"]
                    F[mask & ~r2] += self.lr * kid["v"][0]
                    F[mask & r2] += self.lr * kid["v"][1]
        return F

    def predict(self, X):
        return 1 / (1 + np.exp(-self.raw(X)))

    def to_json(self):
        return {"trees": self.trees, "edges": [e.tolist() for e in self.edges], "base": self.base, "lr": self.lr,
                "gain": (self.gain.tolist() if self.gain is not None else None)}

    @classmethod
    def from_json(cls, d):
        m = cls()
        m.trees, m.base, m.lr = d["trees"], d["base"], d["lr"]
        m.edges = [np.array(e) for e in d["edges"]]
        m.gain = np.array(d["gain"]) if d.get("gain") else None
        return m


def auc(y, p):
    y = np.asarray(y)
    pos, neg = y.sum(), len(y) - y.sum()
    if pos == 0 or neg == 0:
        return 0.5
    r = np.empty(len(p))
    r[np.argsort(p, kind="mergesort")] = np.arange(1, len(p) + 1)
    return float((r[y == 1].sum() - pos * (pos + 1) / 2) / (pos * neg))


def _jsonable(o):
    if isinstance(o, dict):
        return {k: _jsonable(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_jsonable(v) for v in o]
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (np.floating,)):
        return float(o)
    return o


# ===================================================================== المنسّق
class Precision:
    MIN_TRAIN = 400
    MIN_AUC = 0.55
    CAL_MIN = 60
    BUCKET_MIN = 30

    def __init__(self):
        st = memory.kv_get("precision", {}) or {}
        self.model_info = st.get("model_info", {"status": "learning"})
        self.model = GBM.from_json(st["model"]) if st.get("model") else None
        self.cal = st.get("cal", {})
        self.cons = st.get("cons", {})
        self.sess = st.get("sess", {})
        self.drift = st.get("drift", {})       # strategy -> {s, n, until, mu0, alarms}
        self.drift_events = st.get("drift_events", [])
        self.mem = st.get("mem", {"on": True})
        self.last_train = st.get("last_train", 0)
        self.lock = threading.Lock()

    def save(self):
        with self.lock:
            memory.kv_set("precision", _jsonable({
                "model_info": self.model_info, "model": self.model.to_json() if self.model else None,
                "cal": self.cal, "cons": self.cons, "sess": self.sess, "drift": self.drift,
                "drift_events": self.drift_events[-30:], "last_train": self.last_train, "mem": self.mem}))

    # ----------------------------------------------------------------- البيانات
    @staticmethod
    def load_rows(limit=30000):
        with memory._conn() as c:
            rows = c.execute(
                "SELECT strategy, side, features, meta, entry, stop, target, r_mult, opened, closed, market FROM trades "
                "WHERE closed IS NOT NULL AND r_mult IS NOT NULL AND market NOT IN ('near','explore_near') "
                "ORDER BY opened DESC LIMIT ?", (limit,)).fetchall()
        out = []
        for r in rows:
            try:
                f = json.loads(r["features"] or "[]")
                meta = json.loads(r["meta"]) if r["meta"] else {}
            except Exception:
                continue
            if not f or r["strategy"] not in STRATS:
                continue
            risk = abs((r["entry"] or 0) - (r["stop"] or 0))
            rr = abs((r["target"] or 0) - (r["entry"] or 0)) / risk if risk else 0.0
            out.append({"strategy": r["strategy"], "side": r["side"], "f": f, "meta": meta, "rr": rr,
                        "r": float(r["r_mult"]), "ts": r["opened"] or r["closed"], "market": r["market"]})
        out.sort(key=lambda x: x["ts"])
        return out

    # ----------------------------------------------------------------- إعادة التدريب (كل 6 ساعات)
    def retrain(self):
        rows = self.load_rows()
        self._train_model(rows)
        live = [x for x in rows if x["market"] in ("explore", "spot", "futures")]
        self._calibrate([x for x in live if "conf" in x["meta"]])
        self._consensus([x for x in live if "cons" in x["meta"]])
        self._sessions([x for x in rows if x["market"] != "history"] or rows, rows)
        self._audit_memory([x for x in rows if x["market"] == "shadow" and "mem" in x["meta"]
                            and x["ts"] >= time.time() - 30 * 86400])
        self.last_train = time.time()
        self.save()

    def _train_model(self, rows):
        n = len(rows)
        if n < self.MIN_TRAIN:
            self.model_info = {"status": "learning", "n": n, "need": self.MIN_TRAIN}
            self.model = None
            return
        X = np.array([row_vector(x["f"], x["meta"], x["strategy"], x["side"], x["rr"], x["ts"], x["market"]) for x in rows])
        y = np.array([1.0 if x["r"] > 0 else 0.0 for x in rows])
        # الصفقات الحية أصدق من التاريخية: وزن أعلى
        w = np.array([1.0 if x["market"] == "history" else 2.0 for x in rows])
        cut = int(n * 0.75)
        leaf = max(40, n // 80)
        m = GBM(min_leaf=leaf).fit(X[:cut], y[:cut], w[:cut])
        p = m.predict(X[cut:])
        a = auc(y[cut:], p)
        br = float(y[:cut].mean())
        ll = float(-np.mean(y[cut:] * np.log(np.clip(p, 1e-6, 1)) + (1 - y[cut:]) * np.log(np.clip(1 - p, 1e-6, 1))))
        ll0 = float(-np.mean(y[cut:] * math.log(max(br, 1e-6)) + (1 - y[cut:]) * math.log(max(1 - br, 1e-6))))
        # أثر عملي: متوسط R للربع الأعلى ثقةً مقابل الربع الأدنى في الاختبار المخفي
        rt = np.array([x["r"] for x in rows[cut:]])
        q = np.quantile(p, [0.25, 0.75])
        top_r = float(rt[p >= q[1]].mean()) if (p >= q[1]).any() else 0.0
        bot_r = float(rt[p <= q[0]].mean()) if (p <= q[0]).any() else 0.0
        ok = a >= self.MIN_AUC and ll < ll0 and top_r > bot_r
        full = GBM(min_leaf=leaf).fit(X, y, w) if ok else None
        gain = (full or m).gain
        top = [FEATURE_AR[i] for i in np.argsort(-gain)[:5] if gain[i] > 0] if gain is not None else []
        top = list(dict.fromkeys(top))
        self.model = full
        self.model_info = {"status": "approved" if ok else "rejected", "n": n, "test_n": n - cut, "auc": a,
                           "base": float(y.mean()), "top_r": top_r, "bot_r": bot_r, "top": top, "ts": time.time()}
        log.info(f"metamodel n={n} auc={a:.3f} ok={ok}")

    def predict(self, feats, meta, strategy, side, rr, ts):
        """يرجع (احتمال الربح, تعديل الثقة, سبب) — أو (None, 0, '') قبل الاعتماد."""
        m = self.model
        if m is None or self.model_info.get("status") != "approved":
            return None, 0.0, ""
        try:
            p = float(m.predict(np.array([row_vector(feats, meta, strategy, side, rr, ts, "live")]))[0])
        except Exception as e:
            log.debug(f"predict: {e}")
            return None, 0.0, ""
        br = self.model_info.get("base", 0.5)
        adj = max(-0.08, min(0.08, 0.5 * (p - br)))
        if abs(adj) < 0.01:
            return p, 0.0, ""
        return p, adj, (f"🧠 الحَكَم الذكي: تشبه الصفقات الرابحة ({p:.0%})" if adj > 0
                        else f"🧠 الحَكَم الذكي: تشبه الصفقات الخاسرة ({p:.0%})")

    # ----------------------------------------------------------------- محاسبة فلتر الذاكرة
    def _audit_memory(self, rows):
        """هل الفرص التي رفضها فلتر الذاكرة كانت فعلاً أسوأ؟ (من الصفقات الظلية التي تُفتح للجميع)."""
        v = [x["r"] for x in rows if x["meta"]["mem"] == 0]
        a = [x["r"] for x in rows if x["meta"]["mem"] == 1]
        m = {"veto_n": len(v), "ok_n": len(a), "veto_r": float(np.mean(v)) if v else 0.0,
             "ok_r": float(np.mean(a)) if a else 0.0, "on": True}
        if len(v) >= 40 and len(a) >= 40 and m["veto_r"] >= m["ok_r"] - 0.05:
            m["on"] = False  # المرفوضة لم تكن أسوأ => الفلتر يضيّع فرصاً بلا فائدة
        self.mem = m

    def memory_veto_on(self):
        return (getattr(self, "mem", None) or {}).get("on", True)

    # ----------------------------------------------------------------- صدق الثقة
    def _calibrate(self, rows):
        if len(rows) < self.CAL_MIN:
            self.cal = {"status": "learning", "n": len(rows), "need": self.CAL_MIN}
            return
        c = np.array([x["meta"]["conf"] for x in rows])
        y = np.array([1.0 if x["r"] > 0 else 0.0 for x in rows])
        rr = np.array([x["r"] for x in rows])
        o = np.argsort(c)
        c, y, rr = c[o], y[o], rr[o]
        # PAV: انحدار متساوي الرتبة (الثقة الأعلى يجب ألا تعني ربحاً أقل)
        blocks = [[y[i], 1.0, c[i], c[i]] for i in range(len(y))]
        out = []
        for b in blocks:
            out.append(b)
            while len(out) > 1 and out[-2][0] / out[-2][1] > out[-1][0] / out[-1][1]:
                b2 = out.pop()
                out[-1] = [out[-1][0] + b2[0], out[-1][1] + b2[1], out[-1][2], b2[3]]
        xs = [((b[2] + b[3]) / 2) for b in out]
        ys = [b[0] / b[1] for b in out]
        # جدول العرض: 5 مجموعات متساوية العدد
        bins = []
        for part in np.array_split(np.arange(len(c)), 5):
            bins.append({"conf": float(c[part].mean()), "win": float(y[part].mean()), "r": float(rr[part].mean()), "n": int(len(part))})
        slope = float(np.polyfit(c, rr, 1)[0]) if np.ptp(c) > 1e-6 else 0.0
        self.cal = {"status": "ready", "n": len(rows), "xs": xs, "ys": ys, "mean_c": float(c.mean()),
                    "mean_w": float(y.mean()), "bins": bins, "honest": slope > 0,
                    "w": min(0.7, len(rows) / (len(rows) + 150))}

    def calibrate(self, conf):
        """يعيد ترتيب الثقة حسب ما تحقق فعلاً، مع الحفاظ على مستواها العام (حتى لا يتغير معنى حد الدخول)."""
        cal = self.cal
        if cal.get("status") != "ready":
            return conf, ""
        wr = float(np.interp(conf, cal["xs"], cal["ys"]))
        new = conf + cal["w"] * ((wr - cal["mean_w"]) - (conf - cal["mean_c"]))
        new = max(0.0, min(1.0, new))
        if abs(new - conf) < 0.01:
            return conf, ""
        return new, f"🎯 تصحيح الثقة حسب النتائج الفعلية: {conf:.0%} ← {new:.0%}"

    # ----------------------------------------------------------------- التوافق
    @staticmethod
    def cons_bucket(cons, conflict):
        if conflict >= 1:
            return "conflict"
        return "1" if cons < 1.5 else "2" if cons < 2.5 else "3+"

    PRIOR = {"1": 1.0, "2": 1.10, "3+": 1.15, "conflict": 0.85}

    def _consensus(self, rows):
        out = {}
        allr = np.mean([x["r"] for x in rows]) if rows else 0.0
        for k in self.PRIOR:
            rs = [x["r"] for x in rows if self.cons_bucket(x["meta"].get("cons", 1), x["meta"].get("conflict", 0)) == k]
            out[k] = {"n": len(rs), "avg_r": float(np.mean(rs)) if rs else 0.0}
            if len(rs) >= self.BUCKET_MIN:
                out[k]["mult"] = float(max(0.75, min(1.25, 1 + 0.6 * (out[k]["avg_r"] - allr))))
        self.cons = out

    def consensus_mult(self, cons, conflict):
        k = self.cons_bucket(cons, conflict)
        b = self.cons.get(k, {})
        m = b.get("mult", self.PRIOR[k])
        src = "مثبت" if "mult" in b else "مبدئي"
        if abs(m - 1) < 0.03:
            return 1.0, ""
        if k == "conflict":
            return m, f"🤝 إشارات متعارضة على نفس العملة — حجم ×{m:.2f} ({src})"
        return m, f"🤝 {int(cons) if cons < 3 else '3+'} استراتيجيات متفقة — حجم ×{m:.2f} ({src})"

    # ----------------------------------------------------------------- الجلسات
    def _sessions(self, rows, all_rows):
        if len(rows) < 200:
            rows = all_rows
        allr = np.array([x["r"] for x in rows]) if rows else np.zeros(0)
        mu = float(allr.mean()) if len(allr) else 0.0
        out = {}
        for k in SESSIONS:
            rs = np.array([x["r"] for x in rows if session_of(x["ts"]) == k])
            d = {"n": int(len(rs)), "avg_r": float(rs.mean()) if len(rs) else 0.0,
                 "win": float((rs > 0).mean()) if len(rs) else 0.0}
            if len(rs) >= 100:
                diff = d["avg_r"] - mu
                t = diff / (rs.std(ddof=1) / math.sqrt(len(rs)) + 1e-9)
                d["t"] = float(t)
                if abs(t) >= 2:
                    d["mult"] = float(max(0.7, min(1.15, 1 + diff)))
                    d["adj"] = float(max(-0.03, min(0.03, diff * 0.1)))
            out[k] = d
        self.sess = out

    def session_adj(self, ts=None):
        k = session_of(ts or time.time())
        d = self.sess.get(k, {})
        if "mult" not in d:
            return 1.0, 0.0, ""
        good = d["mult"] > 1
        return d["mult"], d["adj"], (f"🕐 {SESSIONS[k]}: وقت {'ناجح' if good else 'ضعيف'} تاريخياً للوكيل "
                                     f"({d['avg_r']:+.2f}R) — حجم ×{d['mult']:.2f}")

    # ----------------------------------------------------------------- الإنذار المبكر
    # معايَرة بمحاكاة: استراتيجية سليمة (+0.15R) إنذار خاطئ ~9% لكل 100 صفقة؛ متدهورة (-0.5R) تُكتشف بعد ~28 صفقة
    K, H, PAUSE = 0.3, 10.0, 48 * 3600

    def on_result(self, strategy, r, mu0=0.05):
        """CUSUM: يتراكم الانحراف السلبي عن المتوقع؛ تجاوز الحد = إنذار وإيقاف 48 ساعة للمال الحقيقي."""
        with self.lock:
            d = self.drift.setdefault(strategy, {"s": 0.0, "n": 0, "until": 0, "alarms": 0})
            if d["until"] and time.time() > d["until"]:
                d.update(s=0.0, until=0)
            r = max(-2.0, min(4.0, r))
            d["s"] = max(0.0, d["s"] + (mu0 - r - self.K))
            d["n"] += 1
            if d["s"] > self.H and not d["until"]:
                d["until"] = time.time() + self.PAUSE
                d["alarms"] += 1
                d["s"] = 0.0
                ev = {"ts": time.time(), "strategy": strategy}
                self.drift_events.append(ev)
                return ev
        return None

    def drift_blocked(self, strategy):
        d = self.drift.get(strategy)
        return bool(d and d.get("until", 0) > time.time())

    def summary(self):
        return {"mem": self.mem, "model": self.model_info, "cal": {k: v for k, v in self.cal.items() if k not in ("xs", "ys")},
                "cons": self.cons, "sess": self.sess, "sess_names": SESSIONS, "now_sess": session_of(time.time()),
                "drift": {k: {"s": round(v["s"], 2), "until": v["until"], "alarms": v["alarms"]}
                          for k, v in self.drift.items() if v.get("until", 0) > time.time() or v.get("s", 0) > self.H * 0.4},
                "drift_events": self.drift_events[-5:][::-1], "last_train": self.last_train, "h": self.H}

"""
⚖️ محكمة الأدلة + منسّق رادار الأموال الكبرى.

لكل مصدر:
  - يُسجَّل ما قالته إشارته كل 4 ساعات، ثم يُقارن بما فعله البيتكوين فعلاً بعدها (24س، أو أسبوع للتقارير الأسبوعية).
  - معامل الصدق (IC): الارتباط بين الإشارة والحركة اللاحقة.
  - لا يحصل المصدر على أي وزن قبل 30 مقارنة وثقة إحصائية كافية (|t| ≥ 2).
  - إن كان يتنبأ بالعكس باستمرار، يتعلم الوكيل قراءته بالعكس.
وللأحداث (إدراج، فتح عملات، إعلانات شركات): متوسط حركة العملة خلال 24س بعد الحدث مقارنةً بالبيتكوين.

قاعدة حماية ثابتة (لا تنتظر الأدلة): لا شراء لعملة قبل فتح كمية كبيرة منها خلال 48 ساعة.
"""
import logging
import math
import os
import time

import numpy as np

from . import memory
from .config import settings
from .smartmoney import SOURCES, _get, parse_farside, tanh

log = logging.getLogger("evidence")
SAMPLE_SEC = 4 * 3600
HORIZON = {"etf": 24, "premium": 24, "stables": 72, "whales": 24, "corporate": 72, "cot": 168,
           "polymarket": 24, "options": 24}
MIN_N, MIN_T, MAX_W = 30, 2.0, 0.6
EV_MIN_N = 8
EVENT_SOURCES = ("listings", "unlocks", "corporate")


class SmartMoney:
    def __init__(self):
        self.state = memory.kv_get("sm_state", {}) or {}
        self.obs = memory.kv_get("sm_obs", {}) or {}
        self.events = memory.kv_get("sm_events", []) or []
        self.btc_log = memory.kv_get("sm_btc", []) or []
        self.verdicts = memory.kv_get("sm_verdicts", {}) or {}
        self.sig = {}
        self._next = {}
        self._last_sample = memory.kv_get("sm_last_sample", 0) or 0
        self._last_eval = 0

    # -------------------------------------------------------------- الجلب
    def refresh(self, ctx):
        """ctx: btc_binance, radar_flow, names, tradable, prices."""
        st = self.state
        st["btc_binance"] = ctx.get("btc")
        st["radar_flow"] = ctx.get("radar_flow")
        st["names"] = ctx.get("names", {})
        st["tradable"] = ctx.get("tradable", set())
        st["whale_key"] = memory.kv_get("whale_key") or os.getenv("WHALE_ALERT_KEY", "")
        now = time.time()
        for key, icon, name, plain, fn, every in SOURCES:
            if now < self._next.get(key, 0):
                continue
            try:
                s, data, text = fn(st)
                self.sig[key] = {"s": float(max(-1, min(1, s))), "data": data, "text": text, "ts": now, "err": None}
                self._next[key] = now + every
                for e in (data or {}).get("events", []):
                    self._add_event(key, e, ctx.get("prices", {}))
            except Exception as e:
                prev = self.sig.get(key, {})
                self.sig[key] = {**prev, "err": str(e)[:100], "err_ts": now}
                self._next[key] = now + min(every, 1800)
                log.debug(f"{key}: {e}")
        if ctx.get("btc"):
            self._log_btc(ctx["btc"])
        self._sample(ctx)
        if now - self._last_eval > 3600:
            self._last_eval = now
            self.evaluate(ctx.get("prices", {}))
        memory.kv_set("sm_state", {k: v for k, v in st.items() if k not in ("tradable", "names", "radar_flow")})

    def _log_btc(self, p):
        h = int(time.time() // 3600) * 3600
        if not self.btc_log or self.btc_log[-1][0] != h:
            self.btc_log.append([h, p])
            self.btc_log = self.btc_log[-24 * 120:]
            memory.kv_set("sm_btc", self.btc_log)

    def _sample(self, ctx):
        now = time.time()
        if now - self._last_sample < SAMPLE_SEC or not ctx.get("btc"):
            return
        self._last_sample = now
        memory.kv_set("sm_last_sample", now)
        for key in HORIZON:
            g = self.sig.get(key)
            if not g or g.get("err") or now - g.get("ts", 0) > 2 * dict((k, e) for k, *_, e in SOURCES)[key] + 3600:
                continue
            self.obs.setdefault(key, []).append([now, g["s"], ctx["btc"]])
            self.obs[key] = self.obs[key][-600:]
        memory.kv_set("sm_obs", self.obs)

    def _add_event(self, source, e, prices):
        base = e.get("base")
        p = prices.get(f"{base}/USDT")
        if not p:
            return
        self.events.append({"source": source, "base": base, "ts": e.get("ts", time.time()), "title": e.get("title", ""),
                            "p0": p, "b0": prices.get("BTC/USDT"), "ret": None})
        self.events = self.events[-300:]
        memory.kv_set("sm_events", self.events)
        memory.log_event(f"🕵️ {e.get('title', '')}", "learn")

    # -------------------------------------------------------------- محكمة الأدلة
    def _btc_series(self):
        """أسعار البيتكوين كل ساعة: من ذاكرة الدراسة التاريخية + سجلنا."""
        ser = {}
        p = os.path.join(settings.data_dir, "hist", "BTC_1h.npy")
        if os.path.exists(p):
            try:
                a = np.load(p)
                for ts, c in zip(a[:, 0], a[:, 4]):
                    ser[int(ts // 1000 // 3600 * 3600)] = float(c)
            except Exception:
                pass
        for h, px in self.btc_log:
            ser[int(h)] = px
        return ser

    def evaluate(self, prices):
        ser = self._btc_series()
        now = time.time()

        def px_at(t):
            h = int(t // 3600 * 3600)
            for dh in (0, 3600, -3600, 7200, -7200):
                if h + dh in ser:
                    return ser[h + dh]
            return None
        for key, hz in HORIZON.items():
            pairs, tss = [], []
            for ts, s, p0 in sorted(self.obs.get(key, [])):
                if ts + hz * 3600 > now:
                    continue
                p1 = px_at(ts + hz * 3600)
                p0 = px_at(ts) or p0
                if p1 and p0:
                    pairs.append((s, p1 / p0 - 1))
                    tss.append(ts)
            # العينات المتداخلة (كل 4س بأفق 24س) ليست أدلة مستقلة: نحسب عدد الأدلة المستقلة فقط
            gap_h = (tss[-1] - tss[0]) / 3600 / max(1, len(tss) - 1) if len(tss) > 1 else hz
            overlap = min(1.0, gap_h / hz)
            v = {"n": len(pairs), "need": MIN_N, "ic": None, "t": None, "w": 0.0, "status": "learning", "n_eff": 0}
            if len(pairs) >= 10:
                x = np.array([a for a, _ in pairs])
                y = np.array([b for _, b in pairs])
                if x.std() > 1e-6 and y.std() > 1e-9:
                    ic = float(np.corrcoef(x, y)[0, 1])
                    n_eff = len(pairs) * overlap
                    t = ic * math.sqrt(max(1.0, n_eff - 2)) / math.sqrt(max(1e-9, 1 - ic * ic))
                    v.update(ic=ic, t=t, n_eff=round(n_eff, 1))
                    if n_eff >= MIN_N and abs(t) >= MIN_T:
                        v["w"] = float(max(-MAX_W, min(MAX_W, ic * 1.5)))
                        v["status"] = "approved" if ic > 0 else "inverse"
                    elif n_eff >= MIN_N:
                        v["status"] = "useless"
            self.verdicts[key] = v
        # الأحداث: حركة العملة 24س بعد الحدث مقارنةً بالبيتكوين
        for e in self.events:
            if e["ret"] is None and now - e["ts"] >= 86400:
                p1, b1 = prices.get(f"{e['base']}/USDT"), prices.get("BTC/USDT")
                if p1 and e.get("p0"):
                    rb = (b1 / e["b0"] - 1) if (b1 and e.get("b0") and e["base"] != "BTC") else 0.0
                    e["ret"] = p1 / e["p0"] - 1 - rb
        for src in EVENT_SOURCES:
            r = [e["ret"] for e in self.events if e["source"] == src and e["ret"] is not None]
            v = {"n": len(r), "need": EV_MIN_N, "mean": None, "t": None, "effect": 0.0, "status": "learning"}
            if len(r) >= 3:
                m, sd = float(np.mean(r)), float(np.std(r) or 1e-9)
                t = m / sd * math.sqrt(len(r))
                v.update(mean=m, t=t)
                if len(r) >= EV_MIN_N and abs(t) >= MIN_T:
                    v["effect"] = float(max(-1, min(1, m / 0.05)))
                    v["status"] = "approved" if m > 0 else "inverse"
                elif len(r) >= EV_MIN_N:
                    v["status"] = "useless"
            self.verdicts["ev_" + src] = v
        memory.kv_set("sm_verdicts", self.verdicts)
        memory.kv_set("sm_events", self.events)

    # -------------------------------------------------------------- التعلم من التاريخ (تسريع)
    def backfill(self):
        """مرة واحدة: بناء أدلة من التاريخ للمصادر التي لها أرشيف مجاني، حتى لا ننتظر أسابيع."""
        if memory.kv_get("sm_backfilled"):
            return
        ser = self._btc_series()
        if len(ser) < 500:
            return  # ننتظر دراسة التاريخ (ملف البيتكوين)
        added = {}

        def px(t):
            return ser.get(int(t // 3600 * 3600))
        # علاوة Coinbase: 45 يوماً كل ساعة
        try:
            end = int(time.time() // 3600 * 3600)
            cb = {}
            for k in range(4):
                e = end - k * 300 * 3600
                s = e - 300 * 3600
                fmt = lambda x: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(x))
                rows = _get(f"https://api.exchange.coinbase.com/products/BTC-USD/candles?granularity=3600&start={fmt(s)}&end={fmt(e)}")
                for r in rows:
                    cb[int(r[0])] = float(r[4])
            prem = sorted((t, (c - px(t)) / px(t) * 100) for t, c in cb.items() if px(t))
            obs = []
            for i in range(24, len(prem), 4):
                avg = sum(p for _, p in prem[i - 24:i]) / 24
                t = prem[i][0]
                obs.append([t, tanh(avg / 0.08), px(t)])
            if obs:
                self.obs["premium"] = obs + self.obs.get("premium", [])
                added["premium"] = len(obs)
        except Exception as e:
            log.info(f"backfill premium: {e}")
        # العملات المستقرة: يومياً
        try:
            rows = _get("https://stablecoins.llama.fi/stablecoincharts/all")
            v = [(int(r["date"]), float(r["totalCirculatingUSD"]["peggedUSD"])) for r in rows
                 if (r.get("totalCirculatingUSD") or {}).get("peggedUSD")][-400:]
            obs = []
            for i in range(7, len(v)):
                t = v[i][0] + 86400
                if px(t):
                    obs.append([t, tanh((v[i][1] - v[i - 1][1]) / 7e8 + (v[i][1] - v[i - 7][1]) / 3e9), px(t)])
            if obs:
                self.obs["stables"] = obs + self.obs.get("stables", [])
                added["stables"] = len(obs)
        except Exception as e:
            log.info(f"backfill stables: {e}")
        # صناديق ETF: يومياً
        try:
            rows = parse_farside(_get("https://farside.co.uk/bitcoin-etf-flow-all-data/", raw=True).decode("utf-8", "ignore"))
            obs = []
            for i in range(3, len(rows)):
                t = rows[i][0] + 22 * 3600  # بعد إغلاق السوق الأمريكي
                if px(t):
                    obs.append([t, tanh(sum(x for _, x in rows[i - 2:i + 1]) / 600), px(t)])
            if obs:
                self.obs["etf"] = obs[-500:] + self.obs.get("etf", [])
                added["etf"] = len(obs)
        except Exception as e:
            log.info(f"backfill etf: {e}")
        # CFTC: أسبوعياً
        try:
            rows = _get("https://publicreporting.cftc.gov/resource/gpe5-46if.json?cftc_contract_market_code=133741"
                        "&$order=report_date_as_yyyy_mm_dd%20DESC&$limit=120")
            f = lambda r, k: float(r.get(k) or 0)
            nets = []
            for r in reversed(rows):
                oi = f(r, "open_interest_all") or 1
                t = time.mktime(time.strptime(r["report_date_as_yyyy_mm_dd"][:10], "%Y-%m-%d")) + 4 * 86400
                nets.append((t, (f(r, "asset_mgr_positions_long") - f(r, "asset_mgr_positions_short")) / oi,
                             (f(r, "lev_money_positions_long") - f(r, "lev_money_positions_short")) / oi))
            obs = [[t, tanh((a - pa) / 0.03 - (l - pl) / 0.06), px(t)]
                   for (t, a, l), (_, pa, pl) in zip(nets[1:], nets[:-1]) if px(t)]
            if obs:
                self.obs["cot"] = obs + self.obs.get("cot", [])
                added["cot"] = len(obs)
        except Exception as e:
            log.info(f"backfill cot: {e}")
        memory.kv_set("sm_obs", self.obs)
        if added:
            memory.kv_set("sm_backfilled", True)
            self.evaluate({})
            memory.log_event("⚖️ محكمة الأدلة درست تاريخ المصادر: " +
                             "، ".join(f"{k} ({v} مقارنة)" for k, v in added.items()), "learn")

    # -------------------------------------------------------------- الاستخدام في القرار
    def market_score(self):
        tot, used = 0.0, []
        now = time.time()
        every = {k: e for k, *_, e in SOURCES}
        for key in HORIZON:
            v, g = self.verdicts.get(key, {}), self.sig.get(key)
            if not g or g.get("err") or not v.get("w") or now - g.get("ts", 0) > 3 * every[key] + 3600:
                continue
            tot += v["w"] * g["s"]
            used.append(key)
        return max(-1.0, min(1.0, tot)), used

    def coin_view(self, base, side):
        """(سبب منع أو None, تعديل الثقة لهذه العملة)."""
        un = (self.state.get("unlocks") or {}).get(base)
        if un and side == "long" and un["ts"] - time.time() < 48 * 3600 and (un["pct"] is None or un["pct"] >= 0.01):
            pct = f" ({un['pct']:.1%} من المعروض)" if un["pct"] else ""
            return f"فتح كمية كبيرة من {base} خلال 48 ساعة{pct}", 0.0
        adj = 0.0
        for e in self.events:
            if e["base"] == base and e["ret"] is None and time.time() - e["ts"] < 86400:
                adj += self.verdicts.get("ev_" + e["source"], {}).get("effect", 0.0)
        d = 1 if side == "long" else -1
        return None, max(-1.0, min(1.0, adj)) * d

    def summary(self):
        m, used = self.market_score()
        rows = []
        for key, icon, name, plain, fn, every in SOURCES:
            g = self.sig.get(key, {})
            v = self.verdicts.get(key) or self.verdicts.get("ev_" + key) or {}
            rows.append({"key": key, "icon": icon, "name": name, "plain": plain, "s": g.get("s"), "text": g.get("text"),
                         "err": g.get("err"), "ts": g.get("ts"), "status": v.get("status", "learning"),
                         "n": v.get("n", 0), "need": v.get("need", MIN_N), "w": v.get("w") if "w" in v else v.get("effect"),
                         "ic": v.get("ic") if "ic" in v else v.get("mean"),
                         "event": key in EVENT_SOURCES and key != "corporate"})
        recent = [{"source": e["source"], "base": e["base"], "title": e["title"], "ts": e["ts"], "ret": e["ret"]}
                  for e in self.events[-10:]][::-1]
        un = self.state.get("unlocks") or {}
        if m > 0.15:
            verdict = ("buy", "المال الكبير يميل للشراء")
        elif m < -0.15:
            verdict = ("sell", "المال الكبير يميل للبيع")
        else:
            verdict = ("neutral", "المال الكبير محايد" if used else "الوكيل ما زال يتعلم من المصادر")
        return {"score": m, "used": used, "verdict": verdict, "sources": rows, "events": recent,
                "unlocks": [{"base": b, "ts": u["ts"], "pct": u["pct"]} for b, u in sorted(un.items(), key=lambda x: x[1]["ts"])],
                "whale_pro": bool(memory.kv_get("whale_key") or os.getenv("WHALE_ALERT_KEY")),
                "options_top": (self.sig.get("polymarket", {}).get("data") or {}).get("top", [])}

"""
🌍 الاقتصاد العالمي + 🔄 دورة السيولة بين العملات.

الاقتصاد العالمي (Yahoo Finance، مجاني):
  الأسهم الأمريكية (عقود S&P 500 الآجلة تتداول تقريباً طوال اليوم)، الدولار (DXY)، الذهب، مؤشر الخوف VIX.
  يدخل "محكمة الأدلة" كمصدر عادي: لا وزن له قبل أن يثبت أنه يتنبأ بحركة البيتكوين.
  قاعدة حماية ثابتة: انهيار عالمي (VIX فوق 35 أو هبوط الأسهم 3%+ في يوم) => نصف الحجم.

دورة السيولة (من بيانات Binance نفسها + CoinGecko):
  هل المال يخرج من البيتكوين إلى العملات البديلة (موسم البديلة) أم يعود للبيتكوين؟
"""
import json
import logging
import math
import time
import urllib.request

import numpy as np

log = logging.getLogger("macro")
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/124 Safari/537.36"
TICKERS = {"spx": "ES=F", "dxy": "DX-Y.NYB", "gold": "GC=F", "vix": "^VIX"}
NAMES = {"spx": "الأسهم الأمريكية", "dxy": "الدولار", "gold": "الذهب", "vix": "مؤشر الخوف"}


def _get(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read())


def yahoo(sym):
    """يرجع (آخر سعر, التغير خلال 24 ساعة)."""
    s = urllib.request.quote(sym)
    j = None
    for host in ("query1", "query2"):
        try:
            j = _get(f"https://{host}.finance.yahoo.com/v8/finance/chart/{s}?interval=1h&range=5d")
            break
        except Exception:
            continue
    if j is None:
        raise RuntimeError("Yahoo غير متاح")
    return parse_chart(j)


def parse_chart(j):
    r = j["chart"]["result"][0]
    ts = r["timestamp"]
    cl = r["indicators"]["quote"][0]["close"]
    pts = [(t, c) for t, c in zip(ts, cl) if c is not None]
    if len(pts) < 2:
        raise ValueError("بيانات ناقصة")
    t_last, last = pts[-1]
    ref = next((c for t, c in reversed(pts) if t <= t_last - 86400 + 1800), pts[0][1])
    return float(last), float(last / ref - 1)


def src_macro(state):
    vals, errs = {}, []
    for k, t in TICKERS.items():
        try:
            vals[k] = yahoo(t)
        except Exception as e:
            errs.append(k)
    if "spx" not in vals and "dxy" not in vals:
        raise RuntimeError("بيانات الأسواق العالمية غير متاحة")
    spx = vals.get("spx", (0, 0.0))[1]
    dxy = vals.get("dxy", (0, 0.0))[1]
    vix = vals.get("vix", (None, 0))[0]
    s = 0.6 * math.tanh(spx / 0.015) - 0.4 * math.tanh(dxy / 0.006)
    if vix and vix > 28:
        s -= min(0.4, (vix - 28) / 20)
    extreme = bool((vix and vix >= 35) or spx <= -0.03)
    data = {"spx": spx, "dxy": dxy, "gold": vals.get("gold", (0, None))[1], "vix": vix, "extreme": extreme}
    parts = []
    if "spx" in vals:
        parts.append(f"الأسهم {spx:+.1%}")
    if "dxy" in vals:
        parts.append(f"الدولار {dxy:+.1%}")
    if "gold" in vals:
        parts.append(f"الذهب {vals['gold'][1]:+.1%}")
    if vix:
        parts.append(f"الخوف VIX {vix:.0f}")
    text = " · ".join(parts) + (" — ⚠️ اضطراب عالمي" if extreme else "")
    state["macro"] = data
    return max(-1.0, min(1.0, s)), data, text


def src_toptraders(state):
    """مراكز أكبر متداولي البيتكوين على Binance (تأتي من وحدة المراكز)."""
    d = state.get("top_btc")
    if not d:
        raise RuntimeError("بانتظار بيانات Binance")
    top, crowd = d["top"], d["crowd"]
    s = math.tanh(math.log(top) / 0.25) * 0.6 + math.tanh(d["top_chg"] / 0.1) * 0.4
    return s, {"top": top, "crowd": crowd}, (f"الكبار: {top / (1 + top):.0%} شراء · الجمهور: {crowd / (1 + crowd):.0%} شراء")


# ============================================================== دورة السيولة
class Rotation:
    def __init__(self, memory):
        self.memory = memory
        self.dom = memory.kv_get("btc_dom", []) or []   # [[ts, هيمنة%]]
        self.state = {"season": "neutral", "alt_vs_btc": 0.0, "dom": None, "dom_chg": None, "ethbtc": None}
        self._t = 0

    def fetch_dom(self):
        j = _get("https://api.coingecko.com/api/v3/global")
        d = float(j["data"]["market_cap_percentage"]["btc"])
        h = int(time.time() // 3600) * 3600
        if not self.dom or self.dom[-1][0] != h:
            self.dom.append([h, d])
            self.dom = self.dom[-24 * 60:]
            self.memory.kv_set("btc_dom", self.dom)
        return d

    def update(self, closes1h, alts):
        """closes1h: base -> سلسلة إغلاق ساعة. alts: العملات البديلة (بدون الميمية والمستقرة)."""
        if time.time() - self._t < 900:
            return self.state
        self._t = time.time()
        try:
            self.state["dom"] = self.fetch_dom()
        except Exception as e:
            log.debug(f"dom: {e}")
        if len(self.dom) >= 2:
            old = [d for t, d in self.dom if t <= time.time() - 7 * 86400]
            ref = old[-1] if old else self.dom[0][1]
            self.state["dom_chg"] = self.dom[-1][1] - ref
        btc = closes1h.get("BTC")
        if btc is None or len(btc) < 170:
            return self.state
        rb = float(btc.iloc[-1] / btc.iloc[-169] - 1)
        rets = []
        for b in alts:
            c = closes1h.get(b)
            if c is not None and len(c) >= 169:
                rets.append(float(c.iloc[-1] / c.iloc[-169] - 1))
        if len(rets) < 5:
            return self.state
        avb = float(np.median(rets)) - rb
        self.state["alt_vs_btc"] = avb
        eth = closes1h.get("ETH")
        if eth is not None and len(eth) >= 169:
            self.state["ethbtc"] = float(eth.iloc[-1] / eth.iloc[-169] - 1) - rb
        dc = self.state.get("dom_chg")
        if avb > 0.03 and (dc is None or dc < 0.3):
            self.state["season"] = "alt"
        elif avb < -0.03 and (dc is None or dc > -0.3):
            self.state["season"] = "btc"
        else:
            self.state["season"] = "neutral"
        return self.state

    def adjust(self, base, side):
        """تعديل صغير للثقة لصفقات العملات البديلة حسب اتجاه السيولة."""
        if base in ("BTC",) or side != "long":
            return 0.0, ""
        s = self.state["season"]
        if s == "btc":
            return -0.03, "🔄 السيولة تعود للبيتكوين — العملات البديلة أضعف الآن"
        if s == "alt":
            return 0.03, "🔄 موسم العملات البديلة — السيولة تتجه إليها"
        return 0.0, ""

    def summary(self):
        s = self.state
        label = {"alt": "🟢 موسم العملات البديلة: المال يخرج من البيتكوين إلى العملات الأخرى",
                 "btc": "🟠 موسم البيتكوين: المال يعود للبيتكوين والعملات البديلة أضعف",
                 "neutral": "⚪ لا اتجاه واضح للسيولة بين البيتكوين والعملات الأخرى"}[s["season"]]
        return {**s, "label": label}

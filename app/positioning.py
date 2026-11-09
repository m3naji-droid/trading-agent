"""
📊 مراكز كبار متداولي Binance + 🔥 خريطة نقاط التصفية التقديرية.

من بيانات Binance العامة المجانية (Futures):
  - نسبة الشراء/البيع لدى أكبر الحسابات (Top traders — حسب حجم المراكز).
  - نسبة الشراء/البيع لعامة الحسابات (الجمهور).
  - تاريخ العقود المفتوحة كل ساعة.

خريطة التصفية (تقدير، لا توجد بيانات رسمية مجانية):
  كل ساعة زادت فيها العقود المفتوحة = مراكز جديدة فُتحت قرب ذلك السعر.
  نفترض توزيعاً معتاداً للرافعات (10x/25x/50x/100x) فنعرف أين ستُصفّى تلك المراكز.
  المناطق التي لم يلمسها السعر بعد = "مغناطيس" قد يجذب السعر إليه.
"""
import json
import logging
import math
import time
import urllib.request

import numpy as np

log = logging.getLogger("positioning")
BASE = "https://fapi.binance.com"
UA = "Mozilla/5.0 (trading-agent)"
REFRESH = 1800
LEV_W = {10: 0.30, 25: 0.35, 50: 0.25, 100: 0.10}
BIN = 0.0025  # عرض خانة الخريطة 0.25%


def _get(path, params):
    q = "&".join(f"{k}={v}" for k, v in params.items())
    req = urllib.request.Request(f"{BASE}{path}?{q}", headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())


def fetch_symbol(sym):
    """sym مثل BTC/USDT. يرجع بيانات المراكز + تاريخ العقود المفتوحة."""
    s = sym.replace("/", "")
    top = _get("/futures/data/topLongShortPositionRatio", {"symbol": s, "period": "1h", "limit": 24})
    crowd = _get("/futures/data/globalLongShortAccountRatio", {"symbol": s, "period": "1h", "limit": 24})
    oi = _get("/futures/data/openInterestHist", {"symbol": s, "period": "1h", "limit": 72})
    return parse(top, crowd, oi)


def parse(top, crowd, oi):
    t = [float(x["longShortRatio"]) for x in top if x.get("longShortRatio")]
    c = [float(x["longShortRatio"]) for x in crowd if x.get("longShortRatio")]
    o = [(int(x["timestamp"]), float(x.get("sumOpenInterestValue") or 0)) for x in oi]
    if not t or not c:
        raise ValueError("لا بيانات مراكز")
    return {"top": t[-1], "top_chg": t[-1] / t[0] - 1 if t[0] else 0.0, "crowd": c[-1],
            "crowd_chg": c[-1] / c[0] - 1 if c[0] else 0.0, "oi": o, "ts": time.time()}


def liq_map(oi_hist, closes, long_frac=0.5):
    """
    oi_hist: [(ts_ms, قيمة العقود المفتوحة $)] كل ساعة. closes: سلسلة pandas (ts_ms -> إغلاق ساعة).
    يرجع {up: أقرب منطقة تصفية بائعين فوق السعر, dn: أقرب منطقة تصفية مشترين تحته, magnet: -1..+1}.
    """
    if len(oi_hist) < 6 or closes is None or len(closes) < 6:
        return None
    px = closes.reindex([t for t, _ in oi_hist], method="nearest")
    price = float(closes.iloc[-1])
    path = closes[closes.index >= oi_hist[0][0]]
    levels = {}
    for i in range(1, len(oi_hist)):
        add = oi_hist[i][1] - oi_hist[i - 1][1]
        if add <= 0:
            continue
        p0 = float(px.iloc[i])
        after = path[path.index > oi_hist[i][0]]
        hi = float(after.max()) if len(after) else p0
        lo = float(after.min()) if len(after) else p0
        for lev, w in LEV_W.items():
            m = 0.9 / lev  # الحد الصيانة تقريباً
            lp, sp = p0 * (1 - m), p0 * (1 + m)  # تصفية المشترين تحت، والبائعين فوق
            if lo > lp:  # لم يلمسها السعر بعد
                k = round(math.log(lp / price) / BIN)
                levels[k] = levels.get(k, 0.0) - add * w * long_frac
            if hi < sp:
                k = round(math.log(sp / price) / BIN)
                levels[k] = levels.get(k, 0.0) + add * w * (1 - long_frac)
    if not levels:
        return None
    ks = np.array(sorted(levels))
    vs = np.array([levels[k] for k in ks])
    tot = float(np.abs(vs).sum()) or 1.0
    near = np.abs(ks * BIN) <= 0.03
    up_m = float(vs[(vs > 0) & near].sum())
    dn_m = float(-vs[(vs < 0) & near].sum())
    out = {"magnet": (up_m - dn_m) / (up_m + dn_m) if up_m + dn_m > 0 else 0.0}
    close = np.abs(ks * BIN) <= 0.10  # فقط المناطق القريبة (10%) لها معنى عملي
    for side, mask in (("up", (vs > 0) & (ks > 0) & close), ("dn", (vs < 0) & (ks < 0) & close)):
        if not mask.any():
            out[side] = None
            continue
        kk, vv = ks[mask], np.abs(vs[mask])
        big = vv >= 0.08 * tot  # منطقة معتبرة فقط
        if not big.any():
            big = vv == vv.max()
        j = np.argmin(np.abs(kk[big]))
        k = int(kk[big][j])
        out[side] = {"price": price * math.exp(k * BIN), "dist": math.exp(k * BIN) - 1, "usd": float(vv[big][j])}
    return out


class Positioning:
    def __init__(self):
        self.data = {}   # sym -> parsed
        self.maps = {}   # sym -> liq map
        self._t = {}
        self.errors = 0

    def due(self, universe, n=6):
        return [s for s in universe if time.time() - self._t.get(s, 0) > REFRESH][:n]

    def refresh(self, syms, closes1h):
        for s in syms:
            self._t[s] = time.time()
            try:
                d = fetch_symbol(s)
            except Exception as e:
                self.errors += 1
                log.debug(f"{s}: {e}")
                continue
            self.data[s] = d
            try:
                lf = d["crowd"] / (1 + d["crowd"])
                m = liq_map(d["oi"], closes1h.get(s.split("/")[0]), lf)
                if m:
                    self.maps[s] = m
            except Exception as e:
                log.debug(f"liqmap {s}: {e}")

    def view(self, sym):
        """قيم رقمية للوكيل والحَكَم الذكي."""
        d = self.data.get(sym)
        m = self.maps.get(sym) or {}
        if not d or time.time() - d["ts"] > 3 * 3600:
            return {}
        return {"top": math.log(max(1e-3, d["top"])), "crowd": math.log(max(1e-3, d["crowd"])),
                "top_chg": d["top_chg"], "magnet": m.get("magnet", 0.0),
                "liq_up": (m.get("up") or {}).get("dist"), "liq_dn": (m.get("dn") or {}).get("dist")}

    def summary(self, sym):
        d = self.data.get(sym)
        if not d:
            return None
        m = self.maps.get(sym) or {}
        return {"top": round(d["top"], 2), "crowd": round(d["crowd"], 2), "top_chg": round(d["top_chg"], 3),
                "up": m.get("up"), "dn": m.get("dn"), "magnet": round(m.get("magnet", 0.0), 2)}

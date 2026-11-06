"""
بيانات أعمق (مصادر مجانية بلا مفاتيح):
  💵 DefiLlama  : إجمالي العملات المستقرة — نموّها = سيولة جديدة تدخل الكريبتو.
  🏦 Coin Metrics (المجتمعي): تدفق البيتكوين من/إلى المنصات — الدخول للمنصات يسبق البيع غالباً.
     (قد لا يتوفر مجاناً دائماً؛ يُتجاهل بهدوء إن فشل)
  🎯 Deribit    : خيارات BTC/ETH — نسبة Put/Call، نقطة Max Pain لأقرب انتهاء، ومؤشر التذبذب DVOL.
"""
import json
import logging
import re
import time
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone

log = logging.getLogger("onchain")
STABLE_URL = "https://stablecoins.llama.fi/stablecoincharts/all"
FLOW_URL = ("https://community-api.coinmetrics.io/v4/timeseries/asset-metrics?assets=btc"
            "&metrics=FlowInExNtv,FlowOutExNtv&frequency=1d&page_size=10")
DERIBIT = "https://www.deribit.com/api/v2/public/"
MONTHS = {m: i + 1 for i, m in enumerate(["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"])}


def _get(url, timeout=25):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (trading-agent)", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def _expiry(code):
    """'27DEC24' -> timestamp (08:00 UTC)."""
    m = re.match(r"(\d{1,2})([A-Z]{3})(\d{2})", code)
    if not m:
        return None
    d, mon, y = int(m.group(1)), MONTHS.get(m.group(2)), 2000 + int(m.group(3))
    return datetime(y, mon, d, 8, tzinfo=timezone.utc).timestamp() if mon else None


def options_summary(cur):
    rows = _get(DERIBIT + f"get_book_summary_by_currency?currency={cur}&kind=option")["result"]
    oi_call = oi_put = 0.0
    by_exp = defaultdict(list)
    under = None
    for r in rows:
        parts = r["instrument_name"].split("-")  # BTC-27DEC24-60000-C
        if len(parts) != 4:
            continue
        oi = float(r.get("open_interest") or 0)
        k, typ = float(parts[2]), parts[3]
        under = under or r.get("underlying_price")
        if typ == "C":
            oi_call += oi
        else:
            oi_put += oi
        ts = _expiry(parts[1])
        if ts:
            by_exp[ts].append((k, typ, oi))
    now = time.time()
    upcoming = sorted(t for t in by_exp if t > now)
    mp = None
    if upcoming:
        exp = upcoming[0]
        # إذا كان أقرب انتهاء ضئيل الحجم (يومي)، خذ أول انتهاء بحجم معتبر
        for t in upcoming[:6]:
            if sum(x[2] for x in by_exp[t]) > 0.05 * (oi_call + oi_put):
                exp = t
                break
        opts = by_exp[exp]
        strikes = sorted({k for k, _, _ in opts})
        best, pain = None, None
        for s in strikes:
            pay = sum(oi * max(0.0, s - k) for k, t_, oi in opts if t_ == "C") + \
                  sum(oi * max(0.0, k - s) for k, t_, oi in opts if t_ == "P")
            if pain is None or pay < pain:
                best, pain = s, pay
        mp = {"price": best, "expiry": exp, "oi": sum(x[2] for x in opts)}
    dv = None
    try:
        end = int(now * 1000)
        d = _get(DERIBIT + f"get_volatility_index_data?currency={cur}&start_timestamp={end - 3 * 86400_000}"
                           f"&end_timestamp={end}&resolution=3600")["result"]["data"]
        if d:
            dv = {"now": float(d[-1][4]), "chg_24h": float(d[-1][4]) - float(d[max(0, len(d) - 25)][4])}
    except Exception as e:
        log.debug(f"dvol {cur}: {e}")
    return {"pc": oi_put / oi_call if oi_call else None, "oi_call": oi_call, "oi_put": oi_put,
            "max_pain": mp, "underlying": under, "dvol": dv}


class OnChain:
    def __init__(self):
        self.stable = None
        self.flows = None
        self.options = {}
        self.errors = {}
        self._t = {"stable": 0, "flows": 0, "options": 0}

    def refresh(self):
        now = time.time()
        if now - self._t["stable"] > 6 * 3600:
            self._t["stable"] = now
            try:
                rows = _get(STABLE_URL)
                v = [(int(r["date"]), float(r["totalCirculatingUSD"]["peggedUSD"])) for r in rows
                     if r.get("totalCirculatingUSD", {}).get("peggedUSD")]
                if len(v) > 31:
                    cur = v[-1][1]
                    self.stable = {"total": cur, "chg_7d": cur / v[-8][1] - 1, "chg_30d": cur / v[-31][1] - 1}
                self.errors.pop("stable", None)
            except Exception as e:
                self.errors["stable"] = str(e)[:80]
                self._t["stable"] = now - 5 * 3600
        if now - self._t["flows"] > 6 * 3600:
            self._t["flows"] = now
            try:
                d = _get(FLOW_URL).get("data", [])
                if d:
                    net = [float(x["FlowInExNtv"]) - float(x["FlowOutExNtv"]) for x in d
                           if x.get("FlowInExNtv") and x.get("FlowOutExNtv")]
                    if net:
                        self.flows = {"net_1d": net[-1], "net_7d": sum(net[-7:])}
                self.errors.pop("flows", None)
            except Exception as e:
                self.errors["flows"] = "غير متاح مجاناً حالياً"
        if now - self._t["options"] > 15 * 60:
            self._t["options"] = now
            for cur in ("BTC", "ETH"):
                try:
                    self.options[cur] = options_summary(cur)
                    self.errors.pop(f"opt_{cur}", None)
                except Exception as e:
                    self.errors[f"opt_{cur}"] = str(e)[:80]

    # ---------------------------------------------------------------- قراءات للمجلس
    def pc(self):
        o = self.options.get("BTC") or {}
        return o.get("pc")

    def summary(self):
        return {"stable": self.stable, "flows": self.flows, "options": self.options, "errors": self.errors}

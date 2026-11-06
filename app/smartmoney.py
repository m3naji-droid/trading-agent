"""
🕵️ رادار الأموال الكبرى — 10 مصادر علنية يتركها المال الكبير خلفه.

كل مصدر يحوّل بياناته إلى إشارة واحدة من -1 (المال الكبير يبيع) إلى +1 (يشتري).
ثم "محكمة الأدلة" (evidence.py) تختبر كل مصدر: هل تنبأت إشارته فعلاً بحركة السعر؟
- مصدر يتنبأ => يأخذ وزناً في قرارات الوكيل.
- مصدر عكسي (يتنبأ بالعكس) => يتعلم الوكيل قراءته بالعكس.
- مصدر لا يضيف شيئاً => يبقى للمراقبة فقط، بلا تأثير.
المستخدم لا يحتاج أن يفهم شيئاً من هذا: الوكيل هو من يقرأ ويقرر.

كل مصدر مستقل: إذا تعطل أحدها (حجب، تغيّر صيغة، حد طلبات) يتجاهله الوكيل بهدوء.
"""
import json
import logging
import math
import re
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

log = logging.getLogger("smart")
UA = "Mozilla/5.0 (TradingAgent; contact admin@trading-agent.local)"


def _get(url, timeout=25, headers=None, raw=False):
    h = {"User-Agent": UA, "Accept": "application/json, text/html, */*"}
    h.update(headers or {})
    with urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=timeout) as r:
        b = r.read()
    return b if raw else json.loads(b)


def tanh(x):
    return math.tanh(x)


def _num(txt):
    """'(123.4)' => -123.4 ، '-' => 0"""
    t = re.sub(r"<[^>]+>", "", txt).replace(",", "").strip()
    if t in ("", "-", "–"):
        return 0.0
    neg = t.startswith("(") and t.endswith(")")
    t = t.strip("()")
    try:
        v = float(t)
    except ValueError:
        return None
    return -v if neg else v


# ============================================================== 1) صناديق ETF
def parse_farside(html: str):
    """جدول Farside: [(تاريخ, صافي التدفق بملايين الدولارات)]."""
    out = []
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.S | re.I):
        cells = re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", tr, re.S | re.I)
        if len(cells) < 3:
            continue
        d = re.sub(r"<[^>]+>", "", cells[0]).strip()
        try:
            day = datetime.strptime(d, "%d %b %Y").replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        tot = _num(cells[-1])
        if tot is not None:
            out.append((day.timestamp(), tot))
    return out


def src_etf(state):
    out = {}
    for coin, url in (("BTC", "https://farside.co.uk/bitcoin-etf-flow-all-data/"),
                      ("ETH", "https://farside.co.uk/ethereum-etf-flow-all-data/")):
        rows = parse_farside(_get(url, raw=True).decode("utf-8", "ignore"))
        if rows:
            out[coin] = rows
    if not out:
        raise RuntimeError("لا بيانات")
    state["etf_hist"] = out
    btc = out.get("BTC", [])
    last3 = sum(v for _, v in btc[-3:])
    eth3 = sum(v for _, v in out.get("ETH", [])[-3:])
    s = tanh(last3 / 600 + eth3 / 400)
    last = btc[-1] if btc else (0, 0)
    return s, {"btc_last": last[1], "btc_last_day": last[0], "btc_3d": last3, "eth_3d": eth3}, \
        f"آخر يوم: {last[1]:+,.0f} مليون$ للبيتكوين · 3 أيام: {last3:+,.0f}م$"


# ============================================================== 2) علاوة Coinbase
def src_premium(state):
    cb = float(_get("https://api.exchange.coinbase.com/products/BTC-USD/ticker")["price"])
    bn = state.get("btc_binance")
    if not bn:
        raise RuntimeError("سعر Binance غير متاح بعد")
    prem = (cb - bn) / bn * 100
    hist = state.setdefault("prem_hist", [])
    hist.append((time.time(), prem))
    state["prem_hist"] = [x for x in hist if x[0] > time.time() - 86400]
    avg = sum(p for _, p in state["prem_hist"]) / len(state["prem_hist"])
    s = tanh(avg / 0.08)
    return s, {"now": prem, "avg24": avg}, f"الأمريكيون يدفعون {prem:+.3f}% (متوسط 24س {avg:+.3f}%)"


# ============================================================== 3) طباعة العملات المستقرة
def src_stables(state):
    d = _get("https://stablecoins.llama.fi/stablecoins?includePrices=false")
    tot = day = week = 0.0
    for a in d.get("peggedAssets", []):
        if a.get("symbol") not in ("USDT", "USDC"):
            continue
        c = (a.get("circulating") or {}).get("peggedUSD") or 0
        tot += c
        day += c - ((a.get("circulatingPrevDay") or {}).get("peggedUSD") or c)
        week += c - ((a.get("circulatingPrevWeek") or {}).get("peggedUSD") or c)
    s = tanh(day / 7e8 + week / 3e9)
    return s, {"total": tot, "day": day, "week": week}, \
        f"طُبع خلال 24س: {day / 1e9:+.2f} مليار$ · خلال أسبوع: {week / 1e9:+.2f} مليار$"


# ============================================================== 4) تحويلات الحيتان
def src_whales(state):
    """مع مفتاح Whale Alert: تحويلات الحيتان المعروفة من/إلى المنصات. بدونه: صفقات الحيتان على Binance (من الرادار)."""
    key = state.get("whale_key")
    if key:
        since = int(time.time() - 3600 * 4)
        d = _get(f"https://api.whale-alert.io/v1/transactions?api_key={urllib.parse.quote(key)}&min_value=2000000&start={since}")
        to_ex = from_ex = st_to_ex = 0.0
        for t in d.get("transactions", []):
            usd = float(t.get("amount_usd") or 0)
            fo = (t.get("from") or {}).get("owner_type")
            to = (t.get("to") or {}).get("owner_type")
            sym = (t.get("symbol") or "").upper()
            if sym in ("USDT", "USDC"):
                if to == "exchange" and fo != "exchange":
                    st_to_ex += usd  # عملات مستقرة تدخل المنصات = قوة شرائية
            elif to == "exchange" and fo != "exchange":
                to_ex += usd       # عملات تدخل المنصات = استعداد للبيع
            elif fo == "exchange" and to != "exchange":
                from_ex += usd     # عملات تخرج للحفظ = تجميع
        s = tanh((from_ex - to_ex + 0.5 * st_to_ex) / 1.5e8)
        return s, {"to_ex": to_ex, "from_ex": from_ex, "stable_in": st_to_ex, "pro": True}, \
            f"إلى المنصات {to_ex / 1e6:,.0f}م$ · خارجها {from_ex / 1e6:,.0f}م$ · عملات مستقرة داخلة {st_to_ex / 1e6:,.0f}م$"
    flow = state.get("radar_flow") or []
    net = sum(f.get("net", 0) for f in flow if f.get("base") in ("BTC", "ETH"))
    liq = sum(f.get("liq_short", 0) - f.get("liq_long", 0) for f in flow if f.get("base") in ("BTC", "ETH"))
    if not flow:
        raise RuntimeError("الرادار لم يجمع بيانات بعد")
    s = tanh(net / 3e7 + liq / 6e7)
    return s, {"net": net, "pro": False}, f"صافي صفقات الحيتان على BTC/ETH (ساعة): {net / 1e6:+,.1f}م$"


# ============================================================== 5) مشتريات الشركات (SEC)
def src_corporate(state):
    now = datetime.now(timezone.utc)
    sub = _get("https://data.sec.gov/submissions/CIK0001050446.json")  # Strategy (MicroStrategy)
    rec = sub.get("filings", {}).get("recent", {})
    mstr = [d for f, d in zip(rec.get("form", []), rec.get("filingDate", [])) if f == "8-K"]
    last = mstr[0] if mstr else None
    q = urllib.parse.urlencode({"q": '"bitcoin" "purchased"', "forms": "8-K", "dateRange": "custom",
                                "startdt": (now - timedelta(days=7)).strftime("%Y-%m-%d"), "enddt": now.strftime("%Y-%m-%d")})
    try:
        hits = _get("https://efts.sec.gov/LATEST/search-index?" + q).get("hits", {}).get("total", {}).get("value", 0)
    except Exception:
        hits = None
    days_since = (now - datetime.strptime(last, "%Y-%m-%d").replace(tzinfo=timezone.utc)).days if last else 99
    s = (0.6 if days_since <= 2 else 0.2 if days_since <= 7 else 0.0) + (tanh((hits or 0) / 15) * 0.4 if hits else 0)
    events = []
    if last and last != state.get("mstr_last"):
        if state.get("mstr_last"):
            events.append({"kind": "corporate", "base": "BTC", "title": "Strategy أعلنت (8-K) — غالباً شراء بيتكوين"})
        state["mstr_last"] = last
    return min(1.0, s), {"mstr_last": last, "filings_7d": hits, "events": events}, \
        f"آخر إعلان لـ Strategy: {last or '—'} · إعلانات شركات عن شراء بيتكوين (أسبوع): {hits if hits is not None else '—'}"


# ============================================================== 6) مراكز صناديق التحوط (CFTC)
def src_cot(state):
    url = ("https://publicreporting.cftc.gov/resource/gpe5-46if.json?cftc_contract_market_code=133741"
           "&$order=report_date_as_yyyy_mm_dd%20DESC&$limit=12")
    rows = _get(url)
    if len(rows) < 2:
        raise RuntimeError("لا بيانات")
    f = lambda r, k: float(r.get(k) or 0)
    def nets(r):
        oi = f(r, "open_interest_all") or 1
        return (f(r, "asset_mgr_positions_long") - f(r, "asset_mgr_positions_short")) / oi, \
               (f(r, "lev_money_positions_long") - f(r, "lev_money_positions_short")) / oi
    am0, lev0 = nets(rows[0])
    am1, lev1 = nets(rows[1])
    s = tanh((am0 - am1) / 0.03 - (lev0 - lev1) / 0.06)  # المؤسسات تزيد الشراء / المضاربون مزدحمون
    state["cot_hist"] = [(r.get("report_date_as_yyyy_mm_dd", "")[:10], *nets(r)) for r in rows]
    return s, {"am_net": am0, "lev_net": lev0, "date": rows[0].get("report_date_as_yyyy_mm_dd", "")[:10]}, \
        f"مديرو الأصول صافي {am0:+.1%} (تغيّر {am0 - am1:+.1%}) · صناديق التحوط {lev0:+.1%}"


# ============================================================== 7) فتح العملات المجمدة
def src_unlocks(state):
    """DefiLlama Emissions: أحداث فتح خلال 7 أيام للعملات التي نتداولها. (قاعدة حماية + إشارة لكل عملة)"""
    data = _get("https://api.llama.fi/emissions", timeout=40)
    names = state.get("names", {})  # اسم أو معرّف CoinGecko => رمز
    now = time.time()
    found = {}
    items = data if isinstance(data, list) else data.get("data", [])
    for it in items:
        keys = {str(it.get(k, "")).lower() for k in ("name", "gecko_id", "token", "symbol")}
        base = next((names[k] for k in keys if k in names), None)
        if not base:
            continue
        evs = it.get("events") or it.get("upcomingEvent") or it.get("nextEvent") or []
        if isinstance(evs, dict):
            evs = [evs]
        circ = float(it.get("circSupply") or it.get("circulatingSupply") or 0)
        for e in evs:
            ts = e.get("timestamp") or e.get("date")
            if not ts:
                continue
            ts = float(ts) / (1000 if float(ts) > 1e12 else 1)
            if not (now < ts < now + 7 * 86400):
                continue
            amt = e.get("noOfTokens") or e.get("amount") or 0
            amt = sum(float(x) for x in amt) if isinstance(amt, list) else float(amt or 0)
            pct = amt / circ if circ else None
            cur = found.get(base)
            if not cur or ts < cur["ts"]:
                found[base] = {"ts": ts, "pct": pct}
    state["unlocks"] = found
    big = sum(1 for v in found.values() if (v["pct"] or 0.01) >= 0.01)
    return -tanh(big / 4) * 0.5, {"coins": found}, \
        (f"فتح عملات خلال أسبوع: {'، '.join(sorted(found))}" if found else "لا فتح كبير قادم لعملاتنا")


# ============================================================== 8) إعلانات الإدراج
TICKER = re.compile(r"\(([A-Z0-9]{2,12})\)")


def src_listings(state):
    """Binance (إعلانات) + Upbit (كوريا — تأثير قوي تاريخياً) + Coinbase (منتجات جديدة)."""
    events, seen = [], state.setdefault("seen_ann", [])
    tradable = state.get("tradable", set())

    def add(src, base, title, ts):
        k = f"{src}:{base}:{title[:40]}"
        if k in seen:
            return
        seen.append(k)
        if base in tradable and time.time() - ts < 6 * 3600:
            events.append({"kind": "listing", "base": base, "title": f"{src}: {title[:90]}", "ts": ts})
    ok = 0
    try:
        d = _get("https://www.binance.com/bapi/composite/v1/public/cms/article/list/query?type=1&pageNo=1&pageSize=20&catalogId=48")
        for a in (d.get("data", {}).get("catalogs") or [{}])[0].get("articles", []):
            t = a.get("title", "")
            ts = (a.get("releaseDate") or 0) / 1000
            for b in set(TICKER.findall(t)) | set(re.findall(r"\b([A-Z0-9]{2,12})USDT\b", t)):
                add("Binance", b, t, ts)
        ok += 1
    except Exception as e:
        log.debug(f"binance ann: {e}")
    try:
        d = _get("https://api-manager.upbit.com/api/v1/announcements?os=web&page=1&per_page=20&category=trade")
        for n in d.get("data", {}).get("notices", []):
            t = n.get("title", "")
            if "추가" not in t and "listing" not in t.lower():
                continue
            ts_s = n.get("listed_at") or n.get("first_listed_at") or ""
            try:
                ts = datetime.fromisoformat(ts_s.replace("Z", "+00:00")).timestamp()
            except ValueError:
                ts = time.time()
            for b in TICKER.findall(t):
                add("Upbit", b, t, ts)
        ok += 1
    except Exception as e:
        log.debug(f"upbit ann: {e}")
    try:
        prods = _get("https://api.exchange.coinbase.com/products")
        bases = {p["base_currency"] for p in prods if p.get("quote_currency") in ("USD", "USDC")}
        prev = set(state.get("cb_bases", []))
        if prev:
            for b in bases - prev:
                add("Coinbase", b, f"إدراج {b} على Coinbase", time.time())
        state["cb_bases"] = sorted(bases)
        ok += 1
    except Exception as e:
        log.debug(f"coinbase products: {e}")
    state["seen_ann"] = seen[-400:]
    if not ok:
        raise RuntimeError("كل مصادر الإعلانات غير متاحة")
    return 0.0, {"events": events}, (f"إعلانات جديدة: {'، '.join(e['base'] for e in events)}" if events
                                     else f"لا إعلانات إدراج جديدة لعملاتنا ({ok}/3 مصادر تعمل)")


# ============================================================== 9) أسواق التوقعات (Polymarket)
def src_polymarket(state):
    ms = _get("https://gamma-api.polymarket.com/markets?active=true&closed=false&limit=500&order=volume24hr&ascending=false")
    fed, rec, btc = [], [], []
    for m in ms:
        q = (m.get("question") or "")
        ql = q.lower()
        try:
            yes = float(json.loads(m.get("outcomePrices") or "[]")[0])
        except Exception:
            continue
        vol = float(m.get("volume24hr") or 0)
        if "fed" in ql and any(w in ql for w in ("cut", "decrease", "lower")):
            fed.append((vol, q, yes))
        elif "recession" in ql:
            rec.append((vol, q, yes))
        elif "bitcoin" in ql and any(w in ql for w in ("above", "reach", "hit")):
            btc.append((vol, q, yes))
    fed.sort(reverse=True)
    rec.sort(reverse=True)
    btc.sort(reverse=True)
    cut = fed[0][2] if fed else None
    hist = state.setdefault("pm_hist", [])
    if cut is not None:
        hist.append((time.time(), cut))
        state["pm_hist"] = [x for x in hist if x[0] > time.time() - 2 * 86400]
    old = next((p for t, p in state["pm_hist"] if t > time.time() - 26 * 3600), cut)
    d_cut = (cut - old) if cut is not None and old is not None else 0
    rp = rec[0][2] if rec else 0
    s = tanh(d_cut * 6 - (rp - 0.3) * 1.5) if (fed or rec) else 0.0
    top = [{"q": q[:110], "p": p} for _, q, p in (fed[:2] + rec[:1] + btc[:2])]
    return s, {"cut": cut, "d_cut": d_cut, "recession": rp, "top": top}, \
        (f"احتمال خفض الفائدة: {cut:.0%} (تغيّر يوم {d_cut:+.0%}) · ركود: {rp:.0%}" if cut is not None
         else "لا أسواق فائدة نشطة الآن")


# ============================================================== 10) صفقات الخيارات الضخمة
def src_options_flow(state):
    seen = state.setdefault("opt_seen", {})
    now = time.time()
    for cur in ("BTC", "ETH"):
        d = _get(f"https://www.deribit.com/api/v2/public/get_last_trades_by_currency?currency={cur}&kind=option&count=1000")
        for t in d.get("result", {}).get("trades", []):
            amt = float(t.get("amount") or 0)
            prem = float(t.get("price") or 0) * amt * float(t.get("index_price") or 0)
            big = t.get("block_trade_id") or amt >= (25 if cur == "BTC" else 300)
            if not big or prem < 50_000:
                continue
            name = t.get("instrument_name", "")
            call = name.endswith("-C")
            buy = t.get("direction") == "buy"
            bull = 1 if (call and buy) or (not call and not buy) else -1
            seen[t.get("trade_id", name + str(t.get("timestamp")))] = (t.get("timestamp", now * 1000) / 1000, bull * prem, cur)
    seen = {k: v for k, v in seen.items() if v[0] > now - 86400}
    state["opt_seen"] = seen
    net = sum(v[1] for v in seen.values())
    s = tanh(net / 4e6)
    return s, {"net": net, "count": len(seen)}, \
        f"{len(seen)} صفقة خيارات ضخمة خلال 24س · صافي رهانات {'الصعود' if net >= 0 else 'الهبوط'}: {abs(net) / 1e6:,.1f}م$"


SOURCES = [
    # key, icon, الاسم، المعنى ببساطة، الدالة، كل كم ثانية
    ("etf", "🏦", "صناديق ETF", "هل المؤسسات الأمريكية تشتري البيتكوين اليوم؟", src_etf, 3 * 3600),
    ("premium", "🇺🇸", "علاوة Coinbase", "هل الأمريكيون يدفعون سعراً أعلى من بقية العالم؟", src_premium, 300),
    ("stables", "💵", "طباعة العملات المستقرة", "هل تُطبع أموال جديدة تستعد لدخول الكريبتو؟", src_stables, 3600),
    ("whales", "🐋", "تحويلات الحيتان", "هل الحيتان ترسل عملاتها للمنصات (للبيع) أم تسحبها (للحفظ)؟", src_whales, 600),
    ("corporate", "🏢", "مشتريات الشركات", "هل أعلنت شركات كبرى شراء بيتكوين؟", src_corporate, 3 * 3600),
    ("cot", "📊", "مراكز صناديق التحوط", "هل الصناديق الكبرى تراهن على الصعود أم الهبوط؟", src_cot, 12 * 3600),
    ("unlocks", "🔓", "فتح العملات المجمدة", "هل ستُطلق كمية ضخمة من عملة قريباً؟", src_unlocks, 6 * 3600),
    ("listings", "📢", "إعلانات الإدراج", "هل أُعلن إدراج عملة في منصة كبرى؟", src_listings, 300),
    ("polymarket", "🎯", "أسواق التوقعات", "ماذا يتوقع من يراهنون بأموالهم على الفائدة والركود؟", src_polymarket, 1800),
    ("options", "🐳", "صفقات الخيارات الضخمة", "هل المؤسسات تراهن بالملايين على الصعود أم الهبوط؟", src_options_flow, 900),
]

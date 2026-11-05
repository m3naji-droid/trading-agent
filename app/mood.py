"""
قارئ مزاج السوق:
- مؤشر الخوف والطمع (alternative.me)
- عناوين أخبار الكريبتو (RSS: CoinDesk / Cointelegraph / Decrypt) مع تقييم بسيط للمعنويات
- الأحداث الاقتصادية الأمريكية عالية التأثير (فائدة، تضخم، وظائف) => إيقاف الدخول قبلها وبعدها
"""
import json
import logging
import re
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

log = logging.getLogger("mood")

FNG_URL = "https://api.alternative.me/fng/?limit=1"
EVENTS_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
FEEDS = {
    "CoinDesk": "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "Cointelegraph": "https://cointelegraph.com/rss",
    "Decrypt": "https://decrypt.co/feed",
}
BLACKOUT_BEFORE = 30 * 60   # لا دخول قبل الحدث بنصف ساعة
BLACKOUT_AFTER = 60 * 60    # ولا بعده بساعة

FNG_AR = {"Extreme Fear": "خوف شديد", "Fear": "خوف", "Neutral": "محايد", "Greed": "طمع", "Extreme Greed": "طمع شديد"}

NEG = ["hack", "hacked", "exploit", "stolen", "drain", "lawsuit", "sues", "sued", "ban", "bans", "crackdown",
       "plunge", "plunges", "crash", "crashes", "collapse", "bankrupt", "insolvent", "delist", "delisting",
       "fraud", "scam", "liquidations", "sell-off", "selloff", "outflows", "investigation", "charges", "halt",
       "suspends", "freeze", "dump", "rug", "vulnerability", "attack", "breach", "fine", "fined"]
POS = ["approval", "approved", "approves", "etf inflows", "inflows", "rally", "rallies", "surge", "surges",
       "soar", "soars", "record high", "all-time high", "ath", "adoption", "partnership", "launches", "upgrade",
       "bullish", "breakout", "buy", "buys", "accumulate", "reserve", "integrates", "wins", "listing"]
# أحداث تهزّ السوق كله
SEVERE = ["hack", "hacked", "exploit", "bankrupt", "insolvent", "collapse", "stolen", "drain"]

# ------------------------------------------------------------ تصنيف الخبر لتوليد النصيحة
CATEGORIES = {
    "security": ["hack", "hacked", "exploit", "stolen", "drain", "drained", "breach", "vulnerability", "attack", "phishing"],
    "regulation": ["sec", "lawsuit", "sues", "sued", "regulator", "regulation", "court", "ban", "bans", "crackdown",
                   "charges", "investigation", "license", "bill", "senate", "congress", "cftc", "mica"],
    "etf": ["etf", "inflows", "outflows", "blackrock", "fidelity", "institutional", "treasury", "reserve", "grayscale"],
    "macro": ["fed", "fomc", "powell", "rate cut", "rate hike", "interest rate", "inflation", "cpi", "jobs", "payrolls",
              "tariff", "tariffs", "dollar", "recession", "treasury yields", "gdp"],
    "listing": ["listing", "lists", "delist", "delisting", "listed"],
    "tech": ["upgrade", "mainnet", "launch", "launches", "fork", "testnet", "integration", "integrates"],
    "liquidation": ["liquidation", "liquidations", "liquidated", "short squeeze", "long squeeze"],
    "whale": ["whale", "whales", "accumulate", "accumulates", "accumulation", "dumps", "transfers"],
}


def categorize(title: str):
    t = title.lower()
    for cat, words in CATEGORIES.items():
        if any(re.search(rf"\b{re.escape(w)}\b", t) for w in words):
            return cat
    return "general"


def advise(n: dict, held: set) -> str:
    """نصيحة عملية مختصرة من الخبر (قراءة آلية — ليست نصيحة مالية)."""
    cat, sc, coins = n.get("cat", "general"), n.get("score", 0), n.get("coins", [])
    names = "، ".join(coins) if coins else "السوق"
    mine = [c for c in coins if c in held]
    pos = f" لديك صفقة مفتوحة في {'، '.join(mine)}: الوكيل يحميها بوقف الخسارة، ولا داعي لتدخلك." if mine else ""
    if n.get("severe"):
        return (f"⚠️ خبر خطير عن {names}: تجنّب الشراء الآن. الوكيل سيعترض تلقائياً على أي صفقة شراء فيها "
                f"لمدة 6 ساعات، والتقلب قد يكون حاداً.{pos}")
    if cat == "security":
        return f"🔒 خبر أمني يخص {names}: الأخبار الأمنية تضغط على السعر غالباً على المدى القصير — الحذر أفضل من مطاردة الارتداد.{pos}"
    if cat == "regulation":
        return ("⚖️ خبر تنظيمي: " + ("إيجابي على المدى المتوسط، لكن السوق قد يكون سعّره مسبقاً — لا تطارد القفزة الأولى." if sc > 0
                else "قد يسبب تقلباً وهبوطاً مؤقتاً. الأفضل انتظار وضوح الصورة قبل الدخول." if sc < 0
                else "تأثيره غير واضح بعد — راقب ردة فعل السعر بدلاً من التخمين.") + pos)
    if cat == "etf":
        return ("🏦 أموال مؤسسات: " + (f"تدفقات داخلة تدعم {names} عادة لأيام. الاتجاه إيجابي، لكن الدخول الأفضل عند الارتداد لا عند القمة." if sc >= 0
                else f"تدفقات خارجة تعني ضغط بيع مؤسسي على {names}. الحذر من الشراء المبكر.") + pos)
    if cat == "macro":
        return ("🌍 خبر اقتصادي عالمي: يحرك الكريبتو كله مع الأسهم والدولار. الوكيل يوقف الدخول تلقائياً حول الأحداث "
                "الكبرى (الفائدة والتضخم) لتجنب التقلب العشوائي." + pos)
    if cat == "listing":
        return ("📋 " + (f"إدراج جديد لـ {names}: يرفع السعر لحظياً غالباً ثم يتراجع. لا تشترِ بعد القفزة." if sc >= 0
                else f"شطب أو إيقاف لـ {names}: خبر سلبي قوي. ابتعد عنها حتى يستقر السعر.") + pos)
    if cat == "liquidation":
        return ("💥 موجة تصفيات: الحركات المبنية على التصفيات تكون مبالغاً فيها وترتد غالباً. "
                "استراتيجية «صيد التصفيات» مصممة لهذا الموقف تحديداً." + pos)
    if cat == "whale":
        return (f"🐋 نشاط حيتان في {names}: " + ("التجميع إشارة إيجابية متوسطة المدى." if sc >= 0 else
                "التحويلات الكبيرة للمنصات قد تسبق البيع.") + " تابع رادار الحيتان لترى إن كان النشاط مستمراً." + pos)
    if cat == "tech":
        return f"🛠️ تطوير تقني في {names}: أثره على السعر بطيء وطويل المدى عادة، وليس سبباً كافياً للدخول وحده.{pos}"
    if sc > 0:
        return f"📈 خبر إيجابي عن {names}: لا تطارد الصعود بعد الخبر مباشرة — الأفضل انتظار ارتداد وتأكيد الاتجاه.{pos}"
    if sc < 0:
        return f"📉 خبر سلبي عن {names}: قد يستمر الضغط قصير المدى. لا تستعجل الشراء حتى يتوقف الهبوط.{pos}"
    return f"ℹ️ خبر عام بلا أثر مباشر واضح على السعر — لا يتطلب أي إجراء.{pos}"


# ------------------------------------------------------------ الترجمة إلى العربية
TR_URL = "https://translate.googleapis.com/translate_a/single?client=gtx&sl=auto&tl=ar&dt=t&q="


def translate_ar(text: str) -> str | None:
    try:
        req = urllib.request.Request(TR_URL + urllib.parse.quote(text), headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read())
        out = "".join(seg[0] for seg in data[0] if seg and seg[0])
        return out.strip() or None
    except Exception as e:
        log.debug(f"translate: {e}")
        return None


ALIASES = {"BTC": ["bitcoin"], "ETH": ["ether", "ethereum"], "SOL": ["solana"], "XRP": ["ripple", "xrp"],
           "BNB": ["binance coin", "bnb"], "DOGE": ["dogecoin"], "ADA": ["cardano"], "TRX": ["tron"],
           "AVAX": ["avalanche"], "LINK": ["chainlink"], "DOT": ["polkadot"], "TON": ["toncoin"],
           "SUI": ["sui"], "LTC": ["litecoin"], "SHIB": ["shiba"], "PEPE": ["pepe"], "NEAR": ["near protocol"],
           "UNI": ["uniswap"], "APT": ["aptos"], "ARB": ["arbitrum"], "OP": ["optimism"], "HBAR": ["hedera"]}


def _get(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (trading-agent)",
                                               "Accept": "application/json, application/rss+xml, */*"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def score_headline(title: str):
    t = title.lower()
    neg = sum(1 for w in NEG if re.search(rf"\b{re.escape(w)}\b", t))
    pos = sum(1 for w in POS if re.search(rf"\b{re.escape(w)}\b", t))
    severe = any(re.search(rf"\b{re.escape(w)}\b", t) for w in SEVERE)
    return (pos - neg) / max(1, pos + neg), severe


def coins_in(title: str, bases):
    t = title.lower()
    out = []
    for b in bases:
        names = [b.lower()] + ALIASES.get(b, [])
        if any(re.search(rf"\b{re.escape(n)}\b", t) for n in names if len(n) >= 3 or n in ("op", "ada")):
            out.append(b)
    return out


class Mood:
    def __init__(self):
        self.fng = None            # {"value", "label", "label_ar", "ts"}
        self.news = []             # [{"title","link","source","ts","score","severe","coins"}]
        self.events = []           # [{"title","ts","impact","country"}]
        self._t = {"fng": 0, "news": 0, "events": 0}
        self.errors = {}
        self.tr = {}  # ذاكرة الترجمات: العنوان الإنجليزي -> العربي

    # ------------------------------------------------------------ جلب البيانات (في خيط منفصل)
    def refresh(self, bases):
        now = time.time()
        if now - self._t["fng"] > 3600:
            try:
                d = json.loads(_get(FNG_URL))["data"][0]
                cls = d.get("value_classification", "")
                self.fng = {"value": int(d["value"]), "label": cls, "label_ar": FNG_AR.get(cls, cls), "ts": now}
                self._t["fng"] = now
                self.errors.pop("fng", None)
            except Exception as e:
                self.errors["fng"] = str(e)[:80]
                self._t["fng"] = now - 3000  # إعادة المحاولة بعد 10 دقائق
        if now - self._t["news"] > 15 * 60:
            items = []
            for src, url in FEEDS.items():
                try:
                    root = ET.fromstring(_get(url))
                    for it in root.iter("item"):
                        title = (it.findtext("title") or "").strip()
                        if not title:
                            continue
                        try:
                            ts = parsedate_to_datetime(it.findtext("pubDate") or "").timestamp()
                        except Exception:
                            ts = now
                        sc, sev = score_headline(title)
                        items.append({"title": title[:180], "link": (it.findtext("link") or "")[:300],
                                      "source": src, "ts": ts, "score": sc, "severe": sev,
                                      "coins": coins_in(title, bases)})
                    self.errors.pop(src, None)
                except Exception as e:
                    self.errors[src] = str(e)[:80]
            if items:
                seen, uniq = set(), []
                for x in sorted(items, key=lambda x: -x["ts"]):
                    k = x["title"].lower()[:60]
                    if k not in seen and now - x["ts"] < 48 * 3600:
                        seen.add(k)
                        uniq.append(x)
                self.news = uniq[:60]
            self._translate()
            self._t["news"] = now
        if now - self._t["events"] > 6 * 3600:
            try:
                evs = []
                for e in json.loads(_get(EVENTS_URL)):
                    if e.get("country") != "USD" or e.get("impact") != "High":
                        continue
                    ts = datetime.fromisoformat(e["date"]).timestamp()
                    evs.append({"title": e.get("title", ""), "ts": ts, "impact": "High", "country": "USD"})
                self.events = sorted(evs, key=lambda x: x["ts"])
                self._t["events"] = now
                self.errors.pop("events", None)
            except Exception as e:
                self.errors["events"] = str(e)[:80]
                self._t["events"] = now - 6 * 3600 + 1800

    def _translate(self, limit=25):
        """يترجم العناوين الجديدة فقط (مرة واحدة لكل عنوان)."""
        if not self.tr:
            try:
                from . import memory
                self.tr = memory.kv_get("news_tr", {}) or {}
            except Exception:
                self.tr = {}
        new = 0
        for n in self.news[:limit]:
            if n["title"] not in self.tr:
                ar = translate_ar(n["title"])
                if ar:
                    self.tr[n["title"]] = ar
                    new += 1
                else:
                    self.errors["translate"] = "تعذرت الترجمة مؤقتاً"
                    break
        if new:
            self.errors.pop("translate", None)
            keep = {n["title"] for n in self.news}
            self.tr = {k: v for k, v in self.tr.items() if k in keep}
            try:
                from . import memory
                memory.kv_set("news_tr", self.tr)
            except Exception:
                pass

    # ------------------------------------------------------------ قراءات للوكيل
    def blackout(self):
        """الحدث الاقتصادي الذي يمنع الدخول الآن (إن وجد)."""
        now = time.time()
        for e in self.events:
            if e["ts"] - BLACKOUT_BEFORE <= now <= e["ts"] + BLACKOUT_AFTER:
                return e
        return None

    def next_event(self):
        now = time.time()
        return next((e for e in self.events if e["ts"] + BLACKOUT_AFTER > now), None)

    def coin_news(self, base, hours=6):
        """أخبار سلبية/إيجابية حديثة عن عملة معيّنة."""
        cut = time.time() - hours * 3600
        rel = [n for n in self.news if n["ts"] >= cut and base in n["coins"]]
        neg = [n for n in rel if n["score"] < 0]
        pos = [n for n in rel if n["score"] > 0]
        return {"neg": neg, "pos": pos, "severe": [n for n in neg if n["severe"]]}

    def market_score(self, hours=12):
        cut = time.time() - hours * 3600
        rel = [n["score"] for n in self.news if n["ts"] >= cut and n["score"] != 0]
        return sum(rel) / len(rel) if rel else 0.0

    def summary(self, held=None):
        held = held or set()
        news = []
        for n in self.news[:25]:
            x = dict(n)
            x["cat"] = categorize(n["title"])
            x["title_ar"] = self.tr.get(n["title"])
            x["advice"] = advise(x, held)
            news.append(x)
        nxt = self.next_event()
        bo = self.blackout()
        return {"fng": self.fng, "news_score": round(self.market_score(), 2),
                "news": news, "blackout": bo, "next_event": nxt,
                "events": [e for e in self.events if e["ts"] > time.time() - 3600][:8],
                "errors": self.errors}

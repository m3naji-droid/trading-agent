"""
🔑 مفاتيح Binance من داخل التطبيق.

- مجموعتان منفصلتان: مفاتيح الحساب التجريبي (Demo/Testnet) ومفاتيح الحساب الحقيقي.
- تُحفظ في ملف مستقل على السيرفر (صلاحية قراءة للمالك فقط)، وليست داخل قاعدة البيانات،
  فلا تدخل أبداً في ملف النسخة الاحتياطية الذي تنزّله.
- لا تُعرض كاملة أبداً في الواجهة: آخر 4 أحرف فقط.
- قبل الحفظ يُختبر المفتاح فعلياً على Binance، ويُرفض إن كان يملك صلاحية السحب.
- إن وُجدت مفاتيح قديمة في ملف .env تبقى تعمل كما هي (التوافق مع الإعداد السابق).
"""
import json
import logging
import os
import time
import urllib.request

from .config import settings

log = logging.getLogger("keys")
PATH = os.path.join(settings.data_dir, "keys.json")
WHICH = ("testnet", "live")


def _load():
    try:
        with open(PATH) as f:
            return json.load(f)
    except Exception:
        return {}


def _save(d):
    os.makedirs(os.path.dirname(PATH) or ".", exist_ok=True)
    tmp = PATH + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(d, f)
    os.replace(tmp, PATH)
    os.chmod(PATH, 0o600)


def get(mode):
    """يرجع (key, secret) للوضع المطلوب، أو ("", "")."""
    if mode not in WHICH:
        return "", ""
    e = _load().get(mode) or {}
    if e.get("key") and e.get("secret"):
        return e["key"], e["secret"]
    if settings.api_key and settings.api_secret:  # الإعداد القديم من ملف .env
        return settings.api_key, settings.api_secret
    return "", ""


def has(mode):
    return bool(get(mode)[0])


def save(mode, key, secret, check):
    d = _load()
    d[mode] = {"key": key, "secret": secret, "saved": time.time(), "check": check}
    _save(d)


def delete(mode):
    d = _load()
    d.pop(mode, None)
    _save(d)


def mask(k):
    return ("•" * 6 + k[-4:]) if k and len(k) > 8 else ""


def status():
    d = _load()
    out = {}
    for m in WHICH:
        e = d.get(m) or {}
        if e.get("key"):
            out[m] = {"set": True, "source": "app", "masked": mask(e["key"]), "saved": e.get("saved"),
                      "check": e.get("check")}
        elif settings.api_key:
            out[m] = {"set": True, "source": "env", "masked": mask(settings.api_key), "saved": None, "check": None}
        else:
            out[m] = {"set": False}
    out["ip"] = _ip["v"] or os.getenv("SERVER_IP") or None  # يُجلب مسبقاً في الخلفية عند التشغيل
    return out


_ip = {"v": None, "t": 0}


def server_ip():
    """عنوان IP السيرفر — يضعه المستخدم في قائمة IP المسموحة في Binance."""
    if _ip["v"] and time.time() - _ip["t"] < 6 * 3600:
        return _ip["v"]
    v = os.getenv("SERVER_IP", "")
    if not v:
        for url in ("https://api.ipify.org", "https://checkip.amazonaws.com"):
            try:
                with urllib.request.urlopen(url, timeout=8) as r:
                    v = r.read().decode().strip()
                    break
            except Exception:
                continue
    if v:
        _ip.update(v=v, t=time.time())
    return v or None


def clean(s):
    """يزيل المسافات والأسطر التي تُضاف عند النسخ من الجوال."""
    return "".join((s or "").split())


async def verify(mode, key, secret, make_client=None):
    """
    يختبر المفتاح فعلياً على Binance. يرجع تقريراً:
    {ok, fatal, items:[(ok|warn|bad, نص)], balance, futures, spot, withdraw, ip_restrict}
    """
    import ccxt.async_support as ccxt
    items, rep = [], {"ok": False, "fatal": None}
    common = {"enableRateLimit": True, "timeout": 20000, "apiKey": key, "secret": secret,
              "options": {"adjustForTimeDifference": True}}
    spot = make_client("spot", common) if make_client else ccxt.binance(common | {"options": {"defaultType": "spot", "adjustForTimeDifference": True}})
    fut = make_client("fut", common) if make_client else ccxt.binanceusdm(common)
    if mode == "testnet" and not make_client:
        for ex in (spot, fut):
            try:
                ex.enable_demo_trading(True)
            except Exception:
                try:
                    ex.set_sandbox_mode(True)
                except Exception:
                    pass
    try:
        # 1) هل المفتاح صحيح؟
        try:
            bal = await spot.fetch_balance()
            usdt = float((bal.get("total") or {}).get("USDT") or 0)
            rep["balance_spot"] = usdt
            items.append(("ok", f"المفتاح صحيح — رصيد Spot: {usdt:,.2f} USDT"))
        except Exception as e:
            msg = str(e)
            if "-2015" in msg or "Invalid API-key" in msg or "IP" in msg:
                rep["fatal"] = ("المفتاح مرفوض من Binance: إما أن المفتاح أو الكلمة السرية غير صحيحة، "
                                "أو أن عنوان IP السيرفر غير مضاف في قائمة المسموح بها.")
            elif "-2014" in msg or "-1022" in msg or "Signature" in msg:
                rep["fatal"] = "الكلمة السرية (Secret Key) غير صحيحة — انسخها مرة أخرى كاملة."
            else:
                rep["fatal"] = f"تعذر الاتصال بـ Binance: {msg[:140]}"
            return {**rep, "items": items}
        # 2) العقود (Futures)
        try:
            fb = await fut.fetch_balance()
            fu = float((fb.get("total") or {}).get("USDT") or 0)
            rep["balance_fut"] = fu
            rep["futures"] = True
            items.append(("ok", f"العقود (Futures) تعمل — رصيدها: {fu:,.2f} USDT"))
        except Exception:
            rep["futures"] = False
            items.append(("warn", "العقود (Futures) غير مفعّلة لهذا المفتاح — سيتداول الوكيل في Spot فقط. "
                                  "للتفعيل: فعّل خيار Enable Futures في إعدادات المفتاح."))
        # 3) الصلاحيات (الحساب الحقيقي فقط — الحساب التجريبي لا يدعم هذا الفحص)
        if mode == "live":
            try:
                r = await spot.sapi_get_account_apirestrictions()
                rep["withdraw"] = bool(r.get("enableWithdrawals"))
                rep["ip_restrict"] = bool(r.get("ipRestrict"))
                rep["spot"] = bool(r.get("enableSpotAndMarginTrading"))
                if rep["withdraw"]:
                    rep["fatal"] = ("⛔ هذا المفتاح يملك صلاحية السحب (Enable Withdrawals). لحماية أموالك يرفضه الوكيل. "
                                    "عطّل السحب من إعدادات المفتاح في Binance ثم أعد المحاولة.")
                    return {**rep, "items": items}
                items.append(("ok", "صلاحية السحب معطّلة ✅ — لا يمكن لأحد سحب أموالك عبر هذا المفتاح"))
                if rep["ip_restrict"]:
                    items.append(("ok", "المفتاح مقيّد بعنوان IP السيرفر ✅ (أعلى درجة أمان)"))
                if not rep["spot"]:
                    items.append(("bad", "صلاحية التداول (Enable Spot & Margin Trading) غير مفعّلة — الوكيل لن يستطيع الشراء والبيع."
                                  + ("" if rep["ip_restrict"] else " السبب: Binance لا يسمح بتفعيل التداول إلا بعد تقييد المفتاح "
                                     "بعنوان IP السيرفر — اختر Restrict access to trusted IPs وألصق عنوان السيرفر، ثم فعّل التداول.")))
            except Exception as e:
                rep["fatal"] = f"تعذر التحقق من صلاحيات المفتاح ({str(e)[:100]}) — لن يُحفظ احتياطاً."
                return {**rep, "items": items}
        else:
            items.append(("ok", "حساب تجريبي — أموال وهمية، لا خطر على أموالك"))
        rep["ok"] = not any(k == "bad" for k, _ in items)
        return {**rep, "items": items}
    finally:
        for ex in (spot, fut):
            try:
                await ex.close()
            except Exception:
                pass

"""
الربط بـ Binance عبر CCXT (Spot + USDT-M Futures).
⚠️ استخدم مفاتيح API بصلاحيات: القراءة + التداول (Spot & Futures) فقط.
   لا تفعّل صلاحية السحب (Withdrawals) أبداً، وقيّد المفتاح بعنوان IP السيرفر.
"""
import logging
import pandas as pd
import ccxt.async_support as ccxt

from .config import settings

log = logging.getLogger("exchange")

STABLES = {"USDC", "FDUSD", "TUSD", "BUSD", "DAI", "USDP", "EUR", "USDE", "PYUSD", "USD1", "AEUR", "XUSD"}


def make_clients(mode: str):
    """يرجع (spot, futures). في وضع paper تُستخدم بيانات السوق العامة فقط."""
    common = {"enableRateLimit": True, "timeout": 20000}
    keyed = mode in ("testnet", "live") and settings.api_key
    if keyed:
        common |= {"apiKey": settings.api_key, "secret": settings.api_secret}
    spot = ccxt.binance(common | {"options": {"defaultType": "spot", "adjustForTimeDifference": True}})
    fut = ccxt.binanceusdm(common | {"options": {"adjustForTimeDifference": True}})
    if mode == "testnet":
        for ex in (spot, fut):
            try:
                ex.enable_demo_trading(True)      # إصدارات ccxt الحديثة (Binance Demo Trading)
            except Exception:
                try:
                    ex.set_sandbox_mode(True)     # الإصدارات الأقدم (testnet)
                except Exception as e:
                    log.warning(f"تعذر تفعيل testnet: {e}")
    return spot, fut


async def check_key_permissions(spot) -> dict:
    """يتحقق أن المفتاح لا يملك صلاحية السحب. يرفض التشغيل إن وُجدت."""
    try:
        r = await spot.sapi_get_account_apirestrictions()
        if r.get("enableWithdrawals"):
            return {"ok": False, "msg": "المفتاح يملك صلاحية سحب! عطّلها من Binance ثم أعد التشغيل."}
        return {"ok": True, "msg": "صلاحيات المفتاح آمنة (بدون سحب)",
                "futures": bool(r.get("enableFutures")), "spot": bool(r.get("enableSpotAndMarginTrading"))}
    except Exception as e:
        return {"ok": True, "msg": f"تعذر فحص الصلاحيات ({e}) — تأكد يدوياً أن السحب معطل"}


async def ohlcv_df(ex, symbol, tf, limit=300) -> pd.DataFrame:
    rows = []
    since = None
    remaining = limit
    # Binance يسمح بـ 1000 شمعة لكل طلب؛ نجمع على دفعات للتاريخ الطويل
    if limit > 1000:
        ms = ex.parse_timeframe(tf) * 1000
        since = ex.milliseconds() - limit * ms
    while remaining > 0:
        batch = await ex.fetch_ohlcv(symbol, tf, since=since, limit=min(1000, remaining))
        if not batch:
            break
        rows += batch
        remaining -= len(batch)
        if since is None or len(batch) < 1000:
            break
        since = batch[-1][0] + 1
    df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume"])
    return df.drop_duplicates("ts").reset_index(drop=True)


async def build_universe(spot, fut, size, min_vol) -> list:
    """أعلى العملات سيولةً المتاحة في Spot و Futures معاً."""
    await spot.load_markets()
    await fut.load_markets()
    tickers = await spot.fetch_tickers()
    fut_bases = {m["base"] for m in fut.markets.values() if m.get("swap") and m.get("quote") == "USDT" and m.get("active")}
    cands = []
    for sym, t in tickers.items():
        m = spot.markets.get(sym)
        if not m or m.get("quote") != "USDT" or not m.get("active") or not m.get("spot"):
            continue
        base = m["base"]
        if base in STABLES or base.endswith(("UP", "DOWN", "BULL", "BEAR")):
            continue
        if base not in fut_bases:
            continue
        qv = t.get("quoteVolume") or 0
        if qv >= min_vol:
            cands.append((qv, sym))
    cands.sort(reverse=True)
    return [s for _, s in cands[:size]]


def fut_symbol(sym: str) -> str:
    return sym if ":" in sym else sym + ":USDT"

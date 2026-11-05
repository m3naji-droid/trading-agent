"""
مجلس الوكلاء: ثلاث شخصيات تصوّت على كل فرصة قبل الدخول.
  🦁 الجريء  — يبحث عن الفرصة: عائد مقابل مخاطرة، ثقة، زخم.
  🛡️ الحذر   — يبحث عن المخاطر: خسائر متتالية، تذبذب، تدفق معاكس، نمط خاسر، أخبار سيئة.
  🔄 المعاكس — يشكك في الجمهور: ازدحام، طمع/خوف متطرف، تمويل مكتظ.
لا تُفتح الصفقة إلا بموافقة صوتين على الأقل.
"""
from .config import settings

MEMBERS = {"bold": ("🦁", "الجريء"), "cautious": ("🛡️", "الحذر"), "contrarian": ("🔄", "المعاكس")}


def _vote(key, yes, reasons):
    icon, name = MEMBERS[key]
    return {"key": key, "icon": icon, "name": name, "vote": bool(yes), "reason": "، ".join(reasons)}


def bold(sig, ctx, conf):
    l = ctx["df"].iloc[-1]
    r = []
    good = 0
    if sig.rr >= 2:
        good += 1; r.append(f"العائد {sig.rr:.1f} ضعف المخاطرة")
    elif sig.rr >= 1.5:
        good += 0.5; r.append(f"عائد مقبول {sig.rr:.1f}R")
    else:
        r.append(f"العائد ضعيف {sig.rr:.1f}R")
    if conf >= settings.min_confidence:
        good += 1; r.append(f"الثقة {conf:.0%}")
    elif conf >= settings.min_confidence - 0.05:
        good += 0.5; r.append(f"ثقة على الحافة {conf:.0%}")
    else:
        r.append(f"ثقة منخفضة {conf:.0%}")
    vz = l.get("vol_z", 0) or 0
    if vz > 1:
        good += 0.5; r.append("حجم تداول قوي")
    return _vote("bold", good >= 2, r)


def cautious(sig, ctx, risk, pattern, mood, base):
    d = 1 if sig.side == "long" else -1
    flow = ctx.get("flow") or {}
    concerns, severe = [], False
    if risk.loss_streak >= 2:
        concerns.append(f"{risk.loss_streak} خسائر متتالية")
    if risk.day_start_equity and risk.daily_dd_now > settings.max_daily_drawdown * 0.5:
        concerns.append("تراجع اليوم تجاوز نصف الحد")
    vr = ctx["regime"].get("vol_rank", 0)
    if vr > 0.95:
        severe = True; concerns.append("تذبذب شاذ جداً")
    elif vr > 0.85:
        concerns.append("تذبذب مرتفع")
    if flow.get("imbalance", 0) * d < -0.2:
        concerns.append("دفتر الأوامر يعاكس")
    if (flow.get("whale") or {}).get("ratio", 0) * d < -0.3:
        concerns.append("الحيتان تعاكس")
    if pattern and pattern["n"] >= 4 and pattern["winrate"] < 0.45:
        concerns.append(f"نمط مشابه ربح {pattern['winrate']:.0%} فقط")
    if mood is not None:
        cn = mood.coin_news(base)
        if cn["severe"]:
            severe = True; concerns.append("خبر خطير: " + cn["severe"][0]["title"][:60])
        elif cn["neg"] and d == 1:
            concerns.append("أخبار سلبية حديثة عن العملة")
        ms = mood.market_score()
        if ms < -0.35 and d == 1:
            concerns.append("معنويات الأخبار سلبية")
    yes = not severe and len(concerns) < 2
    return _vote("cautious", yes, concerns or ["لا مخاطر واضحة"])


def contrarian(sig, ctx, mood):
    d = 1 if sig.side == "long" else -1
    l = ctx["df"].iloc[-1]
    flow = ctx.get("flow") or {}
    crowd, edge = [], []
    fng = (mood.fng or {}).get("value") if mood is not None else None
    if fng is not None:
        if d == 1 and fng >= 80:
            crowd.append(f"الجمهور في طمع شديد ({fng})")
        elif d == -1 and fng <= 20:
            crowd.append(f"الجمهور في خوف شديد ({fng}) — البيع متأخر")
        elif d == 1 and fng <= 25:
            edge.append(f"الجمهور خائف ({fng}) — فرصة معاكسة")
        elif d == -1 and fng >= 75:
            edge.append(f"الجمهور طمّاع ({fng}) — فرصة معاكسة")
    f = flow.get("funding", 0) or 0
    if d == 1 and f > 0.0005:
        crowd.append("المشترون مزدحمون (تمويل مرتفع)")
    if d == -1 and f < -0.0005:
        crowd.append("البائعون مزدحمون (تمويل سالب)")
    rsi = l.get("rsi", 50)
    if sig.strategy == "momentum_breakout" and ((d == 1 and rsi > 75) or (d == -1 and rsi < 25)):
        crowd.append(f"الحركة ممتدة جداً (RSI {rsi:.0f})")
    liq = (flow.get("liq") or {}).get("bias", 0) * d
    if liq > 0.5:
        edge.append("تصفيات الطرف الآخر تدعم الحركة")
    yes = len(crowd) == 0 or (len(crowd) == 1 and len(edge) >= 1)
    return _vote("contrarian", yes, crowd + edge or ["الجمهور ليس مزدحماً في نفس الاتجاه"])


def deliberate(sig, ctx, conf, risk, pattern, mood, base):
    votes = [bold(sig, ctx, conf), cautious(sig, ctx, risk, pattern, mood, base), contrarian(sig, ctx, mood)]
    yes = sum(v["vote"] for v in votes)
    return {"votes": votes, "yes": yes, "approved": yes >= 2}

"""
🛡️ حماية الأرباح — ثلاث أدوات يقرر داروين بالأدلة متى تُستخدم (أو تُطفأ):

1) حارس الأرباح (pg, pg_sig): بعد أن تبلغ الصفقة ربحاً ≥ pg×R، إذا ظهرت pg_sig علامات انعكاس على شمعة مغلقة
   => إغلاق فوراً (بربح صغير أو خسارة صغيرة) بدلاً من انتظار وقف الخسارة الكامل.
   علامات الانعكاس: شمعة انعكاس قوية ضد الصفقة · إغلاق تحت/فوق EMA20 · تراجع الزخم (RSI) ·
   حجم كبير في الاتجاه المعاكس · (حياً فقط) دفتر الأوامر والحيتان ينقلبان ضد الصفقة.
2) جني ربح جزئي (tp1): عند +tp1×R يُغلق نصف الصفقة، ويُنقل وقف الباقي لسعر الدخول.
3) قاعدة إرجاع الربح (gb): بعد بلوغ +1R، إذا أعادت الصفقة نسبة gb من أعلى ربح => إغلاق.

القيم القصوى تعني "مطفأة": pg ≥ 1.95 · tp1 ≥ 1.95 · gb ≥ 0.95.
"""
import math

PG_OFF, TP1_OFF, GB_OFF = 1.95, 1.95, 0.95
PARTIAL = 0.5
GB_ARM_R = 1.0
PG_FLOOR = -0.3   # لا يُفعَّل الحارس إذا كانت الصفقة أصلاً قريبة من وقفها (تجنب الذعر المتأخر)


def pg_on(g):
    return g.get("pg", 2.0) < PG_OFF


def tp1_on(g):
    return g.get("tp1", 2.0) < TP1_OFF


def gb_on(g):
    return g.get("gb", 1.0) < GB_OFF


def _ok(x):
    return x is not None and not (isinstance(x, float) and math.isnan(x))


def warnings(bar, prev, side, flow=None):
    """عدد علامات الانعكاس على شمعة مغلقة (+ تدفق الأوامر إن توفر حياً). يرجع (عدد, أسباب)."""
    d = 1 if side == "long" else -1
    out = []
    o, c, h, l = bar["open"], bar["close"], bar["high"], bar["low"]
    rng = max(h - l, 1e-12)
    atr = bar.get("atr")
    body = (c - o) * d
    if body < 0 and -body >= 0.6 * rng and (not _ok(atr) or rng >= 0.8 * atr):
        out.append("شمعة انعكاس قوية")
    e20 = bar.get("ema20")
    if _ok(e20) and (c - e20) * d < 0:
        out.append("إغلاق " + ("تحت" if d == 1 else "فوق") + " EMA20")
    rsi, prsi = bar.get("rsi"), (prev.get("rsi") if prev is not None else None)
    if _ok(rsi) and _ok(prsi):
        if (d == 1 and rsi < 45 and prsi >= 45) or (d == -1 and rsi > 55 and prsi <= 55) or (rsi - prsi) * d < -12:
            out.append("تراجع الزخم (RSI)")
    vz = bar.get("vol_z")
    if _ok(vz) and vz > 1.5 and body < 0:
        out.append("حجم كبير ضد الصفقة")
    if flow:
        if flow.get("imbalance", 0) * d < -0.25 or (flow.get("whale") or {}).get("ratio", 0) * d < -0.3:
            out.append("الحيتان ودفتر الأوامر ضد الصفقة")
    return len(out), out

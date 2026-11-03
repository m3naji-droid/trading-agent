"""
كشف حالة السوق (Market Regime):
TREND_UP / TREND_DOWN / RANGE / HIGH_RISK
"""
import pandas as pd

TREND_UP, TREND_DOWN, RANGE, HIGH_RISK = "TREND_UP", "TREND_DOWN", "RANGE", "HIGH_RISK"

LABELS_AR = {
    TREND_UP: "اتجاه صاعد",
    TREND_DOWN: "اتجاه هابط",
    RANGE: "سوق عرضي",
    HIGH_RISK: "خطورة عالية",
}


def detect_regime(df: pd.DataFrame) -> dict:
    """df: شموع مُثراة بالمؤشرات (عادة فريم الساعة)."""
    last = df.iloc[-1]
    hist_vol = df["atr_pct"].iloc[-500:]
    vol_rank = float((hist_vol < last["atr_pct"]).mean())  # ترتيب التذبذب الحالي تاريخياً
    ret_std = df["ret"].iloc[-200:].std() or 1e-9
    shock = abs(df["ret"].iloc[-3:]).max() / ret_std
    drop_24 = df["close"].iloc[-1] / df["close"].iloc[-24:].max() - 1 if len(df) > 24 else 0

    reason = []
    if vol_rank > 0.97 or shock > 6 or drop_24 < -0.10:
        reason.append(f"تذبذب شاذ (ترتيب {vol_rank:.2f}, صدمة {shock:.1f}σ, هبوط24س {drop_24:.1%})")
        regime = HIGH_RISK
    else:
        slope = (last["ema50"] - df["ema50"].iloc[-10]) / last["ema50"]
        stacked_up = last["close"] > last["ema20"] > last["ema50"] > last["ema200"]
        stacked_dn = last["close"] < last["ema20"] < last["ema50"] < last["ema200"]
        if last["adx"] > 23 and (stacked_up or (slope > 0.004 and last["close"] > last["ema50"])):
            regime = TREND_UP
        elif last["adx"] > 23 and (stacked_dn or (slope < -0.004 and last["close"] < last["ema50"])):
            regime = TREND_DOWN
        else:
            regime = RANGE
        reason.append(f"ADX={last['adx']:.0f} ميل EMA50={slope:.3%}")

    return {
        "regime": regime,
        "label": LABELS_AR[regime],
        "adx": float(last["adx"]),
        "vol_rank": vol_rank,
        "shock": float(shock),
        "reason": "؛ ".join(reason),
    }

"""
مفاهيم الأموال الذكية (Smart Money Concepts):
- فجوات القيمة العادلة FVG
- كتل الأوامر Order Blocks
- كسر الهيكل BOS وسحب السيولة Liquidity Sweeps
- مناطق التجميع / التصريف
"""
import numpy as np
import pandas as pd


def swing_points(df: pd.DataFrame, k: int = 3):
    h, l = df["high"].values, df["low"].values
    highs, lows = [], []
    for i in range(k, len(df) - k):
        if h[i] == h[i - k:i + k + 1].max():
            highs.append(i)
        if l[i] == l[i - k:i + k + 1].min():
            lows.append(i)
    return highs, lows


def find_fvgs(df: pd.DataFrame, lookback: int = 150, min_size_atr: float = 0.3):
    """فجوات لم تُملأ بعد. bull: قاع الشمعة i فوق قمة i-2."""
    out = []
    start = max(2, len(df) - lookback)
    h, l, a = df["high"].values, df["low"].values, df["atr"].values
    for i in range(start, len(df)):
        if l[i] > h[i - 2] and (l[i] - h[i - 2]) > min_size_atr * a[i]:
            top, bot = l[i], h[i - 2]
            filled = (l[i + 1:] <= bot).any() if i + 1 < len(df) else False
            if not filled:
                out.append({"type": "bull", "top": float(top), "bottom": float(bot), "idx": i})
        if h[i] < l[i - 2] and (l[i - 2] - h[i]) > min_size_atr * a[i]:
            top, bot = l[i - 2], h[i]
            filled = (h[i + 1:] >= top).any() if i + 1 < len(df) else False
            if not filled:
                out.append({"type": "bear", "top": float(top), "bottom": float(bot), "idx": i})
    return out


def find_order_blocks(df: pd.DataFrame, lookback: int = 150, disp_atr: float = 1.8):
    """آخر شمعة معاكسة قبل حركة اندفاعية قوية (displacement)."""
    out = []
    o, c, h, l, a = (df[x].values for x in ("open", "close", "high", "low", "atr"))
    start = max(1, len(df) - lookback)
    for i in range(start, len(df) - 3):
        move = c[i + 3] - c[i]
        if move > disp_atr * a[i] and c[i] < o[i]:  # شمعة هابطة تلتها اندفاعة صاعدة
            mitigated = (l[i + 4:] <= l[i]).any()
            if not mitigated:
                out.append({"type": "bull", "top": float(h[i]), "bottom": float(l[i]), "idx": i})
        if -move > disp_atr * a[i] and c[i] > o[i]:
            mitigated = (h[i + 4:] >= h[i]).any()
            if not mitigated:
                out.append({"type": "bear", "top": float(h[i]), "bottom": float(l[i]), "idx": i})
    return out[-6:]


def liquidity_sweep(df: pd.DataFrame, k: int = 3):
    """ذيل يخترق قمة/قاع سابق ثم إغلاق داخله = اصطياد سيولة."""
    highs, lows = swing_points(df.iloc[-120:], k)
    sub = df.iloc[-120:]
    last = df.iloc[-1]
    res = {"bull_sweep": False, "bear_sweep": False}
    if lows:
        lvl = sub["low"].iloc[lows[-1]]
        if last["low"] < lvl < last["close"]:
            res["bull_sweep"] = True
    if highs:
        lvl = sub["high"].iloc[highs[-1]]
        if last["high"] > lvl > last["close"]:
            res["bear_sweep"] = True
    return res


def structure(df: pd.DataFrame, k: int = 3):
    """كسر الهيكل: الإغلاق فوق آخر قمة متأرجحة أو تحت آخر قاع."""
    sub = df.iloc[-200:]
    highs, lows = swing_points(sub, k)
    c = sub["close"].iloc[-1]
    bos = 0
    if highs and c > sub["high"].iloc[highs[-1]]:
        bos = 1
    elif lows and c < sub["low"].iloc[lows[-1]]:
        bos = -1
    return bos


def accumulation_distribution(df: pd.DataFrame, n: int = 48):
    """
    تجميع: نطاق ضيق + تدفق حجم موجب (OBV صاعد) بينما السعر مستقر.
    تصريف: نطاق ضيق + OBV هابط.
    """
    sub = df.iloc[-n:]
    rng = (sub["high"].max() - sub["low"].min()) / sub["close"].iloc[-1]
    obv = (np.sign(sub["close"].diff().fillna(0)) * sub["volume"]).cumsum()
    obv_slope = np.polyfit(np.arange(len(obv)), obv.values, 1)[0] / (sub["volume"].mean() + 1e-9)
    price_slope = np.polyfit(np.arange(n), sub["close"].values, 1)[0] / sub["close"].mean()
    narrow = rng < 4 * sub["atr_pct"].mean() * np.sqrt(n / 14)
    if narrow and obv_slope > 0.15 and abs(price_slope) < 0.002:
        return "accumulation"
    if narrow and obv_slope < -0.15 and abs(price_slope) < 0.002:
        return "distribution"
    return "none"


def analyze(df: pd.DataFrame) -> dict:
    price = float(df["close"].iloc[-1])
    fvgs = find_fvgs(df)
    obs = find_order_blocks(df)

    def inside(zones, t):
        for z in reversed(zones):
            if z["type"] == t and z["bottom"] * 0.998 <= price <= z["top"] * 1.002:
                return z
        return None

    return {
        "fvgs": fvgs[-8:],
        "order_blocks": obs,
        "in_bull_fvg": inside(fvgs, "bull"),
        "in_bear_fvg": inside(fvgs, "bear"),
        "in_bull_ob": inside(obs, "bull"),
        "in_bear_ob": inside(obs, "bear"),
        "sweep": liquidity_sweep(df),
        "bos": structure(df),
        "phase": accumulation_distribution(df),
    }

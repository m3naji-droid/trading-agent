"""مؤشرات فنية أساسية مكتوبة بـ pandas/numpy (بدون مكتبات خارجية)."""
import numpy as np
import pandas as pd


def ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False).mean()


def rsi(close: pd.Series, n: int = 14) -> pd.Series:
    d = close.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    rs = up / dn.replace(0, np.nan)
    return (100 - 100 / (1 + rs)).fillna(50)


def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    h, l, c = df["high"], df["low"], df["close"]
    tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False).mean()


def adx(df: pd.DataFrame, n: int = 14) -> pd.Series:
    h, l = df["high"], df["low"]
    up = h.diff()
    dn = -l.diff()
    plus_dm = np.where((up > dn) & (up > 0), up, 0.0)
    minus_dm = np.where((dn > up) & (dn > 0), dn, 0.0)
    a = atr(df, n)
    plus_di = 100 * pd.Series(plus_dm, index=df.index).ewm(alpha=1 / n, adjust=False).mean() / a
    minus_di = 100 * pd.Series(minus_dm, index=df.index).ewm(alpha=1 / n, adjust=False).mean() / a
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    return dx.ewm(alpha=1 / n, adjust=False).mean().fillna(0)


def bollinger(close: pd.Series, n: int = 20, k: float = 2.0):
    mid = close.rolling(n).mean()
    sd = close.rolling(n).std()
    return mid - k * sd, mid, mid + k * sd


def enrich(df: pd.DataFrame) -> pd.DataFrame:
    """يضيف كل المؤشرات المطلوبة للشموع."""
    df = df.copy()
    c = df["close"]
    df["ema20"] = ema(c, 20)
    df["ema50"] = ema(c, 50)
    df["ema200"] = ema(c, 200)
    df["rsi"] = rsi(c)
    df["atr"] = atr(df)
    df["atr_pct"] = df["atr"] / c
    df["adx"] = adx(df)
    df["bb_low"], df["bb_mid"], df["bb_up"] = bollinger(c)
    df["bb_width"] = (df["bb_up"] - df["bb_low"]) / df["bb_mid"]
    df["vol_z"] = (df["volume"] - df["volume"].rolling(50).mean()) / df["volume"].rolling(50).std()
    df["ret"] = c.pct_change()
    df["hh20"] = df["high"].rolling(20).max().shift(1)
    df["ll20"] = df["low"].rolling(20).min().shift(1)
    # قناة كيلتنر + الانضغاط (البولنجر داخل كيلتنر)
    df["kc_up"] = df["ema20"] + 1.5 * df["atr"]
    df["kc_low"] = df["ema20"] - 1.5 * df["atr"]
    df["squeeze"] = (df["bb_up"] < df["kc_up"]) & (df["bb_low"] > df["kc_low"])
    df["sq_len"] = df["squeeze"].astype(int).groupby((~df["squeeze"]).cumsum()).cumsum()
    df["obv"] = (np.sign(c.diff().fillna(0)) * df["volume"]).cumsum()
    # v11
    df["rsi2"] = rsi(c, 2)
    df["hh55"] = df["high"].rolling(55).max().shift(1)
    df["ll55"] = df["low"].rolling(55).min().shift(1)
    df["st_line"], df["st_dir"] = supertrend(df)
    day = df["ts"] // 86_400_000
    tp = (df["high"] + df["low"] + c) / 3
    pv = (tp * df["volume"]).groupby(day).cumsum()
    vv = df["volume"].groupby(day).cumsum()
    df["vwap"] = pv / vv.replace(0, np.nan)
    dh = df["high"].groupby(day).max()
    dl = df["low"].groupby(day).min()
    # اليوم الأول في النافذة بلا "أمس": نتركه دون مستوى بدلاً من حذف صفوفه
    df["pdh"] = day.map(dh.shift(1)).fillna(-1.0)
    df["pdl"] = day.map(dl.shift(1)).fillna(-1.0)
    df["vwap"] = df["vwap"].fillna(c)
    return df


def supertrend(df: pd.DataFrame, n: int = 10, mult: float = 3.0):
    """خط الترند الخارق واتجاهه (+1 صاعد / -1 هابط)."""
    a = atr(df, n).values
    h, l, c = df["high"].values, df["low"].values, df["close"].values
    mid = (h + l) / 2
    up, dn = mid - mult * a, mid + mult * a
    line = np.full(len(c), np.nan)
    d = np.ones(len(c), dtype=int)
    fu, fd = up[0], dn[0]
    for i in range(1, len(c)):
        fu = max(up[i], fu) if c[i - 1] > fu else up[i]
        fd = min(dn[i], fd) if c[i - 1] < fd else dn[i]
        if c[i] > fd:
            d[i] = 1
        elif c[i] < fu:
            d[i] = -1
        else:
            d[i] = d[i - 1]
        line[i] = fu if d[i] == 1 else fd
    return pd.Series(line, index=df.index), pd.Series(d, index=df.index)

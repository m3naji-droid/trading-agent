"""
بيانات مشتركة بين العملات (تُضاف كأعمدة للشموع حتى تعمل نفس الاستراتيجيات حياً وعلى التاريخ):
- ظل البيتكوين: اختراق BTC الحديث، معامل بيتا والارتباط بين العملة والبيتكوين.
- القوة النسبية: ترتيب العملة بين العملات الثلاثين مقارنة بالبيتكوين.
- التمويل والعقود المفتوحة (Futures).
"""
import numpy as np
import pandas as pd


def add_btc_link(df: pd.DataFrame, btc: pd.DataFrame, beta_win=200) -> pd.DataFrame:
    """df و btc شموع بنفس الفريم (يجب أن يحوي btc أعمدة enrich)."""
    b = btc[["ts", "close", "hh20", "ll20", "vol_z", "ret"]].rename(
        columns={"close": "btc_close", "hh20": "btc_hh20", "ll20": "btc_ll20", "vol_z": "btc_vz", "ret": "btc_ret"})
    df = df.copy()
    b = b.copy()
    df["ts"] = df["ts"].astype("int64")
    b["ts"] = b["ts"].astype("int64")
    out = df.merge(b, on="ts", how="left")
    brk_up = (out["btc_close"] > out["btc_hh20"]) & (out["btc_vz"] > 0.8)
    brk_dn = (out["btc_close"] < out["btc_ll20"]) & (out["btc_vz"] > 0.8)
    out["btc_brk"] = np.where(brk_up.rolling(3, min_periods=1).max() > 0, 1,
                              np.where(brk_dn.rolling(3, min_periods=1).max() > 0, -1, 0))
    out["btc_ret3"] = out["btc_close"].pct_change(3)
    out["alt_ret3"] = out["close"].pct_change(3)
    r, br = out["ret"], out["btc_ret"]
    cov = r.rolling(beta_win, min_periods=60).cov(br)
    var = br.rolling(beta_win, min_periods=60).var()
    out["beta"] = cov / var.replace(0, np.nan)
    out["corr"] = r.rolling(beta_win, min_periods=60).corr(br)
    return out


def rs_panel(closes: dict, btc_key="BTC", lookback=168) -> pd.DataFrame:
    """
    closes: {base: Series(close, index=ts)} — يرجع جدول ترتيب نسبي (0 = الأقوى، 1 = الأضعف)
    حسب عائد آخر lookback شمعة ناقص عائد البيتكوين.
    """
    panel = pd.DataFrame(closes).sort_index().ffill(limit=3)
    ret = panel.pct_change(lookback)
    rs = ret.sub(ret[btc_key], axis=0) if btc_key in ret else ret
    rs = rs.drop(columns=[btc_key], errors="ignore")
    return rs.rank(axis=1, ascending=False, pct=True)


def add_rs(df: pd.DataFrame, rank_series) -> pd.DataFrame:
    if rank_series is None:
        df["rs_rank"] = np.nan
        return df
    if np.isscalar(rank_series):
        df["rs_rank"] = rank_series
        return df
    s = rank_series.rename("rs_rank")
    s.index = s.index.astype("int64")
    df = df.copy()
    df["ts"] = df["ts"].astype("int64")
    return df.merge(s, left_on="ts", right_index=True, how="left")


def add_funding(df: pd.DataFrame, funding, oi_chg=None) -> pd.DataFrame:
    """funding: رقم (حي) أو DataFrame[ts, rate] تاريخي يُدمج بأقرب قيمة سابقة."""
    if funding is None or np.isscalar(funding):
        df["funding"] = np.nan if funding is None else funding
    else:
        f = funding.sort_values("ts")[["ts", "rate"]].rename(columns={"rate": "funding"}).copy()
        f["ts"] = f["ts"].astype("int64")
        df = df.copy()
        df["ts"] = df["ts"].astype("int64")
        df = pd.merge_asof(df.sort_values("ts"), f, on="ts", direction="backward")
    df["oi_chg"] = np.nan if oi_chg is None else oi_chg
    return df

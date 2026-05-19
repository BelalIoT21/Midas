"""
RSI and MACD divergence detection.
Divergence = price and indicator moving in opposite directions.
Bullish divergence: price makes lower low, indicator makes higher low → BUY
Bearish divergence: price makes higher high, indicator makes lower high → SELL
"""
import pandas as pd
import pandas_ta as ta
from scipy.signal import argrelextrema
import numpy as np


def _find_pivots(series: pd.Series, order: int = 5) -> tuple[np.ndarray, np.ndarray]:
    """Returns indices of local highs and lows."""
    highs = argrelextrema(series.values, np.greater, order=order)[0]
    lows  = argrelextrema(series.values, np.less,    order=order)[0]
    return highs, lows


def _rsi_series(df: pd.DataFrame) -> pd.Series:
    return ta.rsi(df["close"], length=14).dropna()


def _macd_hist(df: pd.DataFrame) -> pd.Series:
    result = ta.macd(df["close"], fast=12, slow=26, signal=9)
    if result is None or result.empty:
        return pd.Series(dtype=float)
    return result.iloc[:, 1].dropna()  # histogram column


def detect_rsi_divergence(df: pd.DataFrame) -> tuple[int, str]:
    """
    Returns (vote, description).
    vote: +1 bullish, -1 bearish, 0 none
    """
    if len(df) < 30:
        return 0, ""

    close = df["close"]
    rsi   = _rsi_series(df)

    # Align by index
    common = close.index.intersection(rsi.index)
    if len(common) < 20:
        return 0, ""

    close = close.loc[common].tail(60)
    rsi   = rsi.loc[common].tail(60)

    _, lows_c  = _find_pivots(close, order=4)
    highs_c, _ = _find_pivots(close, order=4)
    _, lows_r  = _find_pivots(rsi,   order=4)
    highs_r, _ = _find_pivots(rsi,   order=4)

    # Bullish divergence: last two price lows descending, RSI lows ascending
    if len(lows_c) >= 2 and len(lows_r) >= 2:
        price_lower = close.iloc[lows_c[-1]] < close.iloc[lows_c[-2]]
        rsi_higher  = rsi.iloc[lows_r[-1]]   > rsi.iloc[lows_r[-2]]
        if price_lower and rsi_higher:
            return 1, "RSI Bullish Divergence"

    # Bearish divergence: last two price highs ascending, RSI highs descending
    if len(highs_c) >= 2 and len(highs_r) >= 2:
        price_higher = close.iloc[highs_c[-1]] > close.iloc[highs_c[-2]]
        rsi_lower    = rsi.iloc[highs_r[-1]]   < rsi.iloc[highs_r[-2]]
        if price_higher and rsi_lower:
            return -1, "RSI Bearish Divergence"

    return 0, ""


def detect_macd_divergence(df: pd.DataFrame) -> tuple[int, str]:
    """
    Returns (vote, description).
    """
    if len(df) < 40:
        return 0, ""

    close = df["close"]
    hist  = _macd_hist(df)

    common = close.index.intersection(hist.index)
    if len(common) < 20:
        return 0, ""

    close = close.loc[common].tail(60)
    hist  = hist.loc[common].tail(60)

    _, lows_c  = _find_pivots(close, order=4)
    highs_c, _ = _find_pivots(close, order=4)
    _, lows_h  = _find_pivots(hist,  order=4)
    highs_h, _ = _find_pivots(hist,  order=4)

    if len(lows_c) >= 2 and len(lows_h) >= 2:
        price_lower = close.iloc[lows_c[-1]] < close.iloc[lows_c[-2]]
        hist_higher = hist.iloc[lows_h[-1]]  > hist.iloc[lows_h[-2]]
        if price_lower and hist_higher:
            return 1, "MACD Bullish Divergence"

    if len(highs_c) >= 2 and len(highs_h) >= 2:
        price_higher = close.iloc[highs_c[-1]] > close.iloc[highs_c[-2]]
        hist_lower   = hist.iloc[highs_h[-1]]  < hist.iloc[highs_h[-2]]
        if price_higher and hist_lower:
            return -1, "MACD Bearish Divergence"

    return 0, ""

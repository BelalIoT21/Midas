"""
Support/Resistance levels: swing highs/lows, round numbers, daily pivots, Fibonacci.
"""
import pandas as pd
import numpy as np
from scipy.signal import argrelextrema


def swing_levels(df: pd.DataFrame, order: int = 8, lookback: int = 100) -> dict:
    """
    Returns key S/R levels from recent swing highs and lows.
    """
    df = df.tail(lookback)
    highs_idx = argrelextrema(df["high"].values, np.greater, order=order)[0]
    lows_idx  = argrelextrema(df["low"].values,  np.less,    order=order)[0]

    resistance = sorted(df["high"].iloc[highs_idx].tolist(), reverse=True)[:4]
    support    = sorted(df["low"].iloc[lows_idx].tolist(),   reverse=False)[:4]

    return {"resistance": resistance, "support": support}


def daily_pivots(df: pd.DataFrame) -> dict:
    """
    Classic pivot points from the previous day's H/L/C.
    Works on any timeframe — uses last completed day.
    """
    daily = df.resample("1D").agg({"high": "max", "low": "min", "close": "last"}).dropna()
    if len(daily) < 2:
        return {}

    prev = daily.iloc[-2]
    H, L, C = prev["high"], prev["low"], prev["close"]
    P  = (H + L + C) / 3
    R1 = 2 * P - L
    S1 = 2 * P - H
    R2 = P + (H - L)
    S2 = P - (H - L)
    R3 = H + 2 * (P - L)
    S3 = L - 2 * (H - P)

    return {
        "P":  round(P,  2),
        "R1": round(R1, 2), "R2": round(R2, 2), "R3": round(R3, 2),
        "S1": round(S1, 2), "S2": round(S2, 2), "S3": round(S3, 2),
    }


def fibonacci_levels(df: pd.DataFrame, lookback: int = 50) -> dict:
    """
    Fibonacci retracement levels from the most significant recent swing.
    """
    recent = df.tail(lookback)
    swing_high = recent["high"].max()
    swing_low  = recent["low"].min()
    diff = swing_high - swing_low

    ratios = [0.0, 0.236, 0.382, 0.5, 0.618, 0.786, 1.0]
    levels = {}
    for r in ratios:
        levels[f"fib_{int(r*1000)}"] = round(swing_high - diff * r, 2)

    return {"high": round(swing_high, 2), "low": round(swing_low, 2), **levels}


def round_number_levels(price: float, step: float = 50.0) -> list[float]:
    """
    Returns nearest round number levels above and below price.
    Gold moves in $50 increments psychologically.
    """
    base = round(price / step) * step
    return [round(base + i * step, 2) for i in range(-3, 4)]


def nearest_level(price: float, levels: list[float], max_dist_pct: float = 0.003) -> float | None:
    """
    Returns the nearest level within max_dist_pct of price, or None.
    """
    threshold = price * max_dist_pct
    close_levels = [lvl for lvl in levels if abs(lvl - price) <= threshold]
    if not close_levels:
        return None
    return min(close_levels, key=lambda x: abs(x - price))


def all_levels(df: pd.DataFrame) -> dict:
    """Compute all levels at once."""
    swings = swing_levels(df)
    pivots = daily_pivots(df)
    fibs   = fibonacci_levels(df)

    all_sr = (
        swings["resistance"]
        + swings["support"]
        + [v for k, v in pivots.items() if k != "P"]
        + [fibs.get("fib_382", 0), fibs.get("fib_500", 0), fibs.get("fib_618", 0)]
    )
    all_sr = [x for x in all_sr if x > 0]

    return {
        "swings":  swings,
        "pivots":  pivots,
        "fibs":    fibs,
        "all_sr":  all_sr,
    }

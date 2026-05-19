"""
Price-action entry triggers for trend-following.

Used AFTER indicator filters confirm context — the PA pattern is the actual
entry trigger, not the indicator score. This improves win rate by ensuring
we enter when price shows real intent at a meaningful level, not just when
a bunch of lagging indicators happen to agree.

Patterns detected (on 15min candles):
  - Engulfing    : current candle body fully engulfs previous body
  - Hammer       : long lower wick rejection (BUY)
  - Shooting Star: long upper wick rejection (SELL)

Level proximity check:
  - Price must be within 0.8×ATR of a swing high/low, daily pivot, or Fibonacci level
  - Support levels gate BUY entries; resistance levels gate SELL entries
"""
from typing import Optional

import pandas as pd

from indicators.levels import all_levels, nearest_level


# ── Pattern detection ─────────────────────────────────────────────────────────

def _body(candle: pd.Series) -> float:
    return abs(candle["close"] - candle["open"])


def _upper_wick(candle: pd.Series) -> float:
    return candle["high"] - max(candle["open"], candle["close"])


def _lower_wick(candle: pd.Series) -> float:
    return min(candle["open"], candle["close"]) - candle["low"]


def _range(candle: pd.Series) -> float:
    return candle["high"] - candle["low"]


def _is_engulfing(prev: pd.Series, curr: pd.Series, direction: str) -> bool:
    """
    Bullish engulfing: prev is bearish, curr is bullish and fully covers prev body.
    Bearish engulfing: prev is bullish, curr is bearish and fully covers prev body.
    Requires meaningful body size (not a doji on either side).
    """
    prev_body = _body(prev)
    curr_body = _body(curr)
    if prev_body < 1e-6 or curr_body < 1e-6:
        return False

    if direction == "BUY":
        prev_bearish = prev["close"] < prev["open"]
        curr_bullish = curr["close"] > curr["open"]
        engulfs = curr["open"] <= prev["close"] and curr["close"] >= prev["open"]
        return prev_bearish and curr_bullish and engulfs and curr_body >= prev_body * 0.8

    else:  # SELL
        prev_bullish = prev["close"] > prev["open"]
        curr_bearish = curr["close"] < curr["open"]
        engulfs = curr["open"] >= prev["close"] and curr["close"] <= prev["open"]
        return prev_bullish and curr_bearish and engulfs and curr_body >= prev_body * 0.8


def _is_hammer(candle: pd.Series, atr: float, direction: str) -> bool:
    """
    Hammer (BUY): lower wick ≥ 2× body, upper wick ≤ 0.5× body,
                  body in upper half of the candle's range.
    Shooting star (SELL): upper wick ≥ 2× body, lower wick ≤ 0.5× body,
                          body in lower half of range.
    Candle must have meaningful total range (≥ 0.4×ATR).
    """
    total = _range(candle)
    if total < atr * 0.4:
        return False

    body = _body(candle)
    if body < 1e-6:
        return False

    upper = _upper_wick(candle)
    lower = _lower_wick(candle)

    if direction == "BUY":
        return lower >= body * 2.0 and upper <= body * 0.5

    else:  # SELL
        return upper >= body * 2.0 and lower <= body * 0.5


def detect_entry_pattern(df: pd.DataFrame, direction: str, atr: float) -> tuple[bool, str]:
    """
    Check the last two closed candles for a valid PA entry pattern.
    Returns (triggered, pattern_name).
    """
    if df is None or len(df) < 3:
        return False, ""

    prev = df.iloc[-2]
    curr = df.iloc[-1]

    if _is_engulfing(prev, curr, direction):
        label = "Bullish Engulfing" if direction == "BUY" else "Bearish Engulfing"
        return True, label

    if _is_hammer(curr, atr, direction):
        label = "Hammer" if direction == "BUY" else "Shooting Star"
        return True, label

    return False, ""


# ── Level proximity ───────────────────────────────────────────────────────────

def near_entry_level(
    df_1h: pd.DataFrame,
    price: float,
    atr: float,
    direction: str,
    tolerance: float = 0.8,
) -> tuple[bool, Optional[float]]:
    """
    Returns (at_level, level_price).

    For BUY:  price must be near (within tolerance×ATR of) a SUPPORT level.
    For SELL: price must be near a RESISTANCE level.

    Uses swing highs/lows, daily pivots, and Fibonacci from indicators/levels.
    """
    try:
        sr       = all_levels(df_1h)
        swings   = sr.get("swings", {})
        pivots   = sr.get("pivots", {})
        fibs     = sr.get("fibs",   {})
        threshold = atr * tolerance

        if direction == "BUY":
            candidates = list(swings.get("support", [])) + [
                pivots.get("S1"), pivots.get("S2"),
                fibs.get("fib_618"), fibs.get("fib_500"), fibs.get("fib_382"),
            ]
        else:
            candidates = list(swings.get("resistance", [])) + [
                pivots.get("R1"), pivots.get("R2"),
                fibs.get("fib_236"), fibs.get("fib_382"),
            ]

        candidates = [c for c in candidates if c is not None and c > 0]
        if not candidates:
            return False, None

        closest = min(candidates, key=lambda lvl: abs(lvl - price))
        if abs(closest - price) <= threshold:
            return True, round(closest, 2)

        return False, None

    except Exception:
        return False, None

"""
Fib-based level calculator for Tap 'n' Barrel V2.0.

Entry fib (drawn from swing high to swing low, fib 0 = top, fib 1 = bottom):
  0.5   → shallower entry (Goldilocks zone upper bound)
  0.618 → deeper entry   (Goldilocks zone lower bound / preferred entry)
  1.025 → SL (just below the swing low / invalidation)

TP = 3× the distance from entry to SL (3RR hard target).
"""

from typing import Optional


def calculate_fib_entry(
    swing_high: float,
    swing_low:  float,
    bias:       int,
    fib_entry:  float = 0.618,
) -> Optional[dict]:
    """
    Calculate entry, SL, TP from a BoS swing.

    Args:
        swing_high  : swing high of the BoS move
        swing_low   : swing low of the BoS move
        bias        : +1 = bullish (BUY), -1 = bearish (SELL)
        fib_entry   : fib level to use as entry (default 0.618)

    Returns dict or None if fib_range is zero.
    """
    fib_range = swing_high - swing_low
    if fib_range <= 0:
        return None

    if bias == 1:
        # BUY: fib 0 = swing_high, fib 1 = swing_low
        entry   = swing_high - fib_entry * fib_range
        sl      = swing_high - 1.025 * fib_range   # just below swing_low
        sl_dist = abs(entry - sl)
        tp      = entry + 3.0 * sl_dist
    else:
        # SELL: fib 0 = swing_low (inverted), fib 1 = swing_high
        entry   = swing_low + fib_entry * fib_range
        sl      = swing_low + 1.025 * fib_range     # just above swing_high
        sl_dist = abs(sl - entry)
        tp      = entry - 3.0 * sl_dist

    return {
        "entry":   round(entry,   2),
        "sl":      round(sl,      2),
        "tp":      round(tp,      2),
        "sl_dist": round(sl_dist, 2),
        "tp_dist": round(sl_dist * 3.0, 2),
        "rr":      3.0,
    }

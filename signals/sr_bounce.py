"""
S/R Bounce Signal Engine
========================
Detects high-probability reversals when price taps a proven S/R level with
RSI divergence and a rejection candle — a different signal type from the
trend-following engine.  Targets 55-65% win rate with 1.8-2.5 R:R.

Entry criteria (ALL must pass):
  1. Price within 0.3% of a key S/R level
  2. Level proven — touched ≥2 times in last 120 bars
  3. Rejection candle on current 1H bar (wick pointing back from level)
  4. RSI divergence at the level (leading signal)
  5. Daily trend not strongly opposing the bounce direction
"""
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

import pandas as pd
import pandas_ta as ta

from indicators.levels import all_levels
from indicators.calculator import atr_value
from analysis.sessions import get_current_session, should_trade
from config import MIN_RR_RATIO

PROXIMITY_PCT = 0.003   # price must be within 0.3% of level
MIN_TOUCHES   = 2       # level must have been tested this many times
LOOKBACK_BARS = 120     # bars to search for level significance


@dataclass
class BounceSignal:
    direction:    str
    entry:        float
    sl:           float
    tp1:          float
    tp2:          float
    rr1:          float
    rr2:          float
    sl_pips:      float
    tp1_pips:     float
    tp2_pips:     float
    level:        float
    level_type:   str         # "support" | "resistance"
    touches:      int
    divergences:  list[str]
    confidence:   float
    session_name: str
    session_emoji: str
    timestamp:    datetime


# ── Helpers ───────────────────────────────────────────────────────────────────

def _rsi(df: pd.DataFrame, period: int = 14) -> pd.Series:
    delta = df["close"].diff()
    gain  = delta.clip(lower=0).ewm(com=period - 1, adjust=False).mean()
    loss  = (-delta.clip(upper=0)).ewm(com=period - 1, adjust=False).mean()
    rs    = gain / loss.replace(0, float("nan"))
    return 100 - (100 / (1 + rs))


def _level_touches(df: pd.DataFrame, level: float, tol_pct: float = 0.0015) -> int:
    """Count bars where high/low came within tol_pct% of the level."""
    tol = level * tol_pct
    return int(((df["low"] <= level + tol) & (df["high"] >= level - tol)).sum())


def _rejection_candle(bar: pd.Series, direction: str) -> bool:
    """
    BUY  at support:    lower wick ≥ 1.5× body AND close in upper 50% of range.
    SELL at resistance: upper wick ≥ 1.5× body AND close in lower 50% of range.
    """
    body = abs(bar["close"] - bar["open"])
    rng  = bar["high"] - bar["low"]
    if rng < 1e-6:
        return False
    if direction == "BUY":
        wick = min(bar["close"], bar["open"]) - bar["low"]
        return wick >= max(body * 1.5, rng * 0.3) and bar["close"] > bar["low"] + rng * 0.5
    else:
        wick = bar["high"] - max(bar["close"], bar["open"])
        return wick >= max(body * 1.5, rng * 0.3) and bar["close"] < bar["high"] - rng * 0.5


def _rsi_divergence(df: pd.DataFrame, direction: str, window: int = 20) -> bool:
    """
    BUY  divergence: price lower low but RSI higher low  (bullish divergence).
    SELL divergence: price higher high but RSI lower high (bearish divergence).
    """
    if len(df) < window + 5:
        return False
    rsi    = _rsi(df)
    p_rec  = df.tail(window)
    r_rec  = rsi.tail(window)
    split  = window - 5

    if direction == "BUY":
        p_now, p_prev = p_rec["low"].tail(5).min(), p_rec["low"].head(split).min()
        r_now, r_prev = r_rec.tail(5).min(),        r_rec.head(split).min()
        return p_now < p_prev and r_now > r_prev
    else:
        p_now, p_prev = p_rec["high"].tail(5).max(), p_rec["high"].head(split).max()
        r_now, r_prev = r_rec.tail(5).max(),         r_rec.head(split).max()
        return p_now > p_prev and r_now < r_prev


def _next_level(all_sr: list[float], price: float, direction: str) -> Optional[float]:
    """Find the nearest S/R level beyond price in the bounce direction."""
    if direction == "BUY":
        candidates = [l for l in all_sr if l > price * 1.002]
        return min(candidates) if candidates else None
    else:
        candidates = [l for l in all_sr if l < price * 0.998]
        return max(candidates) if candidates else None


def _daily_trend(daily_df: pd.DataFrame) -> str:
    if daily_df is None or len(daily_df) < 50:
        return "NEUTRAL"
    close = float(daily_df["close"].iloc[-1])
    ema50 = float(daily_df["close"].ewm(span=50, adjust=False).mean().iloc[-1])
    band  = ema50 * 0.0005
    if close > ema50 + band:
        return "BULL"
    if close < ema50 - band:
        return "BEAR"
    return "NEUTRAL"


# ── Main detector ─────────────────────────────────────────────────────────────

def detect_bounce(
    df_1h: pd.DataFrame,
    daily_df: Optional[pd.DataFrame] = None,
    dt: Optional[datetime] = None,
) -> Optional[BounceSignal]:
    """
    Returns a BounceSignal if all entry criteria pass, else None.
    Pass dt for backtesting (historical timestamp). Live mode uses datetime.now().
    """
    if df_1h is None or len(df_1h) < 50:
        return None

    session = get_current_session(dt)
    if not should_trade(session):
        return None

    bar           = df_1h.iloc[-1]
    price         = float(bar["close"])
    atr           = atr_value(df_1h)
    if atr is None:
        return None

    # ── Step 1: find a proven S/R level within proximity ─────────────────────
    try:
        sr_data = all_levels(df_1h)
        all_sr  = sr_data.get("all_sr", [])
    except Exception:
        return None
    if not all_sr:
        return None

    threshold    = price * PROXIMITY_PCT
    close_levels = [(abs(l - price), l) for l in all_sr if abs(l - price) <= threshold]
    if not close_levels:
        return None
    _, level = min(close_levels)

    direction  = "BUY" if level <= price else "SELL"
    level_type = "support" if direction == "BUY" else "resistance"

    # ── Step 2: level significance ────────────────────────────────────────────
    lookback_df = df_1h.tail(LOOKBACK_BARS)
    touches     = _level_touches(lookback_df, level)
    if touches < MIN_TOUCHES:
        return None

    # ── Step 3: rejection candle ──────────────────────────────────────────────
    if not _rejection_candle(bar, direction):
        return None

    # ── Step 4: RSI divergence ────────────────────────────────────────────────
    divergences: list[str] = []
    if _rsi_divergence(df_1h, direction):
        divergences.append("RSI divergence")
    if not divergences:
        return None

    # ── Step 5: daily trend filter ────────────────────────────────────────────
    trend = _daily_trend(daily_df)
    if trend == "BULL" and direction == "SELL":
        return None
    if trend == "BEAR" and direction == "BUY":
        return None

    # ── Levels ────────────────────────────────────────────────────────────────
    sl_dist = round(atr * 0.8, 2)   # tight stop — bounce trades use tighter SL

    if direction == "BUY":
        sl   = round(level - sl_dist, 2)
        next = _next_level(all_sr, price, "BUY")
        tp1_dist = round(next - price, 2) if next and (next - price) >= sl_dist * 1.5 else sl_dist * 2.0
        tp1  = round(price + tp1_dist, 2)
        tp2  = round(price + sl_dist * 3.5, 2)
    else:
        sl   = round(level + sl_dist, 2)
        next = _next_level(all_sr, price, "SELL")
        tp1_dist = round(price - next, 2) if next and (price - next) >= sl_dist * 1.5 else sl_dist * 2.0
        tp1  = round(price - tp1_dist, 2)
        tp2  = round(price - sl_dist * 3.5, 2)

    actual_sl_dist  = abs(price - sl)
    actual_tp1_dist = abs(tp1 - price)
    rr1 = round(actual_tp1_dist / actual_sl_dist, 2) if actual_sl_dist > 0 else 0
    rr2 = round(abs(tp2 - price) / actual_sl_dist, 2) if actual_sl_dist > 0 else 0

    if rr1 < MIN_RR_RATIO:
        return None

    confidence = round(min(0.70 + min(touches * 0.02, 0.10) + len(divergences) * 0.05, 0.95), 4)

    return BounceSignal(
        direction=direction,
        entry=round(price, 2),
        sl=sl,
        tp1=tp1,
        tp2=tp2,
        rr1=rr1,
        rr2=rr2,
        sl_pips=round(actual_sl_dist, 2),
        tp1_pips=round(actual_tp1_dist, 2),
        tp2_pips=round(abs(tp2 - price), 2),
        level=round(level, 2),
        level_type=level_type,
        touches=touches,
        divergences=divergences,
        confidence=confidence,
        session_name=session.name,
        session_emoji=session.emoji,
        timestamp=dt or datetime.now(timezone.utc),
    )

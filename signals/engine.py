"""
Tap 'n' Barrel + SMC Signal Engine — V2.0

Strategy flow (all 6 steps required in sequence):
  1. 4H Fractal Break of Structure → sets session bias (BUY/SELL)
  2. Mark today's Asia session extreme (lowest low for BUY, highest high for SELL)
  3. Detect session sweep — London/NY sweeps the Asia level (fakeout move)
  4. Build fib from pre-sweep swing → sweep extreme (fib 0=top, fib 1=bottom)
     Price must return to the 0.079 zone after the sweep
  5. Detect 5min fractal BoS in bias direction (candle BODY close, not wick)
  6. Build entry fib from BoS swing → entry at 0.618, SL at 1.025, TP at 3RR

Fractal definition (Rachel_T style — 5-bar):
  Fractal HIGH at bar[i] if high[i] is the highest of bars [i-2 .. i+2]
  Fractal LOW  at bar[i] if low[i]  is the lowest  of bars [i-2 .. i+2]
  BoS requires BODY close (close price), not a wick.
"""
from dataclasses import dataclass, field
from datetime import datetime, timezone, date
from typing import Optional, Union

import pandas as pd


# ── Signal dataclass ──────────────────────────────────────────────────────────

@dataclass
class Signal:
    direction:      str        # "BUY" or "SELL"
    entry:          float      # market entry price (in 0.618 zone)
    sl:             float      # 1.025 fib level (invalidation)
    tp:             float      # 3RR target
    sl_pips:        float      # price distance entry → SL
    tp_pips:        float      # price distance entry → TP
    rr:             float      # 3.0
    session_name:   str
    session_emoji:  str
    bias:           str        # "BULLISH" or "BEARISH"
    timestamp:      datetime
    symbol:         str        # "XAU/USD" or "BTC/USD"
    # Strategy debug info
    asia_level:     float      # Asia extreme that was swept
    fib_high:       float      # sweep-fib anchor high
    fib_low:        float      # sweep-fib anchor low (the sweep extreme)
    bos_swing_high: float      # 5min BoS swing high
    bos_swing_low:  float      # 5min BoS swing low


# ── Fractal detection ─────────────────────────────────────────────────────────

def _fractal_highs(df: pd.DataFrame, n: int = 2) -> pd.Series:
    """Return Series with fractal-high values (NaN elsewhere). n=2 → 5-bar fractal."""
    highs  = df["high"]
    result = pd.Series(float("nan"), index=df.index)
    for i in range(n, len(df) - n):
        window = highs.iloc[i - n: i + n + 1]
        if float(highs.iloc[i]) == float(window.max()):
            result.iloc[i] = float(highs.iloc[i])
    return result


def _fractal_lows(df: pd.DataFrame, n: int = 2) -> pd.Series:
    """Return Series with fractal-low values (NaN elsewhere). n=2 → 5-bar fractal."""
    lows   = df["low"]
    result = pd.Series(float("nan"), index=df.index)
    for i in range(n, len(df) - n):
        window = lows.iloc[i - n: i + n + 1]
        if float(lows.iloc[i]) == float(window.min()):
            result.iloc[i] = float(lows.iloc[i])
    return result


# ── Step 1: 4H Trend Bias ─────────────────────────────────────────────────────

def detect_4h_bos(df_4h: pd.DataFrame) -> int:
    """
    Determine the current 4H trend bias using EMA20 + recent swing direction.
    Returns +1 (bullish), -1 (bearish), or 0 (insufficient data).

    Primary:   close vs EMA20 (sets broad direction)
    Secondary: last 5 confirmed 4H closes — are they making higher highs or lower lows?
    Fractal BoS acts as a tie-breaker when EMA and swing momentum agree.

    Returns 0 only when there is genuinely no data.
    """
    if df_4h is None or len(df_4h) < 25:
        return 0

    close  = df_4h["close"]
    ema20  = close.ewm(span=20, adjust=False).mean()
    ema50  = close.ewm(span=50, adjust=False).mean()

    # Use confirmed candles (exclude the live forming bar)
    last_close = float(close.iloc[-2])
    last_ema20 = float(ema20.iloc[-2])
    last_ema50 = float(ema50.iloc[-2])

    # Recent swing: compare last confirmed close to 5 bars ago
    swing_now  = float(close.iloc[-2])
    swing_prev = float(close.iloc[-7])
    momentum   = swing_now - swing_prev

    # Fractal BoS — used as extra confirmation
    frac_h = _fractal_highs(df_4h).dropna()
    frac_l = _fractal_lows(df_4h).dropna()
    frac_bull = (not frac_h.empty and last_close > float(frac_h.iloc[-1]))
    frac_bear = (not frac_l.empty and last_close < float(frac_l.iloc[-1]))

    # EMA20 vs EMA50 — medium-term trend alignment (4th confirmation)
    ema_bull = last_ema20 > last_ema50
    ema_bear = last_ema20 < last_ema50

    bull_score = (1 if last_close > last_ema20 else 0) + \
                 (1 if momentum > 0             else 0) + \
                 (1 if frac_bull                else 0) + \
                 (1 if ema_bull                 else 0)
    bear_score = (1 if last_close < last_ema20 else 0) + \
                 (1 if momentum < 0             else 0) + \
                 (1 if frac_bear                else 0) + \
                 (1 if ema_bear                 else 0)

    if bull_score > bear_score and bull_score >= 3:
        return 1
    if bear_score > bull_score and bear_score >= 3:
        return -1
    # Tiebreak or score < 3: use EMA20 vs EMA50 as final arbiter
    if ema_bull and not ema_bear:
        return 1
    if ema_bear and not ema_bull:
        return -1
    return 0


# ── Step 2: Asia session extreme ──────────────────────────────────────────────

def get_asia_extreme(
    df_5m: pd.DataFrame,
    bias: int,
    trade_date: date,
) -> Optional[float]:
    """
    Return the extreme of today's Asia session (00:00–08:00 UTC).
    Bullish bias → lowest low (level to watch for bullish sweep).
    Bearish bias → highest high (level to watch for bearish sweep).
    """
    if df_5m is None or df_5m.empty:
        return None

    idx = df_5m.index
    if isinstance(idx, pd.DatetimeIndex) and idx.tz is not None:
        idx_utc = idx.tz_convert("UTC")
    else:
        idx_utc = pd.to_datetime(idx, utc=True)

    # Use .date/.hour to avoid pandas 2.x datetime64[us] vs Timestamp resolution error
    dates = pd.Index(idx_utc.date)
    mask = (dates == trade_date) & (idx_utc.hour < 8)
    asia = df_5m[mask]

    if asia.empty:
        return None

    return float(asia["low"].min()) if bias == 1 else float(asia["high"].max())


# ── Step 3: Session sweep detection ──────────────────────────────────────────

def find_sweep(
    df_5m: pd.DataFrame,
    asia_level: float,
    bias: int,
    trade_date: Optional[date] = None,
    tolerance: float = 0.0,
) -> Optional[dict]:
    """
    Scan 5min bars (from 08:00 UTC) for a sweep of the Asia extreme.
    Bullish: bar low goes AT OR BELOW asia_level (or within tolerance %).
    Bearish: bar high goes AT OR ABOVE asia_level (or within tolerance %).

    trade_date: restrict to this UTC date to avoid yesterday's candles.
    tolerance:  0.003 = accept within 0.3% of asia_level as a near-miss sweep.
                This catches fakeouts that approach but don't fully cross.

    Returns a dict with:
      idx         – iloc index of the sweep bar in df_5m
      sweep_price – the bar's extreme (low for bull, high for bear)
      fib_high    – swing high leading into the sweep
      fib_low     – swing low (the sweep extreme for bull, or pre-sweep low for bear)
    """
    if df_5m is None or df_5m.empty or asia_level is None:
        return None

    try:
        from config import SWEEP_TOLERANCE
        if tolerance == 0.0:
            tolerance = SWEEP_TOLERANCE
    except Exception:
        pass

    idx = df_5m.index
    if isinstance(idx, pd.DatetimeIndex) and idx.tz is not None:
        idx_utc = idx.tz_convert("UTC")
    else:
        idx_utc = pd.to_datetime(idx, utc=True)

    # Restrict to trade_date so we don't match yesterday's candles (5min data spans ~16h)
    scan_date = trade_date if trade_date is not None else datetime.now(timezone.utc).date()
    dates = pd.Index(idx_utc.date)
    london_mask = (dates == scan_date) & (idx_utc.hour >= 8)

    tol_price = asia_level * tolerance

    for i, in_london in enumerate(london_mask):
        if not in_london:
            continue
        row = df_5m.iloc[i]

        if bias == 1 and float(row["low"]) <= asia_level + tol_price:
            # Bullish fakeout: price swept below (or within tolerance of) Asia low
            lookback_start = max(0, i - 20)
            fib_high = float(df_5m["high"].iloc[lookback_start:i + 1].max())
            fib_low  = float(row["low"])
            return {
                "idx":          i,
                "sweep_time":   df_5m.index[i],   # UTC timestamp — survives candle re-fetches
                "sweep_price":  fib_low,
                "fib_high":     fib_high,
                "fib_low":      fib_low,
            }

        if bias == -1 and float(row["high"]) >= asia_level - tol_price:
            # Bearish fakeout: price swept above (or within tolerance of) Asia high
            lookback_start = max(0, i - 20)
            fib_low  = float(df_5m["low"].iloc[lookback_start:i + 1].min())
            fib_high = float(row["high"])
            return {
                "idx":          i,
                "sweep_time":   df_5m.index[i],   # UTC timestamp — survives candle re-fetches
                "sweep_price":  fib_high,
                "fib_high":     fib_high,
                "fib_low":      fib_low,
            }

    return None


# ── Step 4: Fib 0.079 fakeout zone ───────────────────────────────────────────

def fib_price(fib_high: float, fib_low: float, level: float) -> float:
    """
    Calculate a fib price level.
    Convention (fib 0 = top, fib 1 = bottom):
      level 0.0   = fib_high
      level 0.079 = just below fib_high (near the top)
      level 0.5   = midpoint
      level 1.0   = fib_low
      level 1.025 = just below fib_low (invalidation zone)
    """
    return fib_high - level * (fib_high - fib_low)


def in_fakeout_zone(
    current_price: float,
    fib_high: float,
    fib_low: float,
    bias: int,
) -> bool:
    """
    Price must have bounced back to the fakeout zone after the sweep.

    The fakeout zone level is read from config (FAKEOUT_ZONE_LEVEL, default 0.382):
      - Fib convention: 0 = top (fib_high), 1 = bottom (fib_low)
      - FAKEOUT_ZONE_LEVEL = 0.382 means price must retrace 61.8% of the sweep range
        (i.e. reach the level that is 38.2% below the pre-sweep high)

    Both bullish and bearish use the same retracement requirement (symmetric):
      - Bullish: after sweeping DOWN below Asia low, price must bounce UP into the zone
        (price >= fib_high - level * range)
      - Bearish: after sweeping UP above Asia high, price must fall DOWN into the zone
        (price <= fib_low + level * range)
    """
    if fib_high <= fib_low:
        return False

    try:
        from config import FAKEOUT_ZONE_LEVEL
        level = FAKEOUT_ZONE_LEVEL
    except Exception:
        level = 0.382

    fib_range = fib_high - fib_low

    if bias == 1:
        # Bullish: price must bounce UP from the sweep low to at least (fib_high - level * range)
        zone_bottom = fib_price(fib_high, fib_low, level)   # e.g. 0.382 level from top
        return current_price >= zone_bottom
    else:
        # Bearish: price must fall DOWN from the sweep high to at most (fib_low + level * range)
        # Symmetric with bullish — requires the same proportional retrace
        zone_top_bear = fib_low + level * fib_range         # e.g. 38.2% above fib_low
        return current_price <= zone_top_bear


# ── Step 4b: Historical fakeout zone scan ────────────────────────────────────

def scan_fakeout_zone(
    df_5m: pd.DataFrame,
    sweep: dict,
    bias: int,
) -> bool:
    """
    Return True if ANY bar since the sweep entered the fakeout zone.

    Unlike in_fakeout_zone() which checks a single price, this scans the full
    candle history from the sweep bar forward.  This lets the bot bootstrap
    correctly when /trade is started mid-day and the fakeout bounce already
    happened earlier — instead of waiting forever for a zone entry that passed.
    """
    fib_high = sweep["fib_high"]
    fib_low  = sweep["fib_low"]
    if fib_high <= fib_low:
        return False

    sweep_time = sweep.get("sweep_time")
    if sweep_time is not None:
        bars = df_5m[df_5m.index >= pd.Timestamp(sweep_time)]
    else:
        bars = df_5m.iloc[sweep.get("idx", 0):]

    if bars.empty:
        return False

    try:
        from config import FAKEOUT_ZONE_LEVEL
        level = FAKEOUT_ZONE_LEVEL
    except Exception:
        level = 0.382

    fib_range = fib_high - fib_low

    if bias == 1:
        # Bullish: any candle HIGH reached the zone ceiling
        zone_bottom = fib_high - level * fib_range
        return bool((bars["high"] >= zone_bottom).any())
    else:
        # Bearish: any candle LOW reached the zone floor
        zone_top = fib_low + level * fib_range
        return bool((bars["low"] <= zone_top).any())


# ── Step 5: 5min Fractal Break of Structure ───────────────────────────────────

def find_5min_bos(
    df_5m: pd.DataFrame,
    bias: int,
    start_idx: int = 0,
    *,
    start_time=None,
) -> Optional[dict]:
    """
    Scan bars from start_idx for a 5min fractal BoS in bias direction.
    BODY close required (wick only = invalid).

    start_time: if provided (a UTC Timestamp from sweep["sweep_time"]), resolves the
      correct start_idx regardless of how many new candles have been fetched since the
      sweep was first detected — this avoids the iloc-drift bug.

    Returns dict with:
      idx           – iloc index of the BoS bar
      swing_high    – the fractal that was broken (bullish) or swing high of move (bearish)
      swing_low     – swing low of the BoS move (bullish) or the fractal that broke (bearish)
    """
    if df_5m is None or df_5m.empty:
        return None

    if start_time is not None:
        sweep_ts = pd.Timestamp(start_time)
        after    = df_5m.index >= sweep_ts
        if after.any():
            start_idx = int(after.argmax())
        else:
            return None

    if len(df_5m) < start_idx + 10:
        return None

    window = df_5m.iloc[start_idx:]
    frac_h = _fractal_highs(window)
    frac_l = _fractal_lows(window)

    for i in range(4, len(window)):
        bar_close = float(window["close"].iloc[i])

        # Only use fractals confirmed at or before bar i-2 (no look-ahead).
        # A 5-bar fractal at position j requires bars j+1 and j+2 to be lower/higher,
        # so it is confirmed at bar j+2. At bar i, confirmed fractals are at j <= i-2.
        confirmed_h = frac_h.iloc[: i - 1].dropna()
        confirmed_l = frac_l.iloc[: i - 1].dropna()

        if bias == 1 and not confirmed_h.empty:
            # Bullish BoS: body close above a confirmed fractal high
            last_frac = float(confirmed_h.iloc[-1])
            if bar_close > last_frac:
                lookback = max(0, i - 20)
                swing_low = float(window["low"].iloc[lookback:i + 1].min())
                return {
                    "idx":           start_idx + i,
                    "swing_high":    bar_close,
                    "swing_low":     swing_low,
                    "broken_fractal": last_frac,
                }

        if bias == -1 and not confirmed_l.empty:
            # Bearish BoS: body close below a confirmed fractal low
            last_frac = float(confirmed_l.iloc[-1])
            if bar_close < last_frac:
                lookback = max(0, i - 20)
                swing_high = float(window["high"].iloc[lookback:i + 1].max())
                return {
                    "idx":           start_idx + i,
                    "swing_high":    swing_high,
                    "swing_low":     bar_close,
                    "broken_fractal": last_frac,
                }

    return None


# ── Step 6: Entry, SL, TP calculation ────────────────────────────────────────

def build_signal(
    current_price: float,
    bos: dict,
    bias: int,
    session,                 # Session dataclass from analysis.sessions
    symbol: str,
    asia_level: float,
    fib_high: float,
    fib_low: float,
) -> Optional["Signal"]:
    """
    Build a Signal when price is in the Goldilocks zone (0.5–0.618 fib of BoS swing).
    Uses fib_price() convention: fib 0 = top, fib 1 = bottom.

    Entry fib (from BoS swing):
      0.618 → entry level
      1.025 → SL (just beyond the swing low for BUY, beyond swing high for SELL)
      TP    → entry + 3 × (entry − SL)

    Returns None if current_price is not in the Goldilocks zone.
    """
    swing_high = bos["swing_high"]
    swing_low  = bos["swing_low"]
    fib_range  = swing_high - swing_low

    if fib_range <= 0:
        return None

    try:
        from config import (ENTRY_RR, ENTRY_SL_FIB, ENTRY_ZONE_DEEP,
                            ENTRY_ZONE_SHALLOW, BTC_MIN_SL_DIST, GOLD_MIN_SL_DIST,
                            EURUSD_MIN_SL_DIST, GBPUSD_MIN_SL_DIST, US30_MIN_SL_DIST,
                            ETH_MIN_SL_DIST)
        entry_rr        = ENTRY_RR
        entry_sl_fib    = ENTRY_SL_FIB
        entry_zone_deep = ENTRY_ZONE_DEEP
        entry_zone_shal = ENTRY_ZONE_SHALLOW
        _min_sl_table   = {
            "BTC/USD": BTC_MIN_SL_DIST,
            "ETH/USD": ETH_MIN_SL_DIST,
            "XAU/USD": GOLD_MIN_SL_DIST,
            "EUR/USD": EURUSD_MIN_SL_DIST,
            "GBP/USD": GBPUSD_MIN_SL_DIST,
            "US30":    US30_MIN_SL_DIST,
        }
        min_sl = _min_sl_table.get(symbol, GOLD_MIN_SL_DIST)
    except Exception:
        entry_rr        = 2.0
        entry_sl_fib    = 1.1
        entry_zone_deep = 0.618
        entry_zone_shal = 0.382
        min_sl          = {"BTC/USD": 400.0, "EUR/USD": 0.0005,
                           "GBP/USD": 0.0005, "US30": 20.0}.get(symbol, 8.0)

    if bias == 1:  # BUY
        entry_deep  = fib_price(swing_high, swing_low, entry_zone_deep)
        entry_shal  = fib_price(swing_high, swing_low, entry_zone_shal)
        sl_level    = fib_price(swing_high, swing_low, entry_sl_fib)
        zone_low  = entry_deep - fib_range * 0.03
        zone_high = entry_shal + fib_range * 0.03
        if not (zone_low <= current_price <= zone_high):
            return None
        entry    = current_price
        sl       = sl_level
        sl_dist  = abs(entry - sl)
        tp       = entry + entry_rr * sl_dist
        direction = "BUY"

    else:  # SELL
        entry_deep  = swing_low + entry_zone_deep * fib_range
        entry_shal  = swing_low + entry_zone_shal * fib_range
        sl_level    = swing_low + entry_sl_fib    * fib_range
        zone_low  = entry_shal - fib_range * 0.03
        zone_high = entry_deep + fib_range * 0.03
        if not (zone_low <= current_price <= zone_high):
            return None
        entry    = current_price
        sl       = sl_level
        sl_dist  = abs(sl - entry)
        tp       = entry - entry_rr * sl_dist
        direction = "SELL"

    if sl_dist <= 0:
        return None

    if sl_dist < min_sl:
        return None

    return Signal(
        direction      = direction,
        entry          = round(entry,  2),
        sl             = round(sl,     2),
        tp             = round(tp,     2),
        sl_pips        = round(sl_dist, 2),
        tp_pips        = round(sl_dist * entry_rr, 2),
        rr             = entry_rr,
        session_name   = session.name,
        session_emoji  = session.emoji,
        bias           = "BULLISH" if bias == 1 else "BEARISH",
        timestamp      = datetime.now(timezone.utc),
        symbol         = symbol,
        asia_level     = round(asia_level, 2),
        fib_high       = round(fib_high,   2),
        fib_low        = round(fib_low,    2),
        bos_swing_high = round(swing_high, 2),
        bos_swing_low  = round(swing_low,  2),
    )


# ── Stateless snapshot analyzer (used by /scan command) ──────────────────────

def analyze_snapshot(
    candles: dict,
    symbol: str = "XAU/USD",
) -> dict:
    """
    Returns a diagnostic dict showing which steps have been completed.
    Used by /scan to explain bot status without executing trades.

    Returns:
      {
        "step": 0–6  (highest completed step),
        "bias": 1/-1/0,
        "asia_level": float or None,
        "sweep": dict or None,
        "fakeout": bool,
        "bos": dict or None,
        "signal": Signal or None,
        "reason": str,
      }
    """
    from analysis.sessions import get_current_session
    from datetime import datetime, timezone

    result = {
        "step": 0, "bias": 0, "asia_level": None,
        "sweep": None, "fakeout": False, "bos": None,
        "signal": None, "reason": "",
    }

    df_4h = candles.get("4h")
    df_5m = candles.get("5min")
    if df_4h is None or df_5m is None:
        result["reason"] = "Missing candle data"
        return result

    # Step 1 — bias
    bias = detect_4h_bos(df_4h)
    result["bias"] = bias
    if bias == 0:
        result["reason"] = "No clear 4H fractal BoS — bias not set"
        return result
    result["step"] = 1

    # Step 2 — Asia extreme
    today = datetime.now(timezone.utc).date()
    asia_level = get_asia_extreme(df_5m, bias, today)
    result["asia_level"] = asia_level
    if asia_level is None:
        result["reason"] = "Asia session data not available yet (00:00–08:00 UTC)"
        return result
    result["step"] = 2

    # Step 3 — sweep
    sweep = find_sweep(df_5m, asia_level, bias, trade_date=today)
    result["sweep"] = sweep
    if sweep is None:
        level_type = "Asia low" if bias == 1 else "Asia high"
        result["reason"] = f"Waiting for {level_type} sweep @ {asia_level:.2f}"
        return result
    result["step"] = 3

    # Step 4 — fakeout zone: scan ALL bars since the sweep so mid-day starts
    # correctly detect a fakeout bounce that already occurred.
    fakeout = scan_fakeout_zone(df_5m, sweep, bias)
    result["fakeout"] = fakeout
    if not fakeout:
        try:
            from config import FAKEOUT_ZONE_LEVEL
            level = FAKEOUT_ZONE_LEVEL
        except Exception:
            level = 0.382
        fib_zone = fib_price(sweep["fib_high"], sweep["fib_low"], level)
        current_check = (
            float(df_5m["high"].iloc[-1]) if bias == 1
            else float(df_5m["low"].iloc[-1])
        )
        result["reason"] = (
            f"Waiting for price to return to {level:.3f} fakeout zone ({fib_zone:.2f}) "
            f"— current candle {'high' if bias==1 else 'low'}: {current_check:.2f}"
        )
        return result
    result["step"] = 4

    # Step 5 — 5min BoS
    bos = find_5min_bos(df_5m, bias, start_time=sweep.get("sweep_time"))
    result["bos"] = bos
    if bos is None:
        result["reason"] = f"In fakeout zone — waiting for 5min fractal BoS {'up' if bias == 1 else 'down'}"
        return result
    result["step"] = 5

    # Step 6 — entry zone
    session = get_current_session()
    signal = build_signal(
        current_price, bos, bias, session, symbol,
        asia_level, sweep["fib_high"], sweep["fib_low"],
    )
    result["signal"] = signal
    if signal is None:
        entry_618 = fib_price(bos["swing_high"], bos["swing_low"], 0.618)
        entry_500 = fib_price(bos["swing_high"], bos["swing_low"], 0.500)
        result["reason"] = (
            f"BoS confirmed — waiting for price to enter Goldilocks zone "
            f"({entry_618:.2f}–{entry_500:.2f})"
        )
        return result

    result["step"] = 6
    result["reason"] = "All 6 steps complete — SIGNAL READY"
    return result

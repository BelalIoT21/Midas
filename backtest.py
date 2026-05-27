"""
Midas Strategy Backtester  —  15min Primary Timeframe
======================================================
Fetches XAUUSD data from TwelveData starting 2025-01-20 (Trump inauguration).
Primary candle: 15min. Resamples to 1H, 4H, and daily.

Voting weights: 4H (weight 3) + 1H (weight 2) + 15min (weight 1) = 6 total.

Run: python backtest.py
"""
import sys
import os
import time

sys.path.insert(0, os.path.dirname(__file__))

import requests
import pandas as pd
import pandas_ta as ta
from datetime import datetime, timezone, timedelta
from typing import Optional

from config import (
    TWELVEDATA_API_KEY, SYMBOL,
    ATR_SL_MULT, ATR_TP1_MULT, ATR_TP2_MULT,
    MIN_CONFIDENCE, MIN_RR_RATIO,
)
from indicators.calculator import compute_votes, atr_value, supertrend_vote
from indicators.levels import all_levels, nearest_level
from analysis.sessions import (
    get_current_session, session_multiplier, should_trade, is_market_open,
)
from signals.price_action import detect_entry_pattern, near_entry_level

BASE_URL       = "https://api.twelvedata.com/time_series"
START_DATE     = datetime(2024, 5, 1, tzinfo=timezone.utc)   # 2-year backtest
WARMUP_DAYS    = 60        # extra days before START_DATE for indicator warmup
LOOKBACK_15M   = 300       # 15min bars to keep in sliding window (~75 hours)
LOOKBACK_1H    = 220       # 1H bars — needs >=200 for EMA200 indicator
LOOKBACK_4H    = 220       # 4H bars — needs >=200 for EMA200 indicator
LOOKBACK_DAILY = 100       # daily bars for trend
MAX_HOLD       = 288       # max bars to hold (288 x 15min = 3 days)
COOLDOWN_BARS  = 8         # 8 x 15min = 2 hours cooldown between signals


# -- Data fetching -------------------------------------------------------------

_api_call_times: list = []   # timestamps of recent API calls


def _rate_limited_get(url: str, params: dict) -> dict:
    """GET with a sliding-window rate limiter: max 7 calls per 65 seconds."""
    import time as _time
    while True:
        now = _time.time()
        # Drop calls older than 65s
        while _api_call_times and now - _api_call_times[0] > 65:
            _api_call_times.pop(0)
        if len(_api_call_times) < 7:
            break
        wait = 65 - (now - _api_call_times[0])
        print(f"\n  [rate limit] waiting {wait:.0f}s...", end="", flush=True)
        _time.sleep(wait + 1)
    _api_call_times.append(_time.time())
    resp = requests.get(url, params=params, timeout=30)
    resp.raise_for_status()
    return resp.json()


def _fetch_chunk(interval: str, end_dt: datetime) -> pd.DataFrame:
    params = {
        "symbol":     SYMBOL,
        "interval":   interval,
        "outputsize": 5000,
        "end_date":   end_dt.strftime("%Y-%m-%d %H:%M:%S"),
        "apikey":     TWELVEDATA_API_KEY,
        "format":     "JSON",
        "timezone":   "UTC",
    }
    while True:
        data = _rate_limited_get(BASE_URL, params)
        if data.get("status") == "error":
            msg = data.get("message", "")
            if "run out of API credits" in msg or "429" in msg:
                print("\n  [rate limit] hit — sleeping 65s...", end="", flush=True)
                time.sleep(65)
                _api_call_times.clear()
                continue
            raise ValueError(f"TwelveData: {msg}")
        break
    values = data.get("values", [])
    if not values:
        return pd.DataFrame()

    df = pd.DataFrame(values)
    df["datetime"] = pd.to_datetime(df["datetime"], utc=True)
    for col in ["open", "high", "low", "close"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df["volume"] = 0.0
    return (
        df.set_index("datetime")
        .sort_index()[["open", "high", "low", "close", "volume"]]
    )


def fetch_historical(interval: str, start: datetime) -> pd.DataFrame:
    end     = datetime.now(timezone.utc)
    chunks: list[pd.DataFrame] = []
    current_end = end

    print(f"  Fetching {interval} from {start.date()}...", end="", flush=True)
    while True:
        chunk = _fetch_chunk(interval, current_end)
        if chunk.empty:
            break
        chunks.append(chunk)
        print(".", end="", flush=True)
        earliest = chunk.index.min().to_pydatetime()
        if earliest <= start:
            break
        if len(chunk) < 100:
            break
        current_end = earliest - timedelta(minutes=1)

    print()
    if not chunks:
        raise ValueError(f"No data returned for {interval}")

    df = pd.concat(chunks)
    df = df[~df.index.duplicated(keep="first")].sort_index()
    df = df[df.index >= pd.Timestamp(start)]
    print(f"  {interval}: {len(df):,} candles  "
          f"({df.index[0].date()} to {df.index[-1].date()})")
    return df


# -- Signal scoring ------------------------------------------------------------

def _daily_trend_local(df: pd.DataFrame) -> str:
    if df is None or len(df) < 50:
        return "NEUTRAL"
    close = float(df["close"].iloc[-1])
    ema50 = float(df["close"].ewm(span=50, adjust=False).mean().iloc[-1])
    band  = ema50 * 0.0005
    if close > ema50 + band:
        return "BULL"
    if close < ema50 - band:
        return "BEAR"
    return "NEUTRAL"


def _rsi_series(df: pd.DataFrame) -> pd.Series:
    delta = df["close"].diff()
    gain  = delta.clip(lower=0).ewm(com=13, adjust=False).mean()
    loss  = (-delta.clip(upper=0)).ewm(com=13, adjust=False).mean()
    rs    = gain / loss.replace(0, float("nan"))
    return 100 - (100 / (1 + rs))


def _adx_strong(df: pd.DataFrame) -> bool:
    if df is None or len(df) < 20:
        return True
    result = ta.adx(df["high"], df["low"], df["close"], length=14)
    if result is None or result.empty:
        return True
    val = result.iloc[:, 0].dropna()
    return len(val) == 0 or float(val.iloc[-1]) >= 22


def _rsi_guard(df: pd.DataFrame, direction: str) -> bool:
    if df is None or len(df) < 15:
        return True
    rsi = _rsi_series(df)
    if rsi.empty:
        return True
    latest = float(rsi.iloc[-1])
    if direction == "BUY"  and latest > 70:
        return False
    if direction == "SELL" and latest < 30:
        return False
    return True


def _supertrend_ok(df_4h: pd.DataFrame, df_1h: pd.DataFrame, direction: str) -> bool:
    opposing = -1 if direction == "BUY" else 1
    for df in (df_4h, df_1h):
        if df is not None and len(df) >= 10:
            if supertrend_vote(df) == opposing:
                return False
    return True


def _4h_consensus_ok(breakdown: list, direction: str, threshold: int = 5) -> bool:
    expected = 1 if direction == "BUY" else -1
    votes_4h = [v for _, tf, v in breakdown if tf == "4h"]
    if not votes_4h:
        return True
    return sum(1 for v in votes_4h if v == expected) >= threshold


def score_bar(
    df_15m: pd.DataFrame,
    df_1h:  pd.DataFrame,
    df_4h:  pd.DataFrame,
    df_daily: pd.DataFrame,
    dt: datetime,
) -> Optional[tuple]:
    """
    Returns (direction, confidence, atr, close) or None.
    Mirrors analyze() in signals/engine.py — TREND mode.
    4H(3) + 1H(2) + 15min(1). Uses 15min ATR for level sizing.
    """
    session = get_current_session(dt)
    if not should_trade(session):
        return None

    weighted_bull = 0.0
    weighted_bear = 0.0
    active_weight = 0
    breakdown     = []

    for interval, weight, df in [("4h", 3, df_4h), ("1h", 2, df_1h), ("15min", 1, df_15m)]:
        if df is None or len(df) < 30:
            continue
        votes = compute_votes(df)
        for name, vote in votes:
            weighted_bull += weight * max(vote, 0)
            weighted_bear += weight * max(-vote, 0)
            if vote != 0:
                active_weight += weight
            breakdown.append((name, interval, vote))

    if active_weight == 0:
        return None

    close = float(df_15m["close"].iloc[-1])
    atr   = atr_value(df_15m)
    if atr is None:
        return None

    bull_conf = weighted_bull / active_weight
    bear_conf = weighted_bear / active_weight

    # S/R directional boost
    try:
        sr_data = all_levels(df_1h)
        near    = nearest_level(close, sr_data.get("all_sr", []))
        if near is not None:
            if near < close:
                bull_conf += 0.06
            else:
                bear_conf += 0.06
    except Exception:
        pass

    mult       = session_multiplier(session)
    bull_conf *= mult
    bear_conf *= mult

    direction  = "BUY" if bull_conf >= bear_conf else "SELL"
    confidence = bull_conf if direction == "BUY" else bear_conf

    if confidence < MIN_CONFIDENCE:
        return None

    # Filter 1: supertrend alignment
    if not _supertrend_ok(df_4h, df_1h, direction):
        return None

    # Filter 2: ADX strength >= 18
    if not _adx_strong(df_4h):
        return None

    # Filter 3: RSI guard
    if not _rsi_guard(df_4h, direction):
        return None

    # Filter 4: 4H consensus >= 5/11
    if not _4h_consensus_ok(breakdown, direction):
        return None

    return direction, round(min(confidence, 0.99), 4), atr, close


# -- Trade outcome -------------------------------------------------------------

def resolve_trade(
    df_future: pd.DataFrame,
    direction: str,
    sl: float,
    tp1: float,
) -> tuple[str, int]:
    """Walk future bars until SL or TP1 hit. Conservative: same-bar = loss."""
    for bars, (_, bar) in enumerate(df_future.iterrows(), 1):
        hit_sl = bar["low"] <= sl   if direction == "BUY" else bar["high"] >= sl
        hit_tp = bar["high"] >= tp1 if direction == "BUY" else bar["low"]  <= tp1
        if hit_sl and hit_tp:
            return "loss", bars
        if hit_sl:
            return "loss", bars
        if hit_tp:
            return "win", bars
    return "expired", len(df_future)


# -- Report --------------------------------------------------------------------

def print_report(signals: list[dict], label: str) -> None:
    if not signals:
        print(f"\n  No signals generated ({label}).")
        return

    df = pd.DataFrame(signals)
    closed  = df[df["outcome"] != "expired"].copy()
    wins    = closed[closed["outcome"] == "win"]
    losses  = closed[closed["outcome"] == "loss"]

    n_total   = len(df)
    n_closed  = len(closed)
    n_wins    = len(wins)
    n_losses  = len(losses)
    n_expired = len(df[df["outcome"] == "expired"])
    win_rate  = round(n_wins / n_closed * 100, 1) if n_closed > 0 else 0.0

    avg_rr       = float(df["rr1"].mean())
    gross_profit = n_wins * avg_rr
    gross_loss   = float(n_losses)
    pf = round(gross_profit / gross_loss, 2) if gross_loss > 0 else float("inf")

    streak = max_streak = 0
    for o in closed["outcome"]:
        streak = streak + 1 if o == "loss" else 0
        max_streak = max(max_streak, streak)

    avg_hold_w = round(wins["bars_held"].mean() * 15 / 60, 1) if not wins.empty else 0
    avg_hold_l = round(losses["bars_held"].mean() * 15 / 60, 1) if not losses.empty else 0

    months = max((df['ts'].max() - df['ts'].min()).days / 30, 1)
    w = 58
    sep = "-" * w

    print(f"\n{'='*w}")
    print(f"  MIDAS BACKTEST — {label}")
    print(f"{'='*w}")
    print(f"  Period:            {df['ts'].min().date()} to {df['ts'].max().date()}")
    print(f"  Total signals:     {n_total}")
    print(f"  Closed trades:     {n_closed}  (win {n_wins} / loss {n_losses})")
    print(f"  Expired <3 days:   {n_expired}")
    print(f"  Win rate:          {win_rate}%")
    print(f"  Profit factor:     {pf}x  (gross {gross_profit:.1f}R / {gross_loss:.1f}R lost)")
    print(f"  Avg R:R at entry:  1:{avg_rr:.2f}")
    print(f"  Max consec losses: {max_streak}")
    print(f"  Avg hold to win:   {avg_hold_w}h  |  Avg hold to loss: {avg_hold_l}h")
    print(f"  Signals / month:   {round(n_total / months, 1)}")

    print(f"\n  {sep}")
    print(f"  BY DIRECTION")
    print(f"  {sep}")
    for d in ["BUY", "SELL"]:
        sub = closed[closed["direction"] == d]
        w_  = len(sub[sub["outcome"] == "win"])
        l_  = len(sub[sub["outcome"] == "loss"])
        wr_ = round(w_ / (w_ + l_) * 100, 1) if (w_ + l_) > 0 else 0
        bar_ = "#" * w_ + "." * l_
        print(f"  {d:4s}  {w_:3d}W / {l_:3d}L  ({wr_:5.1f}%)  {bar_[:30]}")

    print(f"\n  {sep}")
    print(f"  BY SESSION")
    print(f"  {sep}")
    for sess in sorted(closed["session"].unique()):
        sub = closed[closed["session"] == sess]
        w_  = len(sub[sub["outcome"] == "win"])
        l_  = len(sub[sub["outcome"] == "loss"])
        wr_ = round(w_ / (w_ + l_) * 100, 1) if (w_ + l_) > 0 else 0
        print(f"  {sess:<22s}  {w_:3d}W / {l_:3d}L  ({wr_:5.1f}%)")

    print(f"\n  {sep}")
    print(f"  MONTHLY BREAKDOWN (closed trades)")
    print(f"  {sep}")
    closed["month"] = pd.to_datetime(closed["ts"]).dt.to_period("M")
    for period, grp in closed.groupby("month"):
        w_ = int((grp["outcome"] == "win").sum())
        l_ = int((grp["outcome"] == "loss").sum())
        wr_ = round(w_ / (w_ + l_) * 100, 1) if (w_ + l_) > 0 else 0
        bar_ = ("#" * w_ + "." * l_)[:20]
        print(f"  {period}  {w_:2d}W {l_:2d}L  {wr_:5.1f}%  {bar_}")

    print(f"\n{'='*w}\n")


def _print_bounce_report(signals: list[dict], label: str) -> None:
    if not signals:
        print("  No bounce signals generated.\n")
        return

    df      = pd.DataFrame(signals)
    closed  = df[df["outcome"] != "expired"]
    wins    = closed[closed["outcome"] == "win"]
    losses  = closed[closed["outcome"] == "loss"]

    n_total  = len(df)
    n_wins   = len(wins)
    n_losses = len(losses)
    n_closed = n_wins + n_losses
    win_rate = round(n_wins / n_closed * 100, 1) if n_closed > 0 else 0
    avg_rr   = float(df["rr1"].mean())
    pf       = round(n_wins * avg_rr / n_losses, 2) if n_losses > 0 else float("inf")
    months   = max((df['ts'].max() - df['ts'].min()).days / 30, 1)

    w = 58
    print(f"\n{'='*w}")
    print(f"  S/R BOUNCE SIGNALS — {label}")
    print(f"{'='*w}")
    print(f"  Total signals:   {n_total}  ({round(n_total/months,1)}/month)")
    print(f"  Closed:          {n_closed}  (win {n_wins} / loss {n_losses})")
    print(f"  Win rate:        {win_rate}%")
    print(f"  Profit factor:   {pf}x  (avg R:R 1:{avg_rr:.2f})")
    print(f"\n  By Direction:")
    for d in ["BUY", "SELL"]:
        sub = closed[closed["direction"] == d]
        w_  = int((sub["outcome"] == "win").sum())
        l_  = int((sub["outcome"] == "loss").sum())
        wr_ = round(w_ / (w_ + l_) * 100, 1) if (w_ + l_) > 0 else 0
        print(f"    {d}:  {w_}W / {l_}L  ({wr_}%)")
    print(f"{'='*w}\n")


# -- Main ----------------------------------------------------------------------

def run_backtest() -> None:
    start_fetch = START_DATE - timedelta(days=WARMUP_DAYS)

    print(f"\n{'='*58}")
    print(f"  MIDAS BACKTEST  —  15min  —  Trump Era (Jan 2025+)")
    print(f"  XAUUSD  |  {START_DATE.date()} to today")
    print(f"  [config] MIN_CONF={MIN_CONFIDENCE}  SL={ATR_SL_MULT}x  "
          f"TP1={ATR_TP1_MULT}x  TP2={ATR_TP2_MULT}x  RR1=1:{ATR_TP1_MULT/ATR_SL_MULT:.2f}")
    print(f"{'='*58}\n")

    # Fetch base 15min data (includes warmup window)
    df_15m_full = fetch_historical("15min", start_fetch)

    agg = {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    df_1h_full    = df_15m_full.resample("1h",  closed="left", label="left").agg(agg).dropna()
    df_4h_full    = df_15m_full.resample("4h",  closed="left", label="left").agg(agg).dropna()
    df_daily_full = df_15m_full.resample("1D",  closed="left", label="left").agg(agg).dropna()

    print(f"\n  Resampled: 1H={len(df_1h_full):,}  4H={len(df_4h_full):,}  Daily={len(df_daily_full):,}")

    # Only walk bars from START_DATE onwards (warmup already loaded)
    walk_idx = df_15m_full.index.searchsorted(pd.Timestamp(START_DATE))
    # Ensure we have enough lookback
    walk_start = max(walk_idx, LOOKBACK_15M)

    print(f"\n  Walking forward through {len(df_15m_full) - walk_start:,} bars "
          f"(from {START_DATE.date()})...\n")

    signals:        list[dict] = []
    bounce_signals: list[dict] = []
    last_sig:    dict[str, int] = {}   # bar index of last signal per direction
    last_bounce: dict[str, int] = {}
    processed  = 0
    skipped_mh = 0
    total_walk = len(df_15m_full) - walk_start

    for i in range(walk_start, len(df_15m_full)):
        ts = df_15m_full.index[i].to_pydatetime()

        if not is_market_open(ts):
            skipped_mh += 1
            continue

        # Build sliding windows
        df_15m_win = df_15m_full.iloc[i - LOOKBACK_15M + 1: i + 1]
        df_1h_win  = df_1h_full[df_1h_full.index <= df_15m_full.index[i]].tail(LOOKBACK_1H)
        df_4h_win  = df_4h_full[df_4h_full.index <= df_15m_full.index[i]].tail(LOOKBACK_4H)
        df_daily_win = df_daily_full[df_daily_full.index <= df_15m_full.index[i]].tail(LOOKBACK_DAILY)

        if len(df_4h_win) < 30 or len(df_daily_win) < 50:
            continue

        result = score_bar(df_15m_win, df_1h_win, df_4h_win, df_daily_win, ts)

        if result is not None:
            direction, confidence, atr, close = result
            last_bar = last_sig.get(direction, -9999)
            if (i - last_bar) < COOLDOWN_BARS:
                result = None

        if result is not None:
            direction, confidence, atr, close = result
            last_sig[direction] = i

            sl_d  = atr * ATR_SL_MULT
            tp1_d = atr * ATR_TP1_MULT
            tp2_d = atr * ATR_TP2_MULT
            sl    = round(close - sl_d  if direction == "BUY" else close + sl_d,  2)
            tp1   = round(close + tp1_d if direction == "BUY" else close - tp1_d, 2)
            rr1   = round(tp1_d / sl_d, 2)

            if rr1 >= MIN_RR_RATIO:
                df_future = df_15m_full.iloc[i + 1: i + 1 + MAX_HOLD]
                outcome, bars_held = resolve_trade(df_future, direction, sl, tp1)
                signals.append({
                    "ts":         ts,
                    "direction":  direction,
                    "confidence": round(confidence * 100, 1),
                    "entry":      close,
                    "sl":         sl,
                    "tp1":        tp1,
                    "rr1":        rr1,
                    "rr2":        round(tp2_d / sl_d, 2),
                    "atr":        round(atr, 2),
                    "outcome":    outcome,
                    "bars_held":  bars_held,
                    "session":    get_current_session(ts).name,
                })

        # -- Bounce signal check -----------------------------------------------
        try:
            from signals.sr_bounce import detect_bounce
            bounce = detect_bounce(df_1h_win, df_daily_win, ts)
            if bounce is not None:
                last_b = last_bounce.get(bounce.direction, -9999)
                if (i - last_b) >= 24:   # 24 x 15min = 6h bounce cooldown
                    last_bounce[bounce.direction] = i
                    df_future_b = df_15m_full.iloc[i + 1: i + 1 + MAX_HOLD]
                    outcome_b, bars_b = resolve_trade(
                        df_future_b, bounce.direction, bounce.sl, bounce.tp1
                    )
                    bounce_signals.append({
                        "ts":        ts,
                        "direction": bounce.direction,
                        "level":     bounce.level,
                        "level_type": bounce.level_type,
                        "rr1":       bounce.rr1,
                        "outcome":   outcome_b,
                        "bars_held": bars_b,
                        "session":   get_current_session(ts).name,
                    })
        except Exception:
            pass

        processed += 1
        if processed % 2000 == 0:
            pct = round(processed / total_walk * 100)
            print(f"  [{pct:3d}%]  bars: {processed:,}  "
                  f"trend: {len(signals)}  bounce: {len(bounce_signals)}")

    print(f"\n  Done.  {processed:,} bars scanned, "
          f"{skipped_mh:,} outside market hours.")

    label = "15min | Trump Era (Jan 2025+)"
    print_report(signals, label)
    _print_bounce_report(bounce_signals, label)


if __name__ == "__main__":
    run_backtest()

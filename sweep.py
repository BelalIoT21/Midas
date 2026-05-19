"""
Midas Strategy Parameter Sweep
================================
Phase 1: Walk Trump-era 15min data once, collecting ALL candidate signal
         metadata + future price paths (no filtering yet).

Phase 2: Sweep ~3000+ parameter combinations in memory (microseconds each).

Phase 3: Report top 25 configs ranked by a composite score (PF × WR × sqrt(n)).

Run: python sweep.py
"""
import sys
import os
import time
import itertools

sys.path.insert(0, os.path.dirname(__file__))

import numpy as np
import requests
import pandas as pd
import pandas_ta as ta
from datetime import datetime, timezone, timedelta
from typing import Optional

from config import TWELVEDATA_API_KEY, SYMBOL
from indicators.calculator import compute_votes, atr_value, supertrend_vote
from indicators.levels import all_levels, nearest_level
from analysis.sessions import (
    get_current_session, session_multiplier, should_trade, is_market_open,
)

BASE_URL      = "https://api.twelvedata.com/time_series"
START_DATE    = datetime(2025, 1, 20, tzinfo=timezone.utc)
WARMUP_DAYS   = 45
LOOKBACK_15M  = 300
LOOKBACK_1H   = 220
LOOKBACK_4H   = 220
LOOKBACK_DAILY = 100
MAX_HOLD      = 288    # 3 days at 15min


# ── Data fetching ────────────────────────────────────────────────────────────

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
    resp = requests.get(BASE_URL, params=params, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    if data.get("status") == "error":
        raise ValueError(f"TwelveData: {data.get('message')}")
    values = data.get("values", [])
    if not values:
        return pd.DataFrame()
    df = pd.DataFrame(values)
    df["datetime"] = pd.to_datetime(df["datetime"], utc=True)
    for col in ["open", "high", "low", "close"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df["volume"] = 0.0
    return df.set_index("datetime").sort_index()[["open", "high", "low", "close", "volume"]]


def fetch_historical(interval: str, start: datetime) -> pd.DataFrame:
    end, chunks, current_end = datetime.now(timezone.utc), [], datetime.now(timezone.utc)
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
        time.sleep(0.4)
    print()
    df = pd.concat(chunks)
    df = df[~df.index.duplicated(keep="first")].sort_index()
    df = df[df.index >= pd.Timestamp(start)]
    print(f"  {interval}: {len(df):,} candles  ({df.index[0].date()} to {df.index[-1].date()})")
    return df


# ── Helpers ───────────────────────────────────────────────────────────────────

def _daily_trend(df: pd.DataFrame) -> str:
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


# ── Phase 1: Collect raw candidates ──────────────────────────────────────────

def collect_candidates(df_15m_full, df_1h_full, df_4h_full, df_daily_full) -> list[dict]:
    """
    Walk every in-hours bar. For bars where ANY direction has conf >= 0.50,
    store ALL metadata needed for parameter sweeping.
    Also stores future 288-bar (high, low) path for fast trade resolution.
    """
    walk_idx   = df_15m_full.index.searchsorted(pd.Timestamp(START_DATE))
    walk_start = max(walk_idx, LOOKBACK_15M)
    total      = len(df_15m_full) - walk_start
    candidates: list[dict] = []
    processed  = 0

    print(f"\n  Collecting candidates over {total:,} bars...\n")

    for i in range(walk_start, len(df_15m_full)):
        ts = df_15m_full.index[i].to_pydatetime()
        if not is_market_open(ts):
            continue

        df_15m_win = df_15m_full.iloc[i - LOOKBACK_15M + 1: i + 1]
        df_1h_win  = df_1h_full[df_1h_full.index  <= df_15m_full.index[i]].tail(LOOKBACK_1H)
        df_4h_win  = df_4h_full[df_4h_full.index  <= df_15m_full.index[i]].tail(LOOKBACK_4H)
        df_d_win   = df_daily_full[df_daily_full.index <= df_15m_full.index[i]].tail(LOOKBACK_DAILY)

        if len(df_4h_win) < 30 or len(df_d_win) < 50:
            continue

        session = get_current_session(ts)
        if not should_trade(session):
            continue

        # ── Compute votes ──────────────────────────────────────────────────
        weighted_bull = weighted_bear = active_weight = 0.0
        breakdown = []
        for interval, weight, df in [("4h", 3, df_4h_win), ("1h", 2, df_1h_win), ("15min", 1, df_15m_win)]:
            if df is None or len(df) < 30:
                continue
            try:
                votes = compute_votes(df)
            except Exception:
                continue
            for name, vote in votes:
                weighted_bull += weight * max(vote, 0)
                weighted_bear += weight * max(-vote, 0)
                if vote != 0:
                    active_weight += weight
                breakdown.append((name, interval, vote))

        if active_weight == 0:
            continue

        close = float(df_15m_win["close"].iloc[-1])
        atr   = atr_value(df_15m_win)
        if atr is None:
            continue

        bull_conf = weighted_bull / active_weight
        bear_conf = weighted_bear / active_weight

        # S/R boost
        try:
            sr_data = all_levels(df_1h_win)
            near = nearest_level(close, sr_data.get("all_sr", []))
            if near is not None:
                if near < close:
                    bull_conf += 0.06
                else:
                    bear_conf += 0.06
        except Exception:
            pass

        mult = session_multiplier(session)
        bull_conf *= mult
        bear_conf *= mult

        # Only collect if at least one direction is above loose threshold
        if max(bull_conf, bear_conf) < 0.50:
            continue

        # Raw 4H vote counts (for consensus threshold sweeping)
        v4h_bull = sum(1 for _, tf, v in breakdown if tf == "4h" and v == 1)
        v4h_bear = sum(1 for _, tf, v in breakdown if tf == "4h" and v == -1)

        # ADX (4H)
        try:
            adx_r = ta.adx(df_4h_win["high"], df_4h_win["low"], df_4h_win["close"], length=14)
            adx_val = float(adx_r.iloc[:, 0].dropna().iloc[-1]) if adx_r is not None else 25.0
        except Exception:
            adx_val = 25.0

        # RSI (4H and 1H)
        try:
            rsi_4h = float(_rsi_series(df_4h_win).iloc[-1])
        except Exception:
            rsi_4h = 50.0
        try:
            rsi_1h = float(_rsi_series(df_1h_win).iloc[-1])
        except Exception:
            rsi_1h = 50.0

        # Supertrend (4H, 1H, 15min)
        try:
            st_4h = supertrend_vote(df_4h_win)
        except Exception:
            st_4h = 0
        try:
            st_1h = supertrend_vote(df_1h_win)
        except Exception:
            st_1h = 0
        try:
            st_15m = supertrend_vote(df_15m_win)
        except Exception:
            st_15m = 0

        daily_tr = _daily_trend(df_d_win)

        # Future price path for trade resolution (numpy, fast)
        future_slice = df_15m_full.iloc[i + 1: i + 1 + MAX_HOLD]
        future_highs = future_slice["high"].values.astype(np.float32)
        future_lows  = future_slice["low"].values.astype(np.float32)

        candidates.append({
            "ts":        ts,
            "close":     close,
            "atr":       atr,
            "bull_conf": bull_conf,
            "bear_conf": bear_conf,
            "v4h_bull":  v4h_bull,
            "v4h_bear":  v4h_bear,
            "adx_4h":    adx_val,
            "rsi_4h":    rsi_4h,
            "rsi_1h":    rsi_1h,
            "st_4h":     st_4h,
            "st_1h":     st_1h,
            "st_15m":    st_15m,
            "daily_tr":  daily_tr,
            "session":   session.name,
            "fh":        future_highs,   # future highs array
            "fl":        future_lows,    # future lows array
        })

        processed += 1
        if processed % 5000 == 0:
            pct = round((i - walk_start) / total * 100)
            print(f"  [{pct:3d}%]  bars processed: {processed:,}  candidates: {len(candidates):,}")

    print(f"\n  Collection done.  {processed:,} processed, {len(candidates):,} candidates.\n")
    return candidates


# ── Phase 2: Sweep ────────────────────────────────────────────────────────────

def resolve_fast(direction: str, close: float, sl: float, tp: float,
                 fh: np.ndarray, fl: np.ndarray) -> str:
    """Vectorized trade resolution: walk future bars until SL or TP hit."""
    if direction == "BUY":
        for h, l in zip(fh, fl):
            if l <= sl and h >= tp:
                return "loss"
            if l <= sl:
                return "loss"
            if h >= tp:
                return "win"
    else:
        for h, l in zip(fh, fl):
            if h >= sl and l <= tp:
                return "loss"
            if h >= sl:
                return "loss"
            if l <= tp:
                return "win"
    return "expired"


def eval_config(
    candidates: list[dict],
    min_conf:       float,
    sl_mult:        float,
    tp_mult:        float,
    direction:      str,   # "BOTH" | "BUY" | "SELL" | "TREND"
    consensus_min:  int,
    trend_filter:   bool,
    use_supertrend: bool,
    adx_min:        float,
    rsi_guard:      float,
    cooldown_h:     int,
) -> Optional[dict]:
    """
    Apply config to pre-collected candidates. Returns metrics dict or None.
    TREND direction = BUY when BULL, SELL when BEAR, skip NEUTRAL.
    """
    wins = losses = expired = 0
    gross_rr = 0.0
    last_ts: dict[str, Optional[datetime]] = {"BUY": None, "SELL": None}
    n_signals = 0

    rsi_ob = rsi_guard        # overbought (no BUY above)
    rsi_os = 100 - rsi_guard  # oversold   (no SELL below)

    for c in candidates:
        # Determine which directions to try for this candidate
        if direction == "BUY":
            dirs = ["BUY"]
        elif direction == "SELL":
            dirs = ["SELL"]
        elif direction == "TREND":
            if c["daily_tr"] == "BULL":
                dirs = ["BUY"]
            elif c["daily_tr"] == "BEAR":
                dirs = ["SELL"]
            else:
                continue
        else:
            dirs = ["BUY", "SELL"]

        for d in dirs:
            conf = c["bull_conf"] if d == "BUY" else c["bear_conf"]
            if conf < min_conf:
                continue

            # Cooldown
            lt = last_ts[d]
            if lt and (c["ts"] - lt).total_seconds() < cooldown_h * 3600:
                continue

            # Trend filter (block counter-trend)
            if trend_filter:
                if c["daily_tr"] == "BULL" and d == "SELL":
                    continue
                if c["daily_tr"] == "BEAR" and d == "BUY":
                    continue

            # Supertrend filter
            if use_supertrend:
                opp = -1 if d == "BUY" else 1
                if c["st_4h"] == opp or c["st_1h"] == opp:
                    continue

            # ADX
            if c["adx_4h"] < adx_min:
                continue

            # RSI guard (4H)
            if d == "BUY"  and c["rsi_4h"] > rsi_ob:
                continue
            if d == "SELL" and c["rsi_4h"] < rsi_os:
                continue

            # 4H consensus
            v4h = c["v4h_bull"] if d == "BUY" else c["v4h_bear"]
            if v4h < consensus_min:
                continue

            last_ts[d] = c["ts"]
            n_signals  += 1

            sl_d = c["atr"] * sl_mult
            tp_d = c["atr"] * tp_mult
            sl   = c["close"] - sl_d if d == "BUY" else c["close"] + sl_d
            tp   = c["close"] + tp_d if d == "BUY" else c["close"] - tp_d
            rr   = tp_d / sl_d

            outcome = resolve_fast(d, c["close"], sl, tp, c["fh"], c["fl"])
            if outcome == "win":
                wins   += 1
                gross_rr += rr
            elif outcome == "loss":
                losses += 1
            else:
                expired += 1

    closed = wins + losses
    if closed < 15:  # too few to be meaningful
        return None
    if losses == 0:
        return None

    wr = wins / closed
    pf = (gross_rr / losses)
    # composite score: reward high PF, reasonable WR, adequate sample size
    score = pf * (wr ** 0.5) * (closed ** 0.3)

    return {
        "pf":      round(pf, 3),
        "wr":      round(wr * 100, 1),
        "wins":    wins,
        "losses":  losses,
        "expired": expired,
        "n":       closed,
        "n_total": n_signals,
        "score":   round(score, 4),
        # config
        "min_conf":    min_conf,
        "sl_mult":     sl_mult,
        "tp_mult":     tp_mult,
        "direction":   direction,
        "consensus":   consensus_min,
        "trend_filt":  trend_filter,
        "supertrend":  use_supertrend,
        "adx_min":     adx_min,
        "rsi_guard":   rsi_guard,
        "cooldown_h":  cooldown_h,
    }


def run_sweep(candidates: list[dict]) -> None:
    print(f"  Building parameter grid...")

    param_grid = list(itertools.product(
        [0.60, 0.63, 0.66, 0.68, 0.70, 0.73],     # min_conf
        [0.8, 1.0, 1.2, 1.5],                       # sl_mult
        [2.0, 2.5, 3.0, 3.5, 4.0, 5.0],             # tp_mult
        ["BOTH", "BUY", "SELL", "TREND"],            # direction
        [5, 6, 7, 8],                                # consensus_min
        [True, False],                               # trend_filter
        [True, False],                               # use_supertrend
        [18.0, 22.0, 25.0],                          # adx_min
        [68.0, 72.0, 75.0],                          # rsi_guard
        [4, 6],                                      # cooldown_h
    ))

    total  = len(param_grid)
    print(f"  Running {total:,} configurations...", flush=True)

    results = []
    t0 = time.time()

    for idx, (min_conf, sl_mult, tp_mult, direction, consensus_min,
              trend_filter, use_supertrend, adx_min, rsi_guard, cooldown_h) in enumerate(param_grid):

        # Skip nonsensical combos: trend direction + trend filter is redundant
        if direction == "TREND" and not trend_filter:
            continue
        # TREND already implies trend alignment, no need for extra trend_filter=True duplication
        # but keep both so we can see if extra filter hurts

        r = eval_config(
            candidates,
            min_conf=min_conf,
            sl_mult=sl_mult,
            tp_mult=tp_mult,
            direction=direction,
            consensus_min=consensus_min,
            trend_filter=trend_filter,
            use_supertrend=use_supertrend,
            adx_min=adx_min,
            rsi_guard=rsi_guard,
            cooldown_h=cooldown_h,
        )
        if r is not None:
            results.append(r)

        if (idx + 1) % 20000 == 0:
            elapsed = time.time() - t0
            rate    = (idx + 1) / elapsed
            eta     = (total - idx - 1) / rate
            print(f"  [{round((idx+1)/total*100):3d}%]  {idx+1:,}/{total:,}  "
                  f"valid results: {len(results):,}  ETA: {eta:.0f}s", flush=True)

    elapsed = time.time() - t0
    print(f"\n  Sweep done in {elapsed:.1f}s.  Valid configs: {len(results):,}\n")

    if not results:
        print("  No valid configurations found.")
        return

    results.sort(key=lambda x: x["score"], reverse=True)
    print_sweep_results(results[:30])

    # Also print by pure profit factor
    results_by_pf = sorted(results, key=lambda x: x["pf"], reverse=True)
    print("\n  --- TOP 15 BY PURE PROFIT FACTOR ---\n")
    print_sweep_results(results_by_pf[:15])

    return results


def print_sweep_results(results: list[dict]) -> None:
    hdr = (f"  {'Rank':<4} {'PF':>5} {'WR%':>6} {'W':>4} {'L':>4} {'N':>5} "
           f"{'Conf':>5} {'SL':>4} {'TP':>4} {'Dir':<8} {'Cons':>4} "
           f"{'TF':>2} {'ST':>2} {'ADX':>5} {'RSI':>5} {'CD':>3}")
    print(hdr)
    print("  " + "-" * (len(hdr) - 2))
    for rank, r in enumerate(results, 1):
        print(
            f"  {rank:<4} "
            f"{r['pf']:>5.2f} "
            f"{r['wr']:>5.1f}% "
            f"{r['wins']:>4} "
            f"{r['losses']:>4} "
            f"{r['n']:>5} "
            f"{r['min_conf']:>5.2f} "
            f"{r['sl_mult']:>4.1f} "
            f"{r['tp_mult']:>4.1f} "
            f"{r['direction']:<8} "
            f"{r['consensus']:>4} "
            f"{'Y' if r['trend_filt'] else 'N':>2} "
            f"{'Y' if r['supertrend'] else 'N':>2} "
            f"{r['adx_min']:>5.0f} "
            f"{r['rsi_guard']:>5.0f} "
            f"{r['cooldown_h']:>3}"
        )


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    start_fetch = START_DATE - timedelta(days=WARMUP_DAYS)

    print(f"\n{'='*60}")
    print(f"  MIDAS PARAMETER SWEEP  —  15min  —  Trump Era")
    print(f"  XAUUSD  |  {START_DATE.date()} to today")
    print(f"{'='*60}\n")

    df_15m_full = fetch_historical("15min", start_fetch)

    agg = {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    df_1h_full    = df_15m_full.resample("1h", closed="left", label="left").agg(agg).dropna()
    df_4h_full    = df_15m_full.resample("4h", closed="left", label="left").agg(agg).dropna()
    df_daily_full = df_15m_full.resample("1D", closed="left", label="left").agg(agg).dropna()

    print(f"\n  Resampled: 1H={len(df_1h_full):,}  4H={len(df_4h_full):,}  Daily={len(df_daily_full):,}")

    candidates = collect_candidates(df_15m_full, df_1h_full, df_4h_full, df_daily_full)

    if not candidates:
        print("  No candidates found — check data.")
        return

    top_results = run_sweep(candidates)

    if top_results:
        best = top_results[0]
        print(f"\n{'='*60}")
        print(f"  BEST CONFIG (by composite score):")
        print(f"{'='*60}")
        print(f"  PF:        {best['pf']}x")
        print(f"  WR:        {best['wr']}%")
        print(f"  Trades:    {best['wins']}W / {best['losses']}L ({best['n']} closed)")
        print(f"  Config:")
        print(f"    min_confidence = {best['min_conf']}")
        print(f"    ATR_SL_MULT    = {best['sl_mult']}")
        print(f"    ATR_TP1_MULT   = {best['tp_mult']}")
        print(f"    direction      = {best['direction']}")
        print(f"    4H consensus   = {best['consensus']}/11")
        print(f"    trend_filter   = {best['trend_filt']}")
        print(f"    supertrend     = {best['supertrend']}")
        print(f"    adx_min        = {best['adx_min']}")
        print(f"    rsi_guard      = {best['rsi_guard']}")
        print(f"    cooldown_h     = {best['cooldown_h']}")
        print(f"{'='*60}\n")


if __name__ == "__main__":
    main()

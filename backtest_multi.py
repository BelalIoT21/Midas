"""
Midas Multi-Symbol 2-Year Backtest  -  15min Primary Timeframe
===============================================================
Fetches 2 years of 15min data from OANDA (no daily API limit).
Runs the full Midas scoring engine on each symbol and prints results.

Symbols: XAU/USD, EUR/USD, GBP/USD, BTC/USD, ETH/USD, US30
Run:     python backtest_multi.py
"""
import sys
import os
sys.path.insert(0, os.path.dirname(__file__))

import pandas as pd
from datetime import datetime, timezone, timedelta

from config import (
    OANDA_API_KEY, OANDA_PRACTICE,
    ATR_SL_MULT, ATR_TP1_MULT, ATR_TP2_MULT,
    MIN_CONFIDENCE, MIN_RR_RATIO,
)
from data.oanda_candles import cached_historical
from backtest import score_bar, resolve_trade, print_report, _print_bounce_report
from signals.sr_bounce import detect_bounce
from analysis.sessions import get_current_session, is_market_open

# -- Config ---------------------------------------------------------------------
START_DATE      = datetime(2024, 5, 27, tzinfo=timezone.utc)   # 2 years back
WARMUP_DAYS     = 60
LOOKBACK_15M    = 300
LOOKBACK_1H     = 220
LOOKBACK_4H     = 220
LOOKBACK_DAILY  = 100
MAX_HOLD        = 288   # 288 x 15min = 3 days
COOLDOWN_BARS   = 96    # 96 x 15min = 24h cooldown — max 1 signal per direction per day
BOUNCE_COOLDOWN = 96    # same for bounce signals

ENV_FLAG = "practice" if OANDA_PRACTICE else "live"

SYMBOLS = [
    "XAU/USD",
    "EUR/USD",
    "GBP/USD",
    "BTC/USD",
    "ETH/USD",
    "US30",
]


# -- Per-symbol backtest --------------------------------------------------------

def run_symbol(symbol: str) -> tuple[list[dict], list[dict]]:
    """Fetch OANDA data and run full backtest for one symbol."""
    start_fetch = START_DATE - timedelta(days=WARMUP_DAYS)
    end_dt      = datetime.now(timezone.utc)

    df_15m_full = cached_historical(
        "15min", start_fetch, end_dt, symbol, OANDA_API_KEY, ENV_FLAG
    )
    print(f"  {symbol} M15: {len(df_15m_full):,} candles  "
          f"({df_15m_full.index[0].date()} -> {df_15m_full.index[-1].date()})")

    agg = {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    df_1h_full    = df_15m_full.resample("1h", closed="left", label="left").agg(agg).dropna()
    df_4h_full    = df_15m_full.resample("4h", closed="left", label="left").agg(agg).dropna()
    df_daily_full = df_15m_full.resample("1D", closed="left", label="left").agg(agg).dropna()

    walk_idx   = df_15m_full.index.searchsorted(pd.Timestamp(START_DATE))
    walk_start = max(walk_idx, LOOKBACK_15M)
    total_walk = len(df_15m_full) - walk_start

    print(f"  Walking {total_walk:,} bars from {START_DATE.date()}...")

    signals:        list[dict] = []
    bounce_signals: list[dict] = []
    last_sig:    dict[str, int] = {}
    last_bounce: dict[str, int] = {}
    processed = 0
    skipped   = 0

    for i in range(walk_start, len(df_15m_full)):
        ts = df_15m_full.index[i].to_pydatetime()

        if not is_market_open(ts, symbol):
            skipped += 1
            continue

        df_15m_win   = df_15m_full.iloc[i - LOOKBACK_15M + 1: i + 1]
        df_1h_win    = df_1h_full[df_1h_full.index <= df_15m_full.index[i]].tail(LOOKBACK_1H)
        df_4h_win    = df_4h_full[df_4h_full.index <= df_15m_full.index[i]].tail(LOOKBACK_4H)
        df_daily_win = df_daily_full[df_daily_full.index <= df_15m_full.index[i]].tail(LOOKBACK_DAILY)

        if len(df_4h_win) < 30 or len(df_daily_win) < 50:
            continue

        result = score_bar(df_15m_win, df_1h_win, df_4h_win, df_daily_win, ts)

        if result is not None:
            direction = result[0]
            if (i - last_sig.get(direction, -9999)) < COOLDOWN_BARS:
                result = None

        if result is not None:
            direction, confidence, atr, close = result
            last_sig[direction] = i

            sl_d  = atr * ATR_SL_MULT
            tp1_d = atr * ATR_TP1_MULT
            tp2_d = atr * ATR_TP2_MULT
            sl    = round(close - sl_d  if direction == "BUY" else close + sl_d,  5)
            tp1   = round(close + tp1_d if direction == "BUY" else close - tp1_d, 5)
            rr1   = round(tp1_d / sl_d, 2)

            if rr1 >= MIN_RR_RATIO:
                df_future          = df_15m_full.iloc[i + 1: i + 1 + MAX_HOLD]
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
                    "atr":        round(atr, 5),
                    "outcome":    outcome,
                    "bars_held":  bars_held,
                    "session":    get_current_session(ts).name,
                })

        # S/R bounce check
        try:
            bounce = detect_bounce(df_1h_win, df_daily_win, ts)
            if bounce is not None:
                if (i - last_bounce.get(bounce.direction, -9999)) >= BOUNCE_COOLDOWN:
                    last_bounce[bounce.direction] = i
                    df_future_b            = df_15m_full.iloc[i + 1: i + 1 + MAX_HOLD]
                    outcome_b, bars_b      = resolve_trade(
                        df_future_b, bounce.direction, bounce.sl, bounce.tp1
                    )
                    bounce_signals.append({
                        "ts":         ts,
                        "direction":  bounce.direction,
                        "level":      bounce.level,
                        "level_type": bounce.level_type,
                        "rr1":        bounce.rr1,
                        "outcome":    outcome_b,
                        "bars_held":  bars_b,
                        "session":    get_current_session(ts).name,
                    })
        except Exception:
            pass

        processed += 1
        if processed % 5000 == 0:
            pct = round(processed / total_walk * 100)
            print(f"    [{pct:3d}%]  trend: {len(signals)}  bounce: {len(bounce_signals)}")

    print(f"  Done: {processed:,} bars scanned  ({skipped:,} outside hours)  "
          f"-> {len(signals)} trend + {len(bounce_signals)} bounce signals\n")
    return signals, bounce_signals


# -- Summary table --------------------------------------------------------------

def print_summary(results: list[tuple[str, list[dict], list[dict]]]) -> None:
    w = 72
    print(f"\n{'='*w}")
    print(f"  MIDAS 2-YEAR MULTI-SYMBOL SUMMARY  |  {START_DATE.date()} to today")
    print(f"  Config: MIN_CONF={MIN_CONFIDENCE}  SL={ATR_SL_MULT}xATR  "
          f"TP1={ATR_TP1_MULT}xATR  RR1=1:{ATR_TP1_MULT/ATR_SL_MULT:.2f}")
    print(f"{'='*w}")
    print(f"  {'Symbol':<12}  {'Sigs':>5}  {'WR%':>7}  {'PF':>6}  {'Sig/Mo':>7}  {'Expired':>8}")
    print(f"  {'-'*64}")

    for symbol, signals, _ in results:
        if not signals:
            print(f"  {symbol:<12}  {'-':>5}  {'-':>7}  {'-':>6}  {'-':>7}  {'-':>8}")
            continue

        df       = pd.DataFrame(signals)
        closed   = df[df["outcome"] != "expired"]
        wins     = closed[closed["outcome"] == "win"]
        losses   = closed[closed["outcome"] == "loss"]
        n_total  = len(df)
        n_closed = len(closed)
        n_wins   = len(wins)
        n_losses = len(losses)
        n_exp    = n_total - n_closed
        wr       = round(n_wins / n_closed * 100, 1) if n_closed > 0 else 0.0
        avg_rr   = float(df["rr1"].mean())
        pf       = round(n_wins * avg_rr / n_losses, 2) if n_losses > 0 else float("inf")
        months   = max((df["ts"].max() - df["ts"].min()).days / 30, 1)
        sig_mo   = round(n_total / months, 1)

        print(f"  {symbol:<12}  {n_total:>5}  {wr:>6.1f}%  {pf:>6.2f}x  {sig_mo:>7.1f}  {n_exp:>8}")

    print(f"{'='*w}\n")


# -- Entry point ----------------------------------------------------------------

def main() -> None:
    if not OANDA_API_KEY:
        print("ERROR: OANDA_API_KEY not set. Add it to .env and retry.")
        return

    print(f"\n{'='*72}")
    print(f"  MIDAS MULTI-SYMBOL 2-YEAR BACKTEST")
    print(f"  Period : {START_DATE.date()} to today  (+ {WARMUP_DAYS}d warmup)")
    print(f"  Engine : 15min primary  |  4H(x3) + 1H(x2) + 15m(x1)")
    print(f"  Config : MIN_CONF={MIN_CONFIDENCE}  SL={ATR_SL_MULT}xATR  "
          f"TP1={ATR_TP1_MULT}xATR  RR>={MIN_RR_RATIO}")
    print(f"  Symbols: {', '.join(SYMBOLS)}")
    print(f"{'='*72}\n")

    results: list[tuple[str, list[dict], list[dict]]] = []

    for symbol in SYMBOLS:
        print(f"\n{'-'*72}")
        print(f"  >> {symbol}")
        print(f"{'-'*72}")
        try:
            signals, bounce = run_symbol(symbol)
            results.append((symbol, signals, bounce))
            print_report(signals,       f"{symbol} | 2-Year")
            _print_bounce_report(bounce, f"{symbol} | 2-Year")
        except Exception as e:
            print(f"  ERROR for {symbol}: {e}\n")
            results.append((symbol, [], []))

    print_summary(results)


if __name__ == "__main__":
    main()

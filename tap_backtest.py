"""
tap_backtest.py — Backtest the actual Tap 'n' Barrel 6-step SMC strategy.

Uses the same engine.py functions the live bot runs:
  1. detect_4h_bos       → bias (BUY/SELL)
  2. get_asia_extreme     → Asia session extreme
  3. find_sweep           → London/NY sweep of Asia level
  4. scan_fakeout_zone    → price bounces back into fakeout zone
  5. find_5min_bos        → 5min fractal BoS in bias direction
  6. build_signal         → entry at 0.618 fib, SL at 1.025, TP at 3RR

Run:  python tap_backtest.py
      python tap_backtest.py XAU/USD        # single symbol
      python tap_backtest.py all            # all 6 symbols combined
"""
import sys, os
sys.path.insert(0, os.path.dirname(__file__))

import pandas as pd
from datetime import datetime, timezone, timedelta, date
from pathlib import Path

from signals.engine import (
    detect_4h_bos, get_asia_extreme, find_sweep,
    scan_fakeout_zone, find_5min_bos, build_signal,
)
from analysis.sessions import get_current_session, is_market_open

CACHE_DIR  = Path(__file__).parent / "data" / "cache"
START_DATE = datetime(2024, 5, 1, tzinfo=timezone.utc)
END_DATE   = datetime.now(timezone.utc)

SYMBOL_SLUGS = {
    "XAU/USD": "XAUUSD",
    "BTC/USD": "BTCUSD",
    "ETH/USD": "ETHUSD",
    "EUR/USD": "EURUSD",
    "GBP/USD": "GBPUSD",
    "US30":    "US30",
}

# Default portfolio — EUR/GBP excluded (low WR + high consec losses)
DEFAULT_SYMBOLS = ["XAU/USD", "BTC/USD", "ETH/USD", "US30"]
ALL_SYMBOLS     = list(SYMBOL_SLUGS.keys())


def _load(slug: str, interval: str) -> pd.DataFrame:
    path = CACHE_DIR / f"{slug}_{interval}.parquet"
    if not path.exists():
        raise FileNotFoundError(f"No cache: {path}  — run fetch_all_data.py first")
    df = pd.read_parquet(path)
    df.index = pd.to_datetime(df.index, utc=True)
    return df.sort_index()


def resolve_trade(
    df_future: pd.DataFrame,
    direction: str,
    sl: float,
    tp: float,
    max_bars: int = 576,   # 576 x 5min = 2 days
) -> tuple[str, int]:
    """Walk future 5min bars until SL or TP hit."""
    for bars, (_, bar) in enumerate(df_future.head(max_bars).iterrows(), 1):
        sl_hit = bar["low"] <= sl   if direction == "BUY" else bar["high"] >= sl
        tp_hit = bar["high"] >= tp  if direction == "BUY" else bar["low"]  <= tp
        if sl_hit and tp_hit:
            return "loss", bars   # conservative: simultaneous = loss
        if sl_hit:
            return "loss", bars
        if tp_hit:
            return "win", bars
    return "expired", max_bars


def run_tap_backtest(symbol: str) -> list[dict]:
    slug = SYMBOL_SLUGS.get(symbol)
    if slug is None:
        print(f"  Unknown symbol: {symbol}")
        return []

    print(f"\n  Loading {symbol}...")
    try:
        df_5m = _load(slug, "5min")
        df_4h = _load(slug, "15min")   # resample to 4H
    except FileNotFoundError as e:
        print(f"  {e}")
        return []

    agg = {"open":"first","high":"max","low":"min","close":"last","volume":"sum"}
    df_4h = df_5m.resample("4h", closed="left", label="left").agg(agg).dropna()

    # All unique trading dates in range
    all_dates = sorted(set(
        df_5m[df_5m.index >= pd.Timestamp(START_DATE)].index.date
    ))

    signals: list[dict] = []
    skipped = {"no_bias":0, "no_asia":0, "no_sweep":0,
               "no_fakeout":0, "no_bos":0, "no_entry":0}

    print(f"  Walking {len(all_dates)} days for {symbol}...")

    for trade_date in all_dates:
        if not is_market_open(
            datetime(trade_date.year, trade_date.month, trade_date.day,
                     12, 0, tzinfo=timezone.utc),
            symbol,
        ):
            continue

        # ── Step 1: 4H bias ───────────────────────────────────────────────────
        df_4h_to = df_4h[df_4h.index.date < trade_date]
        if len(df_4h_to) < 25:
            skipped["no_bias"] += 1
            continue
        bias = detect_4h_bos(df_4h_to)
        if bias == 0:
            skipped["no_bias"] += 1
            continue

        # ── Step 2: Asia extreme ──────────────────────────────────────────────
        # Get all 5min data up to end of this trade date
        today_end = datetime.combine(
            trade_date + timedelta(days=1), datetime.min.time()
        ).replace(tzinfo=timezone.utc)
        df_day = df_5m[
            (df_5m.index.date >= trade_date) &
            (df_5m.index < today_end)
        ]
        if df_day.empty:
            skipped["no_asia"] += 1
            continue

        asia = get_asia_extreme(df_day, bias, trade_date)
        if asia is None:
            skipped["no_asia"] += 1
            continue

        # ── Step 3: Sweep ─────────────────────────────────────────────────────
        sweep = find_sweep(df_day, asia, bias, trade_date=trade_date)
        if sweep is None:
            skipped["no_sweep"] += 1
            continue

        sweep_time = sweep["sweep_time"]

        # ── Steps 4-5-6: Walk bars after sweep ───────────────────────────────
        post_sweep = df_day[df_day.index > pd.Timestamp(sweep_time)]
        if post_sweep.empty:
            skipped["no_fakeout"] += 1
            continue

        fakeout_reached = scan_fakeout_zone(post_sweep, sweep, bias)
        if not fakeout_reached:
            skipped["no_fakeout"] += 1
            continue

        bos = find_5min_bos(post_sweep, bias, start_time=sweep_time)
        if bos is None:
            skipped["no_bos"] += 1
            continue

        # Walk bar by bar looking for entry in Goldilocks zone
        signal_found = False
        for j in range(len(post_sweep)):
            bar_ts    = post_sweep.index[j]
            bar_close = float(post_sweep["close"].iloc[j])
            session   = get_current_session(bar_ts.to_pydatetime())

            signal = build_signal(
                bar_close, bos, bias, session, symbol,
                asia, sweep["fib_high"], sweep["fib_low"],
            )
            if signal is None:
                continue

            # Resolve outcome on future 5min bars
            future = df_5m[df_5m.index > bar_ts]
            outcome, bars_held = resolve_trade(
                future, signal.direction, signal.sl, signal.tp
            )

            signals.append({
                "ts":        bar_ts.to_pydatetime(),
                "symbol":    symbol,
                "direction": signal.direction,
                "entry":     signal.entry,
                "sl":        signal.sl,
                "tp":        signal.tp,
                "rr":        signal.rr,
                "session":   signal.session_name,
                "bias":      signal.bias,
                "outcome":   outcome,
                "bars_held": bars_held,
            })
            signal_found = True
            break   # one signal per day per symbol

        if not signal_found:
            skipped["no_entry"] += 1

    print(f"  {symbol}: {len(signals)} signals found")
    print(f"    Skipped — bias:{skipped['no_bias']}  asia:{skipped['no_asia']}  "
          f"sweep:{skipped['no_sweep']}  fakeout:{skipped['no_fakeout']}  "
          f"bos:{skipped['no_bos']}  entry:{skipped['no_entry']}")
    return signals


def print_report(signals: list[dict], label: str) -> None:
    if not signals:
        print(f"\n  No signals ({label})")
        return

    df      = pd.DataFrame(signals)
    closed  = df[df["outcome"] != "expired"]
    wins    = closed[closed["outcome"] == "win"]
    losses  = closed[closed["outcome"] == "loss"]

    n_total  = len(df)
    n_closed = len(closed)
    n_wins   = len(wins)
    n_losses = len(losses)
    win_rate = round(n_wins / n_closed * 100, 1) if n_closed > 0 else 0.0
    avg_rr   = float(df["rr"].mean())
    pf       = round(n_wins * avg_rr / n_losses, 2) if n_losses > 0 else float("inf")
    months   = max((df["ts"].max() - df["ts"].min()).days / 30, 1)

    w = 58
    print(f"\n{'='*w}")
    print(f"  TAP 'N' BARREL BACKTEST — {label}")
    print(f"{'='*w}")
    print(f"  Period:          {df['ts'].min().date()} to {df['ts'].max().date()}")
    print(f"  Total signals:   {n_total}  ({round(n_total/months,1)}/month)")
    print(f"  Closed:          {n_closed}  (win {n_wins} / loss {n_losses})")
    print(f"  Win rate:        {win_rate}%")
    print(f"  Profit factor:   {pf}x  (R:R 1:{avg_rr:.1f})")
    print(f"  Max consec loss: {_max_streak(closed)}")
    print(f"  Avg hold (win):  {round(wins['bars_held'].mean()*5/60,1) if not wins.empty else 0}h")

    print(f"\n  By Direction:")
    for d in ["BUY", "SELL"]:
        sub = closed[closed["direction"] == d]
        w_ = int((sub["outcome"]=="win").sum())
        l_ = int((sub["outcome"]=="loss").sum())
        wr = round(w_/(w_+l_)*100,1) if (w_+l_)>0 else 0
        print(f"    {d}:  {w_}W / {l_}L  ({wr}%)")

    print(f"\n  By Session:")
    for sess in sorted(closed["session"].unique()):
        sub = closed[closed["session"]==sess]
        w_ = int((sub["outcome"]=="win").sum())
        l_ = int((sub["outcome"]=="loss").sum())
        wr = round(w_/(w_+l_)*100,1) if (w_+l_)>0 else 0
        print(f"    {sess:<24s}  {w_}W / {l_}L  ({wr}%)")

    if "symbol" in df.columns:
        print(f"\n  By Symbol:")
        for sym in sorted(closed["symbol"].unique()):
            sub = closed[closed["symbol"]==sym]
            w_ = int((sub["outcome"]=="win").sum())
            l_ = int((sub["outcome"]=="loss").sum())
            wr = round(w_/(w_+l_)*100,1) if (w_+l_)>0 else 0
            pf_ = round(w_*avg_rr/l_,2) if l_>0 else float("inf")
            print(f"    {sym:10s}  {w_:3d}W/{l_:3d}L  {wr:5.1f}%  PF {pf_}")

    print(f"{'='*w}\n")


def _max_streak(closed: pd.DataFrame) -> int:
    streak = mx = 0
    for o in closed.sort_values("ts")["outcome"]:
        streak = streak+1 if o=="loss" else 0
        mx = max(mx, streak)
    return mx


def run_all(symbols: list[str] = None) -> None:
    if symbols is None:
        symbols = DEFAULT_SYMBOLS
    all_signals: list[dict] = []
    for sym in symbols:
        sigs = run_tap_backtest(sym)
        all_signals.extend(sigs)

    if not all_signals:
        print("No signals across all symbols")
        return

    label = "XAU + BTC + ETH + US30" if symbols == DEFAULT_SYMBOLS else "ALL SYMBOLS"
    print_report(all_signals, label)

    # Individual symbol reports
    df = pd.DataFrame(all_signals)
    for sym in SYMBOL_SLUGS:
        subset = df[df["symbol"]==sym].to_dict("records")
        if subset:
            print_report(subset, sym)


if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) > 1 else "default"
    if arg == "default":
        run_all(DEFAULT_SYMBOLS)
    elif arg == "all":
        run_all(ALL_SYMBOLS)
    elif arg in SYMBOL_SLUGS:
        sigs = run_tap_backtest(arg)
        print_report(sigs, arg)
    else:
        print(f"Usage: python tap_backtest.py [default|all|XAU/USD|BTC/USD|ETH/USD|EUR/USD|GBP/USD|US30]")

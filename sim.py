"""
Midas — Tap 'n' Barrel 6-Step Strategy Simulator & Backtester
==============================================================
Part 1: Live today simulation (shows current state step by step)
Part 2: Historical backtest (BTC Mon-Sat, XAU weekdays)

Usage:
  python sim.py today           # today's BTC simulation
  python sim.py backtest btc    # 1-year BTC 24/7 backtest
  python sim.py backtest gold   # 1-year Gold weekday backtest
  python sim.py backtest eurusd # 1-year EUR/USD weekday backtest
  python sim.py backtest gbpusd # 1-year GBP/USD weekday backtest
  python sim.py backtest eth    # 1-year ETH/USD 24/7 backtest
  python sim.py backtest all    # BTC + Gold + ETH + EUR/USD + GBP/USD
"""
import sys
import os
import time as _time
sys.path.insert(0, os.path.dirname(__file__))

import requests
import pandas as pd
from datetime import datetime, timezone, timedelta, date
from typing import Optional

from config import TWELVEDATA_API_KEY
from signals.engine import (
    detect_4h_bos, get_asia_extreme, find_sweep,
    in_fakeout_zone, find_5min_bos, build_signal, fib_price,
)
from analysis.sessions import get_current_session, is_btc_session, is_gold_session

BASE_URL = "https://api.twelvedata.com/time_series"

# ── Rate limiter ──────────────────────────────────────────────────────────────
_call_times: list = []

def _get(symbol: str, interval: str, outputsize: int = 5000,
         end_date: Optional[str] = None) -> pd.DataFrame:
    global _call_times
    while True:
        now = _time.time()
        _call_times = [t for t in _call_times if now - t < 65]
        if len(_call_times) < 7:
            break
        wait = 65 - (now - _call_times[0])
        print(f"  [rate limit] waiting {wait:.0f}s ...", flush=True)
        _time.sleep(wait + 1)
    _call_times.append(_time.time())

    params = {
        "symbol": symbol, "interval": interval,
        "outputsize": outputsize, "apikey": TWELVEDATA_API_KEY,
        "format": "JSON", "timezone": "UTC",
    }
    if end_date:
        params["end_date"] = end_date

    while True:
        r = requests.get(BASE_URL, params=params, timeout=30)
        r.raise_for_status()
        data = r.json()
        if data.get("status") == "error":
            msg = data.get("message", "")
            if "run out" in msg or "429" in msg:
                print(f"  [credits] sleeping 65s ...", flush=True)
                _time.sleep(65)
                _call_times.clear()
                continue
            raise ValueError(f"TwelveData: {msg}")
        break

    values = data.get("values", [])
    if not values:
        return pd.DataFrame()

    df = pd.DataFrame(values)
    df["datetime"] = pd.to_datetime(df["datetime"]).dt.tz_localize("UTC")
    for col in ["open", "high", "low", "close"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    df["volume"] = 0.0
    df = df.set_index("datetime").sort_index()[["open", "high", "low", "close", "volume"]]
    return df.dropna(subset=["close"])


def fetch_full(symbol: str, interval: str, start: datetime) -> pd.DataFrame:
    end = datetime.now(timezone.utc)
    chunks = []
    cur_end = end
    print(f"  Fetching {symbol} {interval} from {start.date()} ...", end="", flush=True)
    while True:
        chunk = _get(symbol, interval, outputsize=5000,
                     end_date=cur_end.strftime("%Y-%m-%d %H:%M:%S"))
        if chunk.empty:
            break
        chunks.append(chunk)
        print(".", end="", flush=True)
        earliest = chunk.index.min().to_pydatetime().replace(tzinfo=timezone.utc)
        if earliest <= start:
            break
        if len(chunk) < 100:
            break
        cur_end = earliest - timedelta(minutes=1)
    print()
    if not chunks:
        raise ValueError(f"No data for {symbol} {interval}")
    df = pd.concat(chunks)
    df = df[~df.index.duplicated(keep="first")].sort_index()
    return df[df.index >= pd.Timestamp(start)]


# ── Per-day strategy replay ───────────────────────────────────────────────────

def _resolve(df_future: pd.DataFrame, direction: str,
             entry: float, sl: float, tp: float) -> tuple[str, int, float]:
    """
    Walk bars until SL or TP hit.
    Partial-close mechanic: when price reaches 1R (midpoint of 2RR TP),
    close half at +1R and move SL to breakeven.
      - Full TP hit after partial: net +1.5R
      - BE hit after partial:      net +0.5R
      - Full TP without partial:   net +2.0R
      - SL hit before partial:     net -1.0R
      - Timeout without partial:   net  0.0R
    """
    rr = round(abs(tp - entry) / abs(entry - sl), 2) if abs(entry - sl) > 0 else 2.0
    sl_dist = abs(entry - sl)
    partial_level = (entry + sl_dist) if direction == "BUY" else (entry - sl_dist)
    partial_hit = False
    be_sl = entry  # breakeven SL after partial

    for bars, (_, bar) in enumerate(df_future.iterrows(), 1):
        active_sl = be_sl if partial_hit else sl

        hit_sl = bar["low"] <= active_sl if direction == "BUY" else bar["high"] >= active_sl
        hit_tp = bar["high"] >= tp       if direction == "BUY" else bar["low"]  <= tp

        if not partial_hit:
            hit_partial = bar["high"] >= partial_level if direction == "BUY" else bar["low"] <= partial_level

        if hit_sl and hit_tp:
            # Same bar: conservative — if partial already hit, tp takes priority
            if partial_hit:
                return "win", bars, +1.5
            return "loss", bars, -1.0
        if hit_tp:
            return "win", bars, +1.5 if partial_hit else +rr
        if hit_sl:
            return "loss" if not partial_hit else "win", bars, -1.0 if not partial_hit else +0.5
        if not partial_hit and hit_partial:
            partial_hit = True

    if partial_hit:
        return "win", len(df_future), +0.5   # partial close locked in, BE never hit
    return "timeout", len(df_future), 0.0


def _resample_tf(df_5m: pd.DataFrame, rule: str) -> pd.DataFrame:
    """Resample 5min bars to any OHLCV timeframe — avoids extra API calls."""
    df = df_5m.copy()
    df.index = pd.DatetimeIndex(df.index).tz_localize(None)
    agg = {"open": "first", "high": "max", "low": "min", "close": "last"}
    if "volume" in df.columns:
        agg["volume"] = "sum"
    return df.resample(rule).agg(agg).dropna(subset=["open", "close"])


# ── Today simulation ──────────────────────────────────────────────────────────

def run_today_btc() -> None:
    now   = datetime.now(timezone.utc)
    today = now.date()

    print(f"\n{'='*62}")
    print(f"  BTC TODAY SIMULATION  —  {today}  {now.strftime('%H:%M UTC')}")
    print(f"{'='*62}\n")

    # BTC is 24/7 — no session restriction

    # Fetch live data
    print("  Fetching live BTC/USD data ...")
    try:
        df_4h = _get("BTC/USD", "4h", outputsize=100)
        df_5m = _get("BTC/USD", "5min", outputsize=500)
    except Exception as e:
        print(f"  ERROR: {e}")
        return

    print(f"  4H: {len(df_4h)} bars  ({df_4h.index[0].date()} to {df_4h.index[-1].date()})")
    print(f"  5min: {len(df_5m)} bars  ({df_5m.index[0].date()} to {df_5m.index[-1].date()})")

    current_price = float(df_5m["close"].iloc[-1])
    print(f"  Current BTC price: ${current_price:,.2f}\n")

    # Step-by-step walkthrough
    print(f"  --- STEP 1: 4H Fractal BoS ---")
    bias = detect_4h_bos(df_4h)
    if bias == 0:
        print("  No clear 4H fractal BoS. Bias not set. Strategy paused.\n")
        return
    print(f"  Bias: {'BULLISH' if bias==1 else 'BEARISH'}  ({'BUY' if bias==1 else 'SELL'})\n")

    print(f"  --- STEP 2: Asia Session Extreme (00:00-08:00 UTC) ---")
    asia = get_asia_extreme(df_5m, bias, today)
    if asia is None:
        print("  Asia data not available (before 08:00 UTC or no data for today).\n")
        # Try yesterday
        yesterday = today - timedelta(days=1)
        asia = get_asia_extreme(df_5m, bias, yesterday)
        if asia:
            print(f"  Using yesterday's Asia level: ${asia:,.2f}")
            today = yesterday
        else:
            return
    else:
        print(f"  Asia {'Low' if bias==1 else 'High'}: ${asia:,.2f}\n")

    print(f"  --- STEP 3: Sweep Detection ---")
    sweep = find_sweep(df_5m, asia, bias, trade_date=today)
    if sweep is None:
        print(f"  No sweep yet. Watching for price to cross ${asia:,.2f}.\n")
        print(f"  Current price ${current_price:,.2f} vs Asia level ${asia:,.2f}")
        dist = current_price - asia if bias == 1 else asia - current_price
        print(f"  Distance from Asia level: ${abs(dist):,.2f}\n")
        print("  Strategy status: WAITING FOR SWEEP")
        return
    print(f"  Sweep detected @ ${sweep['sweep_price']:,.2f}")
    print(f"  Fib range: ${sweep['fib_low']:,.2f} -- ${sweep['fib_high']:,.2f}\n")

    fib_079 = fib_price(sweep["fib_high"], sweep["fib_low"], 0.079)
    print(f"  --- STEP 4: Fakeout Zone (0.079 fib = ${fib_079:,.2f}) ---")
    fakeout = in_fakeout_zone(current_price, sweep["fib_high"], sweep["fib_low"], bias)
    if not fakeout:
        print(f"  Price ${current_price:,.2f} not yet in fakeout zone (need {'above' if bias==1 else 'below'} ${fib_079:,.2f}).\n")
        print("  Strategy status: WAITING FOR FAKEOUT BOUNCE")
        return
    print(f"  Price ${current_price:,.2f} IS in fakeout zone.\n")

    print(f"  --- STEP 5: 5min Fractal BoS ---")
    bos = find_5min_bos(df_5m, bias, sweep["idx"])
    if bos is None:
        print(f"  No 5min BoS yet. Waiting for fractal {'break up' if bias==1 else 'break down'}.\n")
        print("  Strategy status: WAITING FOR 5MIN BOS")
        return
    print(f"  5min BoS confirmed!")
    print(f"  Swing: ${bos['swing_low']:,.2f} -- ${bos['swing_high']:,.2f}\n")

    print(f"  --- STEP 6: Goldilocks Entry Zone ---")
    session = get_current_session(now)
    signal = build_signal(
        current_price, bos, bias, session, "BTC/USD",
        asia, sweep["fib_high"], sweep["fib_low"],
    )
    if signal is None:
        entry_618 = fib_price(bos["swing_high"], bos["swing_low"], 0.618)
        entry_500 = fib_price(bos["swing_high"], bos["swing_low"], 0.500)
        print(f"  Waiting for price to enter Goldilocks zone:")
        print(f"  Entry zone: ${min(entry_618,entry_500):,.2f} -- ${max(entry_618,entry_500):,.2f}")
        print(f"  Current:    ${current_price:,.2f}")
        print("  Strategy status: WAITING FOR ENTRY ZONE")
        return

    print(f"  SIGNAL READY!")
    print(f"  Direction: {signal.direction}")
    print(f"  Entry:     ${signal.entry:,.2f}")
    print(f"  SL:        ${signal.sl:,.2f}  (-${signal.sl_pips:,.2f})")
    print(f"  TP:        ${signal.tp:,.2f}  (+${signal.tp_pips:,.2f})  3RR")
    print(f"  Session:   {signal.session_emoji} {signal.session_name}")
    print("\n  Strategy status: SIGNAL ACTIVE - WOULD ENTER NOW")


# ── Backtest ──────────────────────────────────────────────────────────────────

def _print_report(trades: list[dict], label: str) -> None:
    if not trades:
        print(f"\n  No trades generated for {label}.")
        return

    df = pd.DataFrame(trades)
    closed   = df[df["outcome"] != "timeout"]
    wins     = closed[closed["outcome"] == "win"]
    losses   = closed[closed["outcome"] == "loss"]

    n_total   = len(df)
    n_closed  = len(closed)
    n_wins    = len(wins)
    n_losses  = len(losses)
    n_timeout = len(df[df["outcome"] == "timeout"])
    win_rate  = round(n_wins / n_closed * 100, 1) if n_closed > 0 else 0.0
    total_r   = df["pnl_r"].sum()
    avg_hold_w = round(wins["bars_held"].mean() * 5 / 60, 1) if not wins.empty else 0
    avg_hold_l = round(losses["bars_held"].mean() * 5 / 60, 1) if not losses.empty else 0
    gross_profit = df[df["pnl_r"] > 0]["pnl_r"].sum()
    gross_loss   = abs(df[df["pnl_r"] < 0]["pnl_r"].sum())
    pf = round(gross_profit / gross_loss, 2) if gross_loss > 0 else float("inf")

    # Max drawdown in R
    running_r = 0.0
    peak_r    = 0.0
    max_dd    = 0.0
    for r in df["pnl_r"]:
        running_r += r
        peak_r = max(peak_r, running_r)
        max_dd = max(max_dd, peak_r - running_r)

    # Consecutive loss streak
    streak = max_streak = 0
    for o in closed["outcome"]:
        streak = streak + 1 if o == "loss" else 0
        max_streak = max(max_streak, streak)

    months = max((pd.to_datetime(df["date"]).max() - pd.to_datetime(df["date"]).min()).days / 30, 1)

    w = 62
    print(f"\n{'='*w}")
    print(f"  MIDAS TAP N BARREL BACKTEST — {label}")
    print(f"{'='*w}")
    print(f"  Period:             {df['date'].min()} to {df['date'].max()}")
    print(f"  Trading days seen:  {n_total}  (days with a full signal)")
    print(f"  Closed trades:      {n_closed}  (W:{n_wins}  L:{n_losses})")
    print(f"  Timeout (<48h):     {n_timeout}")
    print(f"  Win rate:           {win_rate}%")
    print(f"  Profit factor:      {pf}x")
    print(f"  Total R:            {total_r:+.1f}R")
    print(f"  Max drawdown:       -{max_dd:.1f}R")
    print(f"  Max consec losses:  {max_streak}")
    print(f"  Avg hold (win):     {avg_hold_w}h")
    print(f"  Avg hold (loss):    {avg_hold_l}h")
    print(f"  Trades/month:       {round(n_total/months, 1)}")

    # Monthly breakdown
    print(f"\n  {'-'*w}")
    print(f"  MONTHLY")
    print(f"  {'-'*w}")
    df["month"] = pd.to_datetime(df["date"]).dt.to_period("M")
    running = 0.0
    for period, grp in df.groupby("month"):
        w_  = int((grp["outcome"] == "win").sum())
        l_  = int((grp["outcome"] == "loss").sum())
        t_  = int((grp["outcome"] == "timeout").sum())
        r_  = float(grp["pnl_r"].sum())
        running += r_
        wr_ = round(w_ / (w_ + l_) * 100, 0) if (w_ + l_) > 0 else 0
        bar = f"{r_:+.1f}R"
        print(f"  {period}  {w_:2d}W {l_:2d}L {t_:2d}T  {wr_:5.0f}%  {bar:>8s}  (running {running:+.1f}R)")

    # By direction
    print(f"\n  {'-'*w}")
    print(f"  BY DIRECTION")
    print(f"  {'-'*w}")
    for d in ["BUY", "SELL"]:
        sub = closed[closed["direction"] == d]
        w_ = int((sub["outcome"] == "win").sum())
        l_ = int((sub["outcome"] == "loss").sum())
        r_ = float(df[df["direction"] == d]["pnl_r"].sum())
        wr_ = round(w_ / (w_ + l_) * 100, 1) if (w_ + l_) > 0 else 0
        print(f"  {d}:  {w_:3d}W / {l_:3d}L  ({wr_:5.1f}%)  {r_:+.1f}R")

    print(f"{'='*w}\n")


def run_backtest(symbol: str, label: str, is_btc: bool = False) -> None:
    start = datetime.now(timezone.utc) - timedelta(days=365)

    print(f"\n{'='*62}")
    print(f"  BACKTEST: {label}  —  1 YEAR")
    print(f"  {start.date()} to {datetime.now(timezone.utc).date()}")
    print(f"{'='*62}\n")

    # Fetch all data
    df_4h_full = fetch_full(symbol, "4h", start - timedelta(days=30))
    df_5m_full = fetch_full(symbol, "5min", start - timedelta(days=2))

    print(f"\n  4H bars: {len(df_4h_full):,}  |  5min bars: {len(df_5m_full):,}")
    print(f"  Walking day by day ...\n")

    trades: list[dict] = []
    cur = start.date()
    end = datetime.now(timezone.utc).date()

    day_count = 0
    no_bias   = 0
    no_asia   = 0
    no_sweep  = 0
    no_fakeout = 0
    no_bos    = 0
    no_entry  = 0

    while cur <= end:
        # Filter by trading schedule
        wd = cur.weekday()
        if not is_btc and wd >= 5:    # Gold: weekdays only
            cur += timedelta(days=1)
            continue

        day_count += 1

        # 4H slice: all bars up to end of that day
        day_end_ts = pd.Timestamp(datetime(cur.year, cur.month, cur.day, 23, 59, tzinfo=timezone.utc))
        df_4h_slice = df_4h_full[df_4h_full.index <= day_end_ts]
        if len(df_4h_slice) < 10:
            cur += timedelta(days=1)
            continue

        # 5min slice: 2 days window (yesterday + today) so sweep detection has enough bars
        day_start_ts = pd.Timestamp(datetime(cur.year, cur.month, cur.day, 0, 0, tzinfo=timezone.utc))
        prev_start   = day_start_ts - timedelta(hours=16)
        df_5m_slice  = df_5m_full[(df_5m_full.index >= prev_start) &
                                   (df_5m_full.index <= day_end_ts)]
        if len(df_5m_slice) < 20:
            cur += timedelta(days=1)
            continue

        # Step 1 — 4H bias
        bias = detect_4h_bos(df_4h_slice)
        if bias == 0:
            no_bias += 1
            cur += timedelta(days=1)
            continue

        # Step 2
        asia = get_asia_extreme(df_5m_slice, bias, cur)
        if asia is None:
            no_asia += 1
            cur += timedelta(days=1)
            continue

        # Step 3
        sweep = find_sweep(df_5m_slice, asia, bias, trade_date=cur)
        if sweep is None:
            no_sweep += 1
            cur += timedelta(days=1)
            continue

        # Steps 4-6 walk
        sweep_iloc = sweep["idx"]
        fakeout_hit = False
        bos_cache   = None
        trade       = None

        for i in range(sweep_iloc + 1, len(df_5m_slice)):
            cp     = float(df_5m_slice["close"].iloc[i])
            bar_dt = df_5m_slice.index[i].to_pydatetime()

            # Mirror live-bot session gate: Gold only enters during London/NY
            # (08:00–21:00 UTC weekdays). BTC/ETH trade 24/7.
            if not is_btc and not is_gold_session(bar_dt):
                continue

            if not fakeout_hit:
                if not in_fakeout_zone(cp, sweep["fib_high"], sweep["fib_low"], bias):
                    continue
                fakeout_hit = True

            if bos_cache is None:
                bos_cache = find_5min_bos(df_5m_slice, bias, sweep_iloc)
            if bos_cache is None:
                continue

            session = get_current_session(bar_dt)
            signal  = build_signal(cp, bos_cache, bias, session, symbol,
                                   asia, sweep["fib_high"], sweep["fib_low"])
            if signal is None:
                continue

            future = df_5m_slice.iloc[i + 1: i + 289]  # max 24h
            outcome, bars, pnl_r = _resolve(future, signal.direction,
                                            signal.entry, signal.sl, signal.tp)
            sl_dist = abs(signal.entry - signal.sl)
            trade = {
                "date":      cur.isoformat(),
                "symbol":    symbol,
                "bias":      "BULL" if bias == 1 else "BEAR",
                "direction": signal.direction,
                "entry":     round(signal.entry, 2),
                "sl":        round(signal.sl, 2),
                "tp":        round(signal.tp, 2),
                "sl_dist":   round(sl_dist, 2),
                "asia":      round(asia, 2),
                "sweep":     round(sweep["sweep_price"], 2),
                "session":   signal.session_name,
                "outcome":   outcome,
                "bars_held": bars,
                "pnl_r":     pnl_r,
            }
            trades.append(trade)
            outcome_sym = "W" if outcome == "win" else ("L" if outcome == "loss" else "T")
            print(f"  {cur}  {signal.direction:4s}  entry=${signal.entry:,.2f}  "
                  f"SL=${signal.sl:,.2f}  TP=${signal.tp:,.2f}  -> {outcome_sym}  "
                  f"{pnl_r:+.1f}R  ({bars*5//60}h{bars*5%60}m)")
            break

        if trade is None:
            if not fakeout_hit:
                no_fakeout += 1
            elif bos_cache is None:
                no_bos += 1
            else:
                no_entry += 1

        cur += timedelta(days=1)

    # Funnel stats
    print(f"\n  FUNNEL ({day_count} trading days)")
    print(f"  No bias:    {no_bias:3d} days  ({round(no_bias/day_count*100)}%)")
    print(f"  No Asia:    {no_asia:3d} days  ({round(no_asia/day_count*100)}%)")
    print(f"  No sweep:   {no_sweep:3d} days  ({round(no_sweep/day_count*100)}%)")
    print(f"  No fakeout: {no_fakeout:3d} days  ({round(no_fakeout/day_count*100)}%)")
    print(f"  No BoS:     {no_bos:3d} days  ({round(no_bos/day_count*100)}%)")
    print(f"  No entry:   {no_entry:3d} days  ({round(no_entry/day_count*100)}%)")
    print(f"  Trades:     {len(trades):3d} days  ({round(len(trades)/day_count*100)}%)")

    _print_report(trades, label)


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "today"

    if mode == "today":
        run_today_btc()

    elif mode == "backtest":
        target = sys.argv[2] if len(sys.argv) > 2 else "all"
        if target in ("btc", "all"):
            run_backtest("BTC/USD", "BTC/USD  24/7", is_btc=True)
        if target in ("gold", "all"):
            run_backtest("XAU/USD", "XAU/USD  Weekdays", is_btc=False)
        if target in ("eth", "all"):
            run_backtest("ETH/USD", "ETH/USD  24/7", is_btc=True)
        if target in ("eurusd", "all"):
            run_backtest("EUR/USD", "EUR/USD  Weekdays", is_btc=False)
        if target in ("gbpusd", "all"):
            run_backtest("GBP/USD", "GBP/USD  Weekdays", is_btc=False)

    else:
        print(__doc__)

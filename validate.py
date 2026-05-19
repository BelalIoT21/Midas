"""
validate.py — Compare specific strategy configs head-to-head.
Collects candidates once, then tests all configs in memory instantly.
Reports PF, WR, max consec losses, monthly breakdown per config.
"""
import sys
import os
import time
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
from analysis.sessions import get_current_session, session_multiplier, should_trade, is_market_open

BASE_URL      = "https://api.twelvedata.com/time_series"
START_DATE    = datetime(2025, 1, 20, tzinfo=timezone.utc)
WARMUP_DAYS   = 45
LOOKBACK_15M  = 300
LOOKBACK_1H   = 220
LOOKBACK_4H   = 220
LOOKBACK_DAILY = 100
MAX_HOLD      = 288  # 3 days at 15min

# Configs to test: (label, sl_mult, tp_mult, direction, conf, consensus, trend_mode)
# trend_mode: "TREND" = only trade with daily trend direction
#             "BOTH"  = both directions, block counter-trend
#             "BUY"   = BUY only
CONFIGS = [
    ("TREND TP5.0 SL1.2",  1.2, 5.0, "TREND", 0.60, 7),
    ("TREND TP4.0 SL1.2",  1.2, 4.0, "TREND", 0.60, 7),
    ("TREND TP3.5 SL1.2",  1.2, 3.5, "TREND", 0.60, 7),
    ("TREND TP3.0 SL1.2",  1.2, 3.0, "TREND", 0.60, 7),
    ("TREND TP2.5 SL1.2",  1.2, 2.5, "TREND", 0.60, 7),
    ("TREND TP3.5 SL1.5",  1.5, 3.5, "TREND", 0.60, 7),
    ("TREND TP3.0 SL1.5",  1.5, 3.0, "TREND", 0.60, 7),
    ("TREND TP2.5 SL1.5",  1.5, 2.5, "TREND", 0.60, 7),
    ("BOTH  TP5.0 SL1.2",  1.2, 5.0, "BOTH",  0.60, 7),
    ("BOTH  TP4.0 SL1.2",  1.2, 4.0, "BOTH",  0.60, 7),
    ("BOTH  TP3.5 SL1.2",  1.2, 3.5, "BOTH",  0.60, 7),
    ("BOTH  TP3.0 SL1.2",  1.2, 3.0, "BOTH",  0.60, 7),
    ("BOTH  TP2.5 SL1.5",  1.5, 2.5, "BOTH",  0.60, 7),
    ("BUY   TP4.0 SL1.2",  1.2, 4.0, "BUY",   0.60, 7),
    ("BUY   TP3.5 SL1.2",  1.2, 3.5, "BUY",   0.60, 7),
    ("BUY   TP5.0 SL1.5",  1.5, 5.0, "BUY",   0.60, 7),
]


def _fetch_chunk(interval, end_dt):
    params = {"symbol": SYMBOL, "interval": interval, "outputsize": 5000,
              "end_date": end_dt.strftime("%Y-%m-%d %H:%M:%S"),
              "apikey": TWELVEDATA_API_KEY, "format": "JSON", "timezone": "UTC"}
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


def fetch_historical(interval, start):
    end, chunks, current_end = datetime.now(timezone.utc), [], datetime.now(timezone.utc)
    print(f"  Fetching {interval}...", end="", flush=True)
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
    print(f"  {interval}: {len(df):,} candles")
    return df


def _daily_trend(df):
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


def _rsi(df):
    d = df["close"].diff()
    g = d.clip(lower=0).ewm(com=13, adjust=False).mean()
    l = (-d.clip(upper=0)).ewm(com=13, adjust=False).mean()
    rs = g / l.replace(0, float("nan"))
    return 100 - (100 / (1 + rs))


def _adx_val(df):
    try:
        r = ta.adx(df["high"], df["low"], df["close"], length=14)
        if r is not None and not r.empty:
            v = r.iloc[:, 0].dropna()
            if not v.empty:
                return float(v.iloc[-1])
    except Exception:
        pass
    return 25.0


def collect_candidates(df_15m_full, df_1h_full, df_4h_full, df_daily_full):
    walk_idx   = df_15m_full.index.searchsorted(pd.Timestamp(START_DATE))
    walk_start = max(walk_idx, LOOKBACK_15M)
    total      = len(df_15m_full) - walk_start
    candidates = []
    processed  = 0

    print(f"\n  Collecting candidates over {total:,} bars...\n")

    for i in range(walk_start, len(df_15m_full)):
        ts = df_15m_full.index[i].to_pydatetime()
        if not is_market_open(ts):
            continue

        df_15m = df_15m_full.iloc[i - LOOKBACK_15M + 1: i + 1]
        df_1h  = df_1h_full[df_1h_full.index   <= df_15m_full.index[i]].tail(LOOKBACK_1H)
        df_4h  = df_4h_full[df_4h_full.index   <= df_15m_full.index[i]].tail(LOOKBACK_4H)
        df_d   = df_daily_full[df_daily_full.index <= df_15m_full.index[i]].tail(LOOKBACK_DAILY)

        if len(df_4h) < 30 or len(df_d) < 50:
            continue

        session = get_current_session(ts)
        if not should_trade(session):
            continue

        weighted_bull = weighted_bear = active_weight = 0.0
        breakdown = []
        for interval, weight, df in [("4h", 3, df_4h), ("1h", 2, df_1h), ("15min", 1, df_15m)]:
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

        close = float(df_15m["close"].iloc[-1])
        atr   = atr_value(df_15m)
        if atr is None:
            continue

        bull_conf = weighted_bull / active_weight
        bear_conf = weighted_bear / active_weight

        try:
            sr_data = all_levels(df_1h)
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

        if max(bull_conf, bear_conf) < 0.50:
            continue

        v4h_bull = sum(1 for _, tf, v in breakdown if tf == "4h" and v == 1)
        v4h_bear = sum(1 for _, tf, v in breakdown if tf == "4h" and v == -1)

        try:
            rsi_4h = float(_rsi(df_4h).iloc[-1])
        except Exception:
            rsi_4h = 50.0

        try:
            st_4h = supertrend_vote(df_4h)
            st_1h = supertrend_vote(df_1h)
        except Exception:
            st_4h = st_1h = 0

        daily_tr = _daily_trend(df_d)
        adx_4h   = _adx_val(df_4h)

        future = df_15m_full.iloc[i + 1: i + 1 + MAX_HOLD]
        fh = future["high"].values.astype(np.float32)
        fl = future["low"].values.astype(np.float32)

        candidates.append({
            "ts": ts, "close": close, "atr": atr,
            "bull_conf": bull_conf, "bear_conf": bear_conf,
            "v4h_bull": v4h_bull, "v4h_bear": v4h_bear,
            "adx_4h": adx_4h, "rsi_4h": rsi_4h,
            "st_4h": st_4h, "st_1h": st_1h,
            "daily_tr": daily_tr, "session": session.name,
            "fh": fh, "fl": fl,
        })

        processed += 1
        if processed % 5000 == 0:
            pct = round((i - walk_start) / total * 100)
            print(f"  [{pct:3d}%]  processed: {processed:,}  candidates: {len(candidates):,}")

    print(f"\n  Done.  {len(candidates):,} candidates collected.\n")
    return candidates


def resolve_trade(direction, close, sl, tp, fh, fl):
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


def run_config(candidates, label, sl_mult, tp_mult, direction, min_conf, consensus_min):
    COOLDOWN_BARS = 24   # 6h at 15min
    ADX_MIN       = 18
    RSI_GUARD     = 75

    signals = []
    last_bar = {"BUY": -9999, "SELL": -9999}

    for idx, c in enumerate(candidates):
        # Direction candidates for this bar
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
        else:  # BOTH — block counter-trend
            dirs = []
            if c["daily_tr"] != "BEAR":
                dirs.append("BUY")
            if c["daily_tr"] != "BULL":
                dirs.append("SELL")

        for d in dirs:
            conf = c["bull_conf"] if d == "BUY" else c["bear_conf"]
            if conf < min_conf:
                continue
            if (idx - last_bar[d]) < COOLDOWN_BARS:
                continue
            # Supertrend
            opp = -1 if d == "BUY" else 1
            if c["st_4h"] == opp or c["st_1h"] == opp:
                continue
            # ADX
            if c["adx_4h"] < ADX_MIN:
                continue
            # RSI guard
            if d == "BUY"  and c["rsi_4h"] > RSI_GUARD:
                continue
            if d == "SELL" and c["rsi_4h"] < (100 - RSI_GUARD):
                continue
            # 4H consensus
            v4h = c["v4h_bull"] if d == "BUY" else c["v4h_bear"]
            if v4h < consensus_min:
                continue

            last_bar[d] = idx

            sl_d = c["atr"] * sl_mult
            tp_d = c["atr"] * tp_mult
            sl   = c["close"] - sl_d if d == "BUY" else c["close"] + sl_d
            tp   = c["close"] + tp_d if d == "BUY" else c["close"] - tp_d

            outcome = resolve_trade(d, c["close"], sl, tp, c["fh"], c["fl"])
            signals.append({
                "ts": c["ts"], "direction": d, "outcome": outcome,
                "rr": round(tp_d / sl_d, 2), "session": c["session"],
            })

    return signals


def print_config_report(label, signals):
    if not signals:
        print(f"  {label}: no signals\n")
        return

    df = pd.DataFrame(signals)
    closed  = df[df["outcome"] != "expired"]
    wins    = closed[closed["outcome"] == "win"]
    losses  = closed[closed["outcome"] == "loss"]
    n_wins  = len(wins)
    n_loss  = len(losses)
    n_closed = n_wins + n_loss
    n_total  = len(df)
    if n_closed < 5 or n_loss == 0:
        print(f"  {label}: too few trades ({n_closed})\n")
        return

    wr   = n_wins / n_closed * 100
    avg_rr = float(df["rr"].mean())
    pf   = (n_wins * avg_rr) / n_loss
    months = max((df["ts"].max() - df["ts"].min()).days / 30, 1)

    # Max consecutive losses
    streak = max_streak = 0
    for o in closed["outcome"]:
        streak = streak + 1 if o == "loss" else 0
        max_streak = max(max_streak, streak)

    # Monthly WR
    closed_copy = closed.copy()
    closed_copy["month"] = pd.to_datetime(closed_copy["ts"]).dt.to_period("M")
    monthly = []
    for period, grp in closed_copy.groupby("month"):
        mw = int((grp["outcome"] == "win").sum())
        ml = int((grp["outcome"] == "loss").sum())
        mwr = round(mw / (mw + ml) * 100, 0) if (mw + ml) > 0 else 0
        monthly.append(f"{period}:{mwr:.0f}%")

    print(f"\n  {label}")
    print(f"    PF={pf:.2f}x  WR={wr:.1f}%  {n_wins}W/{n_loss}L  RR=1:{avg_rr:.2f}  "
          f"N={n_closed} ({n_total} total)  {round(n_total/months,1)}/mo  "
          f"MaxConsecL={max_streak}")
    print(f"    Monthly: {' | '.join(monthly)}")


def main():
    start_fetch = START_DATE - timedelta(days=WARMUP_DAYS)

    print(f"\n{'='*60}")
    print(f"  MIDAS CONFIG VALIDATOR  —  15min  —  Trump Era")
    print(f"  {START_DATE.date()} to today  |  {len(CONFIGS)} configs")
    print(f"{'='*60}\n")

    df_15m = fetch_historical("15min", start_fetch)

    agg = {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    df_1h    = df_15m.resample("1h", closed="left", label="left").agg(agg).dropna()
    df_4h    = df_15m.resample("4h", closed="left", label="left").agg(agg).dropna()
    df_daily = df_15m.resample("1D", closed="left", label="left").agg(agg).dropna()

    candidates = collect_candidates(df_15m, df_1h, df_4h, df_daily)

    print(f"{'='*60}")
    print(f"  RESULTS")
    print(f"{'='*60}")

    results = []
    for label, sl_mult, tp_mult, direction, min_conf, consensus_min in CONFIGS:
        sigs = run_config(candidates, label, sl_mult, tp_mult, direction, min_conf, consensus_min)
        print_config_report(label, sigs)

        if sigs:
            df = pd.DataFrame(sigs)
            closed = df[df["outcome"] != "expired"]
            wins   = int((closed["outcome"] == "win").sum())
            losses = int((closed["outcome"] == "loss").sum())
            if wins + losses >= 5 and losses > 0:
                avg_rr = float(df["rr"].mean())
                pf = (wins * avg_rr) / losses
                wr = wins / (wins + losses) * 100
                streak = max_streak = 0
                for o in closed["outcome"]:
                    streak = streak + 1 if o == "loss" else 0
                    max_streak = max(max_streak, streak)
                results.append({
                    "label": label, "pf": round(pf, 2), "wr": round(wr, 1),
                    "wins": wins, "losses": losses,
                    "max_streak": max_streak,
                    "n_total": len(df),
                    "sl_mult": sl_mult, "tp_mult": tp_mult,
                    "direction": direction,
                })

    print(f"\n\n{'='*60}")
    print(f"  SUMMARY (sorted by PF)")
    print(f"{'='*60}")
    results.sort(key=lambda x: x["pf"], reverse=True)
    print(f"  {'Config':<22}  {'PF':>5}  {'WR':>6}  {'W':>4}  {'L':>4}  {'MaxL':>5}  {'N':>5}")
    print(f"  {'-'*22}  {'-'*5}  {'-'*6}  {'-'*4}  {'-'*4}  {'-'*5}  {'-'*5}")
    for r in results:
        flag = " <<< BEST" if r == results[0] else ""
        print(f"  {r['label']:<22}  {r['pf']:>5.2f}  {r['wr']:>5.1f}%  "
              f"{r['wins']:>4}  {r['losses']:>4}  {r['max_streak']:>5}  {r['n_total']:>5}{flag}")

    print(f"\n  RECOMMENDATION:")
    if results:
        # Find config with best balance: PF >=1.5 AND lowest max streak AND most signals
        good = [r for r in results if r["pf"] >= 1.5 and r["max_streak"] <= 8]
        if good:
            best = max(good, key=lambda x: x["pf"] * (x["n_total"] ** 0.3))
            print(f"    {best['label']}")
            print(f"    PF={best['pf']}x, WR={best['wr']}%, MaxConsecLoss={best['max_streak']}")
            print(f"    ATR_SL_MULT={best['sl_mult']}, ATR_TP1_MULT={best['tp_mult']}")
            print(f"    Direction={best['direction']}")
        else:
            best = results[0]
            print(f"    Best available (no config met PF>=1.5 + MaxL<=8):")
            print(f"    {best['label']}  PF={best['pf']}x, WR={best['wr']}%, MaxL={best['max_streak']}")

    print(f"\n{'='*60}\n")


if __name__ == "__main__":
    main()

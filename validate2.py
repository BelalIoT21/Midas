"""
validate2.py — Target WR > 50%
================================
Tests adding momentum filters on top of TREND mode:
  - ADX rising (trend has momentum, not fading)
  - ATR expanding (market is moving, not compressing)
  - Require price making higher highs (BUY) / lower lows (SELL)
  - Weekly trend alignment
"""
import sys, os, time
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

BASE_URL       = "https://api.twelvedata.com/time_series"
START_DATE     = datetime(2025, 1, 20, tzinfo=timezone.utc)
WARMUP_DAYS    = 60
LOOKBACK_15M   = 300
LOOKBACK_1H    = 220
LOOKBACK_4H    = 220
LOOKBACK_DAILY = 100
MAX_HOLD       = 288

CONFIGS = [
    # label,                sl,  tp,   dir,    conf, cons, adx_rise, atr_exp, hh_ll, weekly
    ("BASELINE (no extras)", 1.5, 3.5, "TREND", 0.60, 7, False, False, False, False),
    ("+ ADX rising",         1.5, 3.5, "TREND", 0.60, 7, True,  False, False, False),
    ("+ ATR expanding",      1.5, 3.5, "TREND", 0.60, 7, False, True,  False, False),
    ("+ HH/LL",              1.5, 3.5, "TREND", 0.60, 7, False, False, True,  False),
    ("+ ADX+ATR",            1.5, 3.5, "TREND", 0.60, 7, True,  True,  False, False),
    ("+ ADX+HH/LL",          1.5, 3.5, "TREND", 0.60, 7, True,  False, True,  False),
    ("+ ATR+HH/LL",          1.5, 3.5, "TREND", 0.60, 7, False, True,  True,  False),
    ("+ ALL 3 extras",       1.5, 3.5, "TREND", 0.60, 7, True,  True,  True,  False),
    # Same but with TP=2.5 (higher WR)
    ("TP2.5 + ADX+ATR",      1.5, 2.5, "TREND", 0.60, 7, True,  True,  False, False),
    ("TP2.5 + ALL 3",        1.5, 2.5, "TREND", 0.60, 7, True,  True,  True,  False),
    ("TP2.0 + ADX+ATR",      1.5, 2.0, "TREND", 0.60, 7, True,  True,  False, False),
    ("TP2.0 + ALL 3",        1.5, 2.0, "TREND", 0.60, 7, True,  True,  True,  False),
    # Tight consensus
    ("TP3.0+ALL 3+cons8",    1.5, 3.0, "TREND", 0.60, 8, True,  True,  True,  False),
    ("TP2.5+ALL 3+cons8",    1.5, 2.5, "TREND", 0.60, 8, True,  True,  True,  False),
    ("TP2.0+ALL 3+cons8",    1.5, 2.0, "TREND", 0.60, 8, True,  True,  True,  False),
    # Stricter ADX: only fire when ADX rising AND >= 25
    ("TP3.0 ADX25+rise",     1.5, 3.0, "TREND", 0.60, 7, True,  False, False, False),  # adx_min_25 tested separately
    # BUY-only + all filters
    ("BUY+ADX+ATR TP3.5",    1.5, 3.5, "BUY",   0.60, 7, True,  True,  False, False),
    ("BUY+ALL 3  TP3.5",     1.5, 3.5, "BUY",   0.60, 7, True,  True,  True,  False),
    ("BUY+ALL 3  TP2.5",     1.5, 2.5, "BUY",   0.60, 7, True,  True,  True,  False),
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
    return df.set_index("datetime").sort_index()[["open","high","low","close","volume"]]


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
        time.sleep(12)
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
    return 100 - (100 / (1 + g / l.replace(0, float("nan"))))


def _compute_extras(df_4h, df_1h, df_15m):
    """Compute ADX-rising, ATR-expanding, and HH/LL momentum flags."""
    adx_rising = False
    atr_expanding = False
    hh_ll = None   # "BUY" if making higher highs, "SELL" if lower lows, None if neither

    # ── ADX rising (4H): current ADX > ADX 5 bars ago ────────────────────────
    try:
        adx_r = ta.adx(df_4h["high"], df_4h["low"], df_4h["close"], length=14)
        if adx_r is not None and not adx_r.empty:
            adx_col = adx_r.iloc[:, 0].dropna()
            if len(adx_col) >= 6:
                adx_now  = float(adx_col.iloc[-1])
                adx_prev = float(adx_col.iloc[-5])
                adx_rising = adx_now > adx_prev and adx_now >= 18
    except Exception:
        pass

    # ── ATR expanding (1H): current ATR > 20-bar average ─────────────────────
    try:
        atr_series = df_1h["close"].copy()
        # Compute ATR for 1H
        tr = pd.concat([
            df_1h["high"] - df_1h["low"],
            (df_1h["high"] - df_1h["close"].shift()).abs(),
            (df_1h["low"]  - df_1h["close"].shift()).abs(),
        ], axis=1).max(axis=1)
        atr_raw   = tr.ewm(span=14, adjust=False).mean()
        atr_sma20 = atr_raw.rolling(20).mean()
        if not atr_raw.empty and not atr_sma20.empty:
            curr_atr = float(atr_raw.dropna().iloc[-1])
            avg_atr  = float(atr_sma20.dropna().iloc[-1])
            atr_expanding = curr_atr > avg_atr * 1.05  # 5% above average
    except Exception:
        pass

    # ── Higher highs / lower lows (1H, last 8 bars) ──────────────────────────
    try:
        if len(df_1h) >= 12:
            # BUY: recent 4-bar high > prior 4-bar high (upward momentum)
            h_recent = float(df_1h["high"].iloc[-4:].max())
            h_prior  = float(df_1h["high"].iloc[-12:-4].max())
            l_recent = float(df_1h["low"].iloc[-4:].min())
            l_prior  = float(df_1h["low"].iloc[-12:-4].min())
            if h_recent > h_prior * 1.001:
                hh_ll = "BUY"
            elif l_recent < l_prior * 0.999:
                hh_ll = "SELL"
    except Exception:
        pass

    return adx_rising, atr_expanding, hh_ll


def collect_candidates(df_15m_full, df_1h_full, df_4h_full, df_daily_full):
    walk_idx   = df_15m_full.index.searchsorted(pd.Timestamp(START_DATE))
    walk_start = max(walk_idx, LOOKBACK_15M)
    total      = len(df_15m_full) - walk_start
    candidates = []
    processed  = 0

    print(f"\n  Collecting {total:,} bars...\n")

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
                bull_conf += 0.06 if near < close else 0
                bear_conf += 0.06 if near >= close else 0
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

        # Compute extra momentum filters
        adx_rising, atr_expanding, hh_ll = _compute_extras(df_4h, df_1h, df_15m)

        future = df_15m_full.iloc[i + 1: i + 1 + MAX_HOLD]
        fh = future["high"].values.astype(np.float32)
        fl = future["low"].values.astype(np.float32)

        candidates.append({
            "ts": ts, "close": close, "atr": atr,
            "bull_conf": bull_conf, "bear_conf": bear_conf,
            "v4h_bull": v4h_bull, "v4h_bear": v4h_bear,
            "rsi_4h": rsi_4h, "st_4h": st_4h, "st_1h": st_1h,
            "daily_tr": daily_tr, "session": session.name,
            "adx_rising": adx_rising,
            "atr_expanding": atr_expanding,
            "hh_ll": hh_ll,
            "fh": fh, "fl": fl,
        })

        processed += 1
        if processed % 5000 == 0:
            pct = round((i - walk_start) / total * 100)
            print(f"  [{pct:3d}%]  {processed:,} bars  {len(candidates):,} candidates")

    print(f"\n  Done.  {len(candidates):,} candidates.\n")
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


def run_config(candidates, label, sl_mult, tp_mult, direction, min_conf,
               consensus_min, require_adx_rising, require_atr_expanding,
               require_hh_ll, require_weekly):
    COOLDOWN = 24
    ADX_MIN  = 18
    RSI_GUARD = 75

    signals = []
    last_bar = {"BUY": -9999, "SELL": -9999}

    for idx, c in enumerate(candidates):
        # Direction
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
            dirs = []
            if c["daily_tr"] != "BEAR":
                dirs.append("BUY")
            if c["daily_tr"] != "BULL":
                dirs.append("SELL")

        for d in dirs:
            conf = c["bull_conf"] if d == "BUY" else c["bear_conf"]
            if conf < min_conf:
                continue
            if (idx - last_bar[d]) < COOLDOWN:
                continue

            # Supertrend
            opp = -1 if d == "BUY" else 1
            if c["st_4h"] == opp or c["st_1h"] == opp:
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

            # ── Extra momentum filters ──────────────────────────────────────
            if require_adx_rising and not c["adx_rising"]:
                continue

            if require_atr_expanding and not c["atr_expanding"]:
                continue

            if require_hh_ll:
                if c["hh_ll"] != d:  # must have matching momentum
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


def print_result(label, signals):
    if not signals:
        return {"label": label, "pf": 0, "wr": 0, "max_streak": 99, "n": 0}

    df = pd.DataFrame(signals)
    closed  = df[df["outcome"] != "expired"]
    wins    = int((closed["outcome"] == "win").sum())
    losses  = int((closed["outcome"] == "loss").sum())
    n_closed = wins + losses
    if n_closed < 5 or losses == 0:
        return {"label": label, "pf": 0, "wr": 0, "max_streak": 99, "n": n_closed}

    avg_rr = float(df["rr"].mean())
    pf  = (wins * avg_rr) / losses
    wr  = wins / n_closed * 100
    months = max((df["ts"].max() - df["ts"].min()).days / 30, 1)

    streak = max_streak = 0
    for o in closed["outcome"]:
        streak = streak + 1 if o == "loss" else 0
        max_streak = max(max_streak, streak)

    # Monthly
    closed_c = closed.copy()
    closed_c["month"] = pd.to_datetime(closed_c["ts"]).dt.to_period("M")
    monthly_str = []
    bad_months  = 0
    for period, grp in closed_c.groupby("month"):
        mw = int((grp["outcome"] == "win").sum())
        ml = int((grp["outcome"] == "loss").sum())
        mwr = round(mw / (mw + ml) * 100, 0) if (mw + ml) > 0 else 0
        if mwr < 40:
            bad_months += 1
        monthly_str.append(f"{period}:{mwr:.0f}%")

    n_total = len(df)
    print(f"\n  {label}")
    print(f"    PF={pf:.2f}x  WR={wr:.1f}%  {wins}W/{losses}L  RR=1:{avg_rr:.2f}  "
          f"N={n_closed} ({n_total})  {round(n_total/months,1)}/mo  "
          f"MaxConsecL={max_streak}  BadMonths={bad_months}/15")
    print(f"    {' | '.join(monthly_str)}")

    return {
        "label": label, "pf": round(pf,2), "wr": round(wr,1),
        "wins": wins, "losses": losses, "max_streak": max_streak,
        "n": n_closed, "n_total": n_total, "bad_months": bad_months,
        "avg_rr": round(avg_rr, 2),
    }


def main():
    start_fetch = START_DATE - timedelta(days=WARMUP_DAYS)

    print(f"\n{'='*62}")
    print(f"  MIDAS WR>50% FINDER  —  Targeting profitable majority wins")
    print(f"  {START_DATE.date()} to today")
    print(f"{'='*62}\n")

    df_15m = fetch_historical("15min", start_fetch)

    agg = {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    df_1h    = df_15m.resample("1h", closed="left", label="left").agg(agg).dropna()
    df_4h    = df_15m.resample("4h", closed="left", label="left").agg(agg).dropna()
    df_daily = df_15m.resample("1D", closed="left", label="left").agg(agg).dropna()

    candidates = collect_candidates(df_15m, df_1h, df_4h, df_daily)

    print(f"{'='*62}")
    print(f"  RESULTS")
    print(f"{'='*62}")

    results = []
    for cfg in CONFIGS:
        (label, sl, tp, direction, conf, cons,
         adx_rise, atr_exp, hh_ll, weekly) = cfg
        sigs = run_config(candidates, label, sl, tp, direction, conf, cons,
                          adx_rise, atr_exp, hh_ll, weekly)
        r = print_result(label, sigs)
        if r["n"] > 0:
            results.append(r)

    print(f"\n\n{'='*62}")
    print(f"  SUMMARY")
    print(f"{'='*62}")
    results.sort(key=lambda x: (x["wr"] >= 50, -x.get("bad_months", 99), x["pf"]), reverse=True)
    print(f"  {'Config':<26} {'PF':>5} {'WR':>6} {'W':>4} {'L':>4} {'ML':>4} {'Bad':>4} {'N':>5}")
    print(f"  {'-'*26} {'-'*5} {'-'*6} {'-'*4} {'-'*4} {'-'*4} {'-'*4} {'-'*5}")
    for r in results:
        flag = " ***" if r.get("wr", 0) >= 50 and r.get("pf", 0) >= 1.3 else ""
        print(f"  {r['label']:<26} {r['pf']:>5.2f} {r['wr']:>5.1f}% "
              f"{r.get('wins',0):>4} {r.get('losses',0):>4} "
              f"{r['max_streak']:>4} {r.get('bad_months',0):>4} {r['n']:>5}{flag}")

    # Best by WR > 50%
    over50 = [r for r in results if r.get("wr", 0) >= 50 and r.get("pf", 0) >= 1.2]
    if over50:
        best = max(over50, key=lambda x: x["pf"])
        print(f"\n  FOUND WR>50% CONFIG:")
        print(f"  {best['label']}  PF={best['pf']}x  WR={best['wr']}%  MaxL={best['max_streak']}")
    else:
        best = max(results, key=lambda x: x["wr"]) if results else None
        if best:
            print(f"\n  Best WR achieved: {best['wr']}% ({best['label']})")
            print(f"  Could not reach 50% WR — consider looser TP or different strategy.")

    print(f"\n{'='*62}\n")


if __name__ == "__main__":
    main()

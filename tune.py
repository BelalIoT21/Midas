"""
tune.py — Grid-search optimal parameters for each symbol.
Pre-computes indicators once per symbol, then tests all combos in seconds.
Run: python tune.py
"""
import sys, os
sys.path.insert(0, os.path.dirname(__file__))

import time
import itertools
import pandas as pd
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass

from backtest import (
    fetch_historical, resolve_trade,
    START_DATE, WARMUP_DAYS, LOOKBACK_15M, LOOKBACK_1H, LOOKBACK_4H,
    LOOKBACK_DAILY, MAX_HOLD, COOLDOWN_BARS, MAX_TRADES_WEEK,
    ATR_SL_MULT, ATR_TP1_MULT, MIN_RR_RATIO,
)
from indicators.calculator import precompute_votes, precompute_indicators
from indicators.levels import all_levels, nearest_level
from analysis.sessions import (
    get_current_session, session_multiplier, is_market_open,
)


# ── Session lambdas (must be defined at module level for clarity) ──────────

def _london(s):       return s.quality == "best" and s.name == "London"
def _best(s):         return s.quality == "best"
def _ny(s):           return s.name in ("London/NY Overlap", "New York")
def _all_good(s):     return s.quality in ("best", "good")


SESSION_OPTIONS = {
    "london":   _london,
    "best":     _best,
    "ny":       _ny,
    "all_good": _all_good,
}


# ── Grid definitions per symbol ─────────────────────────────────────────────

GRIDS = {
    "XAU/USD": None,   # already tuned — skip
    "BTC/USD": None,   # already tuned — skip
    "ETH/USD": None,   # already tuned — skip
    "US30":    None,   # already tuned — skip

    # FX pairs: very different volatility — need different momentum thresholds
    "EUR/USD": dict(
        trade_dir=  ["SELL", "BUY", "BOTH"],
        session=    ["london", "best"],
        confidence= [0.68, 0.70, 0.72],
        momentum=   [-0.05, -0.08, -0.10, -0.12, -0.15, -0.20],
        consensus=  [5, 6, 7],
        st_stable=  [2, 3],
    ),

    "GBP/USD": dict(
        trade_dir=  ["SELL", "BUY", "BOTH"],
        session=    ["london", "best"],
        confidence= [0.68, 0.70, 0.72],
        momentum=   [-0.05, -0.08, -0.10, -0.12, -0.15, -0.20],
        consensus=  [5, 6, 7],
        st_stable=  [2, 3],
    ),
}

MIN_SIGNALS = 25     # minimum trades required for a result to be valid
TOP_N       = 5      # show top N configs per symbol


@dataclass
class PrecomputedData:
    df_15m:         pd.DataFrame
    df_1h:          pd.DataFrame
    df_4h:          pd.DataFrame
    df_daily:       pd.DataFrame
    votes_15m:      pd.DataFrame
    votes_1h_al:    pd.DataFrame
    votes_4h_al:    pd.DataFrame
    atr_s:          pd.Series
    rsi4h_s:        pd.Series
    adx4h_s:        pd.Series
    st4h_s:         pd.Series
    st1h_s:         pd.Series
    st4h_consec_al: pd.Series
    mom_4h_al:      pd.Series
    trend_4h_al:    pd.Series
    sr_by_1h:       dict
    walk_start:     int


def precompute(symbol: str) -> PrecomputedData:
    import time as _t
    start_fetch = START_DATE - timedelta(days=WARMUP_DAYS)
    df_15m = fetch_historical("15min", start_fetch, symbol)

    agg = {"open": "first", "high": "max", "low": "min",
           "close": "last", "volume": "sum"}
    df_1h    = df_15m.resample("1h",  closed="left", label="left").agg(agg).dropna()
    df_4h    = df_15m.resample("4h",  closed="left", label="left").agg(agg).dropna()
    df_daily = df_15m.resample("1D",  closed="left", label="left").agg(agg).dropna()

    t0 = _t.time()
    print("  pre-computing...", end="", flush=True)

    v15  = precompute_votes(df_15m)
    v1h  = precompute_votes(df_1h)
    v4h  = precompute_votes(df_4h)
    print(".", end="", flush=True)

    i15  = precompute_indicators(df_15m)
    i4h  = precompute_indicators(df_4h)
    i1h  = precompute_indicators(df_1h)
    print(".", end="", flush=True)

    idx  = df_15m.index
    v1h_al  = v1h.reindex(idx, method="ffill")
    v4h_al  = v4h.reindex(idx, method="ffill")
    atr_s   = i15["atr"].reindex(idx, method="ffill")
    rsi4h_s = i4h["rsi14"].reindex(idx, method="ffill")
    adx4h_s = i4h["adx"].reindex(idx, method="ffill")
    st4h_s  = i4h["supertrend_dir"].reindex(idx, method="ffill")
    st1h_s  = i1h["supertrend_dir"].reindex(idx, method="ffill")

    _raw    = i4h["supertrend_dir"].fillna(0)
    _consec = pd.Series(0, index=_raw.index)
    for k in range(1, len(_raw)):
        _consec.iloc[k] = (_consec.iloc[k-1]+1
                           if _raw.iloc[k]==_raw.iloc[k-1] else 0)
    consec_al = _consec.reindex(idx, method="ffill").fillna(0)

    close4h   = df_4h["close"]
    mom_al    = ((close4h - close4h.shift(5)) / close4h.shift(5) * 100
                 ).reindex(idx, method="ffill").fillna(0.0)

    ema50_4h  = df_4h["close"].ewm(span=50, adjust=False).mean()
    band      = ema50_4h * 0.001
    t4h       = pd.Series("NEUTRAL", index=df_4h.index, dtype=object)
    t4h[df_4h["close"] > ema50_4h + band] = "BULL"
    t4h[df_4h["close"] < ema50_4h - band] = "BEAR"
    trend_al  = t4h.reindex(idx, method="ffill").fillna("NEUTRAL")

    print(".", end="", flush=True)
    sr = {}
    for k in range(LOOKBACK_1H, len(df_1h)):
        win = df_1h.iloc[k - LOOKBACK_1H: k + 1]
        try:
            sr[df_1h.index[k]] = all_levels(win).get("all_sr", [])
        except Exception:
            sr[df_1h.index[k]] = []

    walk_idx   = df_15m.index.searchsorted(pd.Timestamp(START_DATE))
    walk_start = max(walk_idx, LOOKBACK_15M)

    print(f" done ({_t.time()-t0:.1f}s)  {len(df_15m):,} bars  "
          f"walk={len(df_15m)-walk_start:,}")
    return PrecomputedData(
        df_15m=df_15m, df_1h=df_1h, df_4h=df_4h, df_daily=df_daily,
        votes_15m=v15, votes_1h_al=v1h_al, votes_4h_al=v4h_al,
        atr_s=atr_s, rsi4h_s=rsi4h_s, adx4h_s=adx4h_s,
        st4h_s=st4h_s, st1h_s=st1h_s, st4h_consec_al=consec_al,
        mom_4h_al=mom_al, trend_4h_al=trend_al, sr_by_1h=sr,
        walk_start=walk_start,
    )


def walk(pc: PrecomputedData, symbol: str, cfg: dict) -> list[dict]:
    """Fast walk using pre-computed series. No indicator recalculation."""
    session_fn  = SESSION_OPTIONS[cfg["session"]]
    trade_dir   = cfg["trade_dir"]
    conf_thresh = cfg["confidence"]
    mom_thresh  = cfg["momentum"]
    consensus   = cfg["consensus"]
    st_stable   = cfg["st_stable"]

    signals:      list[dict] = []
    last_sig:     dict[str, int] = {}
    weekly_trades = 0
    current_week: tuple = (-1, -1)

    df = pc.df_15m

    for i in range(pc.walk_start, len(df)):
        ts = df.index[i].to_pydatetime()

        if not is_market_open(ts, symbol):
            continue

        session = get_current_session(ts)
        if not session_fn(session):
            continue

        v15 = pc.votes_15m.iloc[i]
        v1h = pc.votes_1h_al.iloc[i]
        v4h = pc.votes_4h_al.iloc[i]

        bull = bear = 0.0
        aw   = 0
        bd   = []
        for tf, w, vr in [("4h",3,v4h),("1h",2,v1h),("15min",1,v15)]:
            for name in vr.index:
                v = int(vr[name])
                bull += w * max(v, 0)
                bear += w * max(-v, 0)
                if v != 0: aw += w
                bd.append((name, tf, v))

        if aw == 0: continue

        close = float(df["close"].iat[i])
        atr   = float(pc.atr_s.iat[i])
        if pd.isna(atr): continue

        bc = bull / aw
        sc = bear / aw

        cur_1h_ts = pc.votes_1h_al.index[i]
        sr = pc.sr_by_1h.get(cur_1h_ts, [])
        if sr:
            near = nearest_level(close, sr)
            if near is not None:
                if near < close: bc += 0.06
                else:            sc += 0.06

        mult = session_multiplier(session)
        bc  *= mult
        sc  *= mult

        direction  = "BUY" if bc >= sc else "SELL"
        confidence = bc if direction == "BUY" else sc

        trend = pc.trend_4h_al.iat[i]
        if direction == "BUY"  and trend != "BULL": continue
        if direction == "SELL" and trend != "BEAR": continue

        if trade_dir == "SELL" and direction == "BUY":  continue
        if trade_dir == "BUY"  and direction == "SELL": continue

        if confidence < conf_thresh: continue

        expected = 1 if direction == "BUY" else -1

        st4    = float(pc.st4h_s.iat[i]) if not pd.isna(pc.st4h_s.iat[i]) else 0
        st1    = float(pc.st1h_s.iat[i]) if not pd.isna(pc.st1h_s.iat[i]) else 0
        consec = int(pc.st4h_consec_al.iat[i])
        if not ((st4 == expected) and (st1 == expected) and (consec >= st_stable)):
            continue

        mom = float(pc.mom_4h_al.iat[i])
        if direction == "SELL" and mom > mom_thresh:  continue
        if direction == "BUY"  and mom < -mom_thresh: continue

        adx_v = float(pc.adx4h_s.iat[i]) if not pd.isna(pc.adx4h_s.iat[i]) else 25.0
        if adx_v < 22: continue

        rsi_v = float(pc.rsi4h_s.iat[i]) if not pd.isna(pc.rsi4h_s.iat[i]) else 50.0
        if direction == "BUY"  and rsi_v > 70: continue
        if direction == "SELL" and rsi_v < 30: continue

        if sum(1 for _,tf,v in bd if tf=="4h" and v==expected) < consensus: continue

        if (i - last_sig.get(direction, -9999)) < COOLDOWN_BARS: continue

        this_week = (ts.isocalendar()[0], ts.isocalendar()[1])
        if this_week != current_week:
            current_week  = this_week
            weekly_trades = 0
        if weekly_trades >= MAX_TRADES_WEEK: continue

        sl_d  = atr * ATR_SL_MULT
        tp1_d = atr * ATR_TP1_MULT
        sl  = round(close - sl_d  if direction=="BUY" else close + sl_d, 2)
        tp1 = round(close + tp1_d if direction=="BUY" else close - tp1_d, 2)
        rr1 = round(tp1_d / sl_d, 2)
        if rr1 < MIN_RR_RATIO: continue

        weekly_trades += 1
        last_sig[direction] = i

        future  = df.iloc[i+1: i+1+MAX_HOLD]
        outcome, bars = resolve_trade(future, direction, sl, tp1)
        signals.append({
            "ts": ts, "direction": direction, "outcome": outcome,
            "rr1": rr1, "bars_held": bars,
        })

    return signals


def score(signals: list[dict]) -> tuple[float, float, int]:
    """Returns (win_rate, profit_factor, n_closed)."""
    if not signals: return 0.0, 0.0, 0
    closed = [s for s in signals if s["outcome"] != "expired"]
    wins   = [s for s in closed if s["outcome"] == "win"]
    losses = [s for s in closed if s["outcome"] == "loss"]
    n = len(closed)
    if n == 0: return 0.0, 0.0, 0
    wr  = len(wins) / n * 100
    avg = sum(s["rr1"] for s in signals) / len(signals)
    pf  = round(len(wins)*avg / len(losses), 2) if losses else float("inf")
    return round(wr, 1), pf, n


def tune_symbol(symbol: str) -> dict | None:
    grid = GRIDS.get(symbol)
    if grid is None:
        print(f"  {symbol}: no grid defined — skip")
        return None

    print(f"\n{'='*60}")
    print(f"  Tuning {symbol}")
    print(f"{'='*60}")
    pc = precompute(symbol)

    keys   = list(grid.keys())
    combos = list(itertools.product(*[grid[k] for k in keys]))
    print(f"  Testing {len(combos)} parameter combinations...\n")

    results = []
    t0 = time.time()
    for idx_c, values in enumerate(combos):
        cfg = dict(zip(keys, values))
        sigs = walk(pc, symbol, cfg)
        wr, pf, n = score(sigs)
        if n >= MIN_SIGNALS:
            months = max((sigs[-1]["ts"] - sigs[0]["ts"]).days / 30, 1) if sigs else 1
            results.append((wr, pf, n, round(n/months,1), cfg))

    results.sort(key=lambda x: (x[0], x[1]), reverse=True)

    if not results:
        print(f"  No config reached {MIN_SIGNALS} signals.")
        return None

    print(f"  Done in {time.time()-t0:.1f}s.  "
          f"{len(results)}/{len(combos)} configs hit min {MIN_SIGNALS} signals.\n")
    print(f"  {'WR':>6}  {'PF':>6}  {'N':>5}  {'sig/mo':>7}  config")
    print(f"  {'-'*60}")
    for wr, pf, n, spm, cfg in results[:TOP_N]:
        print(f"  {wr:>5.1f}%  {pf:>6.2f}x  {n:>5}  {spm:>7.1f}  {cfg}")

    best_wr, best_pf, best_n, best_spm, best_cfg = results[0]
    print(f"\n  Best: {best_wr}% WR  {best_pf}x PF  {best_n} trades "
          f"({best_spm}/mo)  {best_cfg}")
    return best_cfg


def tune_all() -> None:
    best_configs = {}
    for sym in GRIDS:
        if GRIDS[sym] is None:
            continue
        best = tune_symbol(sym)
        if best:
            best_configs[sym] = best

    print(f"\n{'='*60}")
    print("  RECOMMENDED SYMBOL_CONFIGS UPDATE")
    print(f"{'='*60}")
    for sym, cfg in best_configs.items():
        print(f"\n  '{sym}': dict(")
        slug = sym.replace("/", "")
        print(f"    slug='{slug}', trade_dir='{cfg['trade_dir']}',")
        print(f"    session_fn={cfg['session']},")
        print(f"    confidence={cfg['confidence']}, momentum_thresh={cfg['momentum']},")
        print(f"    consensus={cfg['consensus']}, st_stable={cfg['st_stable']},")
        print(f"  ),")


if __name__ == "__main__":
    tune_all()

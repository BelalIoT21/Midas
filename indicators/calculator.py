"""
Computes 11 technical indicators on a candle DataFrame.
Each returns a vote: +1 (BUY), 0 (NEUTRAL), -1 (SELL).

Design principle: indicators vote on TREND DIRECTION, not just extremes.
This generates 2-4 actionable signals per day instead of rarely firing.
"""
import pandas as pd
import pandas_ta as ta


def _safe(series) -> float | None:
    if series is None:
        return None
    s = series.dropna()
    return float(s.iloc[-1]) if not s.empty else None


def ema_vote(df: pd.DataFrame) -> int:
    """
    EMA trend structure: close vs EMA21 vs EMA50.
    BUY  = close > EMA21 AND EMA21 > EMA50 (uptrend structure)
    SELL = close < EMA21 AND EMA21 < EMA50
    """
    close = _safe(df["close"])
    e21   = _safe(ta.ema(df["close"], length=21))
    e50   = _safe(ta.ema(df["close"], length=50))
    e200  = _safe(ta.ema(df["close"], length=200))
    if None in (close, e21, e50, e200):
        return 0
    if close > e21 and e21 > e50:
        return 1
    if close < e21 and e21 < e50:
        return -1
    return 0


def macd_vote(df: pd.DataFrame) -> int:
    """
    MACD(12,26,9): histogram direction.
    BUY  = MACD line > signal AND histogram positive
    SELL = MACD line < signal AND histogram negative
    """
    result = ta.macd(df["close"], fast=12, slow=26, signal=9)
    if result is None or result.empty:
        return 0
    macd_line = _safe(result.iloc[:, 0])
    hist      = _safe(result.iloc[:, 1])
    signal    = _safe(result.iloc[:, 2])
    if None in (macd_line, hist, signal):
        return 0
    if macd_line > signal and hist > 0:
        return 1
    if macd_line < signal and hist < 0:
        return -1
    return 0


def rsi_vote(df: pd.DataFrame) -> int:
    """
    RSI(14) momentum: above/below 50.
    BUY  = RSI > 50 (bullish momentum)
    SELL = RSI < 50 (bearish momentum)
    """
    rsi = _safe(ta.rsi(df["close"], length=14))
    if rsi is None:
        return 0
    if rsi > 50:
        return 1
    if rsi < 50:
        return -1
    return 0


def stochrsi_vote(df: pd.DataFrame) -> int:
    """
    Stoch RSI: %K position relative to midpoint.
    BUY  = K > 50
    SELL = K < 50
    """
    result = ta.stochrsi(df["close"], length=14, rsi_length=14, k=3, d=3)
    if result is None or result.empty:
        return 0
    cols = result.columns.tolist()
    k = _safe(result[cols[0]])
    if k is None:
        return 0
    if k > 50:
        return 1
    if k < 50:
        return -1
    return 0


def bbands_vote(df: pd.DataFrame) -> int:
    """
    Bollinger Bands: price vs middle band (20-SMA).
    BUY  = close above middle band (bullish bias)
    SELL = close below middle band (bearish bias)
    """
    result = ta.bbands(df["close"], length=20, std=2)
    if result is None or result.empty:
        return 0
    cols   = result.columns.tolist()
    middle = _safe(result[cols[1]])   # middle band = 20-SMA
    close  = _safe(df["close"])
    if None in (middle, close):
        return 0
    if close > middle:
        return 1
    if close < middle:
        return -1
    return 0


def adx_vote(df: pd.DataFrame) -> int:
    """
    ADX + DI+/DI-: trend direction when trend exists (ADX > 20).
    BUY  = DI+ > DI-
    SELL = DI- > DI+
    """
    result = ta.adx(df["high"], df["low"], df["close"], length=14)
    if result is None or result.empty:
        return 0
    cols    = result.columns.tolist()
    adx_val = _safe(result[cols[0]])
    dmp     = _safe(result[cols[1]])
    dmn     = _safe(result[cols[2]])
    if None in (adx_val, dmp, dmn):
        return 0
    if adx_val < 20:
        return 0
    if dmp > dmn:
        return 1
    if dmn > dmp:
        return -1
    return 0


def cci_vote(df: pd.DataFrame) -> int:
    """
    CCI(20): positive CCI = price above average = bullish.
    BUY  = CCI > 0
    SELL = CCI < 0
    """
    cci = _safe(ta.cci(df["high"], df["low"], df["close"], length=20))
    if cci is None:
        return 0
    if cci > 0:
        return 1
    if cci < 0:
        return -1
    return 0


def williams_r_vote(df: pd.DataFrame) -> int:
    """
    Williams %R(14): above/below midpoint (-50).
    Scale: -100 (most oversold) to 0 (most overbought).
    BUY  = %R < -50 (in oversold half)
    SELL = %R > -50 (in overbought half)
    """
    wr = _safe(ta.willr(df["high"], df["low"], df["close"], length=14))
    if wr is None:
        return 0
    if wr < -50:
        return 1
    if wr > -50:
        return -1
    return 0


def ichimoku_vote(df: pd.DataFrame) -> int:
    """
    Ichimoku Cloud: price vs cloud.
    BUY  = price above cloud
    SELL = price below cloud
    """
    result = ta.ichimoku(df["high"], df["low"], df["close"])
    if result is None or len(result) < 1:
        return 0
    ichi_df = result[0]
    if ichi_df is None or ichi_df.empty:
        return 0
    cols       = ichi_df.columns.tolist()
    span_a_col = next((c for c in cols if "ISA" in c), None)
    span_b_col = next((c for c in cols if "ISB" in c), None)
    close      = _safe(df["close"])
    if None in (span_a_col, span_b_col, close):
        return 0
    span_a = _safe(ichi_df[span_a_col])
    span_b = _safe(ichi_df[span_b_col])
    if None in (span_a, span_b):
        return 0
    cloud_top = max(span_a, span_b)
    cloud_bot = min(span_a, span_b)
    if close > cloud_top:
        return 1
    if close < cloud_bot:
        return -1
    return 0


def supertrend_vote(df: pd.DataFrame) -> int:
    """
    Supertrend(7, 3.0): trailing trend direction.
    BUY  = price above supertrend (uptrend)
    SELL = price below supertrend (downtrend)
    """
    result = ta.supertrend(df["high"], df["low"], df["close"], length=7, multiplier=3.0)
    if result is None or result.empty:
        return 0
    dir_col = next((c for c in result.columns if "SUPERTd" in c), None)
    if dir_col is None:
        return 0
    direction = _safe(result[dir_col])
    if direction is None:
        return 0
    if direction == 1:
        return 1
    if direction == -1:
        return -1
    return 0


def momentum_vote(df: pd.DataFrame) -> int:
    """
    Price Momentum(10): direction of price change over 10 bars.
    Replaces OBV — XAU/USD has no real volume data.
    BUY  = momentum positive (price rising)
    SELL = momentum negative (price falling)
    """
    mom = ta.mom(df["close"], length=10)
    val = _safe(mom)
    if val is None:
        return 0
    if val > 0:
        return 1
    if val < 0:
        return -1
    return 0


def atr_value(df: pd.DataFrame) -> float | None:
    return _safe(ta.atr(df["high"], df["low"], df["close"], length=14))


INDICATORS = [
    ("EMA Trend",    ema_vote),
    ("MACD",         macd_vote),
    ("RSI",          rsi_vote),
    ("Stoch RSI",    stochrsi_vote),
    ("BB Position",  bbands_vote),
    ("ADX",          adx_vote),
    ("CCI",          cci_vote),
    ("Williams %R",  williams_r_vote),
    ("Ichimoku",     ichimoku_vote),
    ("Supertrend",   supertrend_vote),
    ("Momentum",     momentum_vote),
]


def compute_votes(df: pd.DataFrame) -> list[tuple[str, int]]:
    return [(name, fn(df)) for name, fn in INDICATORS]


def precompute_votes(df: pd.DataFrame) -> pd.DataFrame:
    """
    Vectorised compute_votes across the full DataFrame — runs each indicator
    once on the whole series instead of per sliding window.  Returns a
    DataFrame with one column per indicator (values in {-1, 0, 1}).
    """
    close = df["close"]
    high  = df["high"]
    low   = df["low"]
    idx   = df.index
    zero  = pd.Series(0, index=idx, dtype="int8")

    def _s(series) -> pd.Series:
        return series.reindex(idx) if series is not None else zero.copy()

    votes: dict[str, pd.Series] = {}

    # EMA Trend
    e21  = ta.ema(close, length=21)
    e50  = ta.ema(close, length=50)
    e200 = ta.ema(close, length=200)
    v = zero.copy()
    v[(close > e21) & (e21 > e50) & e200.notna()] = 1
    v[(close < e21) & (e21 < e50) & e200.notna()] = -1
    votes["EMA Trend"] = v

    # MACD
    mr = ta.macd(close, fast=12, slow=26, signal=9)
    if mr is not None and not mr.empty:
        ml, hist, sig = mr.iloc[:, 0], mr.iloc[:, 1], mr.iloc[:, 2]
        v = zero.copy()
        v[(ml > sig) & (hist > 0)] = 1
        v[(ml < sig) & (hist < 0)] = -1
        votes["MACD"] = v
    else:
        votes["MACD"] = zero.copy()

    # RSI
    rsi = _s(ta.rsi(close, length=14))
    v = zero.copy()
    v[rsi > 50] = 1
    v[rsi < 50] = -1
    votes["RSI"] = v

    # Stoch RSI
    sr = ta.stochrsi(close, length=14, rsi_length=14, k=3, d=3)
    if sr is not None and not sr.empty:
        k = sr.iloc[:, 0]
        v = zero.copy()
        v[k > 50] = 1
        v[k < 50] = -1
        votes["Stoch RSI"] = v
    else:
        votes["Stoch RSI"] = zero.copy()

    # BB Position
    bb = ta.bbands(close, length=20, std=2)
    if bb is not None and not bb.empty:
        mid = bb.iloc[:, 1]
        v = zero.copy()
        v[close > mid] = 1
        v[close < mid] = -1
        votes["BB Position"] = v
    else:
        votes["BB Position"] = zero.copy()

    # ADX
    ar = ta.adx(high, low, close, length=14)
    if ar is not None and not ar.empty:
        adx_v, dmp, dmn = ar.iloc[:, 0], ar.iloc[:, 1], ar.iloc[:, 2]
        v = zero.copy()
        v[(adx_v >= 20) & (dmp > dmn)] = 1
        v[(adx_v >= 20) & (dmn > dmp)] = -1
        votes["ADX"] = v
    else:
        votes["ADX"] = zero.copy()

    # CCI
    cci = _s(ta.cci(high, low, close, length=20))
    v = zero.copy()
    v[cci > 0] = 1
    v[cci < 0] = -1
    votes["CCI"] = v

    # Williams %R
    wr = _s(ta.willr(high, low, close, length=14))
    v = zero.copy()
    v[wr < -50] = 1
    v[wr > -50] = -1
    votes["Williams %R"] = v

    # Ichimoku
    ir = ta.ichimoku(high, low, close)
    if ir is not None and len(ir) >= 1 and ir[0] is not None and not ir[0].empty:
        cols   = ir[0].columns.tolist()
        sa_col = next((c for c in cols if "ISA" in c), None)
        sb_col = next((c for c in cols if "ISB" in c), None)
        if sa_col and sb_col:
            sa, sb    = ir[0][sa_col], ir[0][sb_col]
            cloud_top = pd.concat([sa, sb], axis=1).max(axis=1).reindex(idx)
            cloud_bot = pd.concat([sa, sb], axis=1).min(axis=1).reindex(idx)
            v = zero.copy()
            v[close > cloud_top] = 1
            v[close < cloud_bot] = -1
            votes["Ichimoku"] = v
        else:
            votes["Ichimoku"] = zero.copy()
    else:
        votes["Ichimoku"] = zero.copy()

    # Supertrend
    str_r = ta.supertrend(high, low, close, length=7, multiplier=3.0)
    if str_r is not None and not str_r.empty:
        dc = next((c for c in str_r.columns if "SUPERTd" in c), None)
        if dc:
            d = str_r[dc]
            v = zero.copy()
            v[d == 1]  = 1
            v[d == -1] = -1
            votes["Supertrend"] = v
        else:
            votes["Supertrend"] = zero.copy()
    else:
        votes["Supertrend"] = zero.copy()

    # Momentum
    mom = _s(ta.mom(close, length=10))
    v = zero.copy()
    v[mom > 0] = 1
    v[mom < 0] = -1
    votes["Momentum"] = v

    return pd.DataFrame(votes, index=idx).fillna(0).astype("int8")


def precompute_indicators(df: pd.DataFrame) -> dict[str, pd.Series]:
    """
    Returns helper Series for backtest filter checks:
      atr, rsi14, adx, adx_dmp, adx_dmn, supertrend_dir
    """
    close = df["close"]
    high  = df["high"]
    low   = df["low"]

    atr_s = ta.atr(high, low, close, length=14)
    rsi_s = ta.rsi(close, length=14)

    ar = ta.adx(high, low, close, length=14)
    adx_s = ar.iloc[:, 0] if ar is not None and not ar.empty else pd.Series(dtype=float)
    dmp_s = ar.iloc[:, 1] if ar is not None and not ar.empty else pd.Series(dtype=float)
    dmn_s = ar.iloc[:, 2] if ar is not None and not ar.empty else pd.Series(dtype=float)

    str_r = ta.supertrend(high, low, close, length=7, multiplier=3.0)
    if str_r is not None and not str_r.empty:
        dc    = next((c for c in str_r.columns if "SUPERTd" in c), None)
        st_s  = str_r[dc] if dc else pd.Series(dtype=float)
    else:
        st_s = pd.Series(dtype=float)

    return {
        "atr":           atr_s,
        "rsi14":         rsi_s,
        "adx":           adx_s,
        "adx_dmp":       dmp_s,
        "adx_dmn":       dmn_s,
        "supertrend_dir": st_s,
    }

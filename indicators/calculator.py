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

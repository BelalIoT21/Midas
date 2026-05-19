"""
Data layer for XAUUSD — yfinance only (free, no API key needed).
Ticker GC=F is Gold Futures, closest to spot XAU/USD.
"""
import pandas as pd
import yfinance as yf
from config import CANDLE_COUNT

# ── Real-time quote ───────────────────────────────────────────────────────────


def fetch_quote() -> dict:
    """
    Returns current XAUUSD quote using yfinance 1-min data.
    Keys match Finnhub shape: c, d, dp, h, l, o, pc
    """
    ticker  = yf.Ticker("GC=F")
    day_df  = ticker.history(period="5d", interval="1d", auto_adjust=True)
    min_df  = ticker.history(period="1d", interval="1m",  auto_adjust=True)

    if day_df.empty or min_df.empty:
        raise ValueError("yfinance returned no data for GC=F")

    current    = float(min_df["Close"].iloc[-1])
    prev_close = float(day_df["Close"].iloc[-2]) if len(day_df) >= 2 else float(day_df["Close"].iloc[-1])
    day_open   = float(day_df["Open"].iloc[-1])
    day_high   = float(day_df["High"].iloc[-1])
    day_low    = float(day_df["Low"].iloc[-1])
    change     = round(current - prev_close, 2)
    change_pct = round(change / prev_close * 100, 2) if prev_close else 0.0

    return {
        "c":  current,
        "d":  change,
        "dp": change_pct,
        "h":  day_high,
        "l":  day_low,
        "o":  day_open,
        "pc": prev_close,
    }


# ── Candle history ────────────────────────────────────────────────────────────

YF_SYMBOL = "GC=F"   # Gold Futures — closest to spot XAU/USD, free via yfinance

YF_INTERVAL = {
    "15min": "15m",
    "1h":    "1h",
    "4h":    "1h",   # fetched as 1H, resampled to 4H below
}

YF_PERIOD = {
    "15min": "7d",
    "1h":    "30d",
    "4h":    "60d",
}


def fetch_candles(interval: str) -> pd.DataFrame:
    """
    OHLCV DataFrame (UTC, timezone-naive index).
    Columns: open, high, low, close, volume
    """
    yf_interval = YF_INTERVAL.get(interval, "1h")
    period      = YF_PERIOD.get(interval, "30d")

    ticker = yf.Ticker(YF_SYMBOL)
    df = ticker.history(period=period, interval=yf_interval, auto_adjust=True)

    if df is None or df.empty:
        raise ValueError(f"No yfinance data for {interval}")

    df.columns = [c.lower() for c in df.columns]
    df = df[["open", "high", "low", "close", "volume"]].copy()

    if df.index.tzinfo is not None:
        df.index = df.index.tz_convert("UTC").tz_localize(None)

    df = df.sort_index().dropna(subset=["close"])

    if interval == "4h":
        df = df.resample("4h").agg({
            "open":   "first",
            "high":   "max",
            "low":    "min",
            "close":  "last",
            "volume": "sum",
        }).dropna(subset=["close"])

    return df.tail(CANDLE_COUNT)


def fetch_all_timeframes(timeframes: list[tuple]) -> dict[str, pd.DataFrame]:
    """Fetch candles for all timeframes. Returns {interval: DataFrame}."""
    result = {}
    for interval, _weight in timeframes:
        try:
            result[interval] = fetch_candles(interval)
            print(f"[data] {interval}: {len(result[interval])} candles | "
                  f"close ${float(result[interval]['close'].iloc[-1]):,.2f}")
        except Exception as e:
            print(f"[data] WARNING: failed to fetch {interval} — {e}")
    return result

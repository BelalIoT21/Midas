"""
Fetches OHLCV candle data and real-time quotes for XAU/USD from TwelveData REST API.
Free tier: 800 calls/day, 8 credits/min.

Responses are cached by (interval, outputsize) to avoid redundant calls:
  - 1min candles: 55s TTL  (data updates every 60s)
  - 5min candles: 270s TTL (data updates every 300s)
  - quote:        15s TTL
"""
import logging
import time
import requests
import pandas as pd
from config import TWELVEDATA_API_KEY, SYMBOL, CANDLE_COUNT, BTC_SYMBOL

logger = logging.getLogger(__name__)

BASE_URL = "https://api.twelvedata.com"

# Circuit breakers
_credits_blocked_until: float = 0.0   # daily exhaustion — wait until midnight
_minute_blocked_until:  float = 0.0   # per-minute rate limit — wait ~60s

# Response cache: {cache_key: (fetched_at, data)}
_cache: dict[str, tuple[float, object]] = {}

# Cache TTLs in seconds per interval
_CACHE_TTL: dict[str, float] = {
    "1min":  180.0,   # 3min — range detection uses last 12 candles, 3min-old data is fine
    "5min":  300.0,   # exact 5min interval
    "15min": 900.0,
    "1h":    3600.0,
    "4h":    14400.0,
    "quote": 30.0,
}


def _check_credits_blocked() -> None:
    global _credits_blocked_until, _minute_blocked_until
    now = time.time()
    if _credits_blocked_until and now < _credits_blocked_until:
        secs = int(_credits_blocked_until - now)
        raise ValueError(f"TwelveData daily limit hit — resets in {secs//3600}h {(secs%3600)//60}m")
    if _minute_blocked_until and now < _minute_blocked_until:
        secs = int(_minute_blocked_until - now)
        raise ValueError(f"TwelveData rate limit — resets in {secs}s")


def _maybe_block_credits(message: str) -> None:
    global _credits_blocked_until, _minute_blocked_until
    if "for the current minute" in message:
        # Per-minute limit — back off for 65s (a little over one minute to be safe)
        _minute_blocked_until = time.time() + 65
        logger.warning("[twelvedata] Per-minute limit hit — pausing 65s")
    elif "run out of API credits" in message or "limit being" in message:
        import datetime
        now = datetime.datetime.utcnow()
        midnight = (now + datetime.timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        _credits_blocked_until = midnight.timestamp()
        logger.warning(f"[twelvedata] Credits exhausted — blocked until {midnight.strftime('%H:%M UTC')}")


def fetch_quote() -> dict:
    """
    Real-time XAU/USD quote via TwelveData.
    Returns keys: c, d, dp, h, l, o, pc  (same shape as before)
    Cached for 15s — calling more often returns the same object.
    """
    _check_credits_blocked()
    cache_key = "quote"
    ttl = _CACHE_TTL["quote"]
    cached = _cache.get(cache_key)
    if cached and (time.time() - cached[0]) < ttl:
        return cached[1]  # type: ignore[return-value]

    resp = requests.get(
        f"{BASE_URL}/quote",
        params={"symbol": SYMBOL, "apikey": TWELVEDATA_API_KEY},
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()

    if data.get("status") == "error":
        msg = data.get("message", "")
        _maybe_block_credits(msg)
        raise ValueError(f"TwelveData error: {msg}")

    result: dict = {
        "c":  float(data["close"]),
        "d":  float(data["change"]),
        "dp": float(data["percent_change"]),
        "h":  float(data["high"]),
        "l":  float(data["low"]),
        "o":  float(data["open"]),
        "pc": float(data["previous_close"]),
    }
    _cache[cache_key] = (time.time(), result)
    return result


def fetch_candles(interval: str, outputsize: int = CANDLE_COUNT, symbol: str = SYMBOL) -> pd.DataFrame:
    """
    Returns a DataFrame with columns: open, high, low, close, volume.
    Index is a DatetimeIndex (UTC).
    Raises on API error or empty response.
    Responses are cached per (symbol, interval, outputsize) — TTL varies by interval.
    """
    _check_credits_blocked()

    cache_key = f"{symbol}:{interval}:{outputsize}"
    ttl = _CACHE_TTL.get(interval, 55.0)
    cached = _cache.get(cache_key)
    if cached and (time.time() - cached[0]) < ttl:
        return cached[1]  # type: ignore[return-value]

    url = f"{BASE_URL}/time_series"
    params = {
        "symbol":     symbol,
        "interval":   interval,
        "outputsize": outputsize,
        "apikey":     TWELVEDATA_API_KEY,
        "format":     "JSON",
        "timezone":   "UTC",
    }

    resp = requests.get(url, params=params, timeout=15)
    resp.raise_for_status()
    data = resp.json()

    if data.get("status") == "error":
        msg = data.get("message", "")
        _maybe_block_credits(msg)
        raise ValueError(f"TwelveData error: {msg}")

    values = data.get("values")
    if not values:
        raise ValueError(f"No candle data returned for {symbol} {interval}")

    df = pd.DataFrame(values)
    df["datetime"] = pd.to_datetime(df["datetime"]).dt.tz_localize("UTC")
    df = df.set_index("datetime").sort_index()

    for col in ["open", "high", "low", "close", "volume"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    if "volume" not in df.columns:
        df["volume"] = 0.0

    df = df[["open", "high", "low", "close", "volume"]].dropna(subset=["close"])
    _cache[cache_key] = (time.time(), df)
    return df


def fetch_price(symbol: str = SYMBOL) -> float | None:
    """Return latest close price for any symbol. Cached with 1min TTL."""
    _check_credits_blocked()
    cache_key = f"price:{symbol}"
    cached = _cache.get(cache_key)
    if cached and (time.time() - cached[0]) < 60.0:
        return cached[1]  # type: ignore[return-value]
    try:
        df = fetch_candles("1min", outputsize=2, symbol=symbol)
        if df is not None and not df.empty:
            price = float(df["close"].iloc[-1])
            _cache[cache_key] = (time.time(), price)
            return price
    except Exception:
        pass
    return None


def fetch_all_timeframes(timeframes: list[tuple], symbol: str = SYMBOL) -> dict[str, pd.DataFrame]:
    """
    Fetch candles for all timeframes. Returns {interval: DataFrame}.
    Skips timeframes that fail (logs the error).
    """
    result = {}
    for interval, _weight in timeframes:
        try:
            result[interval] = fetch_candles(interval, symbol=symbol)
            print(f"[data] {symbol} {interval}: {len(result[interval])} candles loaded")
        except Exception as e:
            print(f"[data] WARNING: failed to fetch {symbol} {interval} — {e}")
    return result

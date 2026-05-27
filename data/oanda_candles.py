"""
Fetches OHLCV candle data from OANDA REST API.
No daily call limit — uses the user's OANDA credentials.
Used by all trading loops instead of TwelveData to avoid the 800 credit/day limit.
"""
import logging
import time
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import requests

CACHE_DIR = Path(__file__).parent / "cache"
CACHE_DIR.mkdir(exist_ok=True)

logger = logging.getLogger(__name__)

_GRANULARITY: dict[str, str] = {
    "5min":  "M5",
    "15min": "M15",
    "1h":    "H1",
    "4h":    "H4",
    "1day":  "D",
}

_INTERVAL_MINUTES: dict[str, int] = {
    "5min": 5, "15min": 15, "1h": 60, "4h": 240, "1day": 1440,
}

_SYMBOL_MAP: dict[str, str] = {
    "XAU/USD": "XAU_USD",
    "BTC/USD": "BTC_USD",
    "ETH/USD": "ETH_USD",
    "GBP/USD": "GBP_USD",
    "EUR/USD": "EUR_USD",
    "US30":    "US30_USD",
}

_CACHE_TTL: dict[str, float] = {
    "M5":  300.0,
    "H1":  3600.0,
    "H4":  14400.0,
    "D":   86400.0,
}

_cache: dict[str, tuple[float, pd.DataFrame]] = {}


def _base_url(env_flag: str) -> str:
    return (
        "https://api-fxtrade.oanda.com"
        if env_flag == "live"
        else "https://api-fxpractice.oanda.com"
    )


def fetch_candles(
    interval: str,
    outputsize: int,
    symbol: str,
    api_key: str,
    env_flag: str = "practice",
) -> pd.DataFrame:
    """Fetch OHLCV candles from OANDA. Returns DataFrame with UTC DatetimeIndex."""
    granularity = _GRANULARITY.get(interval, "M5")
    instrument  = _SYMBOL_MAP.get(symbol, symbol.replace("/", "_"))

    cache_key = f"oanda:{symbol}:{interval}:{outputsize}:{env_flag}"
    ttl       = _CACHE_TTL.get(granularity, 300.0)
    cached    = _cache.get(cache_key)
    if cached and (time.time() - cached[0]) < ttl:
        return cached[1]

    resp = requests.get(
        f"{_base_url(env_flag)}/v3/instruments/{instrument}/candles",
        headers={"Authorization": f"Bearer {api_key}"},
        params={"count": outputsize, "granularity": granularity, "price": "M"},
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()

    all_candles = data.get("candles", [])
    candles     = [c for c in all_candles if c.get("complete")]
    if not candles:
        if all_candles:
            candles = all_candles  # fallback: include forming bar
        else:
            raise ValueError(f"No OANDA candles for {symbol} {interval}")

    rows = [
        {
            "datetime": c["time"],
            "open":   float(c["mid"]["o"]),
            "high":   float(c["mid"]["h"]),
            "low":    float(c["mid"]["l"]),
            "close":  float(c["mid"]["c"]),
            "volume": float(c.get("volume", 0)),
        }
        for c in candles
    ]

    df = pd.DataFrame(rows)
    df["datetime"] = pd.to_datetime(df["datetime"], utc=True)
    df = df.set_index("datetime").sort_index()
    df = df[["open", "high", "low", "close", "volume"]]

    _cache[cache_key] = (time.time(), df)
    logger.debug(f"[oanda_candles] {symbol} {interval}: {len(df)} candles")
    return df


def fetch_historical_range(
    interval: str,
    start: datetime,
    end: datetime,
    symbol: str,
    api_key: str,
    env_flag: str = "practice",
) -> pd.DataFrame:
    """
    Fetch full date-range historical candles from OANDA, paginated in chunks of 5000.
    Uses `from` + `count` pagination — safe for any date range including 2+ years.
    """
    granularity  = _GRANULARITY.get(interval, "M15")
    instrument   = _SYMBOL_MAP.get(symbol, symbol.replace("/", "_"))
    base         = _base_url(env_flag)
    headers      = {"Authorization": f"Bearer {api_key}"}
    step_minutes = _INTERVAL_MINUTES.get(interval, 15)

    chunks: list[pd.DataFrame] = []
    current = start

    print(f"  [{symbol} {interval}] fetching {start.date()} -> {end.date()}...", end="", flush=True)

    while current < end:
        params = {
            "from":        current.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "count":       5000,
            "granularity": granularity,
            "price":       "M",
        }
        resp = requests.get(
            f"{base}/v3/instruments/{instrument}/candles",
            headers=headers, params=params, timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()

        candles = data.get("candles", [])
        if not candles:
            break

        rows = [{
            "datetime": c["time"],
            "open":   float(c["mid"]["o"]),
            "high":   float(c["mid"]["h"]),
            "low":    float(c["mid"]["l"]),
            "close":  float(c["mid"]["c"]),
            "volume": float(c.get("volume", 0)),
        } for c in candles]

        chunk = pd.DataFrame(rows)
        chunk["datetime"] = pd.to_datetime(chunk["datetime"], utc=True)
        chunk = chunk.set_index("datetime").sort_index()
        chunk = chunk[["open", "high", "low", "close", "volume"]]
        chunk = chunk[chunk.index <= pd.Timestamp(end)]

        if not chunk.empty:
            chunks.append(chunk)

        print(".", end="", flush=True)

        last_ts = pd.to_datetime(candles[-1]["time"], utc=True)
        if last_ts >= pd.Timestamp(end) or len(candles) < 100:
            break
        current = last_ts.to_pydatetime() + timedelta(minutes=step_minutes)

    print()
    if not chunks:
        raise ValueError(f"No OANDA data returned for {symbol} {interval}")

    df = pd.concat(chunks)
    df = df[~df.index.duplicated(keep="first")].sort_index()
    logger.info(f"[oanda_candles] {symbol} {interval}: {len(df):,} candles fetched")
    return df


def cached_historical(
    interval: str,
    start: datetime,
    end: datetime,
    symbol: str,
    api_key: str,
    env_flag: str = "practice",
) -> pd.DataFrame:
    """
    Like fetch_historical_range but persists data to a parquet file.
    On subsequent calls only fetches the missing delta since the last saved candle.
    Cache files: data/cache/{symbol_safe}_{interval}.parquet
    """
    safe_symbol = symbol.replace("/", "").replace("_", "")
    cache_file  = CACHE_DIR / f"{safe_symbol}_{interval}.parquet"
    step_minutes = _INTERVAL_MINUTES.get(interval, 15)

    existing: pd.DataFrame | None = None
    fetch_from = start

    if cache_file.exists():
        existing = pd.read_parquet(cache_file)
        if not existing.empty:
            last_cached = existing.index.max().to_pydatetime()
            # Only re-fetch if there are new candles to grab
            if last_cached >= end - timedelta(minutes=step_minutes):
                print(f"  [{symbol} {interval}] loaded {len(existing):,} candles from cache (up to date)")
                return existing[existing.index >= pd.Timestamp(start)]
            fetch_from = last_cached + timedelta(minutes=step_minutes)
            print(f"  [{symbol} {interval}] cache hit ({len(existing):,} rows) — fetching delta from {fetch_from.date()}...")

    delta = fetch_historical_range(interval, fetch_from, end, symbol, api_key, env_flag)

    if existing is not None and not existing.empty:
        combined = pd.concat([existing, delta])
        combined = combined[~combined.index.duplicated(keep="last")].sort_index()
    else:
        combined = delta

    combined.to_parquet(cache_file)
    logger.info(f"[oanda_candles] cache saved: {cache_file.name}  ({len(combined):,} rows)")
    return combined[combined.index >= pd.Timestamp(start)]


def fetch_all_timeframes(
    timeframes: list[tuple],
    symbol: str,
    api_key: str,
    env_flag: str = "practice",
) -> dict[str, pd.DataFrame]:
    """Fetch candles for all timeframes from OANDA. Returns {interval: DataFrame}."""
    result: dict[str, pd.DataFrame] = {}
    for interval, outputsize in timeframes:
        try:
            result[interval] = fetch_candles(interval, outputsize, symbol, api_key, env_flag)
        except Exception as e:
            logger.warning(f"[oanda_candles] failed {symbol} {interval}: {e}")
    return result

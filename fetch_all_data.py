"""
fetch_all_data.py — Pull 15min candle history for all backtest symbols.
Saves to data/cache/<SLUG>_15min.parquet.
Run once; subsequent runs are instant (cache hit).
"""
import sys, os, time, requests
import pandas as pd
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))
from config import TWELVEDATA_API_KEY, OANDA_API_KEY, OANDA_PRACTICE

CACHE_DIR = Path(__file__).parent / "data" / "cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

BASE_URL   = "https://api.twelvedata.com/time_series"
START_DATE = datetime(2024, 3, 1, tzinfo=timezone.utc)   # warmup included

# TwelveData symbol -> slug used in cache filename
SYMBOLS = {
    "XAU/USD":  "XAUUSD",
    "BTC/USD":  "BTCUSD",
    "ETH/USD":  "ETHUSD",
    "EUR/USD":  "EURUSD",
    "GBP/USD":  "GBPUSD",
    # US30 via TwelveData — not available, handled separately via OANDA
}

INTERVALS = ["15min", "5min"]   # fetch both intervals for all symbols

_call_times: list = []


def _rate_get(url: str, params: dict) -> dict:
    while True:
        now = time.time()
        while _call_times and now - _call_times[0] > 65:
            _call_times.pop(0)
        if len(_call_times) < 7:
            break
        wait = 65 - (now - _call_times[0])
        print(f"  [rate limit] waiting {wait:.0f}s…", flush=True)
        time.sleep(wait + 1)
    _call_times.append(time.time())
    r = requests.get(url, params=params, timeout=30)
    r.raise_for_status()
    return r.json()


def _fetch_chunk(symbol: str, end_dt: datetime) -> pd.DataFrame:
    params = {
        "symbol":     symbol,
        "interval":   "15min",
        "outputsize": 5000,
        "end_date":   end_dt.strftime("%Y-%m-%d %H:%M:%S"),
        "apikey":     TWELVEDATA_API_KEY,
        "format":     "JSON",
        "timezone":   "UTC",
    }
    while True:
        try:
            data = _rate_get(BASE_URL, params)
        except Exception as e:
            print(f"  [http error] {symbol}: {e}")
            return pd.DataFrame()
        if data.get("status") == "error":
            msg = data.get("message", "")
            if "credits" in msg or "429" in msg:
                print("  [credits] sleeping 65s…", flush=True)
                time.sleep(65)
                _call_times.clear()
                continue
            print(f"  [error] {symbol}: {msg}")
            return pd.DataFrame()
        break
    values = data.get("values", [])
    if not values:
        return pd.DataFrame()
    df = pd.DataFrame(values)
    df["datetime"] = pd.to_datetime(df["datetime"], utc=True)
    for col in ["open", "high", "low", "close"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df["volume"] = 0.0
    return df.set_index("datetime").sort_index()[["open","high","low","close","volume"]]


def _fetch_chunk_interval(symbol: str, end_dt: datetime, interval: str) -> pd.DataFrame:
    params = {
        "symbol": symbol, "interval": interval, "outputsize": 5000,
        "end_date": end_dt.strftime("%Y-%m-%d %H:%M:%S"),
        "apikey": TWELVEDATA_API_KEY, "format": "JSON", "timezone": "UTC",
    }
    while True:
        try:
            data = _rate_get(BASE_URL, params)
        except Exception as e:
            print(f"  [http error] {symbol}: {e}")
            return pd.DataFrame()
        if data.get("status") == "error":
            msg = data.get("message", "")
            if "credits" in msg or "429" in msg:
                print("  [credits] sleeping 65s…", flush=True)
                time.sleep(65)
                _call_times.clear()
                continue
            print(f"  [error] {symbol}: {msg}")
            return pd.DataFrame()
        break
    values = data.get("values", [])
    if not values:
        return pd.DataFrame()
    df = pd.DataFrame(values)
    df["datetime"] = pd.to_datetime(df["datetime"], utc=True)
    for col in ["open", "high", "low", "close"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df["volume"] = 0.0
    return df.set_index("datetime").sort_index()[["open","high","low","close","volume"]]


def fetch_symbol(symbol: str, slug: str, interval: str = "15min") -> None:
    cache = CACHE_DIR / f"{slug}_{interval}.parquet"

    if cache.exists():
        cached = pd.read_parquet(cache)
        cached.index = pd.to_datetime(cached.index, utc=True)
        if not cached.empty:
            earliest = cached.index.min().date()
            if earliest <= START_DATE.date():
                print(f"  {symbol:10s}  OK cache  {len(cached):,} bars  "
                      f"({cached.index.min().date()} -> {cached.index.max().date()})")
                return

    print(f"  {symbol:10s}  fetching {interval}...", end="", flush=True)
    chunks = []
    end = datetime.now(timezone.utc)
    while True:
        chunk = _fetch_chunk_interval(symbol, end, interval)
        if chunk.empty:
            break
        chunks.append(chunk)
        print(".", end="", flush=True)
        earliest = chunk.index.min().to_pydatetime()
        if earliest <= START_DATE:
            break
        if len(chunk) < 100:
            break
        end = earliest - timedelta(minutes=1)

    print()
    if not chunks:
        print(f"  {symbol}: no data returned — skipping")
        return

    df = pd.concat(chunks)
    df = df[~df.index.duplicated(keep="first")].sort_index()
    df.to_parquet(cache)
    df = df[df.index >= pd.Timestamp(START_DATE)]
    print(f"  {symbol:10s}  saved  {len(df):,} bars  "
          f"({df.index.min().date()} -> {df.index.max().date()})")


def fetch_us30_oanda(interval: str = "15min") -> None:
    """Fetch US30 historical data from OANDA and save to cache."""
    gran_map = {"15min": "M15", "5min": "M5"}
    gran  = gran_map.get(interval, "M15")
    step  = timedelta(minutes=int(interval.replace("min","")))
    cache = CACHE_DIR / f"US30_{interval}.parquet"
    if cache.exists():
        df = pd.read_parquet(cache)
        df.index = pd.to_datetime(df.index, utc=True)
        if not df.empty and df.index.min().date() <= START_DATE.date():
            print(f"  US30       OK cache  {len(df):,} bars  "
                  f"({df.index.min().date()} -> {df.index.max().date()})")
            return

    if not OANDA_API_KEY:
        print("  US30       SKIP — OANDA_API_KEY not set in .env")
        return

    env_flag = "practice" if OANDA_PRACTICE else "live"
    base = ("https://api-fxpractice.oanda.com" if OANDA_PRACTICE
            else "https://api-fxtrade.oanda.com")

    print(f"  US30       fetching {interval} via OANDA ({env_flag})...", end="", flush=True)

    chunks = []
    current = START_DATE
    end     = datetime.now(timezone.utc)

    while current < end:
        params = {
            "from":        current.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "count":       5000,
            "granularity": gran,
            "price":       "M",
        }
        try:
            resp = requests.get(
                f"{base}/v3/instruments/US30_USD/candles",
                headers={"Authorization": f"Bearer {OANDA_API_KEY}"},
                params=params, timeout=30,
            )
            resp.raise_for_status()
        except Exception as e:
            print(f"\n  US30 error: {e}")
            break

        candles = resp.json().get("candles", [])
        if not candles:
            break

        rows = [{
            "datetime": c["time"],
            "open":  float(c["mid"]["o"]),
            "high":  float(c["mid"]["h"]),
            "low":   float(c["mid"]["l"]),
            "close": float(c["mid"]["c"]),
            "volume": float(c.get("volume", 0)),
        } for c in candles]

        chunk = pd.DataFrame(rows)
        chunk["datetime"] = pd.to_datetime(chunk["datetime"], utc=True)
        chunk = chunk.set_index("datetime").sort_index()[["open","high","low","close","volume"]]
        chunks.append(chunk)
        print(".", end="", flush=True)

        last_ts = pd.to_datetime(candles[-1]["time"], utc=True).to_pydatetime()
        if last_ts >= end or len(candles) < 100:
            break
        current = last_ts + step

    print()
    if not chunks:
        print("  US30: no data from OANDA")
        return

    df = pd.concat(chunks)
    df = df[~df.index.duplicated(keep="first")].sort_index()
    df.to_parquet(cache)
    print(f"  US30       saved  {len(df):,} bars  "
          f"({df.index.min().date()} -> {df.index.max().date()})")


if __name__ == "__main__":
    print(f"\n{'='*55}")
    print(f"  Fetching 15min data for all symbols")
    print(f"  Start: {START_DATE.date()}  to  today")
    print(f"{'='*55}\n")

    for interval in INTERVALS:
        print(f"\n--- {interval} ---")
        for symbol, slug in SYMBOLS.items():
            fetch_symbol(symbol, slug, interval=interval)
        fetch_us30_oanda(interval=interval)

    print(f"\n  Done. Cache: {CACHE_DIR}\n")

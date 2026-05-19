import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

# Persistent data directory — set DATA_DIR to a Railway Volume mount path (e.g. /data)
DATA_DIR = Path(os.getenv("DATA_DIR", str(Path(__file__).parent / "data")))
DATA_DIR.mkdir(parents=True, exist_ok=True)

# TwelveData
TWELVEDATA_API_KEY = os.getenv("TWELVEDATA_API_KEY", "c18be945afa141639599176e4fdb145c")
SYMBOL            = "XAU/USD"
BTC_SYMBOL        = "BTC/USD"

# Telegram
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID")

# ── Timeframes fetched per scan ────────────────────────────────────────────────
# 4H for bias, 5min for entry logic
TIMEFRAMES = [
    ("4h",    50),
    ("1h",    50),
    ("5min", 200),
]
CANDLE_COUNT = 200

# ── Tap 'n' Barrel strategy settings ──────────────────────────────────────────
# Scan interval: 5 min (fast enough to catch entries but stays within API limits)
SIGNAL_INTERVAL_MINUTES = 5
BTC_SIGNAL_INTERVAL_MINUTES = 5

# Entry fib level: 0.618 (preferred deeper entry) or 0.5 (shallower)
ENTRY_FIB_LEVEL = float(os.getenv("ENTRY_FIB_LEVEL", "0.618"))

# ── Risk management ────────────────────────────────────────────────────────────
# 2% risk per trade on all assets.
# Gold lot formula: Lot = (Balance × 0.02) ÷ (SL_points × 1.49)
# BTC  lot formula: Lot = (Balance × 0.02) ÷ SL_USD_distance
RISK_PCT = 0.02    # 2%

# Gold pip value constant (GBP per pip per 0.01 lot) — adjust for your broker
GOLD_PIP_VALUE = float(os.getenv("GOLD_PIP_VALUE", "1.49"))

# Hard lot caps per account tier (safety caps regardless of formula result)
# Format: (min_balance, max_balance, max_lot)
GOLD_LOT_CAPS = [
    (0,    400,  0.02),
    (400,  800,  0.03),
    (800,  1500, 0.05),
    (1500, float("inf"), 0.10),
]
BTC_LOT_CAPS = [
    (0,    400,  0.01),
    (400,  800,  0.02),
    (800,  float("inf"), 0.03),
]

# GBP/USD lot formula: Lot = (Balance × 0.02) ÷ (sl_pips × GBPUSD_PIP_VALUE)
# pip_value ≈ £7.87/pip/lot for GBP account at ~1.27 rate
GBPUSD_PIP_VALUE = float(os.getenv("GBPUSD_PIP_VALUE", "7.87"))
GBPUSD_LOT_CAPS = [
    (0,    400,  0.02),
    (400,  800,  0.03),
    (800,  float("inf"), 0.05),
]

# Daily limits
MAX_TRADES_PER_DAY  = 2           # max trades per day per symbol
DAILY_LOSS_LIMIT_PCT = 0.05       # stop trading after 5% daily loss

# ── OANDA ─────────────────────────────────────────────────────────────────────
OANDA_SYMBOL     = os.getenv("OANDA_SYMBOL", "XAU/USD")
OANDA_API_KEY    = os.getenv("OANDA_API_KEY", "")
OANDA_ACCOUNT_ID = os.getenv("OANDA_ACCOUNT_ID", "")
OANDA_PRACTICE   = os.getenv("OANDA_PRACTICE", "true").lower() == "true"

# Additional symbols (all paper trade by default until OANDA access confirmed)
EURUSD_ENABLED     = os.getenv("EURUSD_ENABLED",     "true").lower()  == "true"
EURUSD_PAPER_TRADE = os.getenv("EURUSD_PAPER_TRADE", "true").lower()  == "true"
OANDA_EURUSD_SYMBOL= os.getenv("OANDA_EURUSD_SYMBOL","EUR_USD")

GBPUSD_ENABLED     = os.getenv("GBPUSD_ENABLED",     "true").lower()  == "true"
GBPUSD_PAPER_TRADE = os.getenv("GBPUSD_PAPER_TRADE", "true").lower()  == "true"
OANDA_GBPUSD_SYMBOL= os.getenv("OANDA_GBPUSD_SYMBOL","GBP_USD")

US30_ENABLED       = os.getenv("US30_ENABLED",       "true").lower()  == "true"
US30_PAPER_TRADE   = os.getenv("US30_PAPER_TRADE",   "true").lower()  == "true"
OANDA_US30_SYMBOL  = os.getenv("OANDA_US30_SYMBOL",  "US30_USD")

# BTC trading (24/7)
BTC_ENABLED      = os.getenv("BTC_ENABLED",     "true").lower()  == "true"
BTC_PAPER_TRADE  = os.getenv("BTC_PAPER_TRADE", "true").lower()  == "true"
OANDA_BTC_SYMBOL = os.getenv("OANDA_BTC_SYMBOL", "BTC_USD")
BTC_MAX_SPREAD   = float(os.getenv("BTC_MAX_SPREAD", "150.0"))
BTC_MIN_UNITS    = int(os.getenv("BTC_MIN_UNITS", "1"))

# ETH trading (24/7)
ETH_ENABLED      = os.getenv("ETH_ENABLED",     "true").lower()  == "true"
ETH_PAPER_TRADE  = os.getenv("ETH_PAPER_TRADE", "true").lower()  == "true"
OANDA_ETH_SYMBOL = os.getenv("OANDA_ETH_SYMBOL", "ETH_USD")
ETH_MIN_UNITS    = int(os.getenv("ETH_MIN_UNITS", "1"))
ETH_LOT_CAPS     = [
    (0,    400,  0.01),
    (400,  800,  0.02),
    (800,  float("inf"), 0.05),
]

# Minimum SL distance — rejects noise setups where the 5min swing is too narrow
BTC_MIN_SL_DIST    = float(os.getenv("BTC_MIN_SL_DIST",    "400.0"))
ETH_MIN_SL_DIST    = float(os.getenv("ETH_MIN_SL_DIST",    "30.0"))   # $30 min SL for ETH
GOLD_MIN_SL_DIST   = float(os.getenv("GOLD_MIN_SL_DIST",   "8.0"))
EURUSD_MIN_SL_DIST = float(os.getenv("EURUSD_MIN_SL_DIST", "0.001"))   # 10 pips
GBPUSD_MIN_SL_DIST = float(os.getenv("GBPUSD_MIN_SL_DIST", "0.001"))   # 10 pips
US30_MIN_SL_DIST   = float(os.getenv("US30_MIN_SL_DIST",   "20.0"))    # 20 pts

# Entry parameters (tuned by backtest)
ENTRY_RR          = float(os.getenv("ENTRY_RR",          "2.0"))   # TP = 2×SL
ENTRY_SL_FIB      = float(os.getenv("ENTRY_SL_FIB",      "1.1"))   # SL fib level
ENTRY_ZONE_DEEP   = float(os.getenv("ENTRY_ZONE_DEEP",   "0.764")) # deep end of entry zone (was 0.618)
ENTRY_ZONE_SHALLOW= float(os.getenv("ENTRY_ZONE_SHALLOW","0.236")) # shallow end (was 0.382)
SWEEP_TOLERANCE   = float(os.getenv("SWEEP_TOLERANCE",   "0.003")) # 0.3% — near-miss sweep counts

# ── News blackout windows (UTC) ────────────────────────────────────────────────
# Format: list of "HH:MM-HH:MM" strings. Loaded from NEWS_BLACKOUT env var as
# comma-separated values.  No trades are placed during these windows (gold only).
# Example: NEWS_BLACKOUT="12:00-13:30,14:00-14:30"
_raw_blackout = os.getenv("NEWS_BLACKOUT", "")
NEWS_BLACKOUT_WINDOWS: list[tuple[int, int, int, int]] = []
for w in _raw_blackout.split(","):
    w = w.strip()
    if "-" in w and ":" in w:
        try:
            start_str, end_str = w.split("-")
            sh, sm = map(int, start_str.split(":"))
            eh, em = map(int, end_str.split(":"))
            NEWS_BLACKOUT_WINDOWS.append((sh, sm, eh, em))
        except Exception:
            pass

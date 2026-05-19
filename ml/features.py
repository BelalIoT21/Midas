"""
Feature extraction for the trade ML model.
Reads closed trades from auto_trades and converts them to a numeric feature matrix.
"""
import re
import sqlite3
import logging
import numpy as np
from config import DATA_DIR

logger = logging.getLogger(__name__)

_DB = DATA_DIR / "signals.db"

N_FEATURES = 13

_SESSIONS = {
    "London/US Overlap": 0,
    "London":            1,
    "US Afternoon":      2,
    "Asian":             3,
}
_STATES = {"Trending": 2, "Pulling Back": 1, "Consolidating": 0}
_TRENDS = {"BULL": 1, "NEUTRAL": 0, "BEAR": -1}


def _parse_patterns(patterns: str) -> tuple[int, int, int, int, int]:
    """Parse the patterns string stored in auto_trades.
    Format: "Sniper=7(Trending/Strong) TW=1 Nexus=-1 ext=False"
    Returns (sniper_score, sniper_state, tw_vote, nx_vote, extended).
    """
    sniper_score = 0
    sniper_state = 1
    tw_vote      = 0
    nx_vote      = 0
    extended     = 0

    m = re.search(r'Sniper=(-?\d+)\((\w[\w\s]*?)/', patterns or "")
    if m:
        sniper_score = int(m.group(1))
        sniper_state = _STATES.get(m.group(2), 1)

    m = re.search(r'TW=(-?\d+)', patterns or "")
    if m:
        tw_vote = int(m.group(1))

    m = re.search(r'Nexus=(-?\d+)', patterns or "")
    if m:
        nx_vote = int(m.group(1))

    m = re.search(r'ext=(True|False)', patterns or "")
    if m:
        extended = 1 if m.group(1) == "True" else 0

    return sniper_score, sniper_state, tw_vote, nx_vote, extended


def extract_features(row: dict) -> np.ndarray | None:
    """Convert a single auto_trades row into a feature vector."""
    try:
        import datetime as dt
        direction    = 1 if row.get("direction") == "BUY" else -1
        session      = _SESSIONS.get(row.get("session", ""), 4)
        confidence   = float(row.get("confidence") or 0.55)
        rr           = float(row.get("rr") or 1.5)
        entry_ts     = row.get("entry_time", 0)
        ts           = dt.datetime.fromtimestamp(float(entry_ts), tz=dt.timezone.utc)
        hour         = ts.hour
        dow          = ts.weekday()
        daily_trend  = _TRENDS.get(row.get("daily_trend", "NEUTRAL"), 0)
        symbol       = 1 if row.get("symbol", "XAU/USD") == "BTC/USD" else 0

        sniper_score, sniper_state, tw_vote, nx_vote, extended = _parse_patterns(
            row.get("patterns", "")
        )

        return np.array([
            direction, session, confidence, rr, hour, dow,
            sniper_score, sniper_state, tw_vote, nx_vote,
            extended, daily_trend, symbol,
        ], dtype=float)
    except Exception as e:
        logger.debug(f"[ml.features] extract_features error: {e}")
        return None


def signal_to_features(signal) -> np.ndarray | None:
    """Convert a live Signal object to a feature vector for prediction."""
    import datetime as dt
    now = dt.datetime.now(dt.timezone.utc)
    row = {
        "direction":   signal.direction,
        "session":     signal.session_name,
        "confidence":  signal.confidence,
        "rr":          signal.rr2,
        "entry_time":  now.timestamp(),
        "patterns":    (
            f"Sniper={signal.sniper_score}({signal.sniper_state}/{signal.sniper_strength}) "
            f"TW={signal.trendwave_vote} ext={signal.extended_tp}"
        ),
        "daily_trend": signal.daily_trend,
        "symbol":      signal.symbol,
    }
    return extract_features(row)


def load_training_data() -> tuple[np.ndarray, np.ndarray]:
    """
    Load all completed trades from auto_trades as (X, y).
    Labels: 1 = profitable trade, 0 = loss.
    Excludes manual cancels, emergency stops, and OANDA timeouts.
    """
    try:
        with sqlite3.connect(_DB) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("""
                SELECT direction, confidence, rr, session, entry_time,
                       patterns, daily_trend, symbol, final_pnl
                  FROM auto_trades
                 WHERE final_pnl IS NOT NULL
                   AND exit_reason NOT IN ('manual', 'emergency_stop', 'oanda_timeout')
            """).fetchall()
    except Exception as e:
        logger.warning(f"[ml.features] load_training_data error: {e}")
        return np.empty((0, N_FEATURES)), np.empty(0, dtype=int)

    features_list: list[np.ndarray] = []
    labels: list[int] = []
    for row in rows:
        f = extract_features(dict(row))
        if f is not None:
            features_list.append(f)
            labels.append(1 if float(row["final_pnl"]) > 0 else 0)

    if not features_list:
        return np.empty((0, N_FEATURES)), np.empty(0, dtype=int)

    return np.array(features_list), np.array(labels, dtype=int)


FEATURE_NAMES = [
    "direction", "session", "confidence", "rr", "hour", "day_of_week",
    "sniper_score", "sniper_state", "tw_vote", "nx_vote",
    "extended_tp", "daily_trend", "symbol",
]

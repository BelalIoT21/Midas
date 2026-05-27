"""
Market session detection — V2.0 schedule.

  Gold  (XAU/USD)  : Monday–Friday, London + NY sessions
  Bitcoin (BTC/USD): Monday–Saturday (not Sunday)
  US30             : Monday–Friday, NY session only (13:00–21:00 UTC)

Session windows (UTC):
  Asia    : 00:00 – 08:00
  London  : 08:00 – 13:00
  New York: 13:00 – 21:00
"""
from datetime import datetime, timezone
from dataclasses import dataclass


@dataclass
class Session:
    name: str
    emoji: str
    quality: str   # "best" | "good" | "low"
    active: bool


# ── Market open checks ────────────────────────────────────────────────────────

def is_market_open(dt: datetime | None = None, symbol: str = "XAU/USD") -> bool:
    """
    Gold / US30: weekdays only (Mon–Fri).
    Bitcoin: Mon–Sat (not Sunday — low liquidity / gap risk at weekly open).
    """
    if dt is None:
        dt = datetime.now(timezone.utc)
    wd = dt.weekday()   # 0=Mon, 6=Sun

    if symbol == "BTC/USD":
        return True      # 24/7 — all days
    return wd <= 4       # Mon–Fri for Gold / US30


def is_gold_session(dt: datetime | None = None) -> bool:
    """
    Valid gold trading window: Mon–Fri, London + NY sessions (08:00–21:00 UTC).
    Asia session (00:00–08:00) is used for level marking only, not entry.
    """
    if dt is None:
        dt = datetime.now(timezone.utc)
    if dt.weekday() >= 5:
        return False
    return 8 <= dt.hour < 21


def is_btc_session(dt: datetime | None = None) -> bool:
    """BTC trades 24/7 — all days including Sunday."""
    return True


def is_btc_weekend_session(dt: datetime | None = None) -> bool:
    """Alias for is_btc_session — kept for backward compatibility."""
    return is_btc_session(dt)


def is_friday_close_time(dt: datetime | None = None) -> bool:
    """True when Gold Friday close rule applies (Friday 20:00 UTC or later)."""
    if dt is None:
        dt = datetime.now(timezone.utc)
    return dt.weekday() == 4 and dt.hour >= 20


def is_asia_session(dt: datetime | None = None) -> bool:
    """True during Asia session (00:00–08:00 UTC) on a trading weekday."""
    if dt is None:
        dt = datetime.now(timezone.utc)
    if dt.weekday() >= 5:
        return False
    return 0 <= dt.hour < 8


# ── Current session label ─────────────────────────────────────────────────────

def get_current_session(dt: datetime | None = None) -> Session:
    """Returns the current market session based on UTC time."""
    if dt is None:
        dt = datetime.now(timezone.utc)
    hour = dt.hour
    wd   = dt.weekday()

    if wd == 6:  # Sunday
        return Session("Sunday", "📅", "low", False)
    if wd == 5:  # Saturday
        return Session("Saturday", "📅", "good" if is_btc_session(dt) else "low", True)

    if 13 <= hour < 17:
        return Session("London/NY Overlap", "🔥", "best", True)
    if 8 <= hour < 13:
        return Session("London", "🇬🇧", "best", True)
    if 17 <= hour < 21:
        return Session("New York", "🇺🇸", "good", True)
    if 0 <= hour < 8:
        return Session("Asia", "🌏", "low", True)   # level-marking session
    return Session("Off-Hours", "💤", "low", False)


def session_multiplier(session: Session) -> float:
    """Confidence multiplier based on session quality."""
    return {"best": 1.0, "good": 0.9, "low": 0.75}.get(session.quality, 1.0)


def should_trade(session: Session, symbol: str = "XAU/USD") -> bool:
    """Only trade London and London/NY overlap — highest quality sessions only."""
    return session.quality == "best"

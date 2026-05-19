"""
Position sizing and risk management.
Formula: lots = (balance × risk_pct) / (sl_pips × pip_value)
For XAUUSD: 1 pip = $0.01, pip value per 0.01 lot ≈ $0.01
Standard lot for gold: 1 lot = 100 oz, pip value = $1 per 0.01 lot move
"""
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "db" / "signals.db"

# Default settings
DEFAULT_BALANCE = 10000.0
DEFAULT_RISK_PCT = 0.01   # 1%


def _connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_risk_table() -> None:
    with _connect() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS risk_settings (
                id          INTEGER PRIMARY KEY CHECK (id = 1),
                balance     REAL NOT NULL DEFAULT 10000.0,
                risk_pct    REAL NOT NULL DEFAULT 0.01
            )
        """)
        conn.execute("""
            INSERT OR IGNORE INTO risk_settings (id, balance, risk_pct)
            VALUES (1, ?, ?)
        """, (DEFAULT_BALANCE, DEFAULT_RISK_PCT))
        # Migrate old defaults to new defaults
        conn.execute("""
            UPDATE risk_settings SET risk_pct = ? WHERE id = 1 AND risk_pct = 0.02
        """, (DEFAULT_RISK_PCT,))
        conn.execute("""
            UPDATE risk_settings SET balance = ? WHERE id = 1 AND balance < 10000.0
        """, (DEFAULT_BALANCE,))


def get_settings() -> dict:
    with _connect() as conn:
        row = conn.execute("SELECT balance, risk_pct FROM risk_settings WHERE id = 1").fetchone()
    return {"balance": row["balance"], "risk_pct": row["risk_pct"]} if row else {
        "balance": DEFAULT_BALANCE, "risk_pct": DEFAULT_RISK_PCT
    }


def set_balance(balance: float) -> None:
    with _connect() as conn:
        conn.execute("UPDATE risk_settings SET balance = ? WHERE id = 1", (balance,))


def set_risk_pct(pct: float) -> None:
    with _connect() as conn:
        conn.execute("UPDATE risk_settings SET risk_pct = ? WHERE id = 1", (pct,))


def auto_lots(balance: float, risk_pct: float, sl_pips: float) -> float:
    """
    Calculate lot size for XAUUSD.
    sl_pips: SL distance in $ per oz (e.g. 16.50)
    1 standard lot = 100 oz, so risk per lot = sl_pips × 100
    lots = (balance × risk_pct) / (sl_pips × 100)
    """
    if sl_pips <= 0:
        return 0.01
    risk_usd = balance * risk_pct
    lots = risk_usd / (sl_pips * 100)
    return max(0.01, round(lots, 2))


def calculate_position(sl_pips: float, balance: float | None = None) -> dict:
    """
    Returns position size info. Uses live balance if provided, else DB balance.
    sl_pips: SL distance in $ per oz
    """
    s        = get_settings()
    balance  = balance if balance is not None else s["balance"]
    risk_pct = s["risk_pct"]
    risk_usd = balance * risk_pct
    lots     = auto_lots(balance, risk_pct, sl_pips)

    return {
        "balance":  balance,
        "risk_pct": risk_pct,
        "risk_usd": round(risk_usd, 2),
        "lots":     lots,
        "sl_pips":  round(sl_pips, 2),
    }

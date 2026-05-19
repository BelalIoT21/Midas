"""
SQLite store — signals, performance, price alerts, bot state.
"""
import sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path
from signals.engine import Signal
from config import DATA_DIR

DB_PATH = DATA_DIR / "signals.db"
COOLDOWN_MINUTES = 240   # 4 hours cooldown between signals


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with _connect() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS signals (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                direction   TEXT NOT NULL,
                confidence  REAL NOT NULL,
                entry       REAL NOT NULL,
                sl          REAL NOT NULL,
                tp1         REAL NOT NULL,
                tp2         REAL NOT NULL,
                rr1         REAL NOT NULL,
                rr2         REAL NOT NULL,
                patterns    TEXT DEFAULT '',
                divergences TEXT DEFAULT '',
                session     TEXT DEFAULT '',
                outcome     TEXT DEFAULT NULL,
                sent_at     TEXT NOT NULL,
                sent_epoch  INTEGER NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS risk_settings (
                id       INTEGER PRIMARY KEY CHECK (id = 1),
                balance  REAL NOT NULL DEFAULT 1000.0,
                risk_pct REAL NOT NULL DEFAULT 0.02
            )
        """)
        conn.execute("INSERT OR IGNORE INTO risk_settings (id, balance, risk_pct) VALUES (1, 100000.0, 0.01)")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS price_alerts (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                price      REAL NOT NULL,
                created_at TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS bot_state (
                chat_id TEXT NOT NULL DEFAULT '',
                key     TEXT NOT NULL,
                value   TEXT NOT NULL,
                PRIMARY KEY (chat_id, key)
            )
        """)
        # Migrate old single-column bot_state to per-user schema
        try:
            cols = [r[1] for r in conn.execute("PRAGMA table_info(bot_state)").fetchall()]
            if "chat_id" not in cols:
                conn.execute("DROP TABLE bot_state")
                conn.execute("""
                    CREATE TABLE bot_state (
                        chat_id TEXT NOT NULL DEFAULT '',
                        key     TEXT NOT NULL,
                        value   TEXT NOT NULL,
                        PRIMARY KEY (chat_id, key)
                    )
                """)
        except Exception:
            pass
        conn.execute("""
            CREATE TABLE IF NOT EXISTS subscribers (
                chat_id    TEXT PRIMARY KEY,
                joined_at  TEXT NOT NULL
            )
        """)
        # Add signal_type column if the table already existed without it
        try:
            conn.execute("ALTER TABLE signals ADD COLUMN signal_type TEXT DEFAULT 'trend'")
        except Exception:
            pass

        # auto_trades ML columns — safe to run on existing DBs
        for col_def in [
            "symbol      TEXT NOT NULL DEFAULT 'XAU/USD'",
            "patterns    TEXT NOT NULL DEFAULT ''",
            "daily_trend TEXT NOT NULL DEFAULT 'NEUTRAL'",
        ]:
            col_name = col_def.split()[0]
            try:
                conn.execute(f"ALTER TABLE auto_trades ADD COLUMN {col_def}")
            except Exception:
                pass  # column already exists

        conn.execute("""
            CREATE TABLE IF NOT EXISTS oanda_accounts (
                chat_id      TEXT PRIMARY KEY,
                login        INTEGER NOT NULL DEFAULT 0,
                password     TEXT NOT NULL DEFAULT '',
                server       TEXT NOT NULL DEFAULT '',
                metaapi_account_id TEXT NOT NULL DEFAULT '',
                lot_size     REAL NOT NULL DEFAULT 0.02,
                connected_at TEXT NOT NULL
            )
        """)
        # Migrate old mt5_accounts table if it exists
        try:
            conn.execute("ALTER TABLE mt5_accounts RENAME TO oanda_accounts")
        except Exception:
            pass


# ── Signals ──────────────────────────────────────────────

def is_duplicate(direction: str) -> bool:
    cutoff_epoch = int((datetime.now(timezone.utc) - timedelta(minutes=COOLDOWN_MINUTES)).timestamp())
    with _connect() as conn:
        row = conn.execute(
            "SELECT id FROM signals WHERE direction = ? AND sent_epoch > ? LIMIT 1",
            (direction, cutoff_epoch),
        ).fetchone()
    return row is not None


def save_signal(signal: Signal, signal_type: str = "trend") -> int:
    epoch = int(signal.timestamp.timestamp())
    # Store Sniper score + state + strength in patterns; votes in divergences
    confluence = (
        f"Sniper={signal.sniper_score}({signal.sniper_state}/{signal.sniper_strength}) "
        f"TW={signal.trendwave_vote} ext={signal.extended_tp}"
    )
    with _connect() as conn:
        cur = conn.execute(
            """INSERT INTO signals
               (direction, confidence, entry, sl, tp1, tp2, rr1, rr2,
                patterns, divergences, session, sent_at, sent_epoch, signal_type)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                signal.direction, signal.confidence,
                signal.entry, signal.sl, signal.tp1, signal.tp2,
                signal.rr1, signal.rr2,
                confluence,
                signal.daily_trend,
                signal.session_name,
                signal.timestamp.isoformat(),
                epoch,
                signal_type,
            ),
        )
        return cur.lastrowid


def save_bounce_signal(signal) -> int:
    """Save an S/R bounce signal to the signals table."""
    epoch = int(signal.timestamp.timestamp())
    with _connect() as conn:
        cur = conn.execute(
            """INSERT INTO signals
               (direction, confidence, entry, sl, tp1, tp2, rr1, rr2,
                patterns, divergences, session, sent_at, sent_epoch, signal_type)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                signal.direction, signal.confidence,
                signal.entry, signal.sl, signal.tp1, signal.tp2,
                signal.rr1, signal.rr2,
                "",
                ", ".join(signal.divergences),
                signal.session_name,
                signal.timestamp.isoformat(),
                epoch,
                "bounce",
            ),
        )
        return cur.lastrowid


def is_duplicate_bounce(direction: str, cooldown_minutes: int = 120) -> bool:
    """2-hour cooldown per direction for bounce signals."""
    cutoff = int((datetime.now(timezone.utc) - timedelta(minutes=cooldown_minutes)).timestamp())
    with _connect() as conn:
        row = conn.execute(
            "SELECT id FROM signals WHERE signal_type='bounce' AND direction=? AND sent_epoch>? LIMIT 1",
            (direction, cutoff),
        ).fetchone()
    return row is not None


def mark_last_outcome(outcome: str) -> bool:
    with _connect() as conn:
        row = conn.execute("SELECT id FROM signals ORDER BY sent_epoch DESC LIMIT 1").fetchone()
        if not row:
            return False
        conn.execute("UPDATE signals SET outcome = ? WHERE id = ?", (outcome, row["id"]))
        return True


def mark_outcome_by_id(signal_id: int, outcome: str) -> bool:
    with _connect() as conn:
        cur = conn.execute("UPDATE signals SET outcome = ? WHERE id = ?", (outcome, signal_id))
        return cur.rowcount > 0


def get_performance() -> dict:
    with _connect() as conn:
        rows = conn.execute("SELECT outcome FROM signals WHERE outcome IS NOT NULL").fetchall()
    outcomes = [r["outcome"] for r in rows]
    wins, losses, be = outcomes.count("win"), outcomes.count("loss"), outcomes.count("breakeven")
    total    = wins + losses + be
    win_rate = round(wins / (wins + losses) * 100, 1) if (wins + losses) > 0 else 0.0
    return {"wins": wins, "losses": losses, "breakeven": be, "total": total, "win_rate": win_rate}


def get_last_signals(n: int = 5) -> list:
    with _connect() as conn:
        return conn.execute("SELECT * FROM signals ORDER BY sent_epoch DESC LIMIT ?", (n,)).fetchall()


# ── Price Alerts ─────────────────────────────────────────

def save_alert(price: float) -> None:
    with _connect() as conn:
        conn.execute(
            "INSERT INTO price_alerts (price, created_at) VALUES (?, ?)",
            (price, datetime.now(timezone.utc).isoformat()),
        )


def get_alerts() -> list:
    with _connect() as conn:
        return conn.execute("SELECT * FROM price_alerts ORDER BY price").fetchall()


def delete_alert(alert_id: int | None = None) -> None:
    with _connect() as conn:
        if alert_id:
            conn.execute("DELETE FROM price_alerts WHERE id = ?", (alert_id,))
        else:
            conn.execute("DELETE FROM price_alerts")


# ── Bot State ─────────────────────────────────────────────

def get_bot_state(chat_id: str, key: str) -> str:
    with _connect() as conn:
        row = conn.execute(
            "SELECT value FROM bot_state WHERE chat_id = ? AND key = ?", (chat_id, key)
        ).fetchone()
    return row["value"] if row else "0"


def set_bot_state(chat_id: str, key: str, value: str) -> None:
    with _connect() as conn:
        conn.execute(
            """INSERT INTO bot_state (chat_id, key, value) VALUES (?, ?, ?)
               ON CONFLICT(chat_id, key) DO UPDATE SET value = excluded.value""",
            (chat_id, key, value),
        )


# ── Subscribers ───────────────────────────────────────────

def add_subscriber(chat_id: str) -> bool:
    """Register a chat_id. Returns True if newly added, False if already exists."""
    with _connect() as conn:
        existing = conn.execute(
            "SELECT chat_id FROM subscribers WHERE chat_id = ?", (chat_id,)
        ).fetchone()
        if existing:
            return False
        conn.execute(
            "INSERT INTO subscribers (chat_id, joined_at) VALUES (?, ?)",
            (chat_id, datetime.now(timezone.utc).isoformat()),
        )
    return True


def get_subscribers() -> list[str]:
    with _connect() as conn:
        rows = conn.execute("SELECT chat_id FROM subscribers").fetchall()
    return [r["chat_id"] for r in rows]


# ── OANDA Accounts ────────────────────────────────────────

def save_oanda_account(chat_id: str, login: int, password: str, server: str, metaapi_account_id: str = "", lot_size: float = 0.02) -> None:
    with _connect() as conn:
        conn.execute(
            """INSERT INTO oanda_accounts (chat_id, login, password, server, metaapi_account_id, lot_size, connected_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(chat_id) DO UPDATE SET
                   login=excluded.login, password=excluded.password, server=excluded.server,
                   metaapi_account_id=excluded.metaapi_account_id,
                   lot_size=excluded.lot_size, connected_at=excluded.connected_at""",
            (chat_id, login, password, server, metaapi_account_id, lot_size, datetime.now(timezone.utc).isoformat()),
        )


def get_oanda_accounts() -> list:
    with _connect() as conn:
        rows = conn.execute("SELECT * FROM oanda_accounts").fetchall()
        return [dict(r) for r in rows]


def get_oanda_account(chat_id: str):
    with _connect() as conn:
        row = conn.execute("SELECT * FROM oanda_accounts WHERE chat_id = ?", (chat_id,)).fetchone()
        return dict(row) if row else None


def delete_oanda_account(chat_id: str) -> bool:
    with _connect() as conn:
        cur = conn.execute("DELETE FROM oanda_accounts WHERE chat_id = ?", (chat_id,))
        return cur.rowcount > 0


def set_oanda_lot_size(chat_id: str, lot_size: float) -> bool:
    with _connect() as conn:
        cur = conn.execute("UPDATE oanda_accounts SET lot_size = ? WHERE chat_id = ?", (lot_size, chat_id))
        return cur.rowcount > 0

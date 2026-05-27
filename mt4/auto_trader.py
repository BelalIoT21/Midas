"""
Tap 'n' Barrel Auto-Trader — V3.0

Three loops per user:
  Gold    loop — Mon–Fri, London + NY sessions (XAU/USD)
  GBP/USD loop — Mon–Fri, London + NY sessions (GBP/USD)
  BTC     loop — 24/7                          (BTC/USD)

Each loop runs a 6-step state machine per day:
  1. 4H Fractal BoS → bias
  2. Asia session extreme (marked after 08:00 UTC)
  3. Session sweep of the Asia level
  4. Price returns to 0.079 fakeout zone
  5. 5min Fractal BoS in bias direction
  6. Price enters Goldilocks zone (0.236–0.764 fib) → enter at market

Rules:
  - Max 2 trades per day (per symbol)
  - Daily loss limit: 5% of opening balance → stop all trading
  - Friday rule: close Gold/GBP trades before 20:00 UTC Friday
  - No entry during news blackout windows (Gold only)
"""
import logging
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone, date
from typing import Optional

from config import (
    DATA_DIR, OANDA_BTC_SYMBOL, BTC_MIN_UNITS, BTC_ENABLED, BTC_PAPER_TRADE,
    MAX_TRADES_PER_DAY, DAILY_LOSS_LIMIT_PCT, SIGNAL_INTERVAL_MINUTES,
    BTC_SIGNAL_INTERVAL_MINUTES, GOLD_PIP_VALUE, GOLD_LOT_CAPS, BTC_LOT_CAPS,
    NEWS_BLACKOUT_WINDOWS, ENTRY_FIB_LEVEL, RISK_PCT,
    GBPUSD_ENABLED, GBPUSD_PAPER_TRADE, OANDA_GBPUSD_SYMBOL,
    GBPUSD_PIP_VALUE, GBPUSD_LOT_CAPS,
)

logger = logging.getLogger(__name__)

from mt4.trader import (
    place_trade, fetch_balance, fetch_spread,
    fetch_open_trades, close_trade, get_trade_pnl,
    partial_close_trade, update_trade_sl,
)

_POLL_SECS         = 30
_GOLD_SIGNAL_SECS  = SIGNAL_INTERVAL_MINUTES * 60
_BTC_SIGNAL_SECS   = BTC_SIGNAL_INTERVAL_MINUTES * 60
_MAX_SPREAD_GOLD   = 1.50
_EMERGENCY_STOP    = 200.0  # force-close if unrealized loss > £200

_DB = DATA_DIR / "signals.db"


# ── Risk sizing ───────────────────────────────────────────────────────────────

def _apply_lot_cap(lot: float, balance: float, caps: list) -> float:
    """Apply the table-based lot cap for the given balance tier."""
    for min_bal, max_bal, max_lot in caps:
        if min_bal <= balance < max_bal:
            return min(max(0.01, round(lot, 2)), max_lot)
    return max(0.01, round(lot, 2))


def _calculate_lot_gold(balance: float, sl_distance: float) -> float:
    """Lot = (balance × 2%) ÷ (sl_distance × GOLD_PIP_VALUE)"""
    if sl_distance <= 0:
        return 0.01
    lot = (balance * RISK_PCT) / (sl_distance * GOLD_PIP_VALUE)
    return _apply_lot_cap(lot, balance, GOLD_LOT_CAPS)


def _calculate_lot_btc(balance: float, sl_distance: float) -> float:
    """Lot = (balance × 2%) ÷ sl_distance_in_USD"""
    if sl_distance <= 0:
        return 0.01
    lot = (balance * RISK_PCT) / sl_distance
    return _apply_lot_cap(lot, balance, BTC_LOT_CAPS)


def _calculate_lot_gbpusd(balance: float, sl_distance: float) -> float:
    """Lot = (balance × 2%) ÷ (sl_pips × GBPUSD_PIP_VALUE)
    sl_distance is in price units (e.g. 0.0015 for 15 pips).
    """
    if sl_distance <= 0:
        return 0.01
    sl_pips = sl_distance / 0.0001
    lot = (balance * RISK_PCT) / (sl_pips * GBPUSD_PIP_VALUE)
    return _apply_lot_cap(lot, balance, GBPUSD_LOT_CAPS)


# ── News blackout check ───────────────────────────────────────────────────────

def _in_news_blackout(dt: datetime | None = None) -> bool:
    """Return True if current UTC time is within a configured news blackout window."""
    if not NEWS_BLACKOUT_WINDOWS:
        return False
    if dt is None:
        dt = datetime.now(timezone.utc)
    t = dt.hour * 60 + dt.minute
    for sh, sm, eh, em in NEWS_BLACKOUT_WINDOWS:
        start = sh * 60 + sm
        end   = eh * 60 + em
        if start <= t <= end:
            return True
    return False


# ── Persistent trade log ──────────────────────────────────────────────────────

def _init_table() -> None:
    with sqlite3.connect(_DB) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS auto_trades (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id      TEXT    NOT NULL,
                symbol       TEXT    NOT NULL DEFAULT 'XAU/USD',
                direction    TEXT    NOT NULL,
                lot          REAL    NOT NULL,
                entry        REAL    NOT NULL,
                sl           REAL    NOT NULL,
                tp           REAL    NOT NULL,
                rr           REAL    NOT NULL DEFAULT 3.0,
                session      TEXT    NOT NULL,
                entry_time   REAL    NOT NULL,
                close_time   REAL,
                final_pnl    REAL,
                exit_reason  TEXT,
                asia_level   REAL    NOT NULL DEFAULT 0,
                fib_high     REAL    NOT NULL DEFAULT 0,
                fib_low      REAL    NOT NULL DEFAULT 0,
                bos_swing_high REAL  NOT NULL DEFAULT 0,
                bos_swing_low  REAL  NOT NULL DEFAULT 0
            )
        """)
        for col_def in [
            "asia_level    REAL NOT NULL DEFAULT 0",
            "fib_high      REAL NOT NULL DEFAULT 0",
            "fib_low       REAL NOT NULL DEFAULT 0",
            "bos_swing_high REAL NOT NULL DEFAULT 0",
            "bos_swing_low  REAL NOT NULL DEFAULT 0",
        ]:
            try:
                conn.execute(f"ALTER TABLE auto_trades ADD COLUMN {col_def}")
            except Exception:
                pass


def _log_open(chat_id: str, symbol: str, direction: str, lot: float,
              entry: float, sl: float, tp: float, rr: float,
              session: str, signal) -> int:
    with sqlite3.connect(_DB) as conn:
        cur = conn.execute("""
            INSERT INTO auto_trades
                (chat_id, symbol, direction, lot, entry, sl, tp, rr, session,
                 entry_time, asia_level, fib_high, fib_low, bos_swing_high, bos_swing_low)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (chat_id, symbol, direction, lot, entry, sl, tp, rr, session,
              time.time(),
              getattr(signal, "asia_level", 0),
              getattr(signal, "fib_high", 0),
              getattr(signal, "fib_low", 0),
              getattr(signal, "bos_swing_high", 0),
              getattr(signal, "bos_swing_low", 0),
              ))
        return cur.lastrowid


def _log_close(row_id: int, final_pnl: float, exit_reason: str) -> None:
    with sqlite3.connect(_DB) as conn:
        conn.execute("""
            UPDATE auto_trades
               SET close_time = ?, final_pnl = ?, exit_reason = ?
             WHERE id = ?
        """, (time.time(), final_pnl, exit_reason, row_id))


def get_stats() -> dict:
    try:
        with sqlite3.connect(_DB) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("""
                SELECT COUNT(*) AS total,
                       SUM(CASE WHEN final_pnl > 0 THEN 1 ELSE 0 END) AS wins,
                       AVG(final_pnl) AS avg_pnl
                  FROM auto_trades
                 WHERE final_pnl IS NOT NULL
            """).fetchone()
        if not row or row["total"] == 0:
            return {"total": 0, "wins": 0, "avg_pnl": 0.0}
        return {
            "total":   row["total"],
            "wins":    row["wins"] or 0,
            "avg_pnl": round(row["avg_pnl"] or 0, 2),
        }
    except Exception:
        return {"total": 0, "wins": 0, "avg_pnl": 0.0}


def get_ml_stats() -> str:
    try:
        from ml.model import get_predictor
        p = get_predictor()
        if not p.is_ready():
            return "🤖 ML: building model... (need 20+ closed trades)"
        factors = p.top_factors(5)
        lines = [f"🤖 <b>ML Model</b> — trained on {p.n_samples()} trades"]
        lines += [f"  • {name}: {imp:.0%}" for name, imp in factors]
        return "\n".join(lines)
    except Exception:
        return ""


def get_recent_trades(n: int = 10) -> list[dict]:
    try:
        with sqlite3.connect(_DB) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("""
                SELECT * FROM auto_trades ORDER BY entry_time DESC LIMIT ?
            """, (n,)).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []


# ── Tap 'n' Barrel state machine ──────────────────────────────────────────────

@dataclass
class _TapBarrelState:
    """Per-symbol per-day trading state."""
    date:            Optional[date]  = None
    # Step 1 — 4H bias
    bias:            int             = 0    # +1 BUY, -1 SELL, 0 none
    # Step 2 — Asia extreme
    asia_extreme:    Optional[float] = None
    # Step 3 — Sweep
    sweep:           Optional[dict]  = None   # {idx, sweep_price, fib_high, fib_low}
    # Step 4 — Fakeout zone reached
    fakeout_reached: bool            = False
    # Step 5 — 5min BoS
    bos:             Optional[dict]  = None   # {idx, swing_high, swing_low}
    # Daily counters
    trades_today:    int             = 0
    losses_today:    int             = 0
    opening_balance: Optional[float] = None


_states:     dict[str, dict[str, _TapBarrelState]] = {}
_state_lock  = threading.Lock()


def _get_state(chat_id: str, symbol: str, today: date) -> _TapBarrelState:
    with _state_lock:
        user_states = _states.setdefault(chat_id, {})
        state = user_states.setdefault(symbol, _TapBarrelState())
        if state.date != today:
            # New day — reset state but keep opening balance until we fetch fresh
            user_states[symbol] = _TapBarrelState(date=today)
        return user_states[symbol]


def _update_state(chat_id: str, symbol: str, **kwargs) -> None:
    with _state_lock:
        state = _states.get(chat_id, {}).get(symbol)
        if state:
            for k, v in kwargs.items():
                setattr(state, k, v)


# ── Open trade tracking ───────────────────────────────────────────────────────

@dataclass
class _OpenTrade:
    chat_id:    str
    trade_id:   str
    direction:  str
    lot:        float
    entry:      float
    sl:         float
    tp:         float
    log_id:     int
    start_time: float
    symbol:     str = "XAU/USD"
    stop_event: threading.Event = field(default_factory=threading.Event)


_active_gold:   dict[str, _OpenTrade] = {}
_active_btc:    dict[str, _OpenTrade] = {}
_active_eth:    dict[str, _OpenTrade] = {}
_active_gbpusd: dict[str, _OpenTrade] = {}
_lock_gold    = threading.Lock()
_lock_btc     = threading.Lock()
_lock_eth     = threading.Lock()
_lock_gbpusd  = threading.Lock()

_auto_stop_gold:   dict[str, threading.Event] = {}
_auto_stop_btc:    dict[str, threading.Event] = {}
_auto_stop_eth:    dict[str, threading.Event] = {}
_auto_stop_gbpusd: dict[str, threading.Event] = {}
_auto_lock = threading.Lock()


# ── Public API ────────────────────────────────────────────────────────────────

def is_active(chat_id: str) -> bool:
    return (
        chat_id in _active_gold or chat_id in _active_btc
        or chat_id in _active_eth or chat_id in _active_gbpusd
    )


def is_auto_active(chat_id: str) -> bool:
    gold_evt   = _auto_stop_gold.get(chat_id)
    btc_evt    = _auto_stop_btc.get(chat_id)
    eth_evt    = _auto_stop_eth.get(chat_id)
    gbpusd_evt = _auto_stop_gbpusd.get(chat_id)
    return (
        (gold_evt   is not None and not gold_evt.is_set())   or
        (btc_evt    is not None and not btc_evt.is_set())    or
        (eth_evt    is not None and not eth_evt.is_set())    or
        (gbpusd_evt is not None and not gbpusd_evt.is_set())
    )


def stop_auto_trader(chat_id: str) -> bool:
    stopped = False
    for stop_dict in (_auto_stop_gold, _auto_stop_btc, _auto_stop_eth, _auto_stop_gbpusd):
        evt = stop_dict.get(chat_id)
        if evt and not evt.is_set():
            evt.set()
            stopped = True
    for trade_dict in (_active_gold, _active_btc, _active_eth, _active_gbpusd):
        trade = trade_dict.get(chat_id)
        if trade:
            trade.stop_event.set()
    return stopped


def start_auto_trader(chat_id: str, account: dict, notify_fn) -> bool:
    from config import ETH_ENABLED
    _init_table()
    with _auto_lock:
        gold_running = (
            _auto_stop_gold.get(chat_id) is not None
            and not _auto_stop_gold[chat_id].is_set()
        )
        if gold_running:
            return False
        stop_gold   = threading.Event()
        stop_btc    = threading.Event()
        stop_eth    = threading.Event()
        stop_gbpusd = threading.Event()
        _auto_stop_gold[chat_id]   = stop_gold
        _auto_stop_btc[chat_id]    = stop_btc
        _auto_stop_eth[chat_id]    = stop_eth
        _auto_stop_gbpusd[chat_id] = stop_gbpusd

    threading.Thread(
        target=_gold_loop,
        args=(chat_id, account, notify_fn, stop_gold),
        daemon=True,
        name=f"gold-{chat_id}",
    ).start()

    symbols_started = ["Gold"]

    if BTC_ENABLED:
        threading.Thread(
            target=_btc_loop,
            args=(chat_id, account, notify_fn, stop_btc),
            daemon=True,
            name=f"btc-{chat_id}",
        ).start()
        symbols_started.append("BTC")
    else:
        stop_btc.set()

    if ETH_ENABLED:
        threading.Thread(
            target=_eth_loop,
            args=(chat_id, account, notify_fn, stop_eth),
            daemon=True,
            name=f"eth-{chat_id}",
        ).start()
        symbols_started.append("ETH")
    else:
        stop_eth.set()

    if GBPUSD_ENABLED:
        threading.Thread(
            target=_gbpusd_loop,
            args=(chat_id, account, notify_fn, stop_gbpusd),
            daemon=True,
            name=f"gbpusd-{chat_id}",
        ).start()
        symbols_started.append("GBP/USD")
    else:
        stop_gbpusd.set()

    logger.info(f"[auto_trader] loops started for {chat_id}: {', '.join(symbols_started)}")
    return True


# ── Gold loop ─────────────────────────────────────────────────────────────────

def _gold_loop(
    chat_id:    str,
    account:    dict,
    notify_fn,
    stop_event: threading.Event,
) -> None:
    from analysis.sessions import is_market_open, is_gold_session, is_friday_close_time
    from data.twelvedata import fetch_all_timeframes, fetch_candles
    from signals.engine import (
        detect_4h_bos, get_asia_extreme, find_sweep,
        scan_fakeout_zone, find_5min_bos, build_signal, fib_price,
    )
    from analysis.sessions import get_current_session

    logger.info(f"[gold] loop started for {chat_id}")

    while not stop_event.is_set():
        try:
            now   = datetime.now(timezone.utc)
            today = now.date()
            state = _get_state(chat_id, "XAU/USD", today)

            # ── Hard gates ────────────────────────────────────────────────────
            if not is_market_open(symbol="XAU/USD"):
                stop_event.wait(60)
                continue

            if chat_id in _active_gold:
                # Trade is open — monitor it (handled by _monitor_trade thread)
                stop_event.wait(_POLL_SECS)
                continue

            if state.trades_today >= MAX_TRADES_PER_DAY:
                stop_event.wait(300)
                continue

            # Check daily loss limit (5%)
            if state.opening_balance and state.losses_today > 0:
                balance = fetch_balance(
                    account["metaapi_account_id"],
                    account["password"],
                    account.get("server", "practice"),
                ) or state.opening_balance
                drawdown = (state.opening_balance - balance) / state.opening_balance
                if drawdown >= DAILY_LOSS_LIMIT_PCT:
                    notify_fn(
                        f"⛔ Daily loss limit hit ({drawdown:.1%}) — "
                        f"Gold trading paused for today."
                    )
                    stop_event.wait(3600)
                    continue

            # Spread check (entry sessions only)
            if is_gold_session(now):
                spread = fetch_spread(
                    account["metaapi_account_id"],
                    account["password"],
                    account.get("server", "practice"),
                    instrument="XAU_USD",
                ) or 0.0
                if spread > _MAX_SPREAD_GOLD:
                    stop_event.wait(60)
                    continue

            # ── Fetch candles ─────────────────────────────────────────────────
            try:
                from config import TIMEFRAMES
                candles  = fetch_all_timeframes(TIMEFRAMES, symbol="XAU/USD", account=account)
            except Exception as e:
                wait = 300 if "resets in" in str(e) else 30
                stop_event.wait(wait)
                continue

            df_4h = candles.get("4h")
            df_5m = candles.get("5min")
            if df_4h is None or df_5m is None:
                stop_event.wait(60)
                continue

            # ── Step 1: 4H Bias ───────────────────────────────────────────────
            if state.bias == 0:
                bias = detect_4h_bos(df_4h)
                if bias == 0:
                    stop_event.wait(_GOLD_SIGNAL_SECS)
                    continue
                _update_state(chat_id, "XAU/USD", bias=bias)
                notify_fn(
                    f"📊 <b>Gold Bias Set — {'BULLISH 📈' if bias == 1 else 'BEARISH 📉'}</b>\n"
                    f"4H fractal BoS confirmed. Watching for Asia sweep."
                )
                logger.info(f"[gold] {chat_id} bias={'BUY' if bias==1 else 'SELL'}")
                stop_event.wait(60)
                continue

            bias = state.bias

            # ── Step 2: Asia extreme (only mark after Asia session ends 08:00 UTC) ─
            if state.asia_extreme is None and now.hour >= 8:
                asia = get_asia_extreme(df_5m, bias, today)
                if asia is not None:
                    _update_state(chat_id, "XAU/USD", asia_extreme=asia)
                    level_label = "Low" if bias == 1 else "High"
                    notify_fn(
                        f"📍 <b>Asia Level Marked</b>\n"
                        f"Asia {level_label}: <b>${asia:,.2f}</b>\n"
                        f"Watching for {'below' if bias==1 else 'above'} sweep in London/NY."
                    )
                stop_event.wait(60)
                continue

            if state.asia_extreme is None:
                stop_event.wait(60)
                continue

            # ── Step 3: Sweep detection ───────────────────────────────────────
            if state.sweep is None:
                if not is_gold_session(now):
                    stop_event.wait(60)
                    continue
                sweep = find_sweep(df_5m, state.asia_extreme, bias, trade_date=today)
                if sweep is not None:
                    _update_state(chat_id, "XAU/USD", sweep=sweep)
                    notify_fn(
                        f"🎯 <b>Asia Level Swept!</b>\n"
                        f"Sweep @ <b>${sweep['sweep_price']:,.2f}</b> "
                        f"(Asia level: ${state.asia_extreme:,.2f})\n"
                        f"Fib range: ${sweep['fib_low']:,.2f} – ${sweep['fib_high']:,.2f}\n"
                        f"Watching for bounce to 0.079 fakeout zone "
                        f"(${fib_price(sweep['fib_high'], sweep['fib_low'], 0.079):,.2f}+)."
                    )
                stop_event.wait(60)
                continue

            sweep = state.sweep

            # ── Step 4: Fakeout zone check ────────────────────────────────────
            current_price = float(df_5m["close"].iloc[-1])
            if not state.fakeout_reached:
                # Scan ALL bars since the sweep so a bounce that already happened
                # is detected immediately (not just the current candle).
                if scan_fakeout_zone(df_5m, sweep, bias):
                    _update_state(chat_id, "XAU/USD", fakeout_reached=True)
                    from config import FAKEOUT_ZONE_LEVEL
                    fib_zone = fib_price(sweep["fib_high"], sweep["fib_low"], FAKEOUT_ZONE_LEVEL)
                    notify_fn(
                        f"⚡ <b>In Fakeout Zone!</b>\n"
                        f"Price reached {FAKEOUT_ZONE_LEVEL:.3f} zone "
                        f"(${fib_zone:,.2f}+).\n"
                        f"Watching for 5min fractal BoS {'up' if bias==1 else 'down'}."
                    )
                stop_event.wait(60)
                continue

            # ── Step 5: 5min BoS confirmation ─────────────────────────────────
            if state.bos is None:
                if not is_gold_session(now):
                    stop_event.wait(60)
                    continue
                bos = find_5min_bos(df_5m, bias, start_time=sweep.get("sweep_time"))
                if bos is not None:
                    _update_state(chat_id, "XAU/USD", bos=bos)
                    notify_fn(
                        f"✅ <b>5min BoS Confirmed!</b>\n"
                        f"Swing: ${bos['swing_low']:,.2f} – ${bos['swing_high']:,.2f}\n"
                        f"Watching for price to enter Goldilocks zone "
                        f"(0.5–0.618 fib)."
                    )
                stop_event.wait(30)
                continue

            bos = state.bos

            # ── Step 6: Entry in Goldilocks zone ──────────────────────────────
            if not is_gold_session(now):
                stop_event.wait(60)
                continue

            # News blackout check
            if _in_news_blackout(now):
                stop_event.wait(60)
                continue

            # Friday rule: no new entries after 20:00 UTC Friday
            if is_friday_close_time(now):
                stop_event.wait(300)
                continue

            session = get_current_session(now)
            signal = build_signal(
                current_price, bos, bias, session, "XAU/USD",
                state.asia_extreme, sweep["fib_high"], sweep["fib_low"],
            )

            if signal is None:
                stop_event.wait(30)
                continue

            # ── Set opening balance for daily loss tracking ────────────────────
            balance = fetch_balance(
                account["metaapi_account_id"],
                account["password"],
                account.get("server", "practice"),
            )
            if balance is None:
                stop_event.wait(30)
                continue

            if state.opening_balance is None:
                _update_state(chat_id, "XAU/USD", opening_balance=balance)

            # Calculate lot size
            lot = float(account.get("lot_size") or 0) or _calculate_lot_gold(
                balance, signal.sl_pips
            )

            _execute_trade(
                symbol      = "XAU/USD",
                oanda_instr = "XAU_USD",
                chat_id     = chat_id,
                account     = account,
                notify_fn   = notify_fn,
                stop_event  = stop_event,
                signal      = signal,
                active_dict = _active_gold,
                active_lock = _lock_gold,
                lot         = lot,
                state       = state,
                balance     = balance,
            )

            # Reset state for next trade (keep bias and Asia level for same day)
            _update_state(chat_id, "XAU/USD",
                sweep=None, fakeout_reached=False, bos=None)

        except Exception as e:
            logger.exception(f"[gold] loop error for {chat_id}: {e}")
            stop_event.wait(30)

    logger.info(f"[gold] loop stopped for {chat_id}")
    with _auto_lock:
        _auto_stop_gold.pop(chat_id, None)


# ── BTC weekend loop ──────────────────────────────────────────────────────────

def _btc_loop(
    chat_id:    str,
    account:    dict,
    notify_fn,
    stop_event: threading.Event,
) -> None:
    from analysis.sessions import is_btc_weekend_session
    from data.twelvedata import fetch_all_timeframes
    from signals.engine import (
        detect_4h_bos, get_asia_extreme, find_sweep,
        scan_fakeout_zone, find_5min_bos, build_signal, fib_price,
    )
    from analysis.sessions import get_current_session

    logger.info(f"[btc] loop started for {chat_id}")

    while not stop_event.is_set():
        try:
            now   = datetime.now(timezone.utc)
            today = now.date()
            state = _get_state(chat_id, "BTC/USD", today)

            # Only trade on weekends
            if not is_btc_weekend_session(now):
                stop_event.wait(300)
                continue

            if chat_id in _active_btc:
                stop_event.wait(_POLL_SECS)
                continue

            if state.trades_today >= MAX_TRADES_PER_DAY:
                stop_event.wait(300)
                continue

            # Fetch candles
            try:
                from config import TIMEFRAMES
                candles = fetch_all_timeframes(TIMEFRAMES, symbol="BTC/USD", account=account)
            except Exception as e:
                wait = 300 if "resets in" in str(e) else 30
                stop_event.wait(wait)
                continue

            df_4h = candles.get("4h")
            df_5m = candles.get("5min")
            if df_4h is None or df_5m is None:
                stop_event.wait(60)
                continue

            current_price = float(df_5m["close"].iloc[-1])

            # Step 1: Bias
            if state.bias == 0:
                bias = detect_4h_bos(df_4h)
                if bias == 0:
                    stop_event.wait(_BTC_SIGNAL_SECS)
                    continue
                _update_state(chat_id, "BTC/USD", bias=bias)
                notify_fn(
                    f"₿ <b>BTC Bias — {'BULLISH 📈' if bias==1 else 'BEARISH 📉'}</b>\n"
                    f"4H fractal BoS confirmed. Watching for sweep."
                )
                stop_event.wait(60)
                continue

            bias = state.bias

            # Step 2: Asia extreme
            if state.asia_extreme is None and now.hour >= 8:
                asia = get_asia_extreme(df_5m, bias, today)
                if asia is not None:
                    _update_state(chat_id, "BTC/USD", asia_extreme=asia)
                stop_event.wait(60)
                continue

            if state.asia_extreme is None:
                stop_event.wait(60)
                continue

            # Step 3: Sweep
            if state.sweep is None:
                sweep = find_sweep(df_5m, state.asia_extreme, bias, trade_date=today)
                if sweep is not None:
                    _update_state(chat_id, "BTC/USD", sweep=sweep)
                    notify_fn(
                        f"₿ <b>BTC Sweep Detected!</b>\n"
                        f"@ ${sweep['sweep_price']:,.2f} "
                        f"(Asia level: ${state.asia_extreme:,.2f})"
                    )
                stop_event.wait(60)
                continue

            sweep = state.sweep

            # Step 4: Fakeout zone — scan all bars since the sweep so a bounce
            # that already happened is caught immediately on mid-day /trade start.
            if not state.fakeout_reached:
                if scan_fakeout_zone(df_5m, sweep, bias):
                    _update_state(chat_id, "BTC/USD", fakeout_reached=True)
                stop_event.wait(60)
                continue

            # Step 5: 5min BoS
            if state.bos is None:
                bos = find_5min_bos(df_5m, bias, start_time=sweep.get("sweep_time"))
                if bos is not None:
                    _update_state(chat_id, "BTC/USD", bos=bos)
                stop_event.wait(30)
                continue

            bos = state.bos

            # Step 6: Entry
            session = get_current_session(now)
            signal  = build_signal(
                current_price, bos, bias, session, "BTC/USD",
                state.asia_extreme, sweep["fib_high"], sweep["fib_low"],
            )

            if signal is None:
                stop_event.wait(30)
                continue

            balance = fetch_balance(
                account["metaapi_account_id"],
                account["password"],
                account.get("server", "practice"),
            )
            if balance is None:
                stop_event.wait(30)
                continue

            if state.opening_balance is None:
                _update_state(chat_id, "BTC/USD", opening_balance=balance)

            if BTC_PAPER_TRADE:
                lot = 0.0
                _execute_paper_btc(
                    chat_id=chat_id, notify_fn=notify_fn,
                    stop_event=stop_event, signal=signal, state=state,
                )
            else:
                lot = _calculate_lot_btc(balance, signal.sl_pips)
                _execute_trade(
                    symbol      = "BTC/USD",
                    oanda_instr = OANDA_BTC_SYMBOL,
                    chat_id     = chat_id,
                    account     = account,
                    notify_fn   = notify_fn,
                    stop_event  = stop_event,
                    signal      = signal,
                    active_dict = _active_btc,
                    active_lock = _lock_btc,
                    lot         = lot,
                    state       = state,
                    balance     = balance,
                    is_btc      = True,
                )

            _update_state(chat_id, "BTC/USD", sweep=None, fakeout_reached=False, bos=None)

        except Exception as e:
            logger.exception(f"[btc] loop error for {chat_id}: {e}")
            stop_event.wait(30)

    logger.info(f"[btc] loop stopped for {chat_id}")
    with _auto_lock:
        _auto_stop_btc.pop(chat_id, None)


# ── ETH loop (24/7 — same logic as BTC) ──────────────────────────────────────

def _eth_loop(
    chat_id:    str,
    account:    dict,
    notify_fn,
    stop_event: threading.Event,
) -> None:
    from config import ETH_PAPER_TRADE, OANDA_ETH_SYMBOL, ETH_LOT_CAPS
    from data.twelvedata import fetch_all_timeframes
    from signals.engine import (
        detect_4h_bos, get_asia_extreme, find_sweep,
        scan_fakeout_zone, find_5min_bos, build_signal,
    )
    from analysis.sessions import get_current_session

    def _calc_lot_eth(bal: float, sl_dist: float) -> float:
        from config import RISK_PCT
        if sl_dist <= 0:
            return 0.01
        lot = (bal * RISK_PCT) / sl_dist
        for min_b, max_b, max_l in ETH_LOT_CAPS:
            if min_b <= bal < max_b:
                return min(max(0.01, round(lot, 2)), max_l)
        return max(0.01, round(lot, 2))

    logger.info(f"[eth] loop started for {chat_id}")

    while not stop_event.is_set():
        try:
            now   = datetime.now(timezone.utc)
            today = now.date()
            state = _get_state(chat_id, "ETH/USD", today)

            if chat_id in _active_eth:
                stop_event.wait(_POLL_SECS)
                continue

            if state.trades_today >= MAX_TRADES_PER_DAY:
                stop_event.wait(300)
                continue

            try:
                from config import TIMEFRAMES
                candles = fetch_all_timeframes(TIMEFRAMES, symbol="ETH/USD", account=account)
            except Exception as e:
                wait = 300 if "resets in" in str(e) else 30
                stop_event.wait(wait)
                continue

            df_4h = candles.get("4h")
            df_5m = candles.get("5min")
            if df_4h is None or df_5m is None:
                stop_event.wait(60)
                continue

            balance = fetch_balance(
                account["metaapi_account_id"], account["password"],
                account.get("server", "practice"),
            )
            if balance is None:
                stop_event.wait(30)
                continue

            if state.opening_balance is None:
                _update_state(chat_id, "ETH/USD", opening_balance=balance)

            # Run full 6-step logic regardless of paper/live mode
            bias = detect_4h_bos(df_4h)
            if bias == 0:
                stop_event.wait(_BTC_SIGNAL_SECS)
                continue

            asia = get_asia_extreme(df_5m, bias, today)
            if asia is None:
                stop_event.wait(_BTC_SIGNAL_SECS)
                continue

            sweep = find_sweep(df_5m, asia, bias, trade_date=today)
            if sweep is None:
                stop_event.wait(_BTC_SIGNAL_SECS)
                continue

            current_price = float(df_5m["close"].iloc[-1])
            if not scan_fakeout_zone(df_5m, sweep, bias):
                stop_event.wait(_BTC_SIGNAL_SECS)
                continue

            bos = find_5min_bos(df_5m, bias, start_time=sweep.get("sweep_time"))
            if bos is None:
                stop_event.wait(_BTC_SIGNAL_SECS)
                continue

            session = get_current_session(now)
            signal = build_signal(
                current_price, bos, bias, session, "ETH/USD",
                asia, sweep["fib_high"], sweep["fib_low"],
            )
            if signal is None:
                stop_event.wait(_BTC_SIGNAL_SECS)
                continue

            if ETH_PAPER_TRADE:
                _execute_paper_btc(
                    chat_id=chat_id, notify_fn=notify_fn,
                    stop_event=stop_event, signal=signal, state=state,
                )
            else:
                lot = _calc_lot_eth(balance, signal.sl_pips)
                _execute_trade(
                    symbol      = "ETH/USD",
                    oanda_instr = OANDA_ETH_SYMBOL,
                    chat_id     = chat_id,
                    account     = account,
                    notify_fn   = notify_fn,
                    stop_event  = stop_event,
                    signal      = signal,
                    active_dict = _active_eth,
                    active_lock = _lock_eth,
                    lot         = lot,
                    state       = state,
                    balance     = balance,
                    is_btc      = True,
                )

            _update_state(chat_id, "ETH/USD", sweep=None, fakeout_reached=False, bos=None)

        except Exception as e:
            logger.exception(f"[eth] loop error for {chat_id}: {e}")
            stop_event.wait(30)

    logger.info(f"[eth] loop stopped for {chat_id}")
    with _auto_lock:
        _auto_stop_eth.pop(chat_id, None)


# ── GBP/USD weekday loop ──────────────────────────────────────────────────────

def _gbpusd_loop(
    chat_id:    str,
    account:    dict,
    notify_fn,
    stop_event: threading.Event,
) -> None:
    from analysis.sessions import is_market_open, is_gold_session, is_friday_close_time
    from data.twelvedata import fetch_all_timeframes
    from signals.engine import (
        detect_4h_bos, get_asia_extreme, find_sweep,
        scan_fakeout_zone, find_5min_bos, build_signal, fib_price,
    )
    from analysis.sessions import get_current_session

    logger.info(f"[gbpusd] loop started for {chat_id}")

    while not stop_event.is_set():
        try:
            now   = datetime.now(timezone.utc)
            today = now.date()
            state = _get_state(chat_id, "GBP/USD", today)

            if not is_market_open(symbol="GBP/USD"):
                stop_event.wait(60)
                continue

            if chat_id in _active_gbpusd:
                stop_event.wait(_POLL_SECS)
                continue

            if state.trades_today >= MAX_TRADES_PER_DAY:
                stop_event.wait(300)
                continue

            if state.opening_balance and state.losses_today > 0:
                balance = fetch_balance(
                    account["metaapi_account_id"],
                    account["password"],
                    account.get("server", "practice"),
                ) or state.opening_balance
                drawdown = (state.opening_balance - balance) / state.opening_balance
                if drawdown >= DAILY_LOSS_LIMIT_PCT:
                    notify_fn(
                        f"⛔ Daily loss limit hit ({drawdown:.1%}) — "
                        f"GBP/USD trading paused for today."
                    )
                    stop_event.wait(3600)
                    continue

            try:
                from config import TIMEFRAMES
                candles = fetch_all_timeframes(TIMEFRAMES, symbol="GBP/USD", account=account)
            except Exception as e:
                wait = 300 if "resets in" in str(e) else 30
                stop_event.wait(wait)
                continue

            df_4h = candles.get("4h")
            df_5m = candles.get("5min")
            if df_4h is None or df_5m is None:
                stop_event.wait(60)
                continue

            # Step 1: 4H Bias
            if state.bias == 0:
                bias = detect_4h_bos(df_4h)
                if bias == 0:
                    stop_event.wait(_GOLD_SIGNAL_SECS)
                    continue
                _update_state(chat_id, "GBP/USD", bias=bias)
                notify_fn(
                    f"💷 <b>GBP/USD Bias — {'BULLISH 📈' if bias==1 else 'BEARISH 📉'}</b>\n"
                    f"4H fractal BoS confirmed. Watching for Asia sweep."
                )
                logger.info(f"[gbpusd] {chat_id} bias={'BUY' if bias==1 else 'SELL'}")
                stop_event.wait(60)
                continue

            bias = state.bias

            # Step 2: Asia extreme
            if state.asia_extreme is None and now.hour >= 8:
                asia = get_asia_extreme(df_5m, bias, today)
                if asia is not None:
                    _update_state(chat_id, "GBP/USD", asia_extreme=asia)
                    level_label = "Low" if bias == 1 else "High"
                    notify_fn(
                        f"📍 <b>GBP/USD Asia Level Marked</b>\n"
                        f"Asia {level_label}: <b>{asia:.5f}</b>\n"
                        f"Watching for {'below' if bias==1 else 'above'} sweep in London/NY."
                    )
                stop_event.wait(60)
                continue

            if state.asia_extreme is None:
                stop_event.wait(60)
                continue

            # Step 3: Sweep
            if state.sweep is None:
                if not is_gold_session(now):
                    stop_event.wait(60)
                    continue
                sweep = find_sweep(df_5m, state.asia_extreme, bias, trade_date=today)
                if sweep is not None:
                    _update_state(chat_id, "GBP/USD", sweep=sweep)
                    notify_fn(
                        f"🎯 <b>GBP/USD Asia Level Swept!</b>\n"
                        f"Sweep @ <b>{sweep['sweep_price']:.5f}</b> "
                        f"(Asia level: {state.asia_extreme:.5f})\n"
                        f"Fib range: {sweep['fib_low']:.5f} – {sweep['fib_high']:.5f}\n"
                        f"Watching for bounce to 0.079 fakeout zone "
                        f"({fib_price(sweep['fib_high'], sweep['fib_low'], 0.079):.5f}+)."
                    )
                stop_event.wait(60)
                continue

            sweep = state.sweep

            # Step 4: Fakeout zone — scan all bars since the sweep so a bounce
            # that already happened is detected immediately on mid-day /trade start.
            current_price = float(df_5m["close"].iloc[-1])
            if not state.fakeout_reached:
                if scan_fakeout_zone(df_5m, sweep, bias):
                    _update_state(chat_id, "GBP/USD", fakeout_reached=True)
                    from config import FAKEOUT_ZONE_LEVEL
                    fib_zone = fib_price(sweep["fib_high"], sweep["fib_low"], FAKEOUT_ZONE_LEVEL)
                    notify_fn(
                        f"⚡ <b>GBP/USD In Fakeout Zone!</b>\n"
                        f"Price reached {FAKEOUT_ZONE_LEVEL:.3f} zone "
                        f"({fib_zone:.5f}+).\n"
                        f"Watching for 5min fractal BoS {'up' if bias==1 else 'down'}."
                    )
                stop_event.wait(60)
                continue

            # Step 5: 5min BoS
            if state.bos is None:
                if not is_gold_session(now):
                    stop_event.wait(60)
                    continue
                bos = find_5min_bos(df_5m, bias, start_time=sweep.get("sweep_time"))
                if bos is not None:
                    _update_state(chat_id, "GBP/USD", bos=bos)
                    notify_fn(
                        f"✅ <b>GBP/USD 5min BoS Confirmed!</b>\n"
                        f"Swing: {bos['swing_low']:.5f} – {bos['swing_high']:.5f}\n"
                        f"Watching for price to enter Goldilocks zone (0.236–0.764 fib)."
                    )
                stop_event.wait(30)
                continue

            bos = state.bos

            # Step 6: Entry
            if not is_gold_session(now):
                stop_event.wait(60)
                continue

            if is_friday_close_time(now):
                stop_event.wait(300)
                continue

            session = get_current_session(now)
            signal = build_signal(
                current_price, bos, bias, session, "GBP/USD",
                state.asia_extreme, sweep["fib_high"], sweep["fib_low"],
            )

            if signal is None:
                stop_event.wait(30)
                continue

            balance = fetch_balance(
                account["metaapi_account_id"],
                account["password"],
                account.get("server", "practice"),
            )
            if balance is None:
                stop_event.wait(30)
                continue

            if state.opening_balance is None:
                _update_state(chat_id, "GBP/USD", opening_balance=balance)

            if GBPUSD_PAPER_TRADE:
                _execute_paper_gbpusd(
                    chat_id=chat_id, notify_fn=notify_fn,
                    stop_event=stop_event, signal=signal, state=state,
                )
            else:
                lot = float(account.get("lot_size") or 0) or _calculate_lot_gbpusd(
                    balance, signal.sl_pips
                )
                _execute_trade(
                    symbol      = "GBP/USD",
                    oanda_instr = OANDA_GBPUSD_SYMBOL,
                    chat_id     = chat_id,
                    account     = account,
                    notify_fn   = notify_fn,
                    stop_event  = stop_event,
                    signal      = signal,
                    active_dict = _active_gbpusd,
                    active_lock = _lock_gbpusd,
                    lot         = lot,
                    state       = state,
                    balance     = balance,
                )

            _update_state(chat_id, "GBP/USD",
                sweep=None, fakeout_reached=False, bos=None)

        except Exception as e:
            logger.exception(f"[gbpusd] loop error for {chat_id}: {e}")
            stop_event.wait(30)

    logger.info(f"[gbpusd] loop stopped for {chat_id}")
    with _auto_lock:
        _auto_stop_gbpusd.pop(chat_id, None)


def _execute_paper_gbpusd(
    chat_id:    str,
    notify_fn,
    stop_event: threading.Event,
    signal,
    state:      _TapBarrelState,
) -> None:
    from data.twelvedata import fetch_price

    with _state_lock:
        state.trades_today += 1

    log_id = _log_open(
        chat_id="paper", symbol="GBP/USD", direction=signal.direction,
        lot=0.0, entry=signal.entry, sl=signal.sl, tp=signal.tp,
        rr=2.0, session=signal.session_name, signal=signal,
    )

    notify_fn(
        f"📊 <b>Paper Trade #{log_id} — 💷 GBP/USD (Demo)</b>\n"
        f"{'━'*26}\n"
        f"Direction:  <b>{signal.direction}</b>\n"
        f"Entry:      <b>{signal.entry:.5f}</b>\n"
        f"SL:         {signal.sl:.5f}\n"
        f"TP (2RR):   {signal.tp:.5f}\n"
        f"Session:    {signal.session_emoji} {signal.session_name}\n"
        f"📝 Paper trade — tracking SL/TP on real GBP/USD price"
    )

    with _lock_gbpusd:
        _active_gbpusd[chat_id] = _OpenTrade(
            chat_id=chat_id, trade_id=f"paper-gbpusd-{log_id}",
            direction=signal.direction, entry=signal.entry,
            sl=signal.sl, tp=signal.tp, lot=0.0,
            log_id=log_id, start_time=time.time(), symbol="GBP/USD",
        )

    start_time = time.time()
    won = False

    try:
        while not stop_event.is_set():
            stop_event.wait(_POLL_SECS)
            current_price = fetch_price("GBP/USD")
            if current_price is None:
                continue

            time_held = time.time() - start_time
            sl_hit = (
                (signal.direction == "BUY"  and current_price <= signal.sl) or
                (signal.direction == "SELL" and current_price >= signal.sl)
            )
            tp_hit = (
                (signal.direction == "BUY"  and current_price >= signal.tp) or
                (signal.direction == "SELL" and current_price <= signal.tp)
            )

            if tp_hit or sl_hit:
                price_move = current_price - signal.entry
                if signal.direction == "SELL":
                    price_move = -price_move
                final_pnl = round(price_move, 5)
                won       = final_pnl > 0
                icon      = "✅" if won else "❌"
                reason    = "profit" if won else "stoploss"
                _log_close(log_id, final_pnl, reason)
                notify_fn(
                    f"{icon} <b>Paper Trade #{log_id} Closed — 💷 GBP/USD</b>\n"
                    f"P&L: {'+'if won else ''}{final_pnl:.5f}  |  Held: {time_held/60:.0f}m\n"
                    f"{signal.direction} @ {signal.entry:.5f} → {current_price:.5f}"
                )
                break
    finally:
        with _lock_gbpusd:
            _active_gbpusd.pop(chat_id, None)

    if not won:
        with _state_lock:
            state.losses_today += 1

    stop_event.wait(60)


# ── Trade execution ───────────────────────────────────────────────────────────

def _execute_trade(
    symbol:      str,
    oanda_instr: str,
    chat_id:     str,
    account:     dict,
    notify_fn,
    stop_event:  threading.Event,
    signal,
    active_dict: dict,
    active_lock: threading.Lock,
    lot:         float,
    state:       _TapBarrelState,
    balance:     float = 0.0,
    is_btc:      bool = False,
) -> None:
    units_ov = int(account.get("btc_units") or 0) if is_btc else 0

    trade_id = place_trade(
        oanda_account_id = account["metaapi_account_id"],
        direction        = signal.direction,
        sl               = signal.sl,
        tp               = signal.tp,
        lot_size         = lot,
        api_key          = account["password"],
        env_flag         = account.get("server", "practice"),
        instrument       = oanda_instr,
        units_override   = units_ov,
    )

    if not trade_id:
        stop_event.wait(30)
        return

    with _state_lock:
        state.trades_today += 1

    sym_label = "₿ BTC" if is_btc else "🥇 Gold"
    log_id = _log_open(
        chat_id   = chat_id,
        symbol    = symbol,
        direction = signal.direction,
        lot       = lot if not is_btc else round(units_ov / 100, 4),
        entry     = signal.entry,
        sl        = signal.sl,
        tp        = signal.tp,
        rr        = signal.rr,
        session   = signal.session_name,
        signal    = signal,
    )

    plus  = "+" if signal.direction == "BUY" else "-"
    minus = "-" if signal.direction == "BUY" else "+"
    notify_fn(
        f"📊 <b>Trade #{log_id} — {sym_label}</b>\n"
        f"{'━'*26}\n"
        f"Direction:  <b>{signal.direction}</b>  ({signal.bias})\n"
        f"Entry:      <b>${signal.entry:,.2f}</b>\n"
        f"SL:         ${signal.sl:,.2f}  ({minus}${signal.sl_pips:.2f})\n"
        f"TP (2RR):   ${signal.tp:,.2f}  ({plus}${signal.tp_pips:.2f})\n"
        f"{'Lot:' if not is_btc else 'Units:':<12}{lot if not is_btc else units_ov}\n"
        f"Session:    {signal.session_emoji} {signal.session_name}\n"
        f"Asia level: ${signal.asia_level:,.2f}  |  Sweep fib: ${signal.fib_low:,.2f}–${signal.fib_high:,.2f}\n"
        f"Trade {state.trades_today}/{MAX_TRADES_PER_DAY} today"
    )

    won = _monitor_trade(
        chat_id     = chat_id,
        account     = account,
        notify_fn   = notify_fn,
        stop_event  = stop_event,
        trade_id    = trade_id,
        log_id      = log_id,
        signal      = signal,
        lot         = lot,
        active_dict = active_dict,
        active_lock = active_lock,
        symbol      = symbol,
        oanda_instr = oanda_instr,
        state       = state,
        balance     = balance,
    )

    if not won:
        with _state_lock:
            state.losses_today += 1

    stop_event.wait(60)


# ── BTC paper trading ─────────────────────────────────────────────────────────

def _execute_paper_btc(
    chat_id:    str,
    notify_fn,
    stop_event: threading.Event,
    signal,
    state:      _TapBarrelState,
) -> None:
    from data.twelvedata import fetch_price

    with _state_lock:
        state.trades_today += 1

    log_id = _log_open(
        chat_id="paper", symbol="BTC/USD", direction=signal.direction,
        lot=0.0, entry=signal.entry, sl=signal.sl, tp=signal.tp,
        rr=3.0, session=signal.session_name, signal=signal,
    )

    notify_fn(
        f"📊 <b>Paper Trade #{log_id} — ₿ BTC (Demo)</b>\n"
        f"{'━'*26}\n"
        f"Direction:  <b>{signal.direction}</b>\n"
        f"Entry:      <b>${signal.entry:,.2f}</b>\n"
        f"SL:         ${signal.sl:,.2f}\n"
        f"TP (3RR):   ${signal.tp:,.2f}\n"
        f"Session:    {signal.session_emoji} {signal.session_name}\n"
        f"📝 Paper trade — tracking SL/TP on real BTC price"
    )

    with _lock_btc:
        _active_btc[chat_id] = _OpenTrade(
            chat_id=chat_id, trade_id=f"paper-{log_id}",
            direction=signal.direction, entry=signal.entry,
            sl=signal.sl, tp=signal.tp, lot=0.0,
            log_id=log_id, start_time=time.time(), symbol="BTC/USD",
        )

    start_time = time.time()
    won = False

    try:
        while not stop_event.is_set():
            stop_event.wait(_POLL_SECS)
            current_price = fetch_price("BTC/USD")
            if current_price is None:
                continue

            time_held = time.time() - start_time
            sl_hit = (
                (signal.direction == "BUY"  and current_price <= signal.sl) or
                (signal.direction == "SELL" and current_price >= signal.sl)
            )
            tp_hit = (
                (signal.direction == "BUY"  and current_price >= signal.tp) or
                (signal.direction == "SELL" and current_price <= signal.tp)
            )

            if tp_hit or sl_hit:
                price_move = current_price - signal.entry
                if signal.direction == "SELL":
                    price_move = -price_move
                final_pnl = round(price_move, 2)
                won       = final_pnl > 0
                icon      = "✅" if won else "❌"
                reason    = "profit" if won else "stoploss"
                _log_close(log_id, final_pnl, reason)
                notify_fn(
                    f"{icon} <b>Paper Trade #{log_id} Closed — ₿ BTC</b>\n"
                    f"P&L: {'+'if won else ''}${final_pnl:.2f}  |  Held: {time_held/60:.0f}m\n"
                    f"{signal.direction} @ ${signal.entry:,.2f} → ${current_price:,.2f}"
                )
                break
    finally:
        with _lock_btc:
            _active_btc.pop(chat_id, None)

    if not won:
        with _state_lock:
            state.losses_today += 1

    stop_event.wait(60)


# ── Trade monitor ─────────────────────────────────────────────────────────────

def _monitor_trade(
    chat_id:     str,
    account:     dict,
    notify_fn,
    stop_event:  threading.Event,
    trade_id:    str,
    log_id:      int,
    signal,
    lot:         float,
    active_dict: dict,
    active_lock: threading.Lock,
    symbol:      str,
    oanda_instr: str,
    state:       _TapBarrelState,
    balance:     float = 0.0,
) -> bool:
    from analysis.sessions import is_friday_close_time

    account_id  = account["metaapi_account_id"]
    api_key     = account["password"]
    env_flag    = account.get("server", "practice")
    fetch_fails = 0
    start_time  = time.time()
    # Partial-close state: when unrealized P&L reaches 1R, close half and move SL to BE.
    # 1R is always balance × RISK_PCT by lot-sizing construction.
    one_r_pl     = balance * RISK_PCT if balance > 0 else None
    partial_done = False

    open_trade = _OpenTrade(
        chat_id=chat_id, trade_id=trade_id, direction=signal.direction,
        lot=lot, entry=signal.entry, sl=signal.sl, tp=signal.tp,
        log_id=log_id, start_time=start_time, symbol=symbol,
    )

    with active_lock:
        active_dict[chat_id] = open_trade

    try:
        while not stop_event.is_set() and not open_trade.stop_event.is_set():
            stop_event.wait(_POLL_SECS)

            # Friday rule: force-close Gold before 20:00 UTC
            if symbol == "XAU/USD" and is_friday_close_time():
                close_trade(trade_id, account_id, api_key, env_flag)
                final_pnl = get_trade_pnl(trade_id, account_id, api_key, env_flag) or 0.0
                _log_close(log_id, final_pnl, "friday_close")
                notify_fn(
                    f"📅 <b>Friday Close — Trade #{log_id}</b>\n"
                    f"Closed before weekend.  P&L: £{final_pnl:.2f}"
                )
                return final_pnl > 0

            trades = fetch_open_trades([trade_id], account_id, api_key, env_flag)

            if trades is None:
                fetch_fails += 1
                if fetch_fails >= 10:
                    notify_fn("⚠️ OANDA unreachable for 5 min — check your account manually.")
                    _log_close(log_id, 0.0, "oanda_timeout")
                    return False
                continue

            fetch_fails = 0
            time_held   = time.time() - start_time

            # ── Partial-close at 1R ───────────────────────────────────────────
            if (not partial_done and one_r_pl is not None
                    and len(trades) > 0):
                unrl = trades[0].get("unrealized_pl", 0.0)
                if unrl >= one_r_pl:
                    units_now = abs(trades[0].get("units", 0))
                    units_to_close = max(1, units_now // 2)
                    ok_close = partial_close_trade(
                        trade_id, account_id, api_key, env_flag,
                        units_to_close=units_to_close,
                    )
                    ok_sl = update_trade_sl(
                        trade_id, account_id, api_key, env_flag,
                        new_sl=signal.entry,
                    )
                    if ok_close and ok_sl:
                        partial_done = True
                        notify_fn(
                            f"🔒 <b>Partial Close — Trade #{log_id}</b>\n"
                            f"Closed {units_to_close}u at +1R.  SL moved to breakeven.\n"
                            f"Remaining half rides to TP or closes at entry."
                        )
                    else:
                        logger.warning(
                            f"[monitor] partial close or SL update failed on trade {trade_id}"
                        )

            if len(trades) == 0:
                final_pnl = get_trade_pnl(trade_id, account_id, api_key, env_flag) or 0.0
                won       = final_pnl > 0
                icon      = "✅" if won else "❌"
                reason    = "profit" if won else "stoploss"
                _log_close(log_id, final_pnl, reason)
                sym_label = "₿ BTC" if symbol == "BTC/USD" else "🥇 Gold"
                notify_fn(
                    f"{icon} <b>Trade #{log_id} Closed — {sym_label}</b>\n"
                    f"P&L: {'+'if won else ''}£{final_pnl:.2f}  |  "
                    f"Held: {time_held/60:.0f}m\n"
                    f"{signal.direction} @ ${signal.entry:,.2f}"
                )
                return won

            # Emergency stop
            pnl = trades[0].get("unrealized_pl", 0.0)
            if pnl <= -_EMERGENCY_STOP:
                close_trade(trade_id, account_id, api_key, env_flag)
                _log_close(log_id, pnl, "emergency_stop")
                notify_fn(
                    f"🚨 <b>Emergency Stop — Trade #{log_id}</b>\n"
                    f"P&L: £{pnl:.2f}  |  Held: {time_held/60:.0f}m\n"
                    f"{signal.direction} force-closed. Check OANDA."
                )
                return False

        # Manual cancel
        close_trade(trade_id, account_id, api_key, env_flag)
        final_pnl = get_trade_pnl(trade_id, account_id, api_key, env_flag) or 0.0
        _log_close(log_id, final_pnl, "manual")
        notify_fn(f"⏹ Trade #{log_id} cancelled manually.  P&L: £{final_pnl:.2f}")
        return False

    finally:
        with active_lock:
            active_dict.pop(chat_id, None)

"""
Telegram bot command handlers.
"""
import logging
from telegram import Update, BotCommand
from telegram.ext import (
    Application, CommandHandler, ContextTypes,
    ConversationHandler, MessageHandler, filters,
)

from config import TELEGRAM_BOT_TOKEN, TIMEFRAMES
from db.store import (
    get_bot_state, set_bot_state,
    add_subscriber,
    save_oanda_account, get_oanda_account, delete_oanda_account, set_oanda_lot_size,
)

logger = logging.getLogger(__name__)

_WAIT_API_KEY, _WAIT_ACCOUNT_ID = range(2)


def _log_cmd(update: Update, result: str = "") -> None:
    chat_id = update.effective_chat.id
    cmd     = update.message.text.split()[0] if update.message and update.message.text else "?"
    if result:
        logger.info(f"[cmd] {cmd} | chat={chat_id} | {result}")
    else:
        logger.info(f"[cmd] {cmd} | chat={chat_id}")


# ── /start ────────────────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = str(update.effective_chat.id)
    is_new  = add_subscriber(chat_id)
    acct    = get_oanda_account(chat_id)
    _log_cmd(update, f"new={is_new} oanda={'yes' if acct else 'no'}")

    greeting = "Welcome to MidasXAU! You are now subscribed." if is_new else "Welcome back."

    connect_block = (
        "✅ <b>OANDA connected</b> — use /trade to start trend-following."
        if acct else
        "⚡ <b>No account linked.</b>\nUse /connect to link your OANDA account first."
    )

    await update.message.reply_text(
        f"🥇 <b>Midas — Tap 'n' Barrel + SMC Bot</b>\n"
        f"{'━'*32}\n\n"
        f"{greeting}\n\n"
        "<b>Strategy:</b>\n"
        "• 4H fractal BoS sets daily bias\n"
        "• Marks Asia session extreme, waits for London/NY sweep\n"
        "• Enters in 0.236–0.764 fib zone after 5min BoS confirmation\n"
        "• SL at fib 1.1 · TP at 2RR\n"
        "• 🥇 Gold: Mon–Fri 08:00–21:00 UTC\n"
        "• 💷 GBP/USD + 💶 EUR/USD: Mon–Fri 08:00–21:00 UTC\n"
        "• ₿ Bitcoin: 24/7\n"
        "• 💠 ETH: 24/7\n"
        "• Max 2 trades/day · 2% risk · 5% daily loss limit\n\n"
        f"{connect_block}\n\n"
        "Type /help to see all commands.",
        parse_mode="HTML",
    )


# ── /help ─────────────────────────────────────────────────────────────────────

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = str(update.effective_chat.id)
    acct    = get_oanda_account(chat_id)
    paused  = get_bot_state(chat_id, "paused") == "1"
    status_icon = "⏸ PAUSED" if paused else "▶ RUNNING"
    _log_cmd(update, f"status={status_icon} oanda={'yes' if acct else 'no'}")

    if acct:
        oanda_block = (
            "✅ <b>OANDA connected</b>\n"
            "/oandastatus — connection &amp; balance\n"
            "/setlot      — override lot size  e.g. /setlot 0.02\n"
            "/disconnect  — unlink account"
        )
    else:
        oanda_block = (
            "⚡ <b>No account linked</b>\n"
            "/connect — link your OANDA account"
        )

    await update.message.reply_text(
        f"🥇 <b>Midas — Tap 'n' Barrel + SMC</b>  [{status_icon}]\n"
        f"{'━'*32}\n\n"

        "📈 <b>Auto-Trader</b>\n"
        "/trade          — start auto-trader (Gold + GBP/USD + EUR/USD weekdays, BTC 24/7)\n"
        "/trade_cancel   — stop &amp; close open trade\n"
        "/tradestats     — wins, losses, avg P&amp;L\n\n"

        "📊 <b>Market</b>\n"
        "/scan [symbol] — strategy steps (gold/gbp/btc/eth/us30/eur)\n"
        "/analyze [sym] — day review: price move, steps, signals\n"
        "/price         — live XAU/USD price\n"
        "/chart         — 1H candlestick chart\n"
        "/status        — multi-timeframe snapshot\n\n"

        f"🔗 <b>OANDA</b>\n"
        f"{oanda_block}\n\n"

        "⚙️ <b>Controls</b>\n"
        "/pause         — pause all auto-trading\n"
        "/resume        — resume auto-trading",
        parse_mode="HTML",
    )


# ── /connect ──────────────────────────────────────────────────────────────────

async def connect_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    _log_cmd(update, "step=1 waiting for API key")
    await update.message.reply_text(
        "🔗 <b>Connect your OANDA account</b>\n\n"
        "Go to <b>oanda.com → My Account → API Access</b> to get your key.\n\n"
        "Type /cancel at any time to stop.\n\n"
        "<b>Step 1/2</b> — Send your OANDA API key:",
        parse_mode="HTML",
    )
    return _WAIT_API_KEY


async def connect_api_key(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["oanda_api_key"] = update.message.text.strip()
    await update.message.reply_text(
        "<b>Step 2/2</b> — Send your OANDA Account ID\n\n"
        "<i>Find it in My Account → Manage Funds. Looks like: 001-001-1234567-001</i>",
        parse_mode="HTML",
    )
    return _WAIT_ACCOUNT_ID


async def connect_account_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    oanda_account_id = update.message.text.strip()
    api_key  = context.user_data["oanda_api_key"]
    chat_id  = str(update.effective_chat.id)

    import requests as _req
    headers = {"Authorization": f"Bearer {api_key}"}

    env_flag = None
    currency = ""
    for env, base in [("live", "https://api-fxtrade.oanda.com"),
                      ("practice", "https://api-fxpractice.oanda.com")]:
        try:
            resp = _req.get(f"{base}/v3/accounts/{oanda_account_id}/summary",
                            headers=headers, timeout=10)
            if resp.ok:
                env_flag = env
                currency = resp.json().get("account", {}).get("currency", "")
                break
        except Exception:
            continue

    if not env_flag:
        logger.warning(f"[cmd] /connect | chat={update.effective_chat.id} | FAILED — bad API key or account ID")
        await update.message.reply_text(
            "❌ <b>Connection failed.</b>\n\n"
            "Check your API key and account ID, then try /connect again.",
            parse_mode="HTML",
        )
        return ConversationHandler.END

    save_oanda_account(chat_id, 0, api_key, env_flag, oanda_account_id)
    add_subscriber(chat_id)

    label = "Live" if env_flag == "live" else "Demo"
    logger.info(f"[cmd] /connect | chat={update.effective_chat.id} | account={oanda_account_id} env={label} currency={currency}")
    await update.message.reply_text(
        f"✅ <b>OANDA Connected ({label})</b>\n\n"
        f"Account: <code>{oanda_account_id}</code>  ({currency})\n\n"
        "Lot size is auto-calculated from your balance.\n"
        "Use /trade to start the trend-following auto-trader.\n"
        "Use /disconnect to unlink at any time.",
        parse_mode="HTML",
    )
    return ConversationHandler.END


async def connect_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    await update.message.reply_text("Connection cancelled.")
    return ConversationHandler.END


# ── /disconnect ───────────────────────────────────────────────────────────────

async def disconnect_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = str(update.effective_chat.id)
    _log_cmd(update)
    acct    = get_oanda_account(chat_id)
    if acct and acct["metaapi_account_id"]:
        from mt4.trader import delete_provision
        delete_provision(acct["metaapi_account_id"])
    from mt4.auto_trader import stop_auto_trader
    stop_auto_trader(chat_id)
    removed = delete_oanda_account(chat_id)
    if removed:
        logger.info(f"[cmd] /disconnect | chat={chat_id} | account removed")
        await update.message.reply_text(
            "🔌 <b>OANDA disconnected.</b> Auto-trader stopped.\n"
            "Use /connect to re-link any time.",
            parse_mode="HTML",
        )
    else:
        await update.message.reply_text("No OANDA account connected.")


# ── /oandastatus ──────────────────────────────────────────────────────────────

async def oanda_status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = str(update.effective_chat.id)
    _log_cmd(update)
    acct    = get_oanda_account(chat_id)
    if not acct:
        logger.warning(f"[cmd] /oandastatus | chat={chat_id} | no account linked")
        await update.message.reply_text("No OANDA account. Use /connect first.")
        return

    msg = await update.message.reply_text("⏳ Checking OANDA...")
    from mt4.trader import fetch_balance
    account_id = acct["metaapi_account_id"]
    env        = acct.get("server", "practice")
    label      = "Live" if env == "live" else "Demo"
    balance    = fetch_balance(account_id, acct["password"], env)
    bal_str    = f"£{balance:,.2f}" if balance is not None else "unavailable"
    lot_str    = str(acct.get("lot_size") or "auto")
    logger.info(f"[cmd] /oandastatus | chat={chat_id} | {label} account={account_id} balance={bal_str} lot={lot_str}")

    await msg.edit_text(
        f"✅ <b>OANDA Connected ({label})</b>\n\n"
        f"Account:  <code>{account_id}</code>\n"
        f"Balance:  <b>{bal_str}</b>\n"
        f"Lot:      <b>{lot_str}</b> per trade\n\n"
        f"Use /trade to start the trend-following auto-trader.",
        parse_mode="HTML",
    )


# ── /setlot ───────────────────────────────────────────────────────────────────

async def set_lot_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = str(update.effective_chat.id)
    _log_cmd(update, f"args={context.args}")
    try:
        lot = float(context.args[0])
        if lot < 0.01 or lot > 10:
            await update.message.reply_text("Lot must be between 0.01 and 10.")
            return
        ok = set_oanda_lot_size(chat_id, lot)
        if ok:
            logger.info(f"[cmd] /setlot | chat={chat_id} | lot set to {lot}")
            await update.message.reply_text(
                f"✅ Default lot set to <b>{lot}</b>\n"
                "This overrides the auto balance-based sizing for signal trades.",
                parse_mode="HTML",
            )
        else:
            await update.message.reply_text("No OANDA account. Use /connect first.")
    except (IndexError, ValueError):
        await update.message.reply_text("Usage: /setlot 0.02")


# ── Market commands ───────────────────────────────────────────────────────────

async def price_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    _log_cmd(update)
    msg = await update.message.reply_text("⏳ Fetching price...")
    try:
        from data.twelvedata import fetch_quote
        from analysis.sessions import get_current_session
        q       = fetch_quote()
        session = get_current_session()
        current = q["c"]
        change  = q["d"]
        pct     = q["dp"]
        high    = q["h"]
        low     = q["l"]
        prev    = q["pc"]
        arrow   = "📈" if change >= 0 else "📉"
        sign    = "+" if change >= 0 else ""
        logger.info(f"[cmd] /price | chat={update.effective_chat.id} | XAUUSD={current} {sign}{change} ({sign}{pct:.2f}%) session={session.name}")
        await msg.edit_text(
            f"💰 <b>XAUUSD Live</b>  {session.emoji} {session.name}\n"
            f"{'━'*26}\n"
            f"Price:    <b>${current:,.2f}</b>  {arrow}\n"
            f"Change:   <b>{sign}{change:.2f} ({sign}{pct:.2f}%)</b>\n"
            f"High:     ${high:,.2f}  |  Low: ${low:,.2f}\n"
            f"Prev:     ${prev:,.2f}",
            parse_mode="HTML",
        )
    except Exception as e:
        logger.error(f"[cmd] /price | chat={update.effective_chat.id} | ERROR: {e}")
        await msg.edit_text(f"❌ Error: {e}")


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    _log_cmd(update)
    msg = await update.message.reply_text("⏳ Fetching data...")
    try:
        from data.twelvedata import fetch_all_timeframes
        from analysis.sessions import get_current_session
        from indicators.calculator import atr_value, compute_votes
        session = get_current_session()
        candles = fetch_all_timeframes(TIMEFRAMES)
        lines   = [f"📊 <b>XAUUSD Snapshot</b>  {session.emoji} {session.name}\n"]
        for interval, weight in TIMEFRAMES:
            df = candles.get(interval)
            if df is None or df.empty:
                continue
            close = float(df["close"].iloc[-1])
            atr   = atr_value(df) or 0
            votes = compute_votes(df)
            bulls = sum(1 for _, v in votes if v == 1)
            bears = sum(1 for _, v in votes if v == -1)
            bias  = "BUY" if bulls > bears else ("SELL" if bears > bulls else "NEUTRAL")
            lines.append(
                f"<b>{interval.upper()}</b>  ${close:,.2f}  ATR {atr:.2f}\n"
                f"  🟢 {bulls}  🔴 {bears}  →  <b>{bias}</b>"
            )
        logger.info(f"[cmd] /status | chat={update.effective_chat.id} | sent {len(lines)-1} timeframes")
        await msg.edit_text("\n\n".join(lines), parse_mode="HTML")
    except Exception as e:
        logger.error(f"[cmd] /status | chat={update.effective_chat.id} | ERROR: {e}")
        await msg.edit_text(f"❌ Error: {e}")


async def chart_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    _log_cmd(update)
    msg = await update.message.reply_text("📊 Generating chart...")
    try:
        from data.twelvedata import fetch_all_timeframes
        from indicators.calculator import atr_value
        from charts.generator import generate_chart
        from analysis.sessions import get_current_session
        import requests

        candles = fetch_all_timeframes(TIMEFRAMES)
        df      = candles.get("1h")
        if df is None:
            await msg.edit_text("❌ No candle data.")
            return

        session = get_current_session()
        close   = float(df["close"].iloc[-1])
        atr     = atr_value(df) or 10

        path = generate_chart(
            df=df,
            signal_direction="BUY",
            entry=close,
            sl=close - atr * 1.5,
            tp1=close + atr * 2.0,
            tp2=close + atr * 3.5,
            patterns=[],
            timeframe="1H",
            session=f"{session.emoji} {session.name}",
        )
        with open(path, "rb") as f:
            requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto",
                data={"chat_id": update.effective_chat.id,
                      "caption": f"📊 XAUUSD 1H  |  ${close:,.2f}  |  {session.emoji} {session.name}",
                      "parse_mode": "HTML"},
                files={"photo": f},
                timeout=20,
            )
        logger.info(f"[cmd] /chart | chat={update.effective_chat.id} | 1H chart sent price={close} session={session.name}")
        await msg.delete()
    except Exception as e:
        logger.error(f"[cmd] /chart | chat={update.effective_chat.id} | ERROR: {e}")
        await msg.edit_text(f"❌ Error: {e}")


# ── Controls ──────────────────────────────────────────────────────────────────

async def pause_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = str(update.effective_chat.id)
    _log_cmd(update)
    set_bot_state(chat_id, "paused", "1")
    from mt4.auto_trader import stop_auto_trader
    stop_auto_trader(chat_id)
    logger.info(f"[cmd] /pause | chat={chat_id} | auto-trading stopped")
    await update.message.reply_text(
        "⏸ <b>Paused.</b> Auto-trading stopped. Use /resume then /trade to restart.",
        parse_mode="HTML",
    )


async def resume_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = str(update.effective_chat.id)
    _log_cmd(update)
    set_bot_state(chat_id, "paused", "0")
    logger.info(f"[cmd] /resume | chat={update.effective_chat.id} | bot unpaused")
    await update.message.reply_text(
        "▶ <b>Resumed.</b> Use /trade to start the auto-trader.",
        parse_mode="HTML",
    )


# ── /trade ────────────────────────────────────────────────────────────────────

async def trade_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = str(update.effective_chat.id)
    acct    = get_oanda_account(chat_id)
    if not acct:
        await update.message.reply_text("No OANDA account. Use /connect first.")
        return

    from mt4.auto_trader import is_auto_active, start_auto_trader
    from notifications.bot import send_text_to

    if is_auto_active(chat_id):
        await update.message.reply_text(
            "Trend trader already running. Use /trade_cancel to stop."
        )
        return

    def notify(text: str):
        send_text_to(chat_id, text)

    ok = start_auto_trader(chat_id=chat_id, account=acct, notify_fn=notify)
    if ok:
        from config import BTC_ENABLED, BTC_PAPER_TRADE
        _log_cmd(update, "trend trader started")
        if not BTC_ENABLED:
            btc_line = "BTC:      ⚠️ disabled (set BTC_ENABLED=true in Railway)\n"
        elif BTC_PAPER_TRADE:
            btc_line = "BTC:      ₿ 24/7 paper trading (trains ML, no real orders)\n"
        else:
            btc_line = "BTC:      ₿ 24/7 live loop active\n"
        from config import GBPUSD_ENABLED, GBPUSD_PAPER_TRADE, EURUSD_ENABLED, EURUSD_PAPER_TRADE
        if not GBPUSD_ENABLED:
            gbp_line = "GBP/USD:  ⚠️ disabled (set GBPUSD_ENABLED=true)\n"
        elif GBPUSD_PAPER_TRADE:
            gbp_line = "GBP/USD:  💷 weekdays paper trading\n"
        else:
            gbp_line = "GBP/USD:  💷 weekdays live loop active\n"
        if not EURUSD_ENABLED:
            eur_line = "EUR/USD:  ⚠️ disabled (set EURUSD_ENABLED=true)\n"
        elif EURUSD_PAPER_TRADE:
            eur_line = "EUR/USD:  💶 weekdays paper trading\n"
        else:
            eur_line = "EUR/USD:  💶 weekdays live loop active\n"
        await update.message.reply_text(
            "📈 <b>Trend Trader Started</b>\n"
            f"{'━'*28}\n"
            "Strategy: Tap 'n' Barrel + SMC (6-step)\n"
            "Gold:     🥇 Mon–Fri 08:00–21:00 UTC\n"
            f"{gbp_line}"
            f"{eur_line}"
            f"{btc_line}"
            "R:R:      2RR  |  SL at fib 1.1\n"
            "Limits:   max 2 trades/day · 2% risk · 5% daily loss limit\n\n"
            "You'll get a message when a trade opens or closes.\n"
            "Use /trade_cancel to stop.",
            parse_mode="HTML",
        )
    else:
        await update.message.reply_text("Failed to start. Try again.")


async def trade_cancel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = str(update.effective_chat.id)
    _log_cmd(update)
    from mt4.auto_trader import stop_auto_trader, is_auto_active, is_active
    if is_auto_active(chat_id) or is_active(chat_id):
        stop_auto_trader(chat_id)
        await update.message.reply_text("⏹ Trend trader stopped — closing any open trade.")
    else:
        await update.message.reply_text("Trend trader not running. Use /trade to start.")


async def trade_stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    _log_cmd(update)
    from mt4.auto_trader import get_stats, get_recent_trades
    import html as _html

    s      = get_stats()
    trades = get_recent_trades(10)

    if s["total"] == 0:
        await update.message.reply_text(
            "No trend trades yet.\nUse /trade to start the auto-trader."
        )
        return

    win_rate = round(s["wins"] / s["total"] * 100, 1) if s["total"] else 0
    lines = [
        f"📈 <b>Trend Trader Stats</b>\n"
        f"{'━'*26}\n"
        f"Total:    {s['total']}\n"
        f"Wins:     {s['wins']}  ({win_rate}%)\n"
        f"Avg P&amp;L: £{s['avg_pnl']:.2f}\n"
        f"{'━'*26}\n"
        f"<b>Last {min(len(trades), 10)} trades:</b>"
    ]
    for t in trades:
        import datetime
        ts      = datetime.datetime.utcfromtimestamp(t["entry_time"]).strftime("%m-%d %H:%M")
        pnl     = t.get("final_pnl")
        pnl_str = (f"{'+'if pnl >= 0 else ''}£{pnl:.2f}" if pnl is not None else "open")
        reason  = _html.escape(t.get("exit_reason") or "open")
        sym     = "₿" if t.get("symbol") == "BTC/USD" else "🥇"
        lines.append(
            f"  #{t['id']}  {ts}  {sym}{t['direction']}  "
            f"{pnl_str}  {reason}"
        )

    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


async def ml_stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show ML model status, training sample count, and top influential factors."""
    _log_cmd(update)
    from mt4.auto_trader import get_ml_stats
    msg = get_ml_stats()
    if not msg:
        await update.message.reply_text("🤖 ML model not yet available (need 20+ closed trades).")
    else:
        await update.message.reply_text(msg, parse_mode="HTML")


# ── Symbol parser ─────────────────────────────────────────────────────────────

_SYMBOL_ALIASES: dict[str, tuple[str, str]] = {
    "gold":    ("XAU/USD", "🥇 Gold"),
    "xau":     ("XAU/USD", "🥇 Gold"),
    "xauusd":  ("XAU/USD", "🥇 Gold"),
    "gbp":     ("GBP/USD", "💷 GBP/USD"),
    "gbpusd":  ("GBP/USD", "💷 GBP/USD"),
    "gbp/usd": ("GBP/USD", "💷 GBP/USD"),
    "btc":     ("BTC/USD", "₿ BTC"),
    "bitcoin": ("BTC/USD", "₿ BTC"),
    "btcusd":  ("BTC/USD", "₿ BTC"),
    "eth":     ("ETH/USD", "💠 ETH"),
    "ethereum":("ETH/USD", "💠 ETH"),
    "ethusd":  ("ETH/USD", "💠 ETH"),
    "us30":    ("US30",    "📊 US30"),
    "dow":     ("US30",    "📊 US30"),
    "eur":     ("EUR/USD", "💶 EUR/USD"),
    "eurusd":  ("EUR/USD", "💶 EUR/USD"),
    "eur/usd": ("EUR/USD", "💶 EUR/USD"),
}

# Symbols that TwelveData cannot serve — must use OANDA
_OANDA_ONLY_SYMBOLS = {"US30", "ETH/USD"}


def _parse_symbol_arg(args: list[str] | None, default_now: bool = True) -> tuple[str, str]:
    """Parse symbol from command args. Returns (symbol, display_label)."""
    from datetime import datetime, timezone
    if args:
        key = args[0].lower().strip()
        if key in _SYMBOL_ALIASES:
            return _SYMBOL_ALIASES[key]
    if default_now:
        now = datetime.now(timezone.utc)
        if now.weekday() == 5:   # Saturday — BTC only
            return "BTC/USD", "₿ BTC"
    return "XAU/USD", "🥇 Gold"


def _fetch_candles_for_scan(
    symbol: str,
    chat_id: str,
    timeframes,
) -> dict:
    """
    Fetch candle data for scan/analyze commands.
    Uses the user's OANDA account when available (no daily limit, all symbols).
    Falls back to TwelveData for standard symbols only.
    Raises for OANDA-only symbols (US30, ETH) if no account is linked.
    """
    from data.twelvedata import fetch_all_timeframes
    from db.store import get_oanda_account

    acct = get_oanda_account(chat_id)
    if acct:
        return fetch_all_timeframes(timeframes, symbol=symbol, account=acct)

    if symbol in _OANDA_ONLY_SYMBOLS:
        raise ValueError(
            f"{symbol} data requires an OANDA account. "
            f"Use /connect to link yours first."
        )
    return fetch_all_timeframes(timeframes, symbol=symbol)


# ── /scan ─────────────────────────────────────────────────────────────────────

async def scan_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Strategy step scanner — shows which of the 6 steps is complete/pending.
    Usage: /scan  or  /scan gold  /scan gbp  /scan btc  /scan us30
    """
    _log_cmd(update)
    msg = await update.message.reply_text("🔍 Scanning strategy steps...")

    try:
        from datetime import datetime, timezone
        from analysis.sessions import get_current_session, is_gold_session
        from signals.engine import analyze_snapshot, fib_price

        symbol, sym_label = _parse_symbol_arg(context.args)
        now     = datetime.now(timezone.utc)
        session = get_current_session(now)
        is_sunday = now.weekday() == 6

        lines = [
            f"🔍 <b>Midas Scanner</b>  {now.strftime('%H:%M UTC')}",
            f"{'━'*30}",
            f"{session.emoji} <b>{session.name}</b>  |  {sym_label}",
            f"{'━'*30}",
        ]

        if is_sunday:
            lines.append("⏸ Sunday — no trading. BTC resumes Monday, Gold resumes Monday.")
            await msg.edit_text("\n".join(lines), parse_mode="HTML")
            return
        if symbol == "XAU/USD" and not is_gold_session(now):
            if 0 <= now.hour < 8:
                lines.append("🌏 <b>Asia session</b> — marking levels, no entries yet (trading opens 08:00 UTC)")
            else:
                lines.append("⏸ Outside Gold trading window (entries: Mon–Fri 08:00–21:00 UTC)")

        # Fetch candles — uses OANDA if account linked (required for US30/ETH)
        chat_id_str = str(update.effective_chat.id)
        try:
            candles = _fetch_candles_for_scan(symbol, chat_id_str, TIMEFRAMES)
        except Exception as e:
            lines.append(f"❌ Data fetch failed: {e}")
            await msg.edit_text("\n".join(lines), parse_mode="HTML")
            return

        df_5m = candles.get("5min")
        price = float(df_5m["close"].iloc[-1]) if df_5m is not None and not df_5m.empty else 0

        if price:
            fmt = f"${price:,.2f}" if symbol not in ("GBP/USD", "EUR/USD") else f"{price:.5f}"
            lines.append(f"💰 <b>{fmt}</b>  {symbol}")
        lines.append("")

        # Run the snapshot analyzer
        result = analyze_snapshot(candles, symbol=symbol)
        step   = result["step"]

        try:
            from config import FAKEOUT_ZONE_LEVEL
            fakeout_label = f"In fakeout zone ({FAKEOUT_ZONE_LEVEL:.3f})"
        except Exception:
            fakeout_label = "In fakeout zone"

        step_labels = [
            ("4H Fractal BoS",              result["bias"] != 0),
            ("Asia level marked",            result["asia_level"] is not None),
            ("Asia level swept",             result["sweep"] is not None),
            (fakeout_label,                  result["fakeout"]),
            ("5min BoS confirmed",           result["bos"] is not None),
            ("In Goldilocks zone (0.236–0.764)", result["signal"] is not None),
        ]

        for i, (label, done) in enumerate(step_labels):
            icon = "✅" if done else ("🔄" if i == step else "⬜")
            lines.append(f"{icon} Step {i+1}: {label}")

        lines.append(f"\n{'━'*30}")

        if result["signal"]:
            sig = result["signal"]
            lines += [
                f"🚨 <b>SIGNAL READY — {sig.direction}</b>",
                f"   Entry: <b>${sig.entry:,.2f}</b>",
                f"   SL:    ${sig.sl:,.2f}  (-${sig.sl_pips:.2f})",
                f"   TP:    ${sig.tp:,.2f}  (+${sig.tp_pips:.2f})  2RR",
            ]
        else:
            lines.append(f"⏳ <b>{result['reason']}</b>")

        # Extra detail for completed steps
        if result["asia_level"]:
            al = result["asia_level"]
            bias_label = "BULLISH (watching for low sweep)" if result["bias"] == 1 else "BEARISH (watching for high sweep)"
            lines.append(f"\n📍 Bias: {bias_label}")
            lines.append(f"   Asia level: ${al:,.2f}")

        if result["sweep"]:
            sw = result["sweep"]
            fib_079 = fib_price(sw["fib_high"], sw["fib_low"], 0.079)
            lines.append(f"   Sweep @ ${sw['sweep_price']:,.2f}")
            lines.append(f"   Fib range: ${sw['fib_low']:,.2f} – ${sw['fib_high']:,.2f}")
            lines.append(f"   0.079 zone: ${fib_079:,.2f}+")

        if result["bos"]:
            bos = result["bos"]
            fib_range  = bos["swing_high"] - bos["swing_low"]
            entry_deep = bos["swing_high"] - 0.764 * fib_range
            entry_shal = bos["swing_high"] - 0.236 * fib_range
            lines.append(f"   BoS swing: ${bos['swing_low']:,.2f}–${bos['swing_high']:,.2f}")
            lines.append(f"   Goldilocks: ${entry_deep:,.2f}–${entry_shal:,.2f}")

        logger.info(f"[cmd] /scan | chat={update.effective_chat.id} | {symbol} step={step} reason={result['reason']}")
        await msg.edit_text("\n".join(lines), parse_mode="HTML")

    except Exception as e:
        logger.exception(f"[cmd] /scan | chat={update.effective_chat.id} | ERROR: {e}")
        await msg.edit_text(f"❌ Scan error: {e}")


# ── /analyze ──────────────────────────────────────────────────────────────────

async def analyze_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Day-in-review analysis — reads the whole day, shows each step, signals, outcome.
    Usage: /analyze  or  /analyze gold  /analyze gbp  /analyze us30  etc.
    """
    _log_cmd(update)
    msg = await update.message.reply_text("📈 Analyzing today's price action...")

    try:
        import pandas as pd
        from datetime import datetime, timezone
        from signals.engine import (
            analyze_snapshot, fib_price, detect_4h_bos, build_signal,
        )
        from analysis.sessions import get_current_session

        symbol, sym_label = _parse_symbol_arg(context.args)
        now   = datetime.now(timezone.utc)
        today = now.date()

        chat_id_str = str(update.effective_chat.id)
        candles = _fetch_candles_for_scan(symbol, chat_id_str, TIMEFRAMES)
        df_5m = candles.get("5min")
        df_4h = candles.get("4h")

        if df_5m is None or df_4h is None or df_5m.empty:
            await msg.edit_text("❌ Could not fetch candle data.")
            return

        # Today's candles (UTC date)
        idx_utc = df_5m.index.tz_convert("UTC") if df_5m.index.tz else pd.to_datetime(df_5m.index, utc=True)
        today_mask = pd.Index(idx_utc.date) == today
        today_5m   = df_5m[today_mask]

        lines = [
            f"📈 <b>Midas Day Analysis — {sym_label}</b>",
            f"{'━'*30}",
            f"📅 {today.strftime('%A %d %b %Y')}  {now.strftime('%H:%M UTC')}",
            "",
        ]

        if today_5m.empty:
            lines.append("No candle data for today yet.")
            await msg.edit_text("\n".join(lines), parse_mode="HTML")
            return

        # Day price range
        day_high  = float(today_5m["high"].max())
        day_low   = float(today_5m["low"].min())
        day_open  = float(today_5m["open"].iloc[0])
        day_close = float(today_5m["close"].iloc[-1])
        day_move  = day_close - day_open
        move_icon = "📈" if day_move >= 0 else "📉"
        pf = "${:,.2f}" if symbol not in ("GBP/USD", "EUR/USD") else "{:.5f}"

        def fp(v: float) -> str:
            return pf.format(v)

        lines += [
            f"💰 <b>Price Range</b>",
            f"   Open:  {fp(day_open)}",
            f"   High:  {fp(day_high)}  |  Low: {fp(day_low)}",
            f"   Now:   {fp(day_close)}  {move_icon} ({'+' if day_move >= 0 else ''}{fp(abs(day_move))})",
            f"   Range: {fp(day_high - day_low)}",
            "",
        ]

        # Run full analysis on current candle data
        result = analyze_snapshot(candles, symbol=symbol)
        bias   = result["bias"]
        bias_label = "BULLISH 📈" if bias == 1 else ("BEARISH 📉" if bias == -1 else "NEUTRAL ↔️")
        lines.append(f"<b>4H Bias:</b> {bias_label}")
        lines.append("")
        lines.append("<b>Strategy Steps Today:</b>")

        # Step 2: Asia level
        al = result["asia_level"]
        if al:
            asia_tag = "Low" if bias == 1 else "High"
            lines.append(f"✅ Asia {asia_tag}: {fp(al)}")
        else:
            lines.append("⬜ Asia level: not marked yet (00:00–08:00 UTC)")

        # Step 3: Sweep
        sw = result["sweep"]
        if sw:
            sweep_ts = sw.get("sweep_time")
            t_str = ""
            if sweep_ts:
                st = pd.Timestamp(sweep_ts)
                if st.tzinfo is None:
                    st = st.tz_localize("UTC")
                else:
                    st = st.tz_convert("UTC")
                t_str = f" ({st.strftime('%H:%M UTC')})"
            lines.append(f"✅ Swept{t_str}: {fp(sw['sweep_price'])}  fib {fp(sw['fib_low'])}–{fp(sw['fib_high'])}")
        else:
            lines.append(f"⬜ Sweep: not yet")

        # Step 4: Fakeout zone
        fakeout = result["fakeout"]
        lines.append(f"{'✅' if fakeout else '⬜'} Fakeout zone: {'reached ✓' if fakeout else 'not yet'}")

        # Step 5: 5min BoS
        bos = result["bos"]
        if bos:
            try:
                bos_ts = df_5m.index[bos["idx"]]
                if hasattr(bos_ts, "tz_convert"):
                    bos_ts = bos_ts.tz_convert("UTC")
                t_str = f" ({pd.Timestamp(bos_ts).strftime('%H:%M UTC')})"
            except Exception:
                t_str = ""
            lines.append(f"✅ 5min BoS{t_str}: {fp(bos['swing_low'])}–{fp(bos['swing_high'])}")
        else:
            lines.append("⬜ 5min BoS: not yet")

        lines.append("")

        # Step 6: Signal / outcome
        sig = result["signal"]
        if sig:
            lines += [
                f"🚨 <b>SIGNAL — {sig.direction}</b>",
                f"   Entry: <b>{fp(sig.entry)}</b>",
                f"   SL:    {fp(sig.sl)}  (−{fp(sig.sl_pips)})",
                f"   TP:    {fp(sig.tp)}  (+{fp(sig.tp_pips)})  {sig.rr}RR",
            ]
            # Walk bars from BoS forward to check outcome
            if bos:
                try:
                    future = df_5m.iloc[bos["idx"]:]
                    tp_hit = sl_hit = False
                    for _, bar in future.iterrows():
                        if sig.direction == "BUY":
                            if float(bar["high"]) >= sig.tp:
                                tp_hit = True; break
                            if float(bar["low"]) <= sig.sl:
                                sl_hit = True; break
                        else:
                            if float(bar["low"]) <= sig.tp:
                                tp_hit = True; break
                            if float(bar["high"]) >= sig.sl:
                                sl_hit = True; break
                    if tp_hit:
                        lines.append(f"   ✅ <b>TP hit</b> — {fp(sig.tp)}")
                    elif sl_hit:
                        lines.append(f"   ❌ <b>SL hit</b> — {fp(sig.sl)}")
                    else:
                        lines.append(f"   ⏳ Still in play — current: {fp(day_close)}")
                except Exception:
                    pass
        elif bos:
            # BoS confirmed but not yet in entry zone — show the zone
            try:
                from config import ENTRY_ZONE_DEEP, ENTRY_ZONE_SHALLOW
                zone_deep  = fib_price(bos["swing_high"], bos["swing_low"], ENTRY_ZONE_DEEP)
                zone_shal  = fib_price(bos["swing_high"], bos["swing_low"], ENTRY_ZONE_SHALLOW)
            except Exception:
                zone_deep  = fib_price(bos["swing_high"], bos["swing_low"], 0.764)
                zone_shal  = fib_price(bos["swing_high"], bos["swing_low"], 0.236)
            lines.append(
                f"⏳ BoS confirmed — waiting for entry zone\n"
                f"   Goldilocks: {fp(zone_deep)}–{fp(zone_shal)}\n"
                f"   Current:    {fp(day_close)}"
            )
            # Check if price passed through the zone at any point today
            for _, bar in today_5m.iterrows():
                session = get_current_session(bar.name.to_pydatetime())
                test_sig = build_signal(
                    float(bar["close"]), bos, bias, session, symbol,
                    al or 0.0,
                    sw["fib_high"] if sw else bos["swing_high"],
                    sw["fib_low"]  if sw else bos["swing_low"],
                )
                if test_sig:
                    lines.append(
                        f"\n💡 <b>Signal passed through the zone today</b>\n"
                        f"   {test_sig.direction} opportunity @ ~{fp(test_sig.entry)}\n"
                        f"   SL {fp(test_sig.sl)}  TP {fp(test_sig.tp)}"
                    )
                    break
        else:
            lines.append(f"⏳ {result['reason']}")

        logger.info(f"[cmd] /analyze | chat={update.effective_chat.id} | {symbol} step={result['step']}")
        await msg.edit_text("\n".join(lines), parse_mode="HTML")

    except Exception as e:
        logger.exception(f"[cmd] /analyze | chat={update.effective_chat.id} | ERROR: {e}")
        await msg.edit_text(f"❌ Analysis error: {e}")


# ── Command registry ──────────────────────────────────────────────────────────

BOT_COMMANDS = [
    # Trend trader
    BotCommand("trade",        "Start auto-trader (Gold + GBP/USD + EUR/USD weekdays, BTC 24/7)"),
    BotCommand("trade_cancel", "Stop trend trader & close open trade"),
    BotCommand("tradestats",   "Trend trader wins, losses, avg P&L"),
    BotCommand("mlstats",      "ML model status & top trade factors"),
    # Market
    BotCommand("scan",         "Strategy scanner — /scan gold /scan gbp /scan eth /scan us30"),
    BotCommand("analyze",      "Day review — price move, steps, signals  /analyze [symbol]"),
    BotCommand("price",        "Live XAUUSD price"),
    BotCommand("chart",        "1H candlestick chart"),
    BotCommand("status",       "Multi-timeframe snapshot"),
    # OANDA
    BotCommand("connect",      "Link OANDA account"),
    BotCommand("disconnect",   "Unlink OANDA account"),
    BotCommand("oandastatus",  "OANDA balance & connection"),
    BotCommand("setlot",       "Override lot size e.g. /setlot 0.01"),
    # Controls
    BotCommand("pause",        "Pause all auto-trading"),
    BotCommand("resume",       "Resume auto-trading"),
    BotCommand("help",         "Full command list"),
]


async def _register_commands(app: Application) -> None:
    await app.bot.set_my_commands(BOT_COMMANDS)
    await app.bot.delete_my_commands()  # clear stale cache first
    await app.bot.set_my_commands(BOT_COMMANDS)
    print("[bot] Commands registered.")


def build_app() -> Application:
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).post_init(_register_commands).build()

    connect_conv = ConversationHandler(
        entry_points=[CommandHandler("connect", connect_start)],
        states={
            _WAIT_API_KEY:    [MessageHandler(filters.TEXT & ~filters.COMMAND, connect_api_key)],
            _WAIT_ACCOUNT_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, connect_account_id)],
        },
        fallbacks=[CommandHandler("cancel", connect_cancel)],
        allow_reentry=True,
    )
    app.add_handler(connect_conv)

    app.add_handler(CommandHandler("start",        start))
    app.add_handler(CommandHandler("help",         help_cmd))
    app.add_handler(CommandHandler("price",        price_cmd))
    app.add_handler(CommandHandler("scan",         scan_cmd))
    app.add_handler(CommandHandler("analyze",      analyze_cmd))
    app.add_handler(CommandHandler("status",       status))
    app.add_handler(CommandHandler("chart",        chart_cmd))
    app.add_handler(CommandHandler("disconnect",   disconnect_cmd))
    app.add_handler(CommandHandler("oandastatus",  oanda_status_cmd))
    app.add_handler(CommandHandler("setlot",       set_lot_cmd))
    app.add_handler(CommandHandler("pause",        pause_cmd))
    app.add_handler(CommandHandler("resume",       resume_cmd))
    app.add_handler(CommandHandler("trade",        trade_cmd))
    app.add_handler(CommandHandler("trade_cancel", trade_cancel_cmd))
    app.add_handler(CommandHandler("tradestats",   trade_stats_cmd))
    app.add_handler(CommandHandler("mlstats",      ml_stats_cmd))
    return app

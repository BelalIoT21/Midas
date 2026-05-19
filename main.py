"""
MidasXAU — XAUUSD Trend Trader on OANDA
"""
import logging
import sys
import threading
import requests
from apscheduler.schedulers.background import BackgroundScheduler

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    stream=sys.stdout,
)
# Silence noisy third-party loggers
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("apscheduler").setLevel(logging.WARNING)
logging.getLogger("telegram.ext.Application").setLevel(logging.WARNING)

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from config import TIMEFRAMES, TELEGRAM_BOT_TOKEN
from analysis.sessions import is_market_open
from notifications.bot import send_text
from notifications.commands import build_app, BOT_COMMANDS
from db.store import (
    init_db, get_oanda_accounts,
)

logger = logging.getLogger(__name__)


def market_close_notice() -> None:
    send_text(
        "🔴 <b>Market Closed — Weekend</b>\n"
        "Gold market closed for the weekend. Auto-trading paused.\n"
        "Resumes Sunday ~22:00 UTC."
    )


def market_open_notice() -> None:
    send_text(
        "🟢 <b>Market Reopening</b>\n"
        "Gold market reopens in ~5 minutes.\n"
        "MidasXAU trend trader resumes now."
    )


def send_daily_briefing() -> None:
    if not is_market_open():
        return
    try:
        from data.twelvedata import fetch_candles, fetch_all_timeframes
        from analysis.sessions import get_current_session
        from indicators.calculator import compute_votes, atr_value

        session   = get_current_session()
        candles   = fetch_all_timeframes(TIMEFRAMES)
        accounts  = get_oanda_accounts()

        try:
            daily_df    = fetch_candles("1day")
            close_d     = float(daily_df["close"].iloc[-1])
            ema50_d     = float(daily_df["close"].ewm(span=50, adjust=False).mean().iloc[-1])
            daily_label = ("📈 BULL" if close_d > ema50_d * 1.003
                           else "📉 BEAR" if close_d < ema50_d * 0.997
                           else "↔️ NEUTRAL")
        except Exception:
            daily_label = "N/A"

        lines = [
            f"🌅 <b>MidasXAU Daily Briefing</b>  {session.emoji} {session.name}\n"
            f"{'━'*28}\n"
            f"Daily trend:   <b>{daily_label}</b>\n"
            f"Connected:     <b>{len(accounts)}</b> account(s)"
        ]

        for interval, _ in TIMEFRAMES:
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

        send_text("\n\n".join(lines))
        print("[midas] Daily briefing sent.")
    except Exception as e:
        print(f"[midas] Briefing error: {e}")


def _register_commands() -> None:
    try:
        commands = [{"command": c.command, "description": c.description} for c in BOT_COMMANDS]
        resp = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/setMyCommands",
            json={"commands": commands},
            timeout=10,
        )
        if resp.ok:
            print("[midas] Telegram commands updated.")
        else:
            print(f"[midas] Command update failed: {resp.text}")
    except Exception as e:
        print(f"[midas] Command update error: {e}")


def main() -> None:
    print("[midas] Starting MidasXAU...")
    init_db()
    _register_commands()

    accounts = get_oanda_accounts()

    from mt4.trader import deploy_all_accounts
    account_ids = [a["metaapi_account_id"] for a in accounts if a["metaapi_account_id"]]
    if account_ids:
        deploy_all_accounts(account_ids)
        print(f"[midas] Deploying {len(account_ids)} OANDA account(s)...")

    if accounts:
        send_text(
            f"🥇 <b>MidasXAU — XAUUSD Trend Trader</b>\n"
            "Starting trend-following auto-trader...\n"
            "Trades London + NY session (07:00–17:00 UTC).\n"
            "Type /help for all commands."
        )
        from mt4.auto_trader import start_auto_trader
        from notifications.bot import send_text_to as _notify_user
        for acct in accounts:
            chat_id = str(acct["chat_id"])
            start_auto_trader(chat_id, acct, lambda msg, cid=chat_id: _notify_user(cid, msg))
            print(f"[midas] Trend trader started for {chat_id}")
    else:
        send_text(
            f"🥇 <b>MidasXAU — XAUUSD Trend Trader</b>\n"
            "No account linked yet. Use /connect first.\n"
            "Type /help for all commands."
        )

    def _keepalive():
        from mt4.trader import keepalive_accounts
        keepalive_accounts([a["metaapi_account_id"] for a in get_oanda_accounts()
                            if a["metaapi_account_id"]])

    scheduler = BackgroundScheduler(timezone="UTC")
    scheduler.add_job(_keepalive,          "interval", minutes=10,                         id="keepalive")
    scheduler.add_job(send_daily_briefing, "cron", day_of_week="mon-fri", hour=8, minute=0, id="briefing")
    scheduler.add_job(market_close_notice, "cron", day_of_week="fri",     hour=22, minute=0, id="close")
    scheduler.add_job(market_open_notice,  "cron", day_of_week="sun",     hour=21, minute=55, id="open")
    scheduler.start()
    print("[midas] Scheduler running. Briefings Mon-Fri 08:00 UTC.")

    app = build_app()

    async def _error_handler(update, context) -> None:
        from telegram.error import Conflict
        if isinstance(context.error, Conflict):
            logging.getLogger(__name__).debug("[bot] deploy conflict — old instance still shutting down")
            return
        logging.getLogger(__name__).error(f"[bot] error: {context.error!r}")

    app.add_error_handler(_error_handler)

    print("[midas] Bot polling. Ctrl+C to stop.")
    try:
        app.run_polling(drop_pending_updates=True)
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        scheduler.shutdown()
        send_text("🔴 <b>MidasXAU stopped.</b>")
        print("[midas] Stopped.")


if __name__ == "__main__":
    main()

"""
Sends Midas signals to Telegram — broadcasts to all subscribers.
"""
import requests
from signals.engine import Signal
from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID


def _bar(confidence: float, width: int = 10) -> str:
    filled = round(confidence * width)
    return "█" * filled + "░" * (width - filled)


def format_signal(signal: Signal, signal_id: int | None = None) -> str:
    arrow = "📈" if signal.direction == "BUY" else "📉"
    ts    = signal.timestamp.strftime("%d %b %Y, %H:%M UTC")
    plus  = "+" if signal.direction == "BUY" else "-"
    minus = "-" if signal.direction == "BUY" else "+"
    sym   = "₿ BTC" if signal.symbol == "BTC/USD" else "🥇 Gold"

    checklist = (
        f"\n{'━' * 28}\n"
        f"✅ 4H fractal BoS — {signal.bias}\n"
        f"✅ Asia level swept @ ${signal.asia_level:,.2f}\n"
        f"✅ Fakeout zone reached (fib 0.079)\n"
        f"✅ 5min BoS confirmed\n"
        f"✅ Goldilocks entry (0.618 fib)"
    )

    try:
        from db.store import get_performance
        perf = get_performance()
        perf_line = (
            f"\n📊 <b>Track record:</b> "
            f"{perf['wins']}W / {perf['losses']}L  "
            f"({perf['win_rate']}% win rate)"
        ) if perf["total"] > 0 else ""
    except Exception:
        perf_line = ""

    id_line = ""
    if signal_id is not None:
        id_line = (
            f"\n{'━' * 28}\n"
            f"🆔 Signal <b>#{signal_id}</b>\n"
            f"<i>/won {signal_id}  ·  /lost {signal_id}  ·  /be {signal_id}</i>"
        )

    return (
        f"{sym} <b>SIGNAL — MIDAS V2</b>\n"
        f"{'━' * 28}\n"
        f"Direction:  <b>{signal.direction} {arrow}</b>\n"
        f"Bias:       <b>{signal.bias}</b>\n"
        f"Session:    <b>{signal.session_emoji} {signal.session_name}</b>\n"
        f"\n"
        f"Entry:     <b>${signal.entry:,.2f}</b>\n"
        f"Stop Loss: <b>${signal.sl:,.2f}</b>  ({minus}${signal.sl_pips:.2f})\n"
        f"TP (3RR):  <b>${signal.tp:,.2f}</b>  ({plus}${signal.tp_pips:.2f})\n"
        f"\n"
        f"Fib range: ${signal.fib_low:,.2f} – ${signal.fib_high:,.2f}\n"
        f"BoS swing: ${signal.bos_swing_low:,.2f} – ${signal.bos_swing_high:,.2f}"
        f"{checklist}"
        f"{perf_line}"
        f"{id_line}\n"
        f"\n"
        f"<i>⏰ {ts}</i>"
    )


def _get_chat_ids() -> list[str]:
    """All subscribers + owner as fallback."""
    try:
        from db.store import get_subscribers
        subs = get_subscribers()
        # Always include owner chat id
        if TELEGRAM_CHAT_ID and str(TELEGRAM_CHAT_ID) not in subs:
            subs = [str(TELEGRAM_CHAT_ID)] + subs
        return subs
    except Exception:
        return [str(TELEGRAM_CHAT_ID)] if TELEGRAM_CHAT_ID else []


def send_signal_with_chart(signal: Signal, chart_path: str | None, signal_id: int | None = None) -> bool:
    if not TELEGRAM_BOT_TOKEN:
        return False

    text     = format_signal(signal, signal_id)
    chat_ids = _get_chat_ids()
    success  = False

    for chat_id in chat_ids:
        if chart_path:
            try:
                with open(chart_path, "rb") as f:
                    resp = requests.post(
                        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto",
                        data={"chat_id": chat_id, "caption": text, "parse_mode": "HTML"},
                        files={"photo": f}, timeout=20,
                    )
                if resp.ok:
                    success = True
                    continue
            except Exception as e:
                print(f"[bot] Photo failed for {chat_id}: {e}")

        try:
            resp = requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
                timeout=10,
            )
            if resp.ok:
                success = True
        except Exception as e:
            print(f"[bot] Message failed for {chat_id}: {e}")

    print(f"[bot] Signal broadcast: {signal.direction} @ ${signal.entry} → {len(chat_ids)} subscribers")
    return success


def send_signal(signal: Signal) -> bool:
    return send_signal_with_chart(signal, None)


def send_text(message: str) -> None:
    """Send a text message to the owner only (startup, errors, briefings)."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"},
            timeout=10,
        )
    except Exception:
        pass


def format_bounce_signal(signal, signal_id: int | None = None) -> str:
    from signals.sr_bounce import BounceSignal
    arrow  = "📈" if signal.direction == "BUY" else "📉"
    ts     = signal.timestamp.strftime("%d %b %Y, %H:%M UTC")
    pct    = round(signal.confidence * 100, 1)
    bar    = _bar(signal.confidence)
    divs   = ", ".join(signal.divergences) if signal.divergences else "—"

    try:
        from risk.manager import calculate_position
        pos = calculate_position(signal.sl_pips)
        pos_line = (
            f"\n💼 <b>Position:</b> {pos['lots']} lots  "
            f"(${pos['risk_usd']:.0f} risk / {int(pos['risk_pct']*100)}% of ${pos['balance']:,.0f})"
        )
    except Exception:
        pos_line = ""

    id_line = ""
    if signal_id is not None:
        id_line = (
            f"\n{'━' * 28}\n"
            f"🆔 Signal <b>#{signal_id}</b>\n"
            f"<i>/won {signal_id}  ·  /lost {signal_id}  ·  /be {signal_id}</i>"
        )

    return (
        f"🎯 <b>XAUUSD S/R BOUNCE — MIDAS</b>\n"
        f"{'━' * 28}\n"
        f"Direction:  <b>{signal.direction} {arrow}</b>\n"
        f"Confidence: <b>{pct}%</b>  {bar}\n"
        f"Session:    <b>{signal.session_emoji} {signal.session_name}</b>\n"
        f"\n"
        f"🧱 <b>{signal.level_type.title()}:</b> ${signal.level:,.2f}  "
        f"(tested {signal.touches}×)\n"
        f"🔀 <b>Divergence:</b> {divs}\n"
        f"\n"
        f"Entry:     <b>${signal.entry:,.2f}</b>\n"
        f"Stop Loss: <b>${signal.sl:,.2f}</b>  "
        f"({'-' if signal.direction == 'BUY' else '+'}${signal.sl_pips:.2f})\n"
        f"TP1:       <b>${signal.tp1:,.2f}</b>  "
        f"({'+'  if signal.direction == 'BUY' else '-'}${signal.tp1_pips:.2f})  R:R 1:{signal.rr1}\n"
        f"TP2:       <b>${signal.tp2:,.2f}</b>  "
        f"({'+'  if signal.direction == 'BUY' else '-'}${signal.tp2_pips:.2f})  R:R 1:{signal.rr2}\n"
        f"{pos_line}"
        f"{id_line}\n"
        f"\n"
        f"<i>⏰ {ts}</i>"
    )


def send_bounce_signal(signal, signal_id: int | None = None) -> bool:
    if not TELEGRAM_BOT_TOKEN:
        return False
    text     = format_bounce_signal(signal, signal_id)
    chat_ids = _get_chat_ids()
    success  = False
    for chat_id in chat_ids:
        try:
            resp = requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
                timeout=10,
            )
            if resp.ok:
                success = True
        except Exception as e:
            print(f"[bot] Bounce message failed for {chat_id}: {e}")
    print(f"[bot] Bounce signal: {signal.direction} @ ${signal.entry} "
          f"(level ${signal.level}) → {len(chat_ids)} subscribers")
    return success


def send_text_to(chat_id: str, message: str) -> None:
    """Send a text message to a specific chat (used for per-user OANDA trade confirmations)."""
    if not TELEGRAM_BOT_TOKEN:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": chat_id, "text": message, "parse_mode": "HTML"},
            timeout=10,
        )
    except Exception:
        pass

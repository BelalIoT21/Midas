"""
Midas Signal Diagnostics — runs the live signal engine and shows exactly which step blocks.
"""
import sys, os
sys.path.insert(0, os.path.dirname(__file__))

from datetime import datetime, timezone

from config import TIMEFRAMES, MIN_CONFIDENCE, MIN_RR_RATIO
from data.twelvedata import fetch_all_timeframes, fetch_candles
from analysis.sessions import get_current_session, should_trade, is_market_open, is_gold_session
from signals.engine import sniper_pro, trendwave, _daily_trend, analyze


def diagnose():
    now = datetime.now(timezone.utc)
    print(f"\n{'='*62}")
    print(f"  MIDAS DIAGNOSTICS — {now.strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"{'='*62}\n")

    # ── Session checks ────────────────────────────────────────────────────────
    session  = get_current_session()
    market   = is_market_open(symbol="XAU/USD")
    gold_ses = is_gold_session()
    print(f"  Market open (XAU):  {'✅' if market else '❌'}  {market}")
    print(f"  Gold session window: {'✅' if gold_ses else '❌ BLOCKED (outside 07:00–22:00 UTC)'}  ({session.name})")
    tradeable = should_trade(session)
    print(f"  Signal quality:      {'✅' if tradeable else '❌ BLOCKED (low liquidity)'}  ({session.quality})\n")

    if not market:
        print("  ⛔  Gold market closed (weekend or after 22:00 UTC).")
        return

    # ── Data fetch ────────────────────────────────────────────────────────────
    print("  Fetching live candles...")
    try:
        candles  = fetch_all_timeframes(TIMEFRAMES, symbol="XAU/USD")
        daily_df = fetch_candles("1day", symbol="XAU/USD")
    except Exception as e:
        print(f"  ❌ Data fetch failed: {e}")
        return

    df_1h  = candles.get("1h")
    df_4h  = candles.get("4h")
    df_15m = candles.get("15min")

    if df_1h is None or df_1h.empty:
        print("  ❌ No 1H candle data — cannot continue.")
        return

    price = float(df_1h["close"].iloc[-1])
    print(f"\n  Price (1H close): ${price:,.2f}")

    # ── Indicator breakdown ───────────────────────────────────────────────────
    print(f"\n  {'─'*58}")
    print(f"  INDICATOR VOTES")
    print(f"  {'─'*58}")

    # Sniper Pro
    sniper_score, sniper_dir, sniper_state, sniper_strength, snake_val = sniper_pro(df_1h, df_4h)
    sniper_sym = "🟢" if sniper_dir == 1 else ("🔴" if sniper_dir == -1 else "⚪")
    print(f"\n  {sniper_sym} Sniper Pro:  score={sniper_score:+d}  state={sniper_state}  strength={sniper_strength}  snake={snake_val:.1f}")
    if sniper_dir == 0:
        if sniper_score < 5 and sniper_score > -5:
            print(f"     ↳ Blocked: score {sniper_score} not >= 5 or <= -5")
        elif sniper_state == "Consolidating":
            print(f"     ↳ Blocked: consolidating market state")
        elif sniper_strength in ("Weak", "Very Weak"):
            print(f"     ↳ Blocked: {sniper_strength} strength")
        else:
            print(f"     ↳ Blocked: snake not in correct zone (snake={snake_val:.1f}) — need <55 for LONG or >45 for SHORT, plus recent crossover")

    # TrendWave
    tw_df             = df_15m if (df_15m is not None and len(df_15m) >= 35) else df_1h
    tw_tf_label       = "15min" if tw_df is df_15m else "1H fallback"
    tw_dir, tw_levels = trendwave(tw_df)
    tw_sym = "🟢" if tw_dir == 1 else ("🔴" if tw_dir == -1 else "⚪")
    print(f"\n  {tw_sym} TrendWave ({tw_tf_label}):  dir={tw_dir}")
    if tw_dir == 0:
        # Manual breakdown for verbose output
        import pandas_ta as ta
        close     = tw_df["close"]
        fast_ma   = ta.ema(close, length=9)
        slow_ma   = ta.ema(close, length=21)
        fast_last = float(fast_ma.dropna().iloc[-1]) if not fast_ma.dropna().empty else None
        slow_last = float(slow_ma.dropna().iloc[-1]) if not slow_ma.dropna().empty else None
        osc       = fast_ma - slow_ma
        osc_clean = osc.dropna()
        osc_last  = float(osc_clean.iloc[-1]) if not osc_clean.empty else None
        osc_prev  = float(osc_clean.iloc[-2]) if len(osc_clean) >= 2 else None

        if fast_last and slow_last:
            in_green = float(close.iloc[-1]) > fast_last > slow_last
            in_red   = (fast_last < slow_last) and (float(close.iloc[-1]) < fast_last)
            print(f"     ↳ price=${float(close.iloc[-1]):.2f}  fast={fast_last:.2f}  slow={slow_last:.2f}")
            print(f"     ↳ green cloud={in_green}  red cloud={in_red}")
            if osc_last is not None and osc_prev is not None:
                hist_bull = osc_last > 0 and osc_last > osc_prev
                hist_bear = osc_last < 0 and osc_last < osc_prev
                print(f"     ↳ osc={osc_last:.4f}  hist_bull={hist_bull}  hist_bear={hist_bear}")

                # Check recent crossover
                crossed_bull = any(
                    (float(osc_clean.iloc[-(i+1)]) <= 0 and float(osc_clean.iloc[-i]) > 0)
                    for i in range(1, min(30, len(osc_clean)))
                )
                crossed_bear = any(
                    (float(osc_clean.iloc[-(i+1)]) >= 0 and float(osc_clean.iloc[-i]) < 0)
                    for i in range(1, min(30, len(osc_clean)))
                )
                print(f"     ↳ recent_osc_bull={crossed_bull}  recent_osc_bear={crossed_bear}")

    # ── Confluence result (SniperWave: Sniper then TrendWave) ────────────────
    print(f"\n  {'─'*58}")
    print(f"  SNIPERWAVE:  Sniper={sniper_dir}  TW={tw_dir}  (both must agree)")

    if sniper_dir == 0:
        print(f"  ❌ BLOCKED — Sniper Pro blocked (Trending + Strong/Very Strong required)")
        print(f"\n  If this persists, check Railway logs for '[engine] Skip' messages.")
        return

    if tw_dir != sniper_dir:
        print(f"  ❌ BLOCKED — TrendWave={tw_dir} disagrees with Sniper={sniper_dir}")
        print(f"\n  If this persists, check Railway logs for '[engine] Skip' messages.")
        return

    agreed = sniper_dir

    direction = "BUY" if agreed == 1 else "SELL"
    print(f"  ✅ Confluence achieved: {direction}")

    # ── Daily macro filter ────────────────────────────────────────────────────
    trend = _daily_trend(daily_df)
    print(f"\n  Daily macro trend: {trend}")
    if trend == "BEAR" and direction == "BUY":
        print(f"  ❌ BLOCKED — daily BEAR, refusing BUY")
        return
    if trend == "BULL" and direction == "SELL":
        print(f"  ❌ BLOCKED — daily BULL, refusing SELL")
        return

    # ── Full engine run ───────────────────────────────────────────────────────
    print(f"\n  Running full signal engine...")
    signal = analyze(candles, daily_df=daily_df, symbol="XAU/USD", force=True)

    print(f"\n  {'='*58}")
    if signal:
        print(f"  ✅ SIGNAL READY: {signal.direction}")
        print(f"  Entry:      ${signal.entry:,.2f}")
        print(f"  SL:         ${signal.sl:,.2f}  ({signal.sl_pips:.1f} pips)")
        print(f"  TP1:        ${signal.tp1:,.2f}  1:1")
        print(f"  TP2:        ${signal.tp2:,.2f}  1:2")
        print(f"  Sniper:     {signal.sniper_score:+d}  {signal.sniper_state} / {signal.sniper_strength}")
        print(f"  Confidence: {signal.confidence*100:.1f}%  (min {MIN_CONFIDENCE*100:.0f}%)")
        if signal.confidence < MIN_CONFIDENCE:
            print(f"  ❌ Blocked at confidence gate ({signal.confidence*100:.1f}% < {MIN_CONFIDENCE*100:.0f}%)")
    else:
        print(f"  ❌ Engine returned no signal (force=True) — check logs above")
    print(f"  {'='*58}\n")


if __name__ == "__main__":
    diagnose()

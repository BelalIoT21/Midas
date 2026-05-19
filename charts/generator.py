"""
Generates a TradingView-style candlestick chart with indicators.
Returns path to saved PNG.
"""
import warnings
import pandas as pd
import pandas_ta as ta
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.gridspec as gridspec
from pathlib import Path

warnings.filterwarnings("ignore", category=UserWarning, module="matplotlib")

CHART_DIR = Path(__file__).parent.parent / "charts" / "output"
CHART_DIR.mkdir(parents=True, exist_ok=True)

# Dark theme colors
BG       = "#0d1117"
GRID     = "#21262d"
UP_COLOR = "#26a69a"
DN_COLOR = "#ef5350"
TEXT     = "#c9d1d9"
EMA9_C   = "#ffd700"
EMA21_C  = "#ff6b35"
EMA50_C  = "#4ecdc4"
BB_C     = "#7c3aed"
TP_C     = "#26a69a"
SL_C     = "#ef5350"
ENTRY_C  = "#ffd700"


def _candle_colors(opens, closes):
    return [UP_COLOR if c >= o else DN_COLOR for o, c in zip(opens, closes)]


def generate_chart(
    df: pd.DataFrame,
    signal_direction: str,
    entry: float,
    sl: float,
    tp1: float,
    tp2: float,
    symbol: str = "XAUUSD",
    timeframe: str = "1H",
    patterns: list[str] = None,
    session: str = "",
) -> str:
    """
    Generates chart PNG and returns the file path.
    """
    df = df.copy()
    df.index = pd.to_datetime(df.index)

    # Drop rows with bad OHLC data — provider sometimes returns near-zero values
    # that explode the y-axis. Filter anything more than 20% from median close.
    df = df.dropna(subset=["open", "high", "low", "close"])
    median_close = df["close"].median()
    lo_bound = median_close * 0.80
    hi_bound = median_close * 1.20
    df = df[
        (df["low"]   >= lo_bound) & (df["low"]   <= hi_bound) &
        (df["high"]  >= lo_bound) & (df["high"]  <= hi_bound) &
        (df["open"]  >= lo_bound) & (df["open"]  <= hi_bound) &
        (df["close"] >= lo_bound) & (df["close"] <= hi_bound)
    ]

    # Compute indicators on full history so warmup is complete, then slice display window
    ema9   = ta.ema(df["close"], length=9)
    ema21  = ta.ema(df["close"], length=21)
    ema50  = ta.ema(df["close"], length=50)
    bb     = ta.bbands(df["close"], length=20, std=2)
    rsi    = ta.rsi(df["close"], length=14)
    macd_r = ta.macd(df["close"], fast=12, slow=26, signal=9)

    # Now slice to display window
    display_n = 80
    df     = df.tail(display_n)
    ema9   = ema9.reindex(df.index)
    ema21  = ema21.reindex(df.index)
    ema50  = ema50.reindex(df.index)
    bb     = bb.reindex(df.index) if bb is not None else None
    rsi    = rsi.reindex(df.index)
    macd_r = macd_r.reindex(df.index) if macd_r is not None else None

    opens  = df["open"].values
    highs  = df["high"].values
    lows   = df["low"].values
    closes = df["close"].values
    x      = range(len(df))

    fig = plt.figure(figsize=(14, 10), facecolor=BG)
    gs  = gridspec.GridSpec(3, 1, height_ratios=[3, 1, 1], hspace=0.04)

    ax1 = fig.add_subplot(gs[0])  # candles
    ax2 = fig.add_subplot(gs[1], sharex=ax1)  # RSI
    ax3 = fig.add_subplot(gs[2], sharex=ax1)  # MACD

    for ax in (ax1, ax2, ax3):
        ax.set_facecolor(BG)
        ax.tick_params(colors=TEXT, labelsize=7)
        ax.spines[:].set_color(GRID)
        ax.grid(color=GRID, linewidth=0.5, alpha=0.6)
        ax.yaxis.label.set_color(TEXT)

    # ── Y-axis zoom: compute bounds before drawing anything ──
    candle_lo = lows.min()
    candle_hi = highs.max()
    pad = (candle_hi - candle_lo) * 0.08
    y_lo = min(candle_lo - pad, sl   - pad)
    y_hi = max(candle_hi + pad, tp1  + pad)

    # Disable autoscaling so Rectangle patches can't expand the y-axis
    ax1.set_autoscale_on(False)
    ax1.set_ylim(y_lo, y_hi)

    # ── Candlesticks ──
    for i, (xi, o, h, l, c) in enumerate(zip(x, opens, highs, lows, closes)):
        color = UP_COLOR if c >= o else DN_COLOR
        ax1.plot([xi, xi], [l, h], color=color, linewidth=0.8)
        ax1.add_patch(plt.Rectangle(
            (xi - 0.3, min(o, c)), 0.6, abs(c - o),
            color=color, zorder=2
        ))

    # ── EMAs ──
    ax1.plot(x, ema9,  color=EMA9_C,  linewidth=1.0, label="EMA9",  alpha=0.9)
    ax1.plot(x, ema21, color=EMA21_C, linewidth=1.0, label="EMA21", alpha=0.9)
    ax1.plot(x, ema50, color=EMA50_C, linewidth=1.0, label="EMA50", alpha=0.9)

    # ── Bollinger Bands ──
    if bb is not None and not bb.empty:
        cols = bb.columns.tolist()
        ax1.plot(x, bb[cols[0]], color=BB_C, linewidth=0.7, linestyle="--", alpha=0.7)
        ax1.plot(x, bb[cols[2]], color=BB_C, linewidth=0.7, linestyle="--", alpha=0.7)
        ax1.fill_between(x, bb[cols[0]], bb[cols[2]], color=BB_C, alpha=0.04)

    # ── TP / SL / Entry lines ──
    ax1.axhline(entry, color=ENTRY_C, linewidth=1.2, linestyle="-",  label=f"Entry ${entry:,.2f}")
    ax1.axhline(sl,    color=SL_C,    linewidth=1.2, linestyle="--", label=f"SL    ${sl:,.2f}")
    ax1.axhline(tp1,   color=TP_C,    linewidth=1.0, linestyle=":",  label=f"TP1   ${tp1:,.2f}")
    # TP2 shown as label only if within view, else annotate at edge
    if y_lo <= tp2 <= y_hi:
        ax1.axhline(tp2, color=TP_C, linewidth=1.2, linestyle="-.", label=f"TP2   ${tp2:,.2f}")
    else:
        ax1.annotate(
            f"TP2 ${tp2:,.2f}", xy=(len(df) - 1, y_hi if tp2 > y_hi else y_lo),
            color=TP_C, fontsize=7, ha="right",
            arrowprops=dict(arrowstyle="->", color=TP_C, lw=0.8),
            xytext=(len(df) - 5, y_hi - pad if tp2 > y_hi else y_lo + pad),
        )
        ax1.plot([], [], color=TP_C, linestyle="-.", linewidth=1.2, label=f"TP2   ${tp2:,.2f}")

    # Fill zone
    if signal_direction == "BUY":
        ax1.axhspan(max(sl, y_lo), entry, alpha=0.06, color=SL_C)
        ax1.axhspan(entry, min(tp2, y_hi), alpha=0.06, color=TP_C)
    else:
        ax1.axhspan(entry, min(sl, y_hi), alpha=0.06, color=SL_C)
        ax1.axhspan(max(tp2, y_lo), entry, alpha=0.06, color=TP_C)

    # Enforce y-limits one final time after all patches and spans
    ax1.set_ylim(y_lo, y_hi)

    # Legend & title
    arrow = "▲" if signal_direction == "BUY" else "▼"
    pattern_str = f"  |  {', '.join(patterns)}" if patterns else ""
    # Strip emoji — matplotlib fonts don't support flag/emoji glyphs
    session_ascii = session.encode("ascii", "ignore").decode() if session else ""
    session_str = f"  ·  {session_ascii.strip()}" if session_ascii.strip() else ""
    ax1.set_title(
        f"MIDAS  ·  {symbol}  ·  {timeframe}  ·  {signal_direction} {arrow}{pattern_str}{session_str}",
        color=TEXT, fontsize=10, pad=8, loc="left"
    )
    leg = ax1.legend(loc="upper left", fontsize=7, framealpha=0.3,
                     labelcolor=TEXT, facecolor=BG)

    # ── RSI ──
    ax2.plot(x, rsi, color="#82aaff", linewidth=1.0)
    ax2.axhline(70, color=DN_COLOR, linewidth=0.5, linestyle="--", alpha=0.6)
    ax2.axhline(30, color=UP_COLOR, linewidth=0.5, linestyle="--", alpha=0.6)
    ax2.axhline(50, color=GRID,     linewidth=0.4, alpha=0.5)
    ax2.set_ylim(0, 100)
    ax2.set_ylabel("RSI", color=TEXT, fontsize=7)

    # ── MACD ──
    if macd_r is not None and not macd_r.empty:
        cols = macd_r.columns.tolist()
        hist_vals = macd_r[cols[1]]
        hist_colors = [UP_COLOR if v >= 0 else DN_COLOR for v in hist_vals.fillna(0)]
        ax3.bar(x, hist_vals, color=hist_colors, width=0.7, alpha=0.8)
        ax3.plot(x, macd_r[cols[0]], color="#82aaff",  linewidth=0.8)
        ax3.plot(x, macd_r[cols[2]], color="#ff6b35",  linewidth=0.8)
        ax3.axhline(0, color=GRID, linewidth=0.5)
        ax3.set_ylabel("MACD", color=TEXT, fontsize=7)

    plt.setp(ax1.get_xticklabels(), visible=False)
    plt.setp(ax2.get_xticklabels(), visible=False)

    # X-axis labels in London time
    import zoneinfo
    london_tz = zoneinfo.ZoneInfo("Europe/London")

    # Ensure index is UTC-aware then convert to London
    idx = df.index
    if idx.tzinfo is None:
        idx = idx.tz_localize("UTC")
    idx_london = idx.tz_convert(london_tz)

    step = max(1, len(df) // 10)
    tick_positions = list(range(0, len(df), step))
    tick_labels = [idx_london[i].strftime("%m/%d %H:%M") for i in tick_positions]
    ax3.set_xticks(tick_positions)
    ax3.set_xticklabels(tick_labels, rotation=30, ha="right", fontsize=6, color=TEXT)
    ax3.set_xlabel("London Time (BST/GMT)", color=TEXT, fontsize=6)

    fig.subplots_adjust(hspace=0.04, left=0.06, right=0.98, top=0.95, bottom=0.08)

    out_path = str(CHART_DIR / f"midas_{signal_direction.lower()}.png")
    plt.savefig(out_path, dpi=130, bbox_inches="tight", facecolor=BG)
    plt.close(fig)
    return out_path

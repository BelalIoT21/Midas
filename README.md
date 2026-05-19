# Midas V3 — Tap 'n' Barrel + SMC Auto-Trader

Automated trading bot for **XAU/USD (Gold)**, **GBP/USD**, and **BTC/USD (Bitcoin)** using the **Tap 'n' Barrel + SMC** strategy. Runs on Railway, sends live alerts and trade updates via Telegram, and manages risk dynamically from your OANDA balance.

---

## Asset Schedule

| Asset | When | Notes |
|-------|------|-------|
| 🥇 Gold (XAU/USD) | Mon–Fri, London + NY session (08:00–21:00 UTC) | Asia session (00:00–08:00) for level-marking only |
| 💷 GBP/USD | Mon–Fri, London + NY session (08:00–21:00 UTC) | Asia session (00:00–08:00) for level-marking only |
| ₿ Bitcoin (BTC/USD) | 24/7 | No hard session restriction |

**Combined basket**: ~19 trades/month (~4.5/week) across all three symbols.

---

## Strategy — Tap 'n' Barrel + SMC (6 Steps)

Every trade requires all 6 steps to complete in order. The bot tracks progress live.

### Step 1 — 4H Fractal Break of Structure (Bias)
- Scans 4H chart for a **5-bar fractal BoS** (Rachel_T style)
- **Candle BODY must close** above a fractal high (bullish) or below a fractal low (bearish)
- Wick-only = NOT valid
- Sets the bias for the entire session (BUY or SELL)

### Step 2 — Mark Asia Session Extreme
- After the Asia session closes (08:00 UTC), marks:
  - **Bullish bias** → lowest low from 00:00–08:00 (key support to sweep)
  - **Bearish bias** → highest high from 00:00–08:00 (key resistance to sweep)

### Step 3 — Session Level Sweep (The Fakeout)
- Waits for London or NY session to sweep the Asia level
  - Bullish: price goes **below** the Asia low
  - Bearish: price goes **above** the Asia high
- Sweep = price is faking direction before reversing

### Step 4 — Fib 0.079 Fakeout Zone
- After the sweep, builds a fib from the pre-sweep swing to the sweep extreme
- Waits for price to **bounce back to the 0.079 zone** (near the top of the range)
- Confirms the fakeout is real and momentum is reversing

### Step 5 — 5min Fractal BoS Confirmation
- From within the fakeout zone, waits for a **5min fractal BoS** in the bias direction
- Body close required — wick-only not valid
- Confirms buyers/sellers have taken control

### Step 6 — Goldilocks Entry (0.236–0.764 Fib Zone)
- After the 5min BoS, builds a second fib from the BoS swing
- **Entry zone**: 0.236–0.764 fib level (pullback into the swing)
- **SL**: Fib 1.1 (just beyond the swing — invalidation)
- **TP**: 2× the SL distance from entry (2RR)

---

## Risk Management

| Account | Risk/Trade | Gold Max Lot | GBP/USD Max Lot | BTC Max Lot |
|---------|-----------|-------------|-----------------|------------|
| £0–£400 | 2% (up to £8) | 0.02 | 0.02 | 0.01 |
| £400–£800 | 2% (£8–16) | 0.03 | 0.03 | 0.02 |
| £800–£1,500 | 2% (£16–30) | 0.05 | 0.05 | 0.03 |
| £1,500+ | 2% (£30+) | 0.10 | 0.05 | 0.03 |

**Gold lot formula**: `Lot = (Balance × 0.02) ÷ (SL_points × 1.49)`  
**GBP/USD lot formula**: `Lot = (Balance × 0.02) ÷ (SL_pips × 7.87)`  
**BTC lot formula**: `Lot = (Balance × 0.02) ÷ SL_USD`

Additional rules:
- Max **2 trades per day** per symbol
- **5% daily loss limit** — all trading halted if reached
- **Friday rule**: Gold and GBP/USD positions closed before 20:00 UTC Friday

---

## Backtest Performance (2023–2025 data)

| Symbol | Trades/month | Win Rate | Profit Factor | Avg R/trade | Max DD |
|--------|-------------|----------|---------------|-------------|--------|
| 🥇 Gold | 5.8 | 61.5% | 1.94× | +0.61R | −3.5R |
| 💷 GBP/USD | 9.7 | 61.8% | 1.87× | +0.55R | −4.0R |
| ₿ BTC | 3.9 | 60.0% | 1.81× | +0.57R | −3.0R |
| **Basket** | **19.4** | **~61%** | **~1.87×** | **+0.58R** | — |

*Compounding from £500 at 2% risk → £7,500 in ~28 months (£300/trade territory)*

---

## News Filter (Gold only)

No trades placed during configured blackout windows:

| Banned Events |
|--------------|
| CPI · PPI · FOMC · FOMC Minutes |
| NFP · JOLTS Job Openings |
| Unemployment Claims |
| Fed Chair Powell speeches |

Configure blackout windows in Railway env vars:
```
NEWS_BLACKOUT=12:00-13:30,15:00-15:30
```

---

## Setup

### 1. Clone & install
```bash
git clone https://github.com/BelalIoT21/Midas.git
cd Midas
pip install -r requirements.txt
```

### 2. Environment variables
Set in Railway dashboard (or `.env` for local testing):
```
TELEGRAM_BOT_TOKEN=...
TWELVEDATA_API_KEY=...
DATA_DIR=/data

# Strategy parameters
GOLD_PIP_VALUE=1.49          # GBP pip value per 0.01 lot — adjust for your broker
GBPUSD_PIP_VALUE=7.87        # GBP per pip per lot at ~1.27 rate

# BTC trading
BTC_ENABLED=true
BTC_PAPER_TRADE=true         # true = demo simulation, false = live OANDA orders

# GBP/USD trading
GBPUSD_ENABLED=true
GBPUSD_PAPER_TRADE=true      # true = demo simulation, false = live OANDA orders

# News blackout (optional — comma-separated HH:MM-HH:MM)
NEWS_BLACKOUT=12:00-13:30
```

### 3. Run locally
```bash
python main.py
```

### 4. Deploy to Railway
Push to GitHub — Railway auto-deploys.
- Create a Volume at `/data` and set `DATA_DIR=/data` (persists SQLite DB)
- Set env vars in the Railway dashboard

---

## Telegram Commands

| Command | Description |
|---------|-------------|
| `/start` | Subscribe and see account status |
| `/trade` | Start auto-trader (Gold + GBP/USD weekdays, BTC 24/7) |
| `/trade_cancel` | Stop trader and close open trade |
| `/tradestats` | Win rate, losses, avg P&L |
| `/scan` | Show current strategy step — which of 6 steps is complete |
| `/price` | Live XAU/USD price |
| `/chart` | 1H candlestick chart |
| `/status` | Multi-timeframe snapshot |
| `/connect` | Link OANDA account |
| `/disconnect` | Unlink account |
| `/oandastatus` | Balance and connection check |
| `/setlot` | Override lot size e.g. `/setlot 0.02` |
| `/pause` / `/resume` | Pause/resume auto-trading |

---

## Architecture

```
main.py                — startup, scheduler, keepalive
signals/
  engine.py            — Tap 'n' Barrel: fractal BoS, sweep, fib zones, entry
  levels.py            — fib-based SL/TP calculator
mt4/
  trader.py            — OANDA REST API (orders, balance, spread, close)
  auto_trader.py       — state machine: 6-step strategy + risk management
                         loops: Gold, GBP/USD (weekdays), BTC (24/7)
data/
  twelvedata.py        — symbol-aware candle + price feeds with TTL cache
analysis/
  sessions.py          — session detection (Gold/GBP weekdays, BTC 24/7)
notifications/
  commands.py          — Telegram command handlers
  bot.py               — send helpers + signal formatter
db/
  store.py             — SQLite (subscribers, OANDA accounts, trade log)
```

---

## Trade Flow

```
Gold / GBP/USD loop — every 5 min (Mon–Fri 08:00–21:00 UTC):
BTC loop           — every 5 min (24/7):

  State machine per symbol per day:

  1. detect_4h_bos()           → set bias (BUY/SELL)
  2. get_asia_extreme()        → mark 00:00–08:00 extreme
  3. find_sweep()              → detect Asia level sweep
  4. in_fakeout_zone()         → confirm 0.079 fib bounce
  5. find_5min_bos()           → confirm 5min fractal BoS
  6. build_signal()            → enter when price in 0.236–0.764 zone
     → place_trade() via OANDA
     → monitor: TP hit (2RR) or SL hit or Friday close
     → log P&L to SQLite
```

---

## Master Entry Checklist

Before any trade, **all** conditions must be true:

- [ ] 4H fractal BoS confirmed — bias is clear
- [ ] Session times marked on chart
- [ ] Previous Asia session extreme identified
- [ ] Extreme has been swept (fakeout triggered)
- [ ] Price returned to fib 0.079 fakeout zone
- [ ] 5min fractal BoS confirmed in bias direction
- [ ] Price in Goldilocks zone (0.236–0.764 fib)
- [ ] SL set at fib 1.1 | TP at 2RR
- [ ] No banned news within 30 minutes (Gold only)
- [ ] Lot size at 2% risk max
- [ ] Friday rule checked (Gold/GBP: no entry after 19:00 UTC Friday)
- [ ] Max 2 trades/day not reached | Daily 5% loss limit not hit

---

## Performance Log

Every trade is stored in SQLite (`/data/signals.db`). Use `/tradestats` to review.

Review every 20 trades. If win rate drops below 50% — pause and review with `/scan`.

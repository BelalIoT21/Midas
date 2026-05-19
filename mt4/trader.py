"""
Auto-trader via OANDA REST API — free, no monthly fees.

Users need an OANDA account (live or practice) and an API key.
Set OANDA_API_KEY and OANDA_ACCOUNT_ID in .env / Railway env vars.

Practice API: https://api-fxpractice.oanda.com
Live API:     https://api-fxtrade.oanda.com
"""
import logging
import requests

from config import OANDA_API_KEY, OANDA_ACCOUNT_ID, OANDA_PRACTICE, OANDA_SYMBOL

logger = logging.getLogger(__name__)

_BASE_URL = (
    "https://api-fxpractice.oanda.com"
    if OANDA_PRACTICE
    else "https://api-fxtrade.oanda.com"
)

# OANDA uses XAU_USD format
_INSTRUMENT = OANDA_SYMBOL.replace("/", "_")  # "XAU/USD" → "XAU_USD"


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {OANDA_API_KEY}",
        "Content-Type": "application/json",
    }


def place_trade(
    oanda_account_id: str,
    direction: str,
    sl: float,
    tp: float,
    lot_size: float = 0.02,
    api_key: str = "",
    env_flag: str = "",       # "live" or "practice" — stored in DB server field
    instrument: str = "",     # override instrument e.g. "BTC_USD"; defaults to XAU_USD
    units_override: int = 0,  # if set, use directly (for BTC where lot*100 is wrong)
) -> bool:
    key     = api_key or OANDA_API_KEY
    acct_id = oanda_account_id or OANDA_ACCOUNT_ID

    if not key or not acct_id:
        logger.error("[oanda] No API key or account ID — run /connect in Telegram")
        return False

    # Use per-user env from DB; fall back to global config
    if env_flag == "live":
        base_url = "https://api-fxtrade.oanda.com"
    elif env_flag == "practice":
        base_url = "https://api-fxpractice.oanda.com"
    else:
        base_url = _BASE_URL

    instr = instrument or _INSTRUMENT
    if units_override:
        units = abs(units_override)
    else:
        units = int(lot_size * 100)
    if direction == "SELL":
        units = -units

    # Price format: BTC needs more decimal places; gold uses 2
    price_fmt = ".5f" if "BTC" in instr else ".2f"

    payload = {
        "order": {
            "type": "MARKET",
            "instrument": instr,
            "units": str(units),
            "stopLossOnFill": {"price": format(sl, price_fmt)},
            "takeProfitOnFill": {"price": format(tp, price_fmt)},
            "timeInForce": "FOK",
            "positionFill": "DEFAULT",
        }
    }

    try:
        resp = requests.post(
            f"{base_url}/v3/accounts/{acct_id}/orders",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json=payload,
            timeout=10,
        )
        if resp.ok:
            data = resp.json()
            fill     = data.get("orderFillTransaction", {})
            trade_id = fill.get("tradeOpened", {}).get("tradeID")
            logger.info(
                f"[oanda] {direction} {lot_size}lot filled @ {fill.get('price', '?')} "
                f"(trade {trade_id})"
            )
            return trade_id  # str — truthy on success
        logger.error(f"[oanda] order failed {resp.status_code}: {resp.text[:300]}")
        return None
    except Exception as exc:
        logger.error(f"[oanda] place_trade exception — {exc!r}")
        return None


def fetch_balance(oanda_account_id: str, api_key: str, env_flag: str = "") -> float | None:
    """Fetch live account balance from OANDA. Returns None on failure."""
    key = api_key or OANDA_API_KEY
    acct_id = oanda_account_id or OANDA_ACCOUNT_ID
    if not key or not acct_id:
        return None
    if env_flag == "live":
        base_url = "https://api-fxtrade.oanda.com"
    elif env_flag == "practice":
        base_url = "https://api-fxpractice.oanda.com"
    else:
        base_url = _BASE_URL
    try:
        resp = requests.get(
            f"{base_url}/v3/accounts/{acct_id}/summary",
            headers={"Authorization": f"Bearer {key}"},
            timeout=10,
        )
        if resp.ok:
            return float(resp.json()["account"]["balance"])
        logger.warning(f"[oanda] balance fetch failed {resp.status_code}")
        return None
    except Exception as exc:
        logger.warning(f"[oanda] fetch_balance error: {exc!r}")
        return None


def fetch_spread(account_id: str, api_key: str, env_flag: str = "", instrument: str = "") -> float | None:
    """Return current bid/ask spread. instrument defaults to XAU_USD."""
    key = api_key or OANDA_API_KEY
    acct_id = account_id or OANDA_ACCOUNT_ID
    if not key or not acct_id:
        return None
    if env_flag == "live":
        base_url = "https://api-fxtrade.oanda.com"
    elif env_flag == "practice":
        base_url = "https://api-fxpractice.oanda.com"
    else:
        base_url = _BASE_URL
    instr = instrument or _INSTRUMENT
    try:
        resp = requests.get(
            f"{base_url}/v3/accounts/{acct_id}/pricing",
            headers={"Authorization": f"Bearer {key}"},
            params={"instruments": instr},
            timeout=8,
        )
        if resp.ok:
            prices = resp.json().get("prices", [])
            if prices:
                ask = float(prices[0]["asks"][0]["price"])
                bid = float(prices[0]["bids"][0]["price"])
                return round(ask - bid, 4)
        logger.warning(f"[oanda] spread fetch failed {resp.status_code}")
        return None
    except Exception as exc:
        logger.warning(f"[oanda] fetch_spread error: {exc!r}")
        return None


def open_basket(
    direction: str,
    lot: float,
    num_trades: int,
    account_id: str,
    api_key: str,
    env_flag: str = "",
    sl_price: float | None = None,
    trail_distance: float | None = None,
) -> list[str]:
    """
    Fire num_trades market orders simultaneously.
    Returns list of filled tradeIDs. Uses a thread pool so all orders
    land as close to the same timestamp as possible.
    trail_distance takes priority over sl_price when both are provided.
    """
    if env_flag == "live":
        base = "https://api-fxtrade.oanda.com"
    elif env_flag == "practice":
        base = "https://api-fxpractice.oanda.com"
    else:
        base = _BASE_URL

    units = int(lot * 100)
    if direction == "SELL":
        units = -units

    order: dict = {
        "type":        "MARKET",
        "instrument":  _INSTRUMENT,
        "units":       str(units),
        "timeInForce": "FOK",
        "positionFill": "DEFAULT",
    }
    if trail_distance is not None:
        order["trailingStopLossOnFill"] = {"distance": f"{trail_distance:.2f}"}
    elif sl_price is not None:
        order["stopLossOnFill"] = {"price": f"{sl_price:.2f}"}

    payload = {"order": order}
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    url     = f"{base}/v3/accounts/{account_id}/orders"

    def _fire(_) -> str | None:
        try:
            resp = requests.post(url, headers=headers, json=payload, timeout=10)
            if resp.ok:
                fill = resp.json().get("orderFillTransaction", {})
                return fill.get("tradeOpened", {}).get("tradeID")
            logger.warning(f"[oanda] basket order failed {resp.status_code}: {resp.text[:200]}")
        except Exception as exc:
            logger.warning(f"[oanda] basket order exception: {exc!r}")
        return None

    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=num_trades) as ex:
        results = list(ex.map(_fire, range(num_trades)))

    trade_ids = [tid for tid in results if tid]
    logger.info(f"[oanda] basket: {len(trade_ids)}/{num_trades} filled  {direction} {lot}lot")
    return trade_ids


def fetch_open_trades(
    trade_ids: list[str],
    account_id: str,
    api_key: str,
    env_flag: str = "",
) -> list[dict] | None:
    """
    Fetch unrealized P&L for given open trade IDs.
    Returns list of {trade_id, unrealized_pl, units, price}.
    Returns None on network/auth error so the caller can distinguish
    "fetch failed" from "trades genuinely closed".
    """
    if not trade_ids:
        return []

    if env_flag == "live":
        base = "https://api-fxtrade.oanda.com"
    elif env_flag == "practice":
        base = "https://api-fxpractice.oanda.com"
    else:
        base = _BASE_URL

    # Build URL with literal commas — requests would encode them as %2C
    # which OANDA cannot parse, resulting in an empty response.
    url = f"{base}/v3/accounts/{account_id}/trades?ids={','.join(trade_ids)}"

    try:
        resp = requests.get(
            url,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=10,
        )
        if not resp.ok:
            logger.warning(f"[oanda] fetch_open_trades {resp.status_code}: {resp.text[:200]}")
            return None  # error — caller should not assume trades are closed
        return [
            {
                "trade_id":      t["id"],
                "unrealized_pl": float(t.get("unrealizedPL") or 0),
                "units":         int(float(t.get("currentUnits") or 0)),
                "price":         float(t.get("price") or 0),
            }
            for t in resp.json().get("trades", [])
        ]
    except Exception as exc:
        logger.warning(f"[oanda] fetch_open_trades exception: {exc!r}")
        return None  # error — caller should not assume trades are closed


def close_trade(
    trade_id: str,
    account_id: str,
    api_key: str,
    env_flag: str = "",
) -> bool:
    """Close a single open trade by tradeID."""
    if env_flag == "live":
        base = "https://api-fxtrade.oanda.com"
    elif env_flag == "practice":
        base = "https://api-fxpractice.oanda.com"
    else:
        base = _BASE_URL

    try:
        resp = requests.put(
            f"{base}/v3/accounts/{account_id}/trades/{trade_id}/close",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={},
            timeout=10,
        )
        if resp.ok:
            return True
        logger.warning(f"[oanda] close_trade {trade_id} failed {resp.status_code}: {resp.text[:200]}")
        return False
    except Exception as exc:
        logger.warning(f"[oanda] close_trade exception: {exc!r}")
        return False


def _fetch_ask_bid(base: str, account_id: str, api_key: str) -> tuple[float | None, float | None]:
    """Return (ask, bid) for XAU_USD. Returns (None, None) on failure."""
    try:
        resp = requests.get(
            f"{base}/v3/accounts/{account_id}/pricing",
            headers={"Authorization": f"Bearer {api_key}"},
            params={"instruments": _INSTRUMENT},
            timeout=8,
        )
        if resp.ok:
            prices = resp.json().get("prices", [])
            if prices:
                ask = float(prices[0]["asks"][0]["price"])
                bid = float(prices[0]["bids"][0]["price"])
                return ask, bid
    except Exception:
        pass
    return None, None


def place_breakout_orders(
    range_high: float,
    range_low: float,
    lot: float,
    trail_distance: float,
    tp_distance: float,
    account_id: str,
    api_key: str,
    env_flag: str = "",
) -> tuple[str | None, str | None]:
    """
    Place a buy-stop above range_high and a sell-stop below range_low.
    Both have a trailing stop and fixed TP.
    Returns (buy_order_id, sell_order_id). Either may be None if placement fails.
    """
    if env_flag == "live":
        base = "https://api-fxtrade.oanda.com"
    elif env_flag == "practice":
        base = "https://api-fxpractice.oanda.com"
    else:
        base = _BASE_URL

    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    url     = f"{base}/v3/accounts/{account_id}/orders"
    units   = int(lot * 100)

    def _place(direction: str, stop_price: float, tp_price: float) -> str | None:
        u = units if direction == "BUY" else -units
        payload = {"order": {
            "type":        "STOP",
            "instrument":  _INSTRUMENT,
            "units":       str(u),
            "price":       f"{stop_price:.2f}",
            "timeInForce": "GTC",
            "takeProfitOnFill":       {"price": f"{tp_price:.2f}"},
            "trailingStopLossOnFill": {"distance": f"{trail_distance:.2f}"},
        }}
        try:
            resp = requests.post(url, headers=headers, json=payload, timeout=10)
            if resp.ok:
                data = resp.json()
                oid = (data.get("orderCreateTransaction") or {}).get("id")
                logger.info(f"[oanda] {direction} stop order {oid} @ {stop_price:.2f}")
                return oid
            logger.warning(f"[oanda] {direction} stop order failed {resp.status_code}: {resp.text[:500]}")
        except Exception as exc:
            logger.warning(f"[oanda] place_breakout_orders exception: {exc!r}")
        return None

    # Fetch current bid/ask so stop prices are on the correct side of the market.
    # OANDA rejects: BUY STOP if price <= ask, SELL STOP if price >= bid.
    current_ask, current_bid = _fetch_ask_bid(base, account_id, api_key)

    buy_price  = round(range_high + 0.10, 2)
    sell_price = round(range_low  - 0.10, 2)

    if current_ask and buy_price <= current_ask:
        buy_price = round(current_ask + 0.10, 2)
    if current_bid and sell_price >= current_bid:
        sell_price = round(current_bid - 0.10, 2)

    buy_tp     = round(buy_price  + tp_distance, 2)
    sell_tp    = round(sell_price - tp_distance, 2)

    buy_id  = _place("BUY",  buy_price,  buy_tp)
    sell_id = _place("SELL", sell_price, sell_tp)
    return buy_id, sell_id


def cancel_order(
    order_id: str,
    account_id: str,
    api_key: str,
    env_flag: str = "",
) -> bool:
    """Cancel a pending order by orderID."""
    if env_flag == "live":
        base = "https://api-fxtrade.oanda.com"
    elif env_flag == "practice":
        base = "https://api-fxpractice.oanda.com"
    else:
        base = _BASE_URL
    try:
        resp = requests.put(
            f"{base}/v3/accounts/{account_id}/orders/{order_id}/cancel",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            timeout=10,
        )
        if resp.ok:
            logger.info(f"[oanda] order {order_id} cancelled")
            return True
        if resp.status_code == 404:
            logger.debug(f"[oanda] cancel order {order_id} already gone (404)")
            return False
        logger.warning(f"[oanda] cancel order {order_id} failed {resp.status_code}: {resp.text[:100]}")
        return False
    except Exception as exc:
        logger.warning(f"[oanda] cancel_order exception: {exc!r}")
        return False


def get_order_state(
    order_id: str,
    account_id: str,
    api_key: str,
    env_flag: str = "",
) -> str | None:
    """
    Returns order state: 'PENDING', 'FILLED', 'CANCELLED', 'TRIGGERED', or None on error.
    """
    if env_flag == "live":
        base = "https://api-fxtrade.oanda.com"
    elif env_flag == "practice":
        base = "https://api-fxpractice.oanda.com"
    else:
        base = _BASE_URL
    try:
        resp = requests.get(
            f"{base}/v3/accounts/{account_id}/orders/{order_id}",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=10,
        )
        if resp.ok:
            return resp.json().get("order", {}).get("state")
        return None
    except Exception:
        return None


def get_filled_trade_id(
    order_id: str,
    account_id: str,
    api_key: str,
    env_flag: str = "",
) -> str | None:
    """
    If the order has been FILLED, return the tradeID that was opened.
    Returns None if still pending, cancelled, or on error.
    """
    if env_flag == "live":
        base = "https://api-fxtrade.oanda.com"
    elif env_flag == "practice":
        base = "https://api-fxpractice.oanda.com"
    else:
        base = _BASE_URL
    try:
        resp = requests.get(
            f"{base}/v3/accounts/{account_id}/orders/{order_id}",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=10,
        )
        if resp.ok:
            order = resp.json().get("order", {})
            if order.get("state") == "FILLED":
                return order.get("tradeOpenedID")
        return None
    except Exception:
        return None


def get_trade_pnl(
    trade_id: str,
    account_id: str,
    api_key: str,
    env_flag: str = "",
) -> float | None:
    """Return unrealized P&L for a single trade, or None on error."""
    if env_flag == "live":
        base = "https://api-fxtrade.oanda.com"
    elif env_flag == "practice":
        base = "https://api-fxpractice.oanda.com"
    else:
        base = _BASE_URL
    try:
        resp = requests.get(
            f"{base}/v3/accounts/{account_id}/trades/{trade_id}",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=10,
        )
        if resp.ok:
            trade = resp.json().get("trade", {})
            state = trade.get("state", "")
            if state == "CLOSED":
                return float(trade.get("realizedPL") or 0)
            return float(trade.get("unrealizedPL") or 0)
        return None
    except Exception:
        return None


def partial_close_trade(
    trade_id:       str,
    account_id:     str,
    api_key:        str,
    env_flag:       str = "",
    units_to_close: int = 0,
) -> bool:
    """Close `units_to_close` units of an open trade (partial close)."""
    if env_flag == "live":
        base = "https://api-fxtrade.oanda.com"
    elif env_flag == "practice":
        base = "https://api-fxpractice.oanda.com"
    else:
        base = _BASE_URL
    try:
        resp = requests.put(
            f"{base}/v3/accounts/{account_id}/trades/{trade_id}/close",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={"units": str(abs(units_to_close))},
            timeout=10,
        )
        if resp.ok:
            logger.info(f"[oanda] partial close {units_to_close}u on trade {trade_id}")
            return True
        logger.warning(
            f"[oanda] partial_close {trade_id} failed {resp.status_code}: {resp.text[:200]}"
        )
        return False
    except Exception as exc:
        logger.warning(f"[oanda] partial_close_trade exception: {exc!r}")
        return False


def update_trade_sl(
    trade_id:   str,
    account_id: str,
    api_key:    str,
    env_flag:   str = "",
    new_sl:     float = 0.0,
) -> bool:
    """Move the stop-loss on an open trade to `new_sl`."""
    if env_flag == "live":
        base = "https://api-fxtrade.oanda.com"
    elif env_flag == "practice":
        base = "https://api-fxpractice.oanda.com"
    else:
        base = _BASE_URL
    try:
        resp = requests.put(
            f"{base}/v3/accounts/{account_id}/trades/{trade_id}/orders",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={"stopLoss": {"price": f"{new_sl:.2f}", "timeInForce": "GTC"}},
            timeout=10,
        )
        if resp.ok:
            logger.info(f"[oanda] SL moved to {new_sl:.2f} on trade {trade_id}")
            return True
        logger.warning(
            f"[oanda] update_trade_sl {trade_id} failed {resp.status_code}: {resp.text[:200]}"
        )
        return False
    except Exception as exc:
        logger.warning(f"[oanda] update_trade_sl exception: {exc!r}")
        return False


def provision_account_async(*_args, **_kwargs):
    """Not needed for OANDA — users connect with API key only."""
    raise NotImplementedError("OANDA uses API keys, not account provisioning.")


def delete_provision(*_args, **_kwargs):
    pass


def deploy_all_accounts(*_args, **_kwargs):
    pass


def keepalive_accounts(*_args, **_kwargs):
    pass

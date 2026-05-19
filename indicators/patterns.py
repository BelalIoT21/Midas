"""
Candlestick pattern detection. Returns vote (+1 BUY, -1 SELL, 0 NEUTRAL)
and a list of detected pattern names for display.
"""
import pandas as pd


def _body(o, c): return abs(c - o)
def _upper_wick(o, c, h): return h - max(o, c)
def _lower_wick(o, c, l): return min(o, c) - l
def _is_bull(o, c): return c > o
def _is_bear(o, c): return c < o
def _avg_body(df, n=14):
    return (df["close"] - df["open"]).abs().rolling(n).mean().iloc[-1]


def detect_patterns(df: pd.DataFrame) -> tuple[int, list[str]]:
    """
    Checks last 3 candles for 12 key patterns.
    Returns (vote, [pattern_names_detected])
    """
    if len(df) < 5:
        return 0, []

    o  = df["open"].values
    h  = df["high"].values
    l  = df["low"].values
    c  = df["close"].values
    avg = _avg_body(df)

    patterns = []
    score = 0

    # Current and prior candles
    o1, h1, l1, c1 = o[-1], h[-1], l[-1], c[-1]  # latest
    o2, h2, l2, c2 = o[-2], h[-2], l[-2], c[-2]  # -1
    o3, h3, l3, c3 = o[-3], h[-3], l[-3], c[-3]  # -2

    body1  = _body(o1, c1)
    body2  = _body(o2, c2)
    upper1 = _upper_wick(o1, c1, h1)
    lower1 = _lower_wick(o1, c1, l1)
    upper2 = _upper_wick(o2, c2, h2)
    lower2 = _lower_wick(o2, c2, l2)

    # 1. Hammer (bullish reversal)
    if (lower1 >= 2 * body1 and upper1 <= 0.3 * body1
            and _is_bear(o2, c2) and body1 > 0.3 * avg):
        patterns.append("Hammer")
        score += 2

    # 2. Shooting Star (bearish reversal)
    if (upper1 >= 2 * body1 and lower1 <= 0.3 * body1
            and _is_bull(o2, c2) and body1 > 0.3 * avg):
        patterns.append("Shooting Star")
        score -= 2

    # 3. Bullish Engulfing
    if (_is_bear(o2, c2) and _is_bull(o1, c1)
            and o1 < c2 and c1 > o2 and body1 > body2):
        patterns.append("Bullish Engulfing")
        score += 3

    # 4. Bearish Engulfing
    if (_is_bull(o2, c2) and _is_bear(o1, c1)
            and o1 > c2 and c1 < o2 and body1 > body2):
        patterns.append("Bearish Engulfing")
        score -= 3

    # 5. Doji (indecision — reduces confidence)
    if body1 < 0.1 * avg and (upper1 + lower1) > 2 * body1:
        patterns.append("Doji")
        score += 0  # neutral — handled in engine

    # 6. Bullish Pinbar
    if (lower1 >= 2.5 * body1 and upper1 < body1
            and body1 > 0 and l1 < l2):
        patterns.append("Bullish Pinbar")
        score += 2

    # 7. Bearish Pinbar
    if (upper1 >= 2.5 * body1 and lower1 < body1
            and body1 > 0 and h1 > h2):
        patterns.append("Bearish Pinbar")
        score -= 2

    # 8. Morning Star (3-candle bullish reversal)
    body3 = _body(o3, c3)
    if (len(df) >= 3
            and _is_bear(o3, c3) and body3 > avg
            and _body(o2, c2) < 0.3 * avg
            and _is_bull(o1, c1) and c1 > (o3 + c3) / 2):
        patterns.append("Morning Star")
        score += 3

    # 9. Evening Star (3-candle bearish reversal)
    if (len(df) >= 3
            and _is_bull(o3, c3) and _body(o3, c3) > avg
            and _body(o2, c2) < 0.3 * avg
            and _is_bear(o1, c1) and c1 < (o3 + c3) / 2):
        patterns.append("Evening Star")
        score -= 3

    # 10. Three White Soldiers (strong bullish)
    if (len(df) >= 3
            and _is_bull(o3, c3) and _is_bull(o2, c2) and _is_bull(o1, c1)
            and c3 > o3 and c2 > c3 and c1 > c2
            and _body(o3, c3) > 0.6 * avg
            and _body(o2, c2) > 0.6 * avg
            and _body(o1, c1) > 0.6 * avg):
        patterns.append("Three White Soldiers")
        score += 4

    # 11. Three Black Crows (strong bearish)
    if (len(df) >= 3
            and _is_bear(o3, c3) and _is_bear(o2, c2) and _is_bear(o1, c1)
            and c3 < o3 and c2 < c3 and c1 < c2
            and _body(o3, c3) > 0.6 * avg
            and _body(o2, c2) > 0.6 * avg
            and _body(o1, c1) > 0.6 * avg):
        patterns.append("Three Black Crows")
        score -= 4

    # 12. Tweezer Bottom (bullish)
    if (abs(l1 - l2) < 0.1 * avg and _is_bear(o2, c2) and _is_bull(o1, c1)):
        patterns.append("Tweezer Bottom")
        score += 2

    # 13. Tweezer Top (bearish)
    if (abs(h1 - h2) < 0.1 * avg and _is_bull(o2, c2) and _is_bear(o1, c1)):
        patterns.append("Tweezer Top")
        score -= 2

    vote = 1 if score > 1 else (-1 if score < -1 else 0)
    return vote, patterns

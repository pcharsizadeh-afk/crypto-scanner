import requests
from datetime import datetime, timezone
from statistics import mean


# ============================================================
# OMPFinex Crypto Scanner - Version 1
# Markets:
# BTCUSDT / ETHUSDT / XRPUSDT / ADAUSDT / SOLUSDT
# ============================================================

BASE_URL = "https://api.ompfinex.com/v2/udf/real/history"

SYMBOLS = [
    "BTCUSDT",
    "ETHUSDT",
    "XRPUSDT",
    "ADAUSDT",
    "SOLUSDT",
]

# Number of 1H candles requested
CANDLE_COUNT = 1000

# Swing detection
SWING_LOOKBACK = 3

# EMA
EMA_PERIOD = 20


# ============================================================
# API
# ============================================================

def get_candles(symbol, resolution=60, hours=1000):

    now = int(datetime.now(timezone.utc).timestamp())

    start = now - (hours * 60 * 60)

    params = {
        "symbol": symbol,
        "from": start,
        "to": now,
        "resolution": resolution,
    }

    response = requests.get(
        BASE_URL,
        params=params,
        timeout=30
    )

    response.raise_for_status()

    data = response.json()

    if data.get("s") != "ok":
        raise Exception(f"{symbol}: {data}")

    required = ["t", "o", "h", "l", "c", "v"]

    for key in required:
        if key not in data:
            raise Exception(f"{symbol}: missing field {key}")

    candles = []

    length = min(
        len(data["t"]),
        len(data["o"]),
        len(data["h"]),
        len(data["l"]),
        len(data["c"]),
        len(data["v"])
    )

    for i in range(length):

        candles.append({
            "time": int(data["t"][i]),
            "open": float(data["o"][i]),
            "high": float(data["h"][i]),
            "low": float(data["l"][i]),
            "close": float(data["c"][i]),
            "volume": float(data["v"][i]),
        })

    candles.sort(key=lambda x: x["time"])

    return candles


# ============================================================
# EMA
# ============================================================

def calculate_ema(values, period=20):

    if len(values) < period:
        return None

    multiplier = 2 / (period + 1)

    ema = mean(values[:period])

    for price in values[period:]:
        ema = (price - ema) * multiplier + ema

    return ema


# ============================================================
# 1H -> 4H aggregation
# ============================================================

def aggregate_to_4h(candles):

    if not candles:
        return []

    result = []

    current = None
    current_bucket = None

    FOUR_HOURS = 4 * 60 * 60

    for candle in candles:

        bucket = (candle["time"] // FOUR_HOURS) * FOUR_HOURS

        if current_bucket != bucket:

            if current is not None:
                result.append(current)

            current_bucket = bucket

            current = {
                "time": bucket,
                "open": candle["open"],
                "high": candle["high"],
                "low": candle["low"],
                "close": candle["close"],
                "volume": candle["volume"],
            }

        else:

            current["high"] = max(
                current["high"],
                candle["high"]
            )

            current["low"] = min(
                current["low"],
                candle["low"]
            )

            current["close"] = candle["close"]

            current["volume"] += candle["volume"]

    if current is not None:
        result.append(current)

    return result


# ============================================================
# Swing High / Swing Low
# ============================================================

def find_swing_highs(candles, lookback=3):

    swings = []

    for i in range(
        lookback,
        len(candles) - lookback
    ):

        high = candles[i]["high"]

        left = [
            candles[j]["high"]
            for j in range(i - lookback, i)
        ]

        right = [
            candles[j]["high"]
            for j in range(i + 1, i + lookback + 1)
        ]

        if high > max(left) and high > max(right):
            swings.append({
                "index": i,
                "price": high,
                "time": candles[i]["time"]
            })

    return swings


def find_swing_lows(candles, lookback=3):

    swings = []

    for i in range(
        lookback,
        len(candles) - lookback
    ):

        low = candles[i]["low"]

        left = [
            candles[j]["low"]
            for j in range(i - lookback, i)
        ]

        right = [
            candles[j]["low"]
            for j in range(i + 1, i + lookback + 1)
        ]

        if low < min(left) and low < min(right):
            swings.append({
                "index": i,
                "price": low,
                "time": candles[i]["time"]
            })

    return swings


# ============================================================
# Market Structure
# ============================================================

def determine_structure(candles):

    if len(candles) < 30:
        return {
            "structure": "INSUFFICIENT_DATA",
            "last_swing_high": None,
            "previous_swing_high": None,
            "last_swing_low": None,
            "previous_swing_low": None,
        }

    highs = find_swing_highs(
        candles,
        SWING_LOOKBACK
    )

    lows = find_swing_lows(
        candles,
        SWING_LOOKBACK
    )

    last_high = highs[-1] if highs else None
    previous_high = highs[-2] if len(highs) >= 2 else None

    last_low = lows[-1] if lows else None
    previous_low = lows[-2] if len(lows) >= 2 else None

    structure = "RANGE"

    if last_high and previous_high and last_low and previous_low:

        higher_high = (
            last_high["price"] >
            previous_high["price"]
        )

        higher_low = (
            last_low["price"] >
            previous_low["price"]
        )

        lower_high = (
            last_high["price"] <
            previous_high["price"]
        )

        lower_low = (
            last_low["price"] <
            previous_low["price"]
        )

        if higher_high and higher_low:
            structure = "BULLISH"

        elif lower_high and lower_low:
            structure = "BEARISH"

    return {
        "structure": structure,
        "last_swing_high": (
            last_high["price"]
            if last_high else None
        ),
        "previous_swing_high": (
            previous_high["price"]
            if previous_high else None
        ),
        "last_swing_low": (
            last_low["price"]
            if last_low else None
        ),
        "previous_swing_low": (
            previous_low["price"]
            if previous_low else None
        ),
    }


# ============================================================
# Trend
# ============================================================

def determine_trend(candles):

    if len(candles) < EMA_PERIOD:
        return "INSUFFICIENT_DATA"

    closes = [
        c["close"]
        for c in candles
    ]

    ema20 = calculate_ema(
        closes,
        EMA_PERIOD
    )

    last_close = closes[-1]

    if last_close > ema20:
        return "ABOVE_EMA20"

    if last_close < ema20:
        return "BELOW_EMA20"

    return "AT_EMA20"


# ============================================================
# Bias
# ============================================================

def determine_bias(structure, trend):

    bullish_score = 0
    bearish_score = 0

    if structure == "BULLISH":
        bullish_score += 2

    elif structure == "BEARISH":
        bearish_score += 2

    if trend == "ABOVE_EMA20":
        bullish_score += 1

    elif trend == "BELOW_EMA20":
        bearish_score += 1

    if bullish_score >= 2 and bullish_score > bearish_score:
        return "LONG_BIAS"

    if bearish_score >= 2 and bearish_score > bullish_score:
        return "SHORT_BIAS"

    return "NEUTRAL"


# ============================================================
# Momentum
# ============================================================

def price_change(candles, periods):

    if len(candles) <= periods:
        return None

    old_price = candles[-periods - 1]["close"]
    new_price = candles[-1]["close"]

    if old_price == 0:
        return None

    return ((new_price - old_price) / old_price) * 100


# ============================================================
# Setup Detection
# ============================================================

def detect_setup(bias, candles_1h):

    if len(candles_1h) < 30:
        return "WAIT"

    closes = [
        c["close"]
        for c in candles_1h
    ]

    ema20 = calculate_ema(
        closes,
        EMA_PERIOD
    )

    last = candles_1h[-1]

    if ema20 is None:
        return "WAIT"

    # Basic continuation setup.
    # This is NOT yet the final trading strategy.

    if bias == "LONG_BIAS":

        if last["close"] > ema20:

            if last["close"] > last["open"]:
                return "LONG_CANDIDATE"

    if bias == "SHORT_BIAS":

        if last["close"] < ema20:

            if last["close"] < last["open"]:
                return "SHORT_CANDIDATE"

    return "WAIT"


# ============================================================
# Analyze Symbol
# ============================================================

def analyze_symbol(symbol):

    candles_1h = get_candles(
        symbol,
        resolution=60,
        hours=CANDLE_COUNT
    )

    if len(candles_1h) < 100:
        raise Exception(
            f"Not enough 1H candles: {len(candles_1h)}"
        )

    candles_4h = aggregate_to_4h(
        candles_1h
    )

    structure_4h = determine_structure(
        candles_4h
    )

    trend_4h = determine_trend(
        candles_4h
    )

    bias = determine_bias(
        structure_4h["structure"],
        trend_4h
    )

    trend_1h = determine_trend(
        candles_1h
    )

    setup = detect_setup(
        bias,
        candles_1h
    )

    last = candles_1h[-1]

    return {
        "symbol": symbol,
        "candles_1h": len(candles_1h),
        "candles_4h": len(candles_4h),

        "price": last["close"],

        "4h_structure":
            structure_4h["structure"],

        "4h_trend":
            trend_4h,

        "4h_bias":
            bias,

        "1h_trend":
            trend_1h,

        "setup":
            setup,

        "last_swing_high":
            structure_4h["last_swing_high"],

        "last_swing_low":
            structure_4h["last_swing_low"],

        "change_1h":
            price_change(candles_1h, 1),

        "change_4h":
            price_change(candles_1h, 4),

        "change_24h":
            price_change(candles_1h, 24),
    }


# ============================================================
# Main
# ============================================================

def main():

    print("=" * 70)
    print("OMPFinex CRYPTO SCANNER - VERSION 1")
    print("=" * 70)

    print()
    print("Markets:")
    print(", ".join(SYMBOLS))

    print()
    print("Timeframes:")
    print("4H = Market Bias")
    print("1H = Setup Detection")

    print()
    print("=" * 70)

    results = []

    for symbol in SYMBOLS:

        print()
        print(f"Analyzing {symbol} ...")

        try:

            result = analyze_symbol(symbol)

            results.append(result)

            print(f"✓ {symbol}")
            print(f"  Price       : {result['price']}")
            print(f"  4H Structure: {result['4h_structure']}")
            print(f"  4H Trend    : {result['4h_trend']}")
            print(f"  4H Bias     : {result['4h_bias']}")
            print(f"  1H Trend    : {result['1h_trend']}")
            print(f"  Setup       : {result['setup']}")

            if result["change_1h"] is not None:
                print(
                    f"  1H Change   : "
                    f"{result['change_1h']:.2f}%"
                )

            if result["change_4h"] is not None:
                print(
                    f"  4H Change   : "
                    f"{result['change_4h']:.2f}%"
                )

            if result["change_24h"] is not None:
                print(
                    f"  24H Change  : "
                    f"{result['change_24h']:.2f}%"
                )

        except Exception as e:

            print(f"✗ {symbol}")
            print(f"  ERROR: {e}")

    print()
    print("=" * 70)
    print("SCAN SUMMARY")
    print("=" * 70)

    for result in results:

        print(
            f"{result['symbol']:10} | "
            f"{result['4h_bias']:12} | "
            f"{result['setup']}"
        )

    print()
    print("=" * 70)
    print("SCAN FINISHED")
    print("=" * 70)


if __name__ == "__main__":
    main()

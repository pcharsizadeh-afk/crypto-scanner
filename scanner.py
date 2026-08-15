import requests
from datetime import datetime, timezone


# ============================================================
# CRYPTO FUTURES SCANNER - VERSION 2
# OMPFinex
#
# Markets:
# BTCUSDT / ETHUSDT / XRPUSDT / ADAUSDT / SOLUSDT
#
# 4H = Bias / Market Structure
# 1H = Setup / Entry
#
# IMPORTANT:
# This scanner does NOT place trades.
# It only identifies candidate LONG / SHORT setups.
# ============================================================


BASE_URL = "https://api.ompfinex.com/v2/udf/real/history"

SYMBOLS = [
    "BTCUSDT",
    "ETHUSDT",
    "XRPUSDT",
    "ADAUSDT",
    "SOLUSDT",
]

LOOKBACK_HOURS = 1000
SWING = 3
EMA_PERIOD = 20

MIN_SCORE = 75
MIN_RR = 2.0


# ============================================================
# DATA
# ============================================================

def get_candles(symbol, resolution=60, hours=1000):

    now = int(datetime.now(timezone.utc).timestamp())
    start = now - hours * 60 * 60

    params = {
        "symbol": symbol,
        "from": start,
        "to": now,
        "resolution": resolution,
    }

    r = requests.get(
        BASE_URL,
        params=params,
        timeout=30
    )

    r.raise_for_status()

    data = r.json()

    if data.get("s") != "ok":
        raise Exception(f"{symbol}: {data}")

    required = ["t", "o", "h", "l", "c", "v"]

    for key in required:
        if key not in data:
            raise Exception(f"{symbol}: missing {key}")

    candles = []

    n = min(
        len(data["t"]),
        len(data["o"]),
        len(data["h"]),
        len(data["l"]),
        len(data["c"]),
        len(data["v"])
    )

    for i in range(n):

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
# 1H -> 4H
# ============================================================

def aggregate_4h(candles):

    result = []
    current = None
    bucket_id = None

    bucket_size = 4 * 60 * 60

    for c in candles:

        bucket = (c["time"] // bucket_size) * bucket_size

        if bucket != bucket_id:

            if current is not None:
                result.append(current)

            bucket_id = bucket

            current = {
                "time": bucket,
                "open": c["open"],
                "high": c["high"],
                "low": c["low"],
                "close": c["close"],
                "volume": c["volume"],
            }

        else:

            current["high"] = max(
                current["high"],
                c["high"]
            )

            current["low"] = min(
                current["low"],
                c["low"]
            )

            current["close"] = c["close"]

            current["volume"] += c["volume"]

    if current is not None:
        result.append(current)

    return result


# ============================================================
# EMA
# ============================================================

def ema(values, period=20):

    if len(values) < period:
        return None

    multiplier = 2 / (period + 1)

    result = sum(values[:period]) / period

    for value in values[period:]:
        result = (
            (value - result) * multiplier
            + result
        )

    return result


# ============================================================
# SWINGS
# ============================================================

def swing_highs(candles):

    result = []

    for i in range(SWING, len(candles) - SWING):

        h = candles[i]["high"]

        left = max(
            candles[j]["high"]
            for j in range(i - SWING, i)
        )

        right = max(
            candles[j]["high"]
            for j in range(i + 1, i + SWING + 1)
        )

        if h > left and h > right:

            result.append({
                "index": i,
                "price": h,
                "time": candles[i]["time"]
            })

    return result


def swing_lows(candles):

    result = []

    for i in range(SWING, len(candles) - SWING):

        l = candles[i]["low"]

        left = min(
            candles[j]["low"]
            for j in range(i - SWING, i)
        )

        right = min(
            candles[j]["low"]
            for j in range(i + 1, i + SWING + 1)
        )

        if l < left and l < right:

            result.append({
                "index": i,
                "price": l,
                "time": candles[i]["time"]
            })

    return result


# ============================================================
# MARKET STRUCTURE
# ============================================================

def market_structure(candles):

    highs = swing_highs(candles)
    lows = swing_lows(candles)

    if len(highs) < 2 or len(lows) < 2:

        return {
            "bias": "NEUTRAL",
            "structure": "UNKNOWN",
            "highs": highs,
            "lows": lows,
        }

    h1 = highs[-1]["price"]
    h2 = highs[-2]["price"]

    l1 = lows[-1]["price"]
    l2 = lows[-2]["price"]

    if h1 > h2 and l1 > l2:

        structure = "BULLISH"
        bias = "LONG"

    elif h1 < h2 and l1 < l2:

        structure = "BEARISH"
        bias = "SHORT"

    else:

        structure = "RANGE"
        bias = "NEUTRAL"

    return {
        "bias": bias,
        "structure": structure,
        "highs": highs,
        "lows": lows,
    }


# ============================================================
# BOS
# ============================================================

def detect_bos(candles, direction):

    if len(candles) < 20:
        return False

    recent = candles[-1]

    highs = swing_highs(candles[:-3])
    lows = swing_lows(candles[:-3])

    if direction == "LONG" and highs:

        last_high = highs[-1]["price"]

        if recent["close"] > last_high:
            return True

    if direction == "SHORT" and lows:

        last_low = lows[-1]["price"]

        if recent["close"] < last_low:
            return True

    return False


# ============================================================
# LIQUIDITY SWEEP
# ============================================================

def detect_liquidity_sweep(candles, direction):

    if len(candles) < 15:
        return False

    recent = candles[-1]

    highs = swing_highs(candles[:-2])
    lows = swing_lows(candles[:-2])

    if direction == "LONG" and lows:

        liquidity = lows[-1]["price"]

        swept = recent["low"] < liquidity
        recovered = recent["close"] > liquidity

        return swept and recovered

    if direction == "SHORT" and highs:

        liquidity = highs[-1]["price"]

        swept = recent["high"] > liquidity
        rejected = recent["close"] < liquidity

        return swept and rejected

    return False


# ============================================================
# MOMENTUM
# ============================================================

def bullish_candle(c):

    return c["close"] > c["open"]


def bearish_candle(c):

    return c["close"] < c["open"]


def momentum_confirmation(candles, direction):

    if len(candles) < 5:
        return False

    recent = candles[-3:]

    if direction == "LONG":

        bullish = sum(
            bullish_candle(c)
            for c in recent
        )

        return bullish >= 2

    if direction == "SHORT":

        bearish = sum(
            bearish_candle(c)
            for c in recent
        )

        return bearish >= 2

    return False


# ============================================================
# ATR
# ============================================================

def calculate_atr(candles, period=14):

    if len(candles) < period + 1:
        return None

    trs = []

    for i in range(
        len(candles) - period,
        len(candles)
    ):

        current = candles[i]
        previous = candles[i - 1]

        tr = max(
            current["high"] - current["low"],
            abs(
                current["high"]
                - previous["close"]
            ),
            abs(
                current["low"]
                - previous["close"]
            )
        )

        trs.append(tr)

    return sum(trs) / len(trs)


# ============================================================
# ENTRY / SL / TP
# ============================================================

def build_trade(candles, direction):

    last = candles[-1]

    entry = last["close"]

    atr = calculate_atr(candles)

    if atr is None:
        return None

    lows = swing_lows(candles[:-2])
    highs = swing_highs(candles[:-2])

    if direction == "LONG":

        if not lows:
            return None

        swing_low = lows[-1]["price"]

        sl = min(
            swing_low,
            entry - atr * 1.2
        )

        risk = entry - sl

        if risk <= 0:
            return None

        tp1 = entry + risk * 2
        tp2 = entry + risk * 3
        tp3 = entry + risk * 4

    else:

        if not highs:
            return None

        swing_high = highs[-1]["price"]

        sl = max(
            swing_high,
            entry + atr * 1.2
        )

        risk = sl - entry

        if risk <= 0:
            return None

        tp1 = entry - risk * 2
        tp2 = entry - risk * 3
        tp3 = entry - risk * 4

    rr = abs(tp2 - entry) / risk

    return {
        "direction": direction,
        "entry": entry,
        "sl": sl,
        "tp1": tp1,
        "tp2": tp2,
        "tp3": tp3,
        "rr": rr,
        "atr": atr,
    }


# ============================================================
# SCORE
# ============================================================

def calculate_score(
    direction,
    structure,
    bos,
    sweep,
    momentum,
    trend
):

    score = 0

    if structure == direction:
        score += 25

    if bos:
        score += 20

    if sweep:
        score += 20

    if momentum:
        score += 15

    if trend == direction:
        score += 20

    return score


# ============================================================
# ANALYZE
# ============================================================

def analyze(symbol):

    candles_1h = get_candles(
        symbol,
        resolution=60,
        hours=LOOKBACK_HOURS
    )

    candles_4h = aggregate_4h(
        candles_1h
    )

    if len(candles_4h) < 50:

        return {
            "symbol": symbol,
            "signal": None,
            "reason": "INSUFFICIENT_4H_DATA"
        }

    # -------------------------
    # 4H
    # -------------------------

    structure_4h = market_structure(
        candles_4h
    )

    bias = structure_4h["bias"]

    if bias == "NEUTRAL":

        return {
            "symbol": symbol,
            "signal": None,
            "reason": "4H_BIAS_NEUTRAL"
        }

    direction = bias

    # -------------------------
    # 1H
    # -------------------------

    closes = [
        c["close"]
        for c in candles_1h
    ]

    ema20 = ema(
        closes,
        EMA_PERIOD
    )

    last = candles_1h[-1]

    if ema20 is None:

        return {
            "symbol": symbol,
            "signal": None,
            "reason": "NO_EMA"
        }

    if direction == "LONG":

        trend_ok = last["close"] > ema20

    else:

        trend_ok = last["close"] < ema20

    if not trend_ok:

        return {
            "symbol": symbol,
            "signal": None,
            "reason": "1H_NOT_ALIGNED"
        }

    # -------------------------
    # Structure confirmation
    # -------------------------

    bos = detect_bos(
        candles_1h,
        direction
    )

    sweep = detect_liquidity_sweep(
        candles_1h,
        direction
    )

    momentum = momentum_confirmation(
        candles_1h,
        direction
    )

    trend = direction

    score = calculate_score(
        direction,
        structure_4h["structure"],
        bos,
        sweep,
        momentum,
        trend
    )

    # -------------------------
    # Trade
    # -------------------------

    trade = build_trade(
        candles_1h,
        direction
    )

    if trade is None:

        return {
            "symbol": symbol,
            "signal": None,
            "reason": "TRADE_CALCULATION_FAILED"
        }

    if trade["rr"] < MIN_RR:

        return {
            "symbol": symbol,
            "signal": None,
            "reason": "RR_TOO_LOW"
        }

    if score < MIN_SCORE:

        return {
            "symbol": symbol,
            "signal": None,
            "reason": f"SCORE_{score}_BELOW_{MIN_SCORE}"
        }

    return {
        "symbol": symbol,
        "signal": direction,
        "score": score,
        "bias": bias,
        "structure": structure_4h["structure"],
        "bos": bos,
        "liquidity_sweep": sweep,
        "momentum": momentum,
        "entry": trade["entry"],
        "sl": trade["sl"],
        "tp1": trade["tp1"],
        "tp2": trade["tp2"],
        "tp3": trade["tp3"],
        "rr": trade["rr"],
    }


# ============================================================
# OUTPUT
# ============================================================

def print_signal(result):

    if result.get("signal") is None:

        print(
            f"[NO TRADE] "
            f"{result['symbol']} | "
            f"{result['reason']}"
        )

        return

    print()
    print("=" * 70)

    if result["signal"] == "LONG":
        print(f"🟢 LONG SIGNAL | {result['symbol']}")
    else:
        print(f"🔴 SHORT SIGNAL | {result['symbol']}")

    print("=" * 70)

    print(f"Bias : {result['bias']}")
    print(f"Structure : {result['structure']}")
    print(f"BOS : {result['bos']}")
    print(f"Liquidity Sweep : {result['liquidity_sweep']}")
    print(f"Momentum : {result['momentum']}")

    print()
    print(f"Entry : {result['entry']}")
    print(f"Stop Loss : {result['sl']}")
    print(f"TP1 : {result['tp1']}")
    print(f"TP2 : {result['tp2']}")
    print(f"TP3 : {result['tp3']}")

    print()
    print(f"Risk / Reward : 1:{result['rr']:.2f}")
    print(f"Score : {result['score']}/100")

    print("=" * 70)


# ============================================================
# MAIN
# ============================================================

def main():

    print()
    print("=" * 70)
    print("OMPFinex FUTURES SCANNER - VERSION 2")
    print("=" * 70)

    print()
    print("Scanning:")
    print(", ".join(SYMBOLS))

    print()
    print("Rule:")
    print("ONLY VALID LONG / SHORT SETUPS ARE SIGNALS")
    print("NO TRADE RESULTS WILL NOT BE SENT TO TELEGRAM")

    signals = []

    for symbol in SYMBOLS:

        try:

            result = analyze(symbol)

            print_signal(result)

            if result.get("signal") in [
                "LONG",
                "SHORT"
            ]:

                signals.append(result)

        except Exception as e:

            print(
                f"[ERROR] {symbol}: {e}"
            )

    print()
    print("=" * 70)
    print("FINAL SIGNALS")
    print("=" * 70)

    if not signals:

        print("NO VALID LONG/SHORT SETUP FOUND")

    else:

        for signal in signals:

            print(
                f"{signal['symbol']} → "
                f"{signal['signal']} | "
                f"Score: {signal['score']}/100 | "
                f"RR: 1:{signal['rr']:.2f}"
            )

    print("=" * 70)
    print("SCAN FINISHED")
    print("=" * 70)


if __name__ == "__main__":
    main()

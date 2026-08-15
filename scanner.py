import os
import requests
from datetime import datetime, timezone

# ============================================================
# OMPFinex FUTURES SCANNER - VERSION 4
# 4H CONTEXT -> 1H SETUP -> ENTRY -> SL -> TP
# ONLY QUALIFIED LONG / SHORT SIGNALS ARE SENT
# ============================================================

BASE_URL = "https://api.ompfinex.com/v2/udf/real/history"

SYMBOLS = [
    "BTCUSDT",
    "ETHUSDT",
    "XRPUSDT",
    "ADAUSDT",
    "SOLUSDT",
]

LOOKBACK_HOURS = 720
PIVOT = 2

MIN_SCORE = 75
MIN_RR = 2.0

# Telegram secrets
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")


# ============================================================
# DATA
# ============================================================

def get_1h_candles(symbol, hours=LOOKBACK_HOURS):

    now = int(datetime.now(timezone.utc).timestamp())
    start = now - hours * 60 * 60

    params = {
        "symbol": symbol,
        "from": start,
        "to": now,
        "resolution": 60
    }

    response = requests.get(
        BASE_URL,
        params=params,
        timeout=30
    )

    response.raise_for_status()

    data = response.json()

    if data.get("s") not in ("ok", "OK"):
        raise Exception(f"{symbol}: API status = {data.get('s')}")

    required = ["t", "o", "h", "l", "c", "v"]

    for key in required:
        if key not in data:
            raise Exception(f"{symbol}: missing field {key}")

    n = min(
        len(data["t"]),
        len(data["o"]),
        len(data["h"]),
        len(data["l"]),
        len(data["c"]),
        len(data["v"])
    )

    candles = []

    for i in range(n):

        candles.append({
            "time": int(data["t"][i]),
            "open": float(data["o"][i]),
            "high": float(data["h"][i]),
            "low": float(data["l"][i]),
            "close": float(data["c"][i]),
            "volume": float(data["v"][i])
        })

    candles.sort(key=lambda x: x["time"])

    # Remove duplicates
    unique = {}

    for c in candles:
        unique[c["time"]] = c

    candles = list(unique.values())
    candles.sort(key=lambda x: x["time"])

    # Ignore currently forming candle
    if candles:
        current_hour = int(
            datetime.now(timezone.utc)
            .replace(minute=0, second=0, microsecond=0)
            .timestamp()
        )

        candles = [
            c for c in candles
            if c["time"] < current_hour
        ]

    return candles


# ============================================================
# 1H -> 4H AGGREGATION
# ============================================================

def aggregate_4h(candles):

    groups = {}

    FOUR_HOURS = 4 * 60 * 60

    for c in candles:

        bucket = (c["time"] // FOUR_HOURS) * FOUR_HOURS

        if bucket not in groups:
            groups[bucket] = {
                "time": bucket,
                "open": c["open"],
                "high": c["high"],
                "low": c["low"],
                "close": c["close"],
                "volume": c["volume"],
                "count": 1
            }

        else:

            g = groups[bucket]

            g["high"] = max(g["high"], c["high"])
            g["low"] = min(g["low"], c["low"])
            g["close"] = c["close"]
            g["volume"] += c["volume"]
            g["count"] += 1

    result = []

    for g in groups.values():

        # Only complete 4H candles
        if g["count"] >= 4:

            result.append({
                "time": g["time"],
                "open": g["open"],
                "high": g["high"],
                "low": g["low"],
                "close": g["close"],
                "volume": g["volume"]
            })

    result.sort(key=lambda x: x["time"])

    return result


# ============================================================
# MARKET STRUCTURE
# ============================================================

def is_swing_high(candles, i):

    if i < PIVOT or i >= len(candles) - PIVOT:
        return False

    h = candles[i]["high"]

    for j in range(1, PIVOT + 1):

        if h <= candles[i - j]["high"]:
            return False

        if h <= candles[i + j]["high"]:
            return False

    return True


def is_swing_low(candles, i):

    if i < PIVOT or i >= len(candles) - PIVOT:
        return False

    l = candles[i]["low"]

    for j in range(1, PIVOT + 1):

        if l >= candles[i - j]["low"]:
            return False

        if l >= candles[i + j]["low"]:
            return False

    return True


def get_swings(candles):

    highs = []
    lows = []

    for i in range(len(candles)):

        if is_swing_high(candles, i):
            highs.append((i, candles[i]["high"]))

        if is_swing_low(candles, i):
            lows.append((i, candles[i]["low"]))

    return highs, lows


# ============================================================
# 4H BIAS
# ============================================================

def get_4h_bias(candles):

    if len(candles) < 20:
        return "NEUTRAL", 0

    highs, lows = get_swings(candles)

    recent_highs = highs[-4:]
    recent_lows = lows[-4:]

    if len(recent_highs) < 2 or len(recent_lows) < 2:
        return "NEUTRAL", 0

    h1 = recent_highs[-2][1]
    h2 = recent_highs[-1][1]

    l1 = recent_lows[-2][1]
    l2 = recent_lows[-1][1]

    close = candles[-1]["close"]

    bullish_structure = h2 > h1 and l2 > l1
    bearish_structure = h2 < h1 and l2 < l1

    if bullish_structure and close > l2:
        return "LONG", 30

    if bearish_structure and close < h2:
        return "SHORT", 30

    return "NEUTRAL", 0


# ============================================================
# LIQUIDITY SWEEP
# ============================================================

def bullish_liquidity_sweep(candles):

    if len(candles) < 8:
        return False

    current = candles[-1]

    recent_lows = [
        c["low"]
        for c in candles[-7:-1]
    ]

    liquidity = min(recent_lows)

    swept = current["low"] < liquidity
    reclaimed = current["close"] > liquidity

    return swept and reclaimed


def bearish_liquidity_sweep(candles):

    if len(candles) < 8:
        return False

    current = candles[-1]

    recent_highs = [
        c["high"]
        for c in candles[-7:-1]
    ]

    liquidity = max(recent_highs)

    swept = current["high"] > liquidity
    reclaimed = current["close"] < liquidity

    return swept and reclaimed


# ============================================================
# 1H MARKET STRUCTURE BREAK
# ============================================================

def bullish_bos(candles):

    if len(candles) < 10:
        return False

    current = candles[-1]

    previous_high = max(
        c["high"]
        for c in candles[-7:-1]
    )

    return current["close"] > previous_high


def bearish_bos(candles):

    if len(candles) < 10:
        return False

    current = candles[-1]

    previous_low = min(
        c["low"]
        for c in candles[-7:-1]
    )

    return current["close"] < previous_low


# ============================================================
# CANDLE CONFIRMATION
# ============================================================

def bullish_candle(c):

    body = abs(c["close"] - c["open"])
    rng = c["high"] - c["low"]

    if rng <= 0:
        return False

    return (
        c["close"] > c["open"]
        and body / rng >= 0.50
        and c["close"] >= c["low"] + rng * 0.65
    )


def bearish_candle(c):

    body = abs(c["close"] - c["open"])
    rng = c["high"] - c["low"]

    if rng <= 0:
        return False

    return (
        c["close"] < c["open"]
        and body / rng >= 0.50
        and c["close"] <= c["high"] - rng * 0.65
    )


# ============================================================
# VOLUME CONFIRMATION
# ============================================================

def volume_confirmation(candles):

    if len(candles) < 21:
        return False

    current_volume = candles[-1]["volume"]

    previous = [
        c["volume"]
        for c in candles[-21:-1]
    ]

    average_volume = sum(previous) / len(previous)

    return current_volume >= average_volume * 1.05


# ============================================================
# ATR
# ============================================================

def calculate_atr(candles, period=14):

    if len(candles) < period + 1:
        return None

    trs = []

    for i in range(len(candles) - period, len(candles)):

        current = candles[i]
        previous = candles[i - 1]

        tr = max(
            current["high"] - current["low"],
            abs(current["high"] - previous["close"]),
            abs(current["low"] - previous["close"])
        )

        trs.append(tr)

    return sum(trs) / len(trs)


# ============================================================
# SIGNAL
# ============================================================

def analyze(symbol, candles_1h):

    candles_4h = aggregate_4h(candles_1h)

    if len(candles_4h) < 20 or len(candles_1h) < 30:
        return None, "INSUFFICIENT_DATA"

    bias, bias_score = get_4h_bias(candles_4h)

    if bias == "NEUTRAL":
        return None, "4H_BIAS_NEUTRAL"

    c = candles_1h[-1]

    score = bias_score
    reasons = []

    # ------------------------------------------
    # LONG
    # ------------------------------------------

    if bias == "LONG":

        if bullish_liquidity_sweep(candles_1h):
            score += 20
            reasons.append("LIQUIDITY_SWEEP")

        if bullish_bos(candles_1h):
            score += 20
            reasons.append("1H_BOS")

        if bullish_candle(c):
            score += 10
            reasons.append("BULLISH_CONFIRMATION")

        if volume_confirmation(candles_1h):
            score += 10
            reasons.append("VOLUME_CONFIRMATION")

        if score < MIN_SCORE:
            return None, f"SCORE_{score}_BELOW_{MIN_SCORE}"

        atr = calculate_atr(candles_1h)

        if atr is None:
            return None, "ATR_UNAVAILABLE"

        entry = c["close"]

        sweep_low = min(
            x["low"]
            for x in candles_1h[-7:]
        )

        sl = sweep_low - atr * 0.20

        risk = entry - sl

        if risk <= 0:
            return None, "INVALID_RISK"

        tp1 = entry + risk * MIN_RR
        tp2 = entry + risk * 3.0

        rr = (tp1 - entry) / risk

        if rr < MIN_RR:
            return None, "RR_TOO_LOW"

        signal = {
            "symbol": symbol,
            "side": "LONG",
            "entry": entry,
            "sl": sl,
            "tp1": tp1,
            "tp2": tp2,
            "rr": rr,
            "score": score,
            "time": c["time"],
            "reasons": reasons
        }

        return signal, "VALID_LONG"

    # ------------------------------------------
    # SHORT
    # ------------------------------------------

    if bias == "SHORT":

        if bearish_liquidity_sweep(candles_1h):
            score += 20
            reasons.append("LIQUIDITY_SWEEP")

        if bearish_bos(candles_1h):
            score += 20
            reasons.append("1H_BOS")

        if bearish_candle(c):
            score += 10
            reasons.append("BEARISH_CONFIRMATION")

        if volume_confirmation(candles_1h):
            score += 10
            reasons.append("VOLUME_CONFIRMATION")

        if score < MIN_SCORE:
            return None, f"SCORE_{score}_BELOW_{MIN_SCORE}"

        atr = calculate_atr(candles_1h)

        if atr is None:
            return None, "ATR_UNAVAILABLE"

        entry = c["close"]

        sweep_high = max(
            x["high"]
            for x in candles_1h[-7:]
        )

        sl = sweep_high + atr * 0.20

        risk = sl - entry

        if risk <= 0:
            return None, "INVALID_RISK"

        tp1 = entry - risk * MIN_RR
        tp2 = entry - risk * 3.0

        rr = (entry - tp1) / risk

        if rr < MIN_RR:
            return None, "RR_TOO_LOW"

        signal = {
            "symbol": symbol,
            "side": "SHORT",
            "entry": entry,
            "sl": sl,
            "tp1": tp1,
            "tp2": tp2,
            "rr": rr,
            "score": score,
            "time": c["time"],
            "reasons": reasons
        }

        return signal, "VALID_SHORT"

    return None, "NO_SIGNAL"


# ============================================================
# TELEGRAM
# ============================================================

def send_telegram(signal):

    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram secrets are not configured.")
        return False

    dt = datetime.fromtimestamp(
        signal["time"],
        tz=timezone.utc
    )

    side_emoji = "🟢" if signal["side"] == "LONG" else "🔴"

    message = f"""
{side_emoji} OMPFinex FUTURES SIGNAL

━━━━━━━━━━━━━━━━━━

💎 Symbol: {signal["symbol"]}
📌 Direction: {signal["side"]}

🎯 Entry:
{signal["entry"]}

🛑 Stop Loss:
{signal["sl"]}

✅ TP1:
{signal["tp1"]}

🚀 TP2:
{signal["tp2"]}

📊 R:R:
1:{signal["rr"]:.2f}

🏆 Score:
{signal["score"]}/100

🔎 Confirmation:
{", ".join(signal["reasons"])}

🕐 Candle:
{dt.strftime("%Y-%m-%d %H:%M UTC")}

━━━━━━━━━━━━━━━━━━
ONLY QUALIFIED SETUP
"""

    url = (
        f"https://api.telegram.org/bot"
        f"{TELEGRAM_BOT_TOKEN}/sendMessage"
    )

    response = requests.post(
        url,
        json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message
        },
        timeout=20
    )

    response.raise_for_status()

    return True


# ============================================================
# MAIN
# ============================================================

print("=" * 70)
print("OMPFinex FUTURES SCANNER - VERSION 4")
print("4H CONTEXT -> 1H SETUP -> ENTRY -> SL -> TP")
print("=" * 70)

signals = []

for symbol in SYMBOLS:

    try:

        candles = get_1h_candles(symbol)

        print()
        print(
            f"[SCAN] {symbol} | "
            f"1H candles={len(candles)}"
        )

        signal, status = analyze(
            symbol,
            candles
        )

        if signal:

            signals.append(signal)

            print(
                f"[VALID {signal['side']}] "
                f"{symbol} | "
                f"Score={signal['score']} | "
                f"RR=1:{signal['rr']:.2f}"
            )

            print(
                f"Entry={signal['entry']} | "
                f"SL={signal['sl']} | "
                f"TP1={signal['tp1']} | "
                f"TP2={signal['tp2']}"
            )

        else:

            print(
                f"[FILTERED] {symbol} | {status}"
            )

    except Exception as e:

        print()
        print(
            f"[ERROR] {symbol} | {e}"
        )


# ============================================================
# FINAL SIGNALS
# ============================================================

print()
print("=" * 70)
print("FINAL SIGNALS")
print("=" * 70)

if not signals:

    print("NO QUALIFIED LONG/SHORT SETUP.")
    print("NOTHING WILL BE SENT TO TELEGRAM.")

else:

    for signal in signals:

        print()
        print(
            f"{signal['side']} | "
            f"{signal['symbol']} | "
            f"Score {signal['score']}/100"
        )

        try:

            sent = send_telegram(signal)

            if sent:
                print(
                    f"✓ TELEGRAM SENT: "
                    f"{signal['symbol']} {signal['side']}"
                )

        except Exception as e:

            print(
                f"✗ TELEGRAM ERROR: "
                f"{signal['symbol']} | {e}"
            )


print()
print("=" * 70)
print("SCAN FINISHED")
print("=" * 70)

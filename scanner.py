import os
import requests
from datetime import datetime, timezone

BASE_URL = "https://api.ompfinex.com/v2/udf/real/history"
SYMBOLS = ["BTCUSDT", "ETHUSDT", "XRPUSDT", "ADAUSDT", "SOLUSDT"]
LOOKBACK_HOURS = 720
MIN_SCORE = 65

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()


def get_1h_candles(symbol):
    now = int(datetime.now(timezone.utc).timestamp())
    start = now - LOOKBACK_HOURS * 3600

    r = requests.get(
        BASE_URL,
        params={"symbol": symbol, "from": start, "to": now, "resolution": 60},
        timeout=30,
    )
    r.raise_for_status()
    data = r.json()

    if data.get("s") != "ok":
        raise RuntimeError(f"{symbol}: API status={data.get('s')}")

    n = min(len(data["t"]), len(data["o"]), len(data["h"]),
            len(data["l"]), len(data["c"]), len(data["v"]))

    candles = [{
        "time": int(data["t"][i]),
        "open": float(data["o"][i]),
        "high": float(data["h"][i]),
        "low": float(data["l"][i]),
        "close": float(data["c"][i]),
        "volume": float(data["v"][i]),
    } for i in range(n)]

    candles.sort(key=lambda x: x["time"])

    unique = {c["time"]: c for c in candles}
    candles = [unique[t] for t in sorted(unique)]

    current_hour = int(
        datetime.now(timezone.utc).replace(
            minute=0, second=0, microsecond=0
        ).timestamp()
    )
    return [c for c in candles if c["time"] < current_hour]


def aggregate_4h(candles):
    groups = {}

    for c in candles:
        bucket = (c["time"] // 14400) * 14400
        if bucket not in groups:
            groups[bucket] = {
                "time": bucket, "open": c["open"], "high": c["high"],
                "low": c["low"], "close": c["close"],
                "volume": c["volume"], "count": 1
            }
        else:
            g = groups[bucket]
            g["high"] = max(g["high"], c["high"])
            g["low"] = min(g["low"], c["low"])
            g["close"] = c["close"]
            g["volume"] += c["volume"]
            g["count"] += 1

    return [
        {k: g[k] for k in
         ("time", "open", "high", "low", "close", "volume")}
        for g in sorted(groups.values(), key=lambda x: x["time"])
        if g["count"] == 4
    ]


def ema(values, period):
    if len(values) < period:
        return None
    value = sum(values[:period]) / period
    k = 2 / (period + 1)
    for x in values[period:]:
        value = x * k + value * (1 - k)
    return value


def atr(candles, period=14):
    if len(candles) < period + 1:
        return None
    trs = []
    for i in range(len(candles) - period, len(candles)):
        c, p = candles[i], candles[i - 1]
        trs.append(max(
            c["high"] - c["low"],
            abs(c["high"] - p["close"]),
            abs(c["low"] - p["close"])
        ))
    return sum(trs) / len(trs)


def get_context(c4):
    closes = [c["close"] for c in c4]
    e20, e50 = ema(closes, 20), ema(closes, 50)

    if e20 is None or e50 is None:
        return "NEUTRAL", 0, "INSUFFICIENT_CONTEXT"

    last = c4[-1]["close"]
    prev = c4[-2]["close"]

    hh = c4[-1]["high"] > c4[-2]["high"]
    hl = c4[-1]["low"] > c4[-2]["low"]
    lh = c4[-1]["high"] < c4[-2]["high"]
    ll = c4[-1]["low"] < c4[-2]["low"]

    if last > e20 > e50:
        return "LONG", 30 if (hh or hl) else 20, "BULLISH_EMA_CONTEXT"
    if last < e20 < e50:
        return "SHORT", 30 if (lh or ll) else 20, "BEARISH_EMA_CONTEXT"
    if last > e20 and prev > e20:
        return "LONG", 15, "4H_MOMENTUM_UP"
    if last < e20 and prev < e20:
        return "SHORT", 15, "4H_MOMENTUM_DOWN"

    return "NEUTRAL", 0, "4H_MIXED"


def candle_flags(c):
    rng = c["high"] - c["low"]
    if rng <= 0:
        return False, False
    body_ratio = abs(c["close"] - c["open"]) / rng
    bullish = (
        c["close"] > c["open"]
        and body_ratio >= 0.45
        and c["close"] >= c["low"] + rng * 0.60
    )
    bearish = (
        c["close"] < c["open"]
        and body_ratio >= 0.45
        and c["close"] <= c["high"] - rng * 0.60
    )
    return bullish, bearish


def volume_score(candles):
    if len(candles) < 21:
        return 0
    avg = sum(c["volume"] for c in candles[-21:-1]) / 20
    if avg <= 0:
        return 0
    ratio = candles[-1]["volume"] / avg
    return 10 if ratio >= 1.30 else 5 if ratio >= 1.05 else 0


def detect_setup(c1, bias, context_score):
    if len(c1) < 60 or bias == "NEUTRAL":
        return None, "NO_CONTEXT"

    last, prev = c1[-1], c1[-2]
    e20, a14 = ema([c["close"] for c in c1], 20), atr(c1)
    if e20 is None or a14 is None or a14 <= 0:
        return None, "INDICATOR_DATA_MISSING"

    resistance = max(c["high"] for c in c1[-21:-1])
    support = min(c["low"] for c in c1[-21:-1])

    bullish, bearish = candle_flags(last)
    vol = volume_score(c1)

    bullish_break = last["close"] > resistance
    bearish_break = last["close"] < support
    swept_low = last["low"] < support and last["close"] > support
    swept_high = last["high"] > resistance and last["close"] < resistance
    bull_reclaim = prev["close"] < e20 < last["close"] and bullish
    bear_reclaim = prev["close"] > e20 > last["close"] and bearish

    if bias == "LONG":
        score = context_score
        reasons = []

        if bullish:
            score += 10
            reasons.append("1H_BULLISH_CANDLE")
        if bullish_break:
            score += 20
            reasons.append("1H_BREAKOUT")
        if swept_low:
            score += 20
            reasons.append("LIQUIDITY_SWEEP")
        if bull_reclaim:
            score += 15
            reasons.append("EMA20_RECLAIM")
        if vol:
            score += vol
            reasons.append("VOLUME_CONFIRMATION")

        if not (bullish_break or swept_low or bull_reclaim):
            return None, f"NO_LONG_TRIGGER_SCORE_{score}"
        if score < MIN_SCORE:
            return None, f"LONG_SCORE_{score}_BELOW_{MIN_SCORE}"

        entry = last["close"]
        sl = min(c["low"] for c in c1[-8:]) - 0.15 * a14
        risk = entry - sl
        if risk <= 0:
            return None, "INVALID_LONG_RISK"

        return {
            "side": "LONG", "entry": entry, "sl": sl,
            "tp1": entry + 2 * risk, "tp2": entry + 3 * risk,
            "rr": 2.0, "score": min(score, 100),
            "reasons": reasons, "time": last["time"]
        }, "VALID_LONG"

    score = context_score
    reasons = []

    if bearish:
        score += 10
        reasons.append("1H_BEARISH_CANDLE")
    if bearish_break:
        score += 20
        reasons.append("1H_BREAKDOWN")
    if swept_high:
        score += 20
        reasons.append("LIQUIDITY_SWEEP")
    if bear_reclaim:
        score += 15
        reasons.append("EMA20_REJECTION")
    if vol:
        score += vol
        reasons.append("VOLUME_CONFIRMATION")

    if not (bearish_break or swept_high or bear_reclaim):
        return None, f"NO_SHORT_TRIGGER_SCORE_{score}"
    if score < MIN_SCORE:
        return None, f"SHORT_SCORE_{score}_BELOW_{MIN_SCORE}"

    entry = last["close"]
    sl = max(c["high"] for c in c1[-8:]) + 0.15 * a14
    risk = sl - entry
    if risk <= 0:
        return None, "INVALID_SHORT_RISK"

    return {
        "side": "SHORT", "entry": entry, "sl": sl,
        "tp1": entry - 2 * risk, "tp2": entry - 3 * risk,
        "rr": 2.0, "score": min(score, 100),
        "reasons": reasons, "time": last["time"]
    }, "VALID_SHORT"


def format_signal(symbol, context, signal):
    dt = datetime.fromtimestamp(signal["time"], tz=timezone.utc)
    emoji = "🟢" if signal["side"] == "LONG" else "🔴"
    return (
        f"{emoji} OMPFinex SIGNAL\n\n"
        f"#{symbol}\n"
        f"Direction: {signal['side']}\n"
        f"4H Context: {context}\n"
        f"Score: {signal['score']}/100\n\n"
        f"Entry: {signal['entry']:.8g}\n"
        f"SL: {signal['sl']:.8g}\n"
        f"TP1: {signal['tp1']:.8g}\n"
        f"TP2: {signal['tp2']:.8g}\n"
        f"R:R: 1:{signal['rr']:.2f}\n\n"
        f"Confirmation: {', '.join(signal['reasons'])}\n"
        f"Candle: {dt.strftime('%Y-%m-%d %H:%M UTC')}\n\n"
        "⚠️ Signal only — no order was placed."
    )


def send_telegram(text):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram secrets are not configured; no message sent.")
        print("SIGNAL PREVIEW:\n" + text)
        return False

    r = requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
        json={"chat_id": TELEGRAM_CHAT_ID, "text": text},
        timeout=20,
    )
    r.raise_for_status()
    if not r.json().get("ok"):
        raise RuntimeError(r.json())
    return True


def main():
    print("=" * 72)
    print("OMPFinex CANDLE SCANNER - VERSION 5")
    print("4H CONTEXT -> 1H ENTRY | ONLY LONG/SHORT ALERTS")
    print("=" * 72)

    signals = []

    for symbol in SYMBOLS:
        try:
            c1 = get_1h_candles(symbol)
            c4 = aggregate_4h(c1)

            if len(c1) < 60 or len(c4) < 50:
                print(f"[SKIP] {symbol}: insufficient candles")
                continue

            bias, context_score, context_reason = get_context(c4)
            signal, status = detect_setup(c1, bias, context_score)

            print(
                f"[SCAN] {symbol} | 4H={bias} | "
                f"context={context_score} | {context_reason}"
            )

            if signal:
                print(
                    f"[VALID {signal['side']}] {symbol} | "
                    f"score={signal['score']} | RR=1:{signal['rr']:.2f}"
                )
                signals.append((symbol, bias, signal))
            else:
                print(f"[FILTERED] {symbol} | {status}")

        except Exception as e:
            print(f"[ERROR] {symbol} | {e}")

    print("\n" + "=" * 72)
    print("FINAL RESULT")
    print("=" * 72)

    if not signals:
        print("NO VALID LONG/SHORT SETUP FOUND")
        print("NO TELEGRAM MESSAGE WILL BE SENT.")
        return

    for symbol, bias, signal in signals:
        message = format_signal(symbol, bias, signal)
        print("\n" + message)
        try:
            if send_telegram(message):
                print(f"[TELEGRAM] SENT {symbol} {signal['side']}")
        except Exception as e:
            print(f"[TELEGRAM ERROR] {symbol} | {e}")

    print("\nSCAN FINISHED")


if __name__ == "__main__":
    main()

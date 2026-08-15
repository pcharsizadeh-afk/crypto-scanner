import os
import math
import requests
from datetime import datetime, timezone

# ============================================================
# OMPFinex Signal Scanner V6
# Read-only: fetches candles and sends Telegram alerts.
# It NEVER places an exchange order.
# ============================================================

BASE_URL = "https://api.ompfinex.com/v2/udf/real/history"
SYMBOLS = ["BTCUSDT", "ETHUSDT", "XRPUSDT", "ADAUSDT", "SOLUSDT"]

LOOKBACK_HOURS = 720
MIN_SCORE = 60
MIN_RR = 2.0
MAX_DISTANCE_FROM_EMA20_ATR = 1.8
ATR_STOP_BUFFER = 0.20

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "OMPFinex-Scanner/6.0"})


def get_candles(symbol, resolution, hours):
    now = int(datetime.now(timezone.utc).timestamp())
    start = now - hours * 3600

    r = SESSION.get(
        BASE_URL,
        params={
            "symbol": symbol,
            "from": start,
            "to": now,
            "resolution": resolution,
        },
        timeout=30,
    )
    r.raise_for_status()
    data = r.json()

    if data.get("s") != "ok":
        raise RuntimeError(f"{symbol}/{resolution}: {data}")

    keys = ("t", "o", "h", "l", "c", "v")
    if any(k not in data for k in keys):
        raise RuntimeError(f"{symbol}/{resolution}: incomplete API response")

    n = min(len(data[k]) for k in keys)
    out = []

    for i in range(n):
        out.append({
            "time": int(data["t"][i]),
            "open": float(data["o"][i]),
            "high": float(data["h"][i]),
            "low": float(data["l"][i]),
            "close": float(data["c"][i]),
            "volume": float(data["v"][i]),
        })

    out.sort(key=lambda x: x["time"])

    # Deduplicate timestamps.
    unique = {}
    for c in out:
        unique[c["time"]] = c
    out = [unique[t] for t in sorted(unique)]

    # Remove the currently forming candle.
    bucket_seconds = resolution * 60
    current_bucket = (
        int(datetime.now(timezone.utc).timestamp()) // bucket_seconds
    ) * bucket_seconds

    return [c for c in out if c["time"] < current_bucket]


def aggregate_4h(candles_1h):
    groups = {}

    for c in candles_1h:
        bucket = (c["time"] // 14400) * 14400

        if bucket not in groups:
            groups[bucket] = {
                "time": bucket,
                "open": c["open"],
                "high": c["high"],
                "low": c["low"],
                "close": c["close"],
                "volume": c["volume"],
                "count": 1,
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
    alpha = 2 / (period + 1)

    for x in values[period:]:
        value = alpha * x + (1 - alpha) * value

    return value


def rsi(values, period=14):
    if len(values) < period + 1:
        return None

    gains = []
    losses = []

    for i in range(1, len(values)):
        d = values[i] - values[i - 1]
        gains.append(max(d, 0))
        losses.append(max(-d, 0))

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period

    if avg_loss == 0:
        return 100.0

    return 100 - (100 / (1 + avg_gain / avg_loss))


def atr(candles, period=14):
    if len(candles) < period + 1:
        return None

    trs = []
    start = len(candles) - period

    for i in range(start, len(candles)):
        c = candles[i]
        p = candles[i - 1]

        trs.append(max(
            c["high"] - c["low"],
            abs(c["high"] - p["close"]),
            abs(c["low"] - p["close"]),
        ))

    return sum(trs) / len(trs)


def avg_volume(candles, period=20):
    if len(candles) < period:
        return None
    return sum(c["volume"] for c in candles[-period:]) / period


def trend_context(c4):
    closes = [c["close"] for c in c4]

    e20 = ema(closes, 20)
    e50 = ema(closes, 50)
    e20_prev = ema(closes[:-3], 20)
    e50_prev = ema(closes[:-3], 50)

    if None in (e20, e50, e20_prev, e50_prev):
        return {"side": "NEUTRAL", "score": 0, "reason": "INSUFFICIENT_4H_DATA"}

    price = closes[-1]
    slope20 = e20 - e20_prev
    slope50 = e50 - e50_prev

    if price > e20 > e50 and slope20 > 0 and slope50 >= 0:
        score = 35
        reason = "4H_UPTREND"
        side = "LONG"
    elif price < e20 < e50 and slope20 < 0 and slope50 <= 0:
        score = 35
        reason = "4H_DOWNTREND"
        side = "SHORT"
    elif price > e50 and e20 > e50:
        score = 25
        reason = "4H_BULLISH_BIAS"
        side = "LONG"
    elif price < e50 and e20 < e50:
        score = 25
        reason = "4H_BEARISH_BIAS"
        side = "SHORT"
    else:
        return {"side": "NEUTRAL", "score": 0, "reason": "4H_MIXED"}

    # Penalize an exhausted 4H move rather than blindly chasing it.
    a = atr(c4)
    if a and abs(price - e20) > 2.5 * a:
        score -= 10
        reason += "_EXTENDED"

    return {"side": side, "score": max(score, 0), "reason": reason}


def candle_quality(c):
    rng = c["high"] - c["low"]
    if rng <= 0:
        return 0, False, False

    body = abs(c["close"] - c["open"])
    ratio = body / rng

    bull = (
        c["close"] > c["open"]
        and ratio >= 0.45
        and c["close"] >= c["low"] + 0.60 * rng
    )
    bear = (
        c["close"] < c["open"]
        and ratio >= 0.45
        and c["close"] <= c["high"] - 0.60 * rng
    )

    return ratio, bull, bear


def build_signal(c1, context):
    side = context["side"]

    if side == "NEUTRAL" or len(c1) < 80:
        return None, "NO_4H_DIRECTION"

    closes = [c["close"] for c in c1]
    e20 = ema(closes, 20)
    e50 = ema(closes, 50)
    r = rsi(closes, 14)
    a = atr(c1, 14)

    if None in (e20, e50, r, a) or a <= 0:
        return None, "INDICATORS_UNAVAILABLE"

    last = c1[-1]
    prev = c1[-2]
    prior = c1[-21:-1]

    resistance = max(c["high"] for c in prior)
    support = min(c["low"] for c in prior)

    body_ratio, bull, bear = candle_quality(last)

    avol = avg_volume(c1[-21:], 20)
    relvol = (last["volume"] / avol) if avol and avol > 0 else 0

    score = context["score"]
    reasons = []

    # Trend alignment on 1H.
    if side == "LONG" and last["close"] > e20 > e50:
        score += 10
        reasons.append("1H_EMA_ALIGNMENT")
    elif side == "SHORT" and last["close"] < e20 < e50:
        score += 10
        reasons.append("1H_EMA_ALIGNMENT")

    # Breakout / breakdown.
    breakout = last["close"] > resistance
    breakdown = last["close"] < support

    # Sweep and reclaim.
    long_sweep = last["low"] < support and last["close"] > support
    short_sweep = last["high"] > resistance and last["close"] < resistance

    # Pullback through EMA20 followed by reclaim/rejection.
    long_reclaim = (
        prev["close"] <= e20
        and last["close"] > e20
        and bull
    )
    short_reject = (
        prev["close"] >= e20
        and last["close"] < e20
        and bear
    )

    # Continuation: previous candle held the EMA and current candle
    # continues in trend direction.
    long_cont = (
        prev["low"] <= e20 * 1.003
        and last["close"] > prev["high"]
        and bull
    )
    short_cont = (
        prev["high"] >= e20 * 0.997
        and last["close"] < prev["low"]
        and bear
    )

    trigger = False

    if side == "LONG":
        if breakout:
            score += 20
            reasons.append("BREAKOUT")
            trigger = True
        if long_sweep:
            score += 20
            reasons.append("SWEEP_RECLAIM")
            trigger = True
        if long_reclaim:
            score += 15
            reasons.append("EMA20_RECLAIM")
            trigger = True
        if long_cont:
            score += 15
            reasons.append("TREND_CONTINUATION")
            trigger = True
        if bull:
            score += 5
            reasons.append("BULLISH_CANDLE")

        if 55 <= r <= 72:
            score += 5
            reasons.append("RSI_CONFIRMATION")
        elif r > 78:
            score -= 10
            reasons.append("RSI_OVEREXTENDED")

        if relvol >= 1.30:
            score += 10
            reasons.append("HIGH_RELATIVE_VOLUME")
        elif relvol >= 1.05:
            score += 5
            reasons.append("VOLUME_CONFIRMATION")

        distance = (last["close"] - e20) / a
        if distance > MAX_DISTANCE_FROM_EMA20_ATR:
            score -= 15
            reasons.append("PRICE_TOO_EXTENDED")

        if not trigger:
            return None, f"NO_LONG_TRIGGER_SCORE_{score}"

        if score < MIN_SCORE:
            return None, f"LONG_SCORE_{score}_BELOW_{MIN_SCORE}"

        entry = last["close"]

        structural_sl = min(c["low"] for c in c1[-8:])
        sl = structural_sl - ATR_STOP_BUFFER * a

        risk = entry - sl
        if risk <= 0:
            return None, "INVALID_LONG_RISK"

        tp1 = entry + 2.0 * risk
        tp2 = entry + 3.0 * risk

        return {
            "side": "LONG",
            "entry": entry,
            "sl": sl,
            "tp1": tp1,
            "tp2": tp2,
            "rr": 2.0,
            "score": min(max(score, 0), 100),
            "rsi": r,
            "relvol": relvol,
            "reasons": reasons,
            "time": last["time"],
        }, "VALID_LONG"

    # SHORT
    if breakdown:
        score += 20
        reasons.append("BREAKDOWN")
        trigger = True
    if short_sweep:
        score += 20
        reasons.append("SWEEP_REJECTION")
        trigger = True
    if short_reject:
        score += 15
        reasons.append("EMA20_REJECTION")
        trigger = True
    if short_cont:
        score += 15
        reasons.append("TREND_CONTINUATION")
        trigger = True
    if bear:
        score += 5
        reasons.append("BEARISH_CANDLE")

    if 28 <= r <= 45:
        score += 5
        reasons.append("RSI_CONFIRMATION")
    elif r < 22:
        score -= 10
        reasons.append("RSI_OVEREXTENDED")

    if relvol >= 1.30:
        score += 10
        reasons.append("HIGH_RELATIVE_VOLUME")
    elif relvol >= 1.05:
        score += 5
        reasons.append("VOLUME_CONFIRMATION")

    distance = (e20 - last["close"]) / a
    if distance > MAX_DISTANCE_FROM_EMA20_ATR:
        score -= 15
        reasons.append("PRICE_TOO_EXTENDED")

    if not trigger:
        return None, f"NO_SHORT_TRIGGER_SCORE_{score}"

    if score < MIN_SCORE:
        return None, f"SHORT_SCORE_{score}_BELOW_{MIN_SCORE}"

    entry = last["close"]

    structural_sl = max(c["high"] for c in c1[-8:])
    sl = structural_sl + ATR_STOP_BUFFER * a

    risk = sl - entry
    if risk <= 0:
        return None, "INVALID_SHORT_RISK"

    tp1 = entry - 2.0 * risk
    tp2 = entry - 3.0 * risk

    return {
        "side": "SHORT",
        "entry": entry,
        "sl": sl,
        "tp1": tp1,
        "tp2": tp2,
        "rr": 2.0,
        "score": min(max(score, 0), 100),
        "rsi": r,
        "relvol": relvol,
        "reasons": reasons,
        "time": last["time"],
    }, "VALID_SHORT"


def fmt_price(x):
    if x == 0:
        return "0"
    return f"{x:.10g}"


def telegram_message(symbol, context, signal):
    dt = datetime.fromtimestamp(signal["time"], tz=timezone.utc)
    emoji = "🟢" if signal["side"] == "LONG" else "🔴"

    return (
        f"{emoji} OMPFinex SIGNAL V6\n\n"
        f"#{symbol}\n"
        f"Direction: {signal['side']}\n"
        f"4H: {context['reason']}\n"
        f"Score: {signal['score']}/100\n\n"
        f"Entry: {fmt_price(signal['entry'])}\n"
        f"SL: {fmt_price(signal['sl'])}\n"
        f"TP1: {fmt_price(signal['tp1'])}\n"
        f"TP2: {fmt_price(signal['tp2'])}\n"
        f"R:R: 1:{signal['rr']:.1f}\n\n"
        f"RSI(1H): {signal['rsi']:.1f}\n"
        f"Relative Volume: {signal['relvol']:.2f}x\n"
        f"Setup: {', '.join(signal['reasons'])}\n"
        f"Candle: {dt.strftime('%Y-%m-%d %H:%M UTC')}\n\n"
        "⚠️ Signal only. No order was placed."
    )


def send_telegram(text):
    if not BOT_TOKEN or not CHAT_ID:
        print("[TELEGRAM] Secrets not configured; message not sent.")
        return False

    r = SESSION.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        json={"chat_id": CHAT_ID, "text": text},
        timeout=20,
    )
    r.raise_for_status()

    data = r.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram error: {data}")

    return True


def main():
    print("=" * 78)
    print("OMPFinex SIGNAL SCANNER - VERSION 6")
    print("4H TREND -> 1H TRIGGER | LONG/SHORT ONLY")
    print("=" * 78)

    valid = []

    for symbol in SYMBOLS:
        try:
            c1 = get_candles(symbol, 60, LOOKBACK_HOURS)
            c4 = aggregate_4h(c1)

            if len(c1) < 80 or len(c4) < 50:
                print(f"[SKIP] {symbol} | insufficient closed candles")
                continue

            context = trend_context(c4)
            signal, status = build_signal(c1, context)

            print(
                f"[SCAN] {symbol} | "
                f"4H={context['side']} | "
                f"CTX={context['score']} | "
                f"{context['reason']}"
            )

            if signal:
                print(
                    f"[VALID {signal['side']}] {symbol} | "
                    f"score={signal['score']} | "
                    f"RR=1:{signal['rr']:.1f}"
                )
                valid.append((symbol, context, signal))
            else:
                print(f"[FILTERED] {symbol} | {status}")

        except requests.RequestException as e:
            print(f"[ERROR] {symbol} | HTTP: {e}")
        except Exception as e:
            print(f"[ERROR] {symbol} | {type(e).__name__}: {e}")

    print("\n" + "=" * 78)
    print("FINAL RESULT")
    print("=" * 78)

    if not valid:
        print("NO VALID LONG/SHORT SETUP FOUND")
        print("NO TELEGRAM MESSAGE WILL BE SENT.")
        return

    # One message per valid symbol. No WAIT messages.
    for symbol, context, signal in valid:
        text = telegram_message(symbol, context, signal)
        print("\n" + text)

        try:
            if send_telegram(text):
                print(f"[TELEGRAM] SENT | {symbol} | {signal['side']}")
        except Exception as e:
            print(f"[TELEGRAM ERROR] {symbol} | {e}")

    print("\nSCAN FINISHED")


if __name__ == "__main__":
    main()

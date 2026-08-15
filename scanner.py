import os
import requests
from datetime import datetime, timezone

BASE_URL = "https://api.ompfinex.com/v2/udf/real/history"
SYMBOLS = ["BTCUSDT", "ETHUSDT", "XRPUSDT", "ADAUSDT", "SOLUSDT"]
MIN_SCORE = 75
MIN_RR = 2.0
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()


def get_1h_candles(symbol, hours=520):
    now = int(datetime.now(timezone.utc).timestamp())
    params = {"symbol": symbol, "from": now - hours * 3600, "to": now, "resolution": 60}
    r = requests.get(BASE_URL, params=params, timeout=30)
    r.raise_for_status()
    data = r.json()
    if data.get("s") != "ok":
        raise RuntimeError(f"{symbol}: {data}")
    candles = [{
        "time": int(data["t"][i]), "open": float(data["o"][i]),
        "high": float(data["h"][i]), "low": float(data["l"][i]),
        "close": float(data["c"][i]), "volume": float(data["v"][i])
    } for i in range(len(data["t"]))]
    candles.sort(key=lambda x: x["time"])
    return candles[:-1] if len(candles) > 1 else candles


def aggregate_4h(candles):
    out, bucket = [], []
    for c in candles:
        bucket.append(c)
        if len(bucket) == 4:
            out.append({
                "time": bucket[0]["time"], "open": bucket[0]["open"],
                "high": max(x["high"] for x in bucket), "low": min(x["low"] for x in bucket),
                "close": bucket[-1]["close"], "volume": sum(x["volume"] for x in bucket)
            })
            bucket = []
    return out


def ema(values, period):
    if len(values) < period: return None
    k = 2 / (period + 1)
    e = sum(values[:period]) / period
    for v in values[period:]: e = v * k + e * (1 - k)
    return e


def atr(candles, period=14):
    if len(candles) < period + 1: return None
    trs = []
    for i in range(1, len(candles)):
        h, l, pc = candles[i]["high"], candles[i]["low"], candles[i-1]["close"]
        trs.append(max(h-l, abs(h-pc), abs(l-pc)))
    return sum(trs[-period:]) / period if len(trs) >= period else None


def swing_highs(candles, left=2, right=2):
    return [i for i in range(left, len(candles)-right)
            if all(candles[i]["high"] > candles[j]["high"] for j in range(i-left, i))
            and all(candles[i]["high"] >= candles[j]["high"] for j in range(i+1, i+right+1))]


def swing_lows(candles, left=2, right=2):
    return [i for i in range(left, len(candles)-right)
            if all(candles[i]["low"] < candles[j]["low"] for j in range(i-left, i))
            and all(candles[i]["low"] <= candles[j]["low"] for j in range(i+1, i+right+1))]


def get_4h_bias(c4):
    if len(c4) < 100: return "NEUTRAL", "INSUFFICIENT_4H_DATA"
    e20 = ema([x["close"] for x in c4], 20)
    hs, ls = swing_highs(c4), swing_lows(c4)
    if e20 is None or len(hs) < 3 or len(ls) < 3: return "NEUTRAL", "INSUFFICIENT_STRUCTURE"
    h1, h2, l1, l2 = hs[-1], hs[-2], ls[-1], ls[-2]
    hh = c4[h1]["high"] > c4[h2]["high"]
    hl = c4[l1]["low"] > c4[l2]["low"]
    lh = c4[h1]["high"] < c4[h2]["high"]
    ll = c4[l1]["low"] < c4[l2]["low"]
    close = c4[-1]["close"]
    if close > e20 and hh and hl and close > c4[h1]["high"]:
        return "LONG", "HH_HL_EMA20_BULLISH_BOS"
    if close < e20 and lh and ll and close < c4[l1]["low"]:
        return "SHORT", "LH_LL_EMA20_BEARISH_BOS"
    return "NEUTRAL", "4H_BIAS_NOT_CONFIRMED"


def detect_setup(c1, bias):
    if len(c1) < 100 or bias not in ("LONG", "SHORT"): return None
    last, prev = c1[-1], c1[:-1]
    e20, a14 = ema([x["close"] for x in c1], 20), atr(c1, 14)
    if e20 is None or a14 is None or a14 <= 0: return None
    hs, ls = swing_highs(prev), swing_lows(prev)
    if len(hs) < 3 or len(ls) < 3: return None
    recent_high, recent_low = prev[hs[-1]]["high"], prev[ls[-1]]["low"]
    body, rng = abs(last["close"]-last["open"]), last["high"]-last["low"]
    if rng <= 0 or rng > 2.2*a14: return None
    bull, bear = last["close"] > last["open"], last["close"] < last["open"]

    if bias == "LONG":
        structure = 25 if last["close"] > e20 else 0
        trend = 20 if last["close"] > e20 else 0
        momentum = 15 if bull and body >= .35*rng else 0
        sweep = last["low"] < recent_low and last["close"] > recent_low and bull
        br = prev[-1]["close"] > recent_high and last["low"] <= recent_high and last["close"] > recent_high and bull
        bos = 20 if prev[-1]["close"] > recent_high or last["close"] > recent_high else 0
        score = structure + bos + (20 if sweep else 0) + momentum + trend
        if not (sweep or br) or score < MIN_SCORE: return None
        entry = last["close"]
        sl = (last["low"] - .10*a14) if sweep else (min(last["low"], recent_high) - .10*a14)
        risk = entry-sl
        if risk <= 0: return None
        tp1, tp2 = entry+2*risk, entry+3*risk
        return {"side":"LONG", "type":"LIQUIDITY SWEEP" if sweep else "BREAK & RETEST", "score":score, "entry":entry, "sl":sl, "tp1":tp1, "tp2":tp2, "rr":2.0}

    structure = 25 if last["close"] < e20 else 0
    trend = 20 if last["close"] < e20 else 0
    momentum = 15 if bear and body >= .35*rng else 0
    sweep = last["high"] > recent_high and last["close"] < recent_high and bear
    br = prev[-1]["close"] < recent_low and last["high"] >= recent_low and last["close"] < recent_low and bear
    bos = 20 if prev[-1]["close"] < recent_low or last["close"] < recent_low else 0
    score = structure + bos + (20 if sweep else 0) + momentum + trend
    if not (sweep or br) or score < MIN_SCORE: return None
    entry = last["close"]
    sl = (last["high"] + .10*a14) if sweep else (max(last["high"], recent_low) + .10*a14)
    risk = sl-entry
    if risk <= 0: return None
    tp1, tp2 = entry-2*risk, entry-3*risk
    return {"side":"SHORT", "type":"LIQUIDITY SWEEP" if sweep else "BREAK & RETEST", "score":score, "entry":entry, "sl":sl, "tp1":tp1, "tp2":tp2, "rr":2.0}


def send_telegram(message):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram secrets are not configured; signal preview only.")
        print(message)
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    r = requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": message}, timeout=20)
    r.raise_for_status()


def format_signal(symbol, s):
    return ("🚨 OMPFINEX FUTURES SIGNAL\n\n"
            f"#{symbol}\n📌 {s['side']}\n🧠 Setup: {s['type']}\n⭐ Score: {s['score']}/100\n\n"
            f"Entry: {s['entry']:.8g}\nSL: {s['sl']:.8g}\nTP1: {s['tp1']:.8g}\nTP2: {s['tp2']:.8g}\nRR: 1:{s['rr']:.2f}\n\n"
            "⚠️ Closed-candle confirmation only")


def main():
    print("="*72)
    print("OMPFinex FUTURES SCANNER - VERSION 3")
    print("ONLY qualified LONG / SHORT signals are sent to Telegram.")
    print("="*72)
    signals = []
    for symbol in SYMBOLS:
        try:
            c1 = get_1h_candles(symbol)
            if len(c1) < 500:
                print(f"[SKIP] {symbol} | insufficient 1H candles: {len(c1)}")
                continue
            c4 = aggregate_4h(c1)
            if len(c4) < 100:
                print(f"[SKIP] {symbol} | insufficient 4H candles: {len(c4)}")
                continue
            bias, reason = get_4h_bias(c4)
            signal = detect_setup(c1, bias)
            print(f"[SCAN] {symbol} | 4H={bias} | {reason}")
            if signal:
                msg = format_signal(symbol, signal)
                signals.append(msg)
                print(f"[SIGNAL] {symbol} {signal['side']} | score={signal['score']} | RR=1:{signal['rr']:.2f}")
            else:
                print(f"[NO SIGNAL] {symbol}")
        except Exception as e:
            print(f"[ERROR] {symbol}: {e}")
    print("\n" + "="*72 + "\nFINAL SIGNALS\n" + "="*72)
    if not signals:
        print("NO VALID LONG/SHORT SETUP FOUND")
        print("Nothing will be sent to Telegram.")
    else:
        for msg in signals:
            print(msg)
            print("-"*72)
            send_telegram(msg)
    print("SCAN FINISHED")


if __name__ == "__main__":
    main()

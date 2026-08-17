#!/usr/bin/env python3
"""
OMPFinex SIGNAL SCANNER - VERSION 7
4H TREND -> 1H TRIGGER | LONG/SHORT ONLY
Dynamic universe: up to 100 USDT crypto markets discovered from OMPFinex.

This version keeps the signal philosophy of V6 but removes the fixed 5-symbol list.
It discovers OMPFinex USDT symbols through the official UDF search API, validates
them with candle data, then scans up to 100 symbols.

Environment variables:
  OMPFINEX_BASE_URL      default: https://api.ompfinex.com
  OMPFINEX_SYMBOL_PREFIX default: OMPFinex:
  TELEGRAM_BOT_TOKEN
  TELEGRAM_CHAT_ID

No orders are placed. Telegram receives only qualified LONG/SHORT signals.
"""

from __future__ import annotations

import os
import time
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

import requests


BASE_URL = os.getenv("OMPFINEX_BASE_URL", "https://api.ompfinex.com").rstrip("/")
SYMBOL_PREFIX = os.getenv("OMPFINEX_SYMBOL_PREFIX", "OMPFinex:")
TARGET_SYMBOL_COUNT = int(os.getenv("SCAN_SYMBOL_COUNT", "100"))
REQUEST_TIMEOUT = float(os.getenv("REQUEST_TIMEOUT", "15"))
DISCOVERY_WORKERS = int(os.getenv("DISCOVERY_WORKERS", "6"))

# V6-style thresholds.
MIN_SIGNAL_SCORE = 65
ATR_PERIOD = 14
RSI_PERIOD = 14
EMA_FAST = 9
EMA_SLOW = 21
EMA_CONTEXT_FAST = 50
EMA_CONTEXT_SLOW = 200
REL_VOL_PERIOD = 20

session = requests.Session()
session.headers.update({"User-Agent": "OMPFinex-Signal-Scanner/7.0"})


@dataclass
class Candle:
    ts: int
    o: float
    h: float
    l: float
    c: float
    v: float


@dataclass
class Signal:
    symbol: str
    direction: str
    score: int
    entry: float
    sl: float
    tp1: float
    tp2: float
    rr: float
    rsi: float
    rel_volume: float
    context: str
    setup: list[str]
    candle_ts: int


def http_get(path: str, params: Optional[dict[str, Any]] = None) -> Any:
    url = f"{BASE_URL}{path}"
    r = session.get(url, params=params, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    return r.json()


def discover_usdt_symbols(target: int = TARGET_SYMBOL_COUNT) -> list[str]:
    """
    OMPFinex's documented search endpoint accepts a query and max limit 50.
    To avoid depending on an undocumented pagination parameter, query several
    USDT prefixes and deduplicate. We then validate each symbol with history.
    """
    found: set[str] = set()

    queries = ["USDT"]
    queries += [f"{chr(65+i)}USDT" for i in range(26)]
    queries += [f"{chr(65+i)}" for i in range(26)]

    for q in queries:
        try:
            data = http_get(
                "/v2/udf/real/search",
                {"query": q, "limit": 50},
            )
        except Exception as exc:
            print(f"[DISCOVERY ERROR] query={q} | {exc}")
            continue

        if not isinstance(data, list):
            continue

        for item in data:
            if not isinstance(item, dict):
                continue
            symbol = str(item.get("ticker") or item.get("symbol") or "").upper().strip()
            typ = str(item.get("type") or "").lower()
            exchange = str(item.get("exchange") or "").lower()

            if (
                symbol.endswith("USDT")
                and symbol.isalnum()
                and typ in ("", "crypto")
                and (not exchange or "ompfinex" in exchange)
            ):
                found.add(symbol)

        if len(found) >= target * 2:
            break

    # Stable order makes runs reproducible. We intentionally do not claim
    # alphabetical order means "best"; this is the exchange-listed universe.
    return sorted(found)[:target]


def fetch_history(symbol: str, resolution: int, bars: int = 260) -> list[Candle]:
    """
    Fetch enough candles for EMA-200/context plus trigger indicators.
    OMPFinex documents UDF history as a public endpoint. Different deployments
    may accept a prefixed or plain symbol, so try the configured prefix first
    and then the plain ticker.
    """
    now = int(time.time())
    # Extra lookback gives the endpoint room to return a full EMA-200 window.
    seconds = resolution * 60
    start = now - seconds * max(bars + 50, 320)

    candidates = []
    if SYMBOL_PREFIX:
        candidates.append(f"{SYMBOL_PREFIX}{symbol}")
    candidates.append(symbol)

    last_error = None

    for api_symbol in candidates:
        try:
            data = http_get(
                "/v2/udf/real/history",
                {
                    "symbol": api_symbol,
                    "from": start,
                    "to": now,
                    "resolution": resolution,
                },
            )

            if not isinstance(data, dict):
                continue
            if data.get("s") != "ok":
                continue

            o, h, l, c, v, t = (
                data.get("o", []),
                data.get("h", []),
                data.get("l", []),
                data.get("c", []),
                data.get("v", []),
                data.get("t", []),
            )

            n = min(len(o), len(h), len(l), len(c), len(v), len(t))
            if n < 220:
                continue

            candles = []
            for i in range(n):
                try:
                    candles.append(
                        Candle(
                            int(t[i]),
                            float(o[i]),
                            float(h[i]),
                            float(l[i]),
                            float(c[i]),
                            float(v[i]),
                        )
                    )
                except (TypeError, ValueError):
                    pass

            if len(candles) >= 220:
                return candles[-bars:]

        except Exception as exc:
            last_error = exc

    raise RuntimeError(f"history unavailable for {symbol}: {last_error}")


def ema(values: list[float], period: int) -> list[float]:
    if len(values) < period:
        return [math.nan] * len(values)

    out = [math.nan] * (period - 1)
    seed = sum(values[:period]) / period
    out.append(seed)
    alpha = 2.0 / (period + 1)

    prev = seed
    for x in values[period:]:
        prev = alpha * x + (1 - alpha) * prev
        out.append(prev)
    return out


def rsi(values: list[float], period: int = RSI_PERIOD) -> float:
    if len(values) <= period:
        return math.nan

    gains = []
    losses = []
    for i in range(1, len(values)):
        d = values[i] - values[i - 1]
        gains.append(max(d, 0.0))
        losses.append(max(-d, 0.0))

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period

    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def atr(candles: list[Candle], period: int = ATR_PERIOD) -> float:
    if len(candles) < period + 1:
        return math.nan

    trs = []
    for i in range(1, len(candles)):
        prev = candles[i - 1].c
        cur = candles[i]
        trs.append(max(cur.h - cur.l, abs(cur.h - prev), abs(cur.l - prev)))

    return sum(trs[-period:]) / period


def last_closed_index(candles: list[Candle], resolution_minutes: int) -> int:
    """
    Avoid using a still-forming candle. If the newest candle is not closed,
    use the previous candle.
    """
    if not candles:
        return -1
    now = int(time.time())
    idx = len(candles) - 1
    if now < candles[idx].ts + resolution_minutes * 60:
        idx -= 1
    return idx


def context_4h(candles: list[Candle]) -> tuple[str, int, str]:
    closes = [x.c for x in candles]
    e50 = ema(closes, EMA_CONTEXT_FAST)
    e200 = ema(closes, EMA_CONTEXT_SLOW)

    idx = last_closed_index(candles, 240)
    if idx < 2 or math.isnan(e50[idx]) or math.isnan(e200[idx]):
        return "NEUTRAL", 0, "INSUFFICIENT_CONTEXT"

    # Require both EMA structure and recent slope.
    slope50 = e50[idx] - e50[max(0, idx - 5)]
    slope200 = e200[idx] - e200[max(0, idx - 5)]
    close = closes[idx]

    bullish = close > e50[idx] > e200[idx] and slope50 > 0 and slope200 >= 0
    bearish = close < e50[idx] < e200[idx] and slope50 < 0 and slope200 <= 0

    if bullish:
        return "LONG", 35, "4H_UPTREND"
    if bearish:
        return "SHORT", 35, "4H_DOWNTREND"
    return "NEUTRAL", 0, "4H_MIXED"


def trigger_1h(candles: list[Candle], direction: str, context_score: int) -> Optional[Signal]:
    idx = last_closed_index(candles, 60)
    if idx < 30:
        return None

    c = candles[: idx + 1]
    closes = [x.c for x in c]
    highs = [x.h for x in c]
    lows = [x.l for x in c]
    volumes = [x.v for x in c]

    e9 = ema(closes, EMA_FAST)
    e21 = ema(closes, EMA_SLOW)
    r = rsi(closes)
    a = atr(c)
    if any(math.isnan(x) for x in (e9[-1], e21[-1], r, a)) or a <= 0:
        return None

    recent_vol = volumes[-1]
    base_vols = volumes[-REL_VOL_PERIOD-1:-1]
    avg_vol = sum(base_vols) / len(base_vols) if base_vols else 0
    rel_vol = recent_vol / avg_vol if avg_vol > 0 else 0

    cur = c[-1]
    prev = c[-2]
    recent_high = max(highs[-6:-1])
    recent_low = min(lows[-6:-1])

    score = context_score
    setup = []

    if direction == "LONG":
        if e9[-1] > e21[-1]:
            score += 15
            setup.append("1H_EMA_ALIGNMENT")

        breakout = cur.c > recent_high
        if breakout:
            score += 20
            setup.append("BREAKOUT")

        if r >= 50:
            score += 15
            setup.append("RSI_CONFIRMATION")

        if rel_vol >= 1.5:
            score += 15
            setup.append("HIGH_RELATIVE_VOLUME")

        # Momentum candle quality.
        if cur.c > cur.o and cur.c > prev.c:
            score += 5

        if score < MIN_SIGNAL_SCORE or not (e9[-1] > e21[-1] and r >= 50 and breakout):
            return None

        entry = cur.c
        sl = min(cur.l, entry - 1.0 * a)
        risk = entry - sl
        if risk <= 0:
            return None
        tp1 = entry + risk * 1.5
        tp2 = entry + risk * 2.0
        rr = (tp2 - entry) / risk

        return Signal(
            symbol="", direction="LONG", score=min(score, 100),
            entry=entry, sl=sl, tp1=tp1, tp2=tp2, rr=rr,
            rsi=r, rel_volume=rel_vol,
            context="4H_UPTREND", setup=setup, candle_ts=cur.ts,
        )

    if direction == "SHORT":
        if e9[-1] < e21[-1]:
            score += 15
            setup.append("1H_EMA_ALIGNMENT")

        breakdown = cur.c < recent_low
        if breakdown:
            score += 20
            setup.append("BREAKDOWN")

        if r <= 50:
            score += 15
            setup.append("RSI_CONFIRMATION")

        if rel_vol >= 1.5:
            score += 15
            setup.append("HIGH_RELATIVE_VOLUME")

        if cur.c < cur.o and cur.c < prev.c:
            score += 5

        if score < MIN_SIGNAL_SCORE or not (e9[-1] < e21[-1] and r <= 50 and breakdown):
            return None

        entry = cur.c
        sl = max(cur.h, entry + 1.0 * a)
        risk = sl - entry
        if risk <= 0:
            return None
        tp1 = entry - risk * 1.5
        tp2 = entry - risk * 2.0
        rr = (entry - tp2) / risk

        return Signal(
            symbol="", direction="SHORT", score=min(score, 100),
            entry=entry, sl=sl, tp1=tp1, tp2=tp2, rr=rr,
            rsi=r, rel_volume=rel_vol,
            context="4H_DOWNTREND", setup=setup, candle_ts=cur.ts,
        )

    return None


def scan_symbol(symbol: str) -> Optional[Signal]:
    try:
        c4h = fetch_history(symbol, 240, 260)
        direction, ctx_score, ctx_reason = context_4h(c4h)

        print(f"[SCAN] {symbol} | 4H={direction} | CTX={ctx_score} | {ctx_reason}")

        if direction == "NEUTRAL":
            print(f"[FILTERED] {symbol} | NO_4H_DIRECTION")
            return None

        c1h = fetch_history(symbol, 60, 260)
        sig = trigger_1h(c1h, direction, ctx_score)

        if sig is None:
            print(f"[FILTERED] {symbol} | NO_{direction}_TRIGGER")
            return None

        sig.symbol = symbol
        print(
            f"[VALID {sig.direction}] {symbol} | score={sig.score} | "
            f"RR=1:{sig.rr:.1f}"
        )
        return sig

    except Exception as exc:
        print(f"[ERROR] {symbol} | {exc}")
        return None


def fmt_price(x: float) -> str:
    if x >= 1000:
        return f"{x:.2f}"
    if x >= 1:
        return f"{x:.4f}"
    if x >= 0.01:
        return f"{x:.6f}"
    return f"{x:.10f}".rstrip("0").rstrip(".")


def telegram_send(text: str) -> bool:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()

    if not token or not chat_id:
        print("[TELEGRAM] Secrets not configured; message not sent.")
        return False

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        r = session.post(
            url,
            json={"chat_id": chat_id, "text": text},
            timeout=REQUEST_TIMEOUT,
        )
        r.raise_for_status()
        print("[TELEGRAM] Message sent.")
        return True
    except Exception as exc:
        print(f"[TELEGRAM] Send failed: {exc}")
        return False


def signal_message(sig: Signal) -> str:
    icon = "🟢" if sig.direction == "LONG" else "🔴"
    dt = datetime.fromtimestamp(sig.candle_ts, tz=timezone.utc)
    return (
        f"{icon} OMPFinex SIGNAL V7\n"
        f"#{sig.symbol}\n"
        f"Direction: {sig.direction}\n"
        f"4H: {sig.context}\n"
        f"Score: {sig.score}/100\n"
        f"Entry: {fmt_price(sig.entry)}\n"
        f"SL: {fmt_price(sig.sl)}\n"
        f"TP1: {fmt_price(sig.tp1)}\n"
        f"TP2: {fmt_price(sig.tp2)}\n"
        f"R:R: 1:{sig.rr:.1f}\n"
        f"RSI(1H): {sig.rsi:.1f}\n"
        f"Relative Volume: {sig.rel_volume:.2f}x\n"
        f"Setup: {', '.join(sig.setup)}\n"
        f"Candle: {dt:%Y-%m-%d %H:%M} UTC\n"
        f"⚠️ Signal only. No order was placed."
    )


def main() -> None:
    print("=" * 78)
    print("OMPFinex SIGNAL SCANNER - VERSION 7")
    print("DYNAMIC USDT UNIVERSE -> 4H TREND -> 1H TRIGGER | LONG/SHORT ONLY")
    print("=" * 78)

    print(f"[DISCOVERY] Target symbols: {TARGET_SYMBOL_COUNT}")
    symbols = discover_usdt_symbols(TARGET_SYMBOL_COUNT)

    if not symbols:
        print("[FATAL] No USDT symbols discovered.")
        return

    print(f"[DISCOVERY] Found {len(symbols)} candidate USDT symbols.")
    print("[UNIVERSE] " + ", ".join(symbols))

    signals: list[Signal] = []
    for i, symbol in enumerate(symbols, 1):
        print(f"\n[{i}/{len(symbols)}] ------------------------------")
        sig = scan_symbol(symbol)
        if sig:
            signals.append(sig)

    print("\n" + "=" * 78)
    print("FINAL RESULT")
    print("=" * 78)

    if not signals:
        print("NO VALID LONG/SHORT SETUP FOUND")
        print("NO TELEGRAM MESSAGE WILL BE SENT")
    else:
        # Highest score first. Do not send duplicate alerts for the same symbol.
        signals.sort(key=lambda s: (-s.score, -s.rr))
        for sig in signals:
            print(signal_message(sig))
            telegram_send(signal_message(sig))

    print("=" * 78)
    print(f"SCAN FINISHED | symbols={len(symbols)} | signals={len(signals)}")


if __name__ == "__main__":
    main()

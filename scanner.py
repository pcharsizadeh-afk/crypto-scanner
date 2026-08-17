#!/usr/bin/env python3
"""
OMPFinex SIGNAL SCANNER - VERSION 8
4H TREND -> 1H MULTI-SETUP TRIGGER | LONG/SHORT ONLY

Purpose:
- Scan up to 100 OMPFinex USDT crypto markets.
- Use only CLOSED candles.
- Keep the 4H directional filter from V6/V7.
- Replace the single "breakout/breakdown on the last candle" trigger with
  several independent 1H setups.
- Inspect the last N CLOSED 1H candles so a valid setup is not missed merely
  because it happened one or two candles ago.
- Send ONLY qualified LONG/SHORT alerts to Telegram.
- Never place an order.

Environment variables:
  OMPFINEX_BASE_URL        default https://api.ompfinex.com
  OMPFINEX_SYMBOL_PREFIX   default OMPFinex:
  SCAN_SYMBOL_COUNT        default 100
  SCAN_LOOKBACK_TRIGGERS   default 5
  MIN_SIGNAL_SCORE         default 70
  TELEGRAM_BOT_TOKEN
  TELEGRAM_CHAT_ID

Important:
- This is a signal scanner, not an execution bot.
- The strategy is intentionally not loosened by simply lowering the score.
  Instead, several valid entry structures are recognized and scored.
"""

from __future__ import annotations

import math
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

import requests


BASE_URL = os.getenv("OMPFINEX_BASE_URL", "https://api.ompfinex.com").rstrip("/")
SYMBOL_PREFIX = os.getenv("OMPFINEX_SYMBOL_PREFIX", "OMPFinex:")

TARGET_SYMBOL_COUNT = int(os.getenv("SCAN_SYMBOL_COUNT", "100"))
LOOKBACK_TRIGGERS = max(3, int(os.getenv("SCAN_LOOKBACK_TRIGGERS", "5")))
MIN_SIGNAL_SCORE = int(os.getenv("MIN_SIGNAL_SCORE", "70"))

REQUEST_TIMEOUT = float(os.getenv("REQUEST_TIMEOUT", "15"))

ATR_PERIOD = 14
RSI_PERIOD = 14
EMA_FAST = 9
EMA_SLOW = 21
EMA_CONTEXT_FAST = 50
EMA_CONTEXT_SLOW = 200
REL_VOL_PERIOD = 20

session = requests.Session()
session.headers.update({"User-Agent": "OMPFinex-Signal-Scanner/8.0"})


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
    r = session.get(f"{BASE_URL}{path}", params=params, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    return r.json()


def discover_usdt_symbols(target: int = TARGET_SYMBOL_COUNT) -> list[str]:
    """Discover OMPFinex USDT markets and return up to target unique symbols."""
    found: set[str] = set()

    queries = ["USDT"]
    queries += [f"{chr(65+i)}USDT" for i in range(26)]
    queries += [chr(65+i) for i in range(26)]

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

            symbol = str(
                item.get("ticker") or item.get("symbol") or ""
            ).upper().strip()
            typ = str(item.get("type") or "").lower()
            exchange = str(item.get("exchange") or "").lower()

            if (
                symbol.endswith("USDT")
                and symbol.isalnum()
                and typ in ("", "crypto")
                and (not exchange or "ompfinex" in exchange)
            ):
                found.add(symbol)

        if len(found) >= target:
            break

    return sorted(found)[:target]


def fetch_history(
    symbol: str,
    resolution: int,
    bars: int = 300,
) -> list[Candle]:
    """Fetch enough history for EMA200 and the 1H trigger logic."""
    now = int(time.time())
    seconds = resolution * 60
    start = now - seconds * max(bars + 80, 380)

    candidates = []
    if SYMBOL_PREFIX:
        candidates.append(f"{SYMBOL_PREFIX}{symbol}")
    candidates.append(symbol)

    last_error: Optional[Exception] = None

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

            if not isinstance(data, dict) or data.get("s") != "ok":
                continue

            arrays = (
                data.get("o", []),
                data.get("h", []),
                data.get("l", []),
                data.get("c", []),
                data.get("v", []),
                data.get("t", []),
            )
            n = min(map(len, arrays))

            if n < 220:
                continue

            candles: list[Candle] = []
            for i in range(n):
                try:
                    candles.append(
                        Candle(
                            int(arrays[5][i]),
                            float(arrays[0][i]),
                            float(arrays[1][i]),
                            float(arrays[2][i]),
                            float(arrays[3][i]),
                            float(arrays[4][i]),
                        )
                    )
                except (TypeError, ValueError):
                    continue

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
        prev = alpha * x + (1.0 - alpha) * prev
        out.append(prev)

    return out


def rsi_series(values: list[float], period: int = RSI_PERIOD) -> list[float]:
    out = [math.nan] * len(values)
    if len(values) <= period:
        return out

    gains: list[float] = []
    losses: list[float] = []

    for i in range(1, len(values)):
        d = values[i] - values[i - 1]
        gains.append(max(d, 0.0))
        losses.append(max(-d, 0.0))

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    def value() -> float:
        if avg_loss == 0:
            return 100.0
        rs = avg_gain / avg_loss
        return 100.0 - 100.0 / (1.0 + rs)

    out[period] = value()

    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        out[i + 1] = value()

    return out


def atr_series(
    candles: list[Candle],
    period: int = ATR_PERIOD,
) -> list[float]:
    out = [math.nan] * len(candles)
    if len(candles) <= period:
        return out

    trs: list[float] = []
    for i in range(1, len(candles)):
        prev_close = candles[i - 1].c
        cur = candles[i]
        trs.append(
            max(
                cur.h - cur.l,
                abs(cur.h - prev_close),
                abs(cur.l - prev_close),
            )
        )

    if len(trs) < period:
        return out

    out[period] = sum(trs[:period]) / period

    # Wilder-style ATR smoothing.
    prev = out[period]
    for j in range(period, len(trs)):
        prev = (prev * (period - 1) + trs[j]) / period
        out[j + 1] = prev

    return out


def last_closed_index(
    candles: list[Candle],
    resolution_minutes: int,
) -> int:
    if not candles:
        return -1

    idx = len(candles) - 1
    now = int(time.time())

    if now < candles[idx].ts + resolution_minutes * 60:
        idx -= 1

    return idx


def context_4h(candles: list[Candle]) -> tuple[str, int, str]:
    closes = [x.c for x in candles]
    e50 = ema(closes, EMA_CONTEXT_FAST)
    e200 = ema(closes, EMA_CONTEXT_SLOW)

    idx = last_closed_index(candles, 240)

    if (
        idx < 10
        or math.isnan(e50[idx])
        or math.isnan(e200[idx])
    ):
        return "NEUTRAL", 0, "INSUFFICIENT_CONTEXT"

    slope50 = e50[idx] - e50[max(0, idx - 5)]
    slope200 = e200[idx] - e200[max(0, idx - 5)]
    close = closes[idx]

    bullish = (
        close > e50[idx] > e200[idx]
        and slope50 > 0
        and slope200 >= 0
    )
    bearish = (
        close < e50[idx] < e200[idx]
        and slope50 < 0
        and slope200 <= 0
    )

    if bullish:
        return "LONG", 35, "4H_UPTREND"

    if bearish:
        return "SHORT", 35, "4H_DOWNTREND"

    return "NEUTRAL", 0, "4H_MIXED"


def candle_body_ratio(cur: Candle) -> float:
    rng = cur.h - cur.l
    if rng <= 0:
        return 0.0
    return abs(cur.c - cur.o) / rng


def build_signal_for_index(
    candles: list[Candle],
    idx: int,
    direction: str,
    context_score: int,
    context_name: str,
    e9: list[float],
    e21: list[float],
    rsis: list[float],
    atrs: list[float],
    prior_high: float,
    prior_low: float,
    avg_volume: float,
) -> Optional[Signal]:
    """Evaluate one CLOSED 1H candle as a possible trigger."""
    if idx < 30:
        return None

    cur = candles[idx]
    prev = candles[idx - 1]

    e9v = e9[idx]
    e21v = e21[idx]
    rvsi = rsis[idx]
    av = atrs[idx]

    if any(math.isnan(x) for x in (e9v, e21v, rvsi, av)) or av <= 0:
        return None

    rel_vol = cur.v / avg_volume if avg_volume > 0 else 0.0
    body_ratio = candle_body_ratio(cur)

    score = context_score
    setup: list[str] = []

    if direction == "LONG":
        # Core trend alignment.
        if e9v > e21v:
            score += 15
            setup.append("1H_EMA_ALIGNMENT")
        else:
            return None

        # Setup A: breakout of recent range.
        breakout = cur.c > prior_high
        if breakout:
            score += 20
            setup.append("BREAKOUT")

        # Setup B: bullish pullback/reclaim.
        pullback = (
            prev.l <= e21[idx - 1]
            and cur.c > e21v
            and cur.c > cur.o
        )
        if pullback:
            score += 18
            setup.append("EMA_PULLBACK_RECLAIM")

        # Setup C: momentum continuation.
        momentum = (
            cur.c > prev.c
            and cur.c > cur.o
            and cur.c > e9v
            and body_ratio >= 0.45
        )
        if momentum:
            score += 12
            setup.append("MOMENTUM_CONTINUATION")

        # Confirmation.
        if rvsi >= 52:
            score += 12
            setup.append("RSI_CONFIRMATION")
        elif rvsi >= 48:
            score += 5
            setup.append("RSI_NEUTRAL_BULLISH")

        if rel_vol >= 1.5:
            score += 10
            setup.append("HIGH_RELATIVE_VOLUME")
        elif rel_vol >= 1.1:
            score += 4
            setup.append("VOLUME_SUPPORT")

        # At least one real trigger is mandatory.
        trigger_count = sum(
            [breakout, pullback, momentum]
        )
        if trigger_count == 0:
            return None

        # Avoid buying an exhausted move too far from EMA9.
        if cur.c > e9v + 2.5 * av:
            return None

        if score < MIN_SIGNAL_SCORE:
            return None

        entry = cur.c

        # Structural stop: recent swing + ATR safety.
        swing_low = min(
            x.l for x in candles[max(0, idx - 5):idx + 1]
        )
        sl = min(swing_low, entry - 1.2 * av)
        risk = entry - sl

        if risk <= 0:
            return None

        tp1 = entry + risk * 1.5
        tp2 = entry + risk * 2.0
        rr = 2.0

        return Signal(
            symbol="",
            direction="LONG",
            score=min(score, 100),
            entry=entry,
            sl=sl,
            tp1=tp1,
            tp2=tp2,
            rr=rr,
            rsi=rvsi,
            rel_volume=rel_vol,
            context=context_name,
            setup=setup,
            candle_ts=cur.ts,
        )

    if direction == "SHORT":
        if e9v < e21v:
            score += 15
            setup.append("1H_EMA_ALIGNMENT")
        else:
            return None

        breakdown = cur.c < prior_low
        if breakdown:
            score += 20
            setup.append("BREAKDOWN")

        pullback = (
            prev.h >= e21[idx - 1]
            and cur.c < e21v
            and cur.c < cur.o
        )
        if pullback:
            score += 18
            setup.append("EMA_PULLBACK_REJECTION")

        momentum = (
            cur.c < prev.c
            and cur.c < cur.o
            and cur.c < e9v
            and body_ratio >= 0.45
        )
        if momentum:
            score += 12
            setup.append("MOMENTUM_CONTINUATION")

        if rvsi <= 48:
            score += 12
            setup.append("RSI_CONFIRMATION")
        elif rvsi <= 52:
            score += 5
            setup.append("RSI_NEUTRAL_BEARISH")

        if rel_vol >= 1.5:
            score += 10
            setup.append("HIGH_RELATIVE_VOLUME")
        elif rel_vol >= 1.1:
            score += 4
            setup.append("VOLUME_SUPPORT")

        trigger_count = sum(
            [breakdown, pullback, momentum]
        )
        if trigger_count == 0:
            return None

        if cur.c < e9v - 2.5 * av:
            return None

        entry = cur.c

        swing_high = max(
            x.h for x in candles[max(0, idx - 5):idx + 1]
        )
        sl = max(swing_high, entry + 1.2 * av)
        risk = sl - entry

        if risk <= 0:
            return None

        tp1 = entry - risk * 1.5
        tp2 = entry - risk * 2.0
        rr = 2.0

        return Signal(
            symbol="",
            direction="SHORT",
            score=min(score, 100),
            entry=entry,
            sl=sl,
            tp1=tp1,
            tp2=tp2,
            rr=rr,
            rsi=rvsi,
            rel_volume=rel_vol,
            context=context_name,
            setup=setup,
            candle_ts=cur.ts,
        )

    return None


def trigger_1h(
    candles: list[Candle],
    direction: str,
    context_score: int,
    context_name: str,
) -> Optional[Signal]:
    """
    Inspect the latest LOOKBACK_TRIGGERS CLOSED candles.
    The newest valid setup wins; if several exist, highest score wins.
    """
    idx = last_closed_index(candles, 60)

    if idx < 40:
        return None

    c = candles[:idx + 1]
    closes = [x.c for x in c]
    highs = [x.h for x in c]
    lows = [x.l for x in c]
    volumes = [x.v for x in c]

    e9 = ema(closes, EMA_FAST)
    e21 = ema(closes, EMA_SLOW)
    rsis = rsi_series(closes)
    atrs = atr_series(c)

    candidates: list[Signal] = []

    start_idx = max(30, idx - LOOKBACK_TRIGGERS + 1)

    for j in range(start_idx, idx + 1):
        # The setup's breakout range must be known BEFORE the trigger candle.
        range_start = max(0, j - 6)
        prior_high = max(highs[range_start:j])
        prior_low = min(lows[range_start:j])

        vol_start = max(0, j - REL_VOL_PERIOD)
        base_vols = volumes[vol_start:j]
        avg_volume = (
            sum(base_vols) / len(base_vols)
            if base_vols
            else 0.0
        )

        sig = build_signal_for_index(
            c,
            j,
            direction,
            context_score,
            context_name,
            e9,
            e21,
            rsis,
            atrs,
            prior_high,
            prior_low,
            avg_volume,
        )

        if sig is not None:
            candidates.append(sig)

    if not candidates:
        return None

    # Prefer higher quality, then the newest setup.
    candidates.sort(
        key=lambda s: (-s.score, -s.candle_ts)
    )
    return candidates[0]


def scan_symbol(symbol: str) -> Optional[Signal]:
    try:
        c4h = fetch_history(symbol, 240, 300)
        direction, ctx_score, ctx_reason = context_4h(c4h)

        print(
            f"[SCAN] {symbol} | 4H={direction} | "
            f"CTX={ctx_score} | {ctx_reason}"
        )

        if direction == "NEUTRAL":
            print(f"[FILTERED] {symbol} | NO_4H_DIRECTION")
            return None

        c1h = fetch_history(symbol, 60, 300)
        sig = trigger_1h(
            c1h,
            direction,
            ctx_score,
            ctx_reason,
        )

        if sig is None:
            print(
                f"[FILTERED] {symbol} | "
                f"NO_{direction}_TRIGGER_IN_LAST_{LOOKBACK_TRIGGERS}_CLOSED_BARS"
            )
            return None

        sig.symbol = symbol

        dt = datetime.fromtimestamp(
            sig.candle_ts,
            tz=timezone.utc,
        )

        print(
            f"[VALID {sig.direction}] {symbol} | "
            f"score={sig.score} | RR=1:{sig.rr:.1f} | "
            f"candle={dt:%Y-%m-%d %H:%M} UTC | "
            f"setup={','.join(sig.setup)}"
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
    dt = datetime.fromtimestamp(
        sig.candle_ts,
        tz=timezone.utc,
    )

    return (
        f"{icon} OMPFinex SIGNAL V8\n"
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
    print("OMPFinex SIGNAL SCANNER - VERSION 8")
    print("4H TREND -> 1H MULTI-SETUP TRIGGER | LONG/SHORT ONLY")
    print(
        f"Universe: up to {TARGET_SYMBOL_COUNT} USDT markets | "
        f"Trigger lookback: {LOOKBACK_TRIGGERS} closed 1H candles | "
        f"Min score: {MIN_SIGNAL_SCORE}"
    )
    print("=" * 78)

    print(
        f"[DISCOVERY] Target symbols: {TARGET_SYMBOL_COUNT}"
    )

    symbols = discover_usdt_symbols(TARGET_SYMBOL_COUNT)

    if not symbols:
        print("[FATAL] No USDT symbols discovered.")
        return

    print(
        f"[DISCOVERY] Found {len(symbols)} candidate USDT symbols."
    )
    print("[UNIVERSE] " + ", ".join(symbols))

    signals: list[Signal] = []

    for i, symbol in enumerate(symbols, 1):
        print(
            f"\n[{i}/{len(symbols)}] "
            + "-" * 50
        )

        sig = scan_symbol(symbol)
        if sig is not None:
            signals.append(sig)

    print("\n" + "=" * 78)
    print("FINAL RESULT")
    print("=" * 78)

    if not signals:
        print("NO VALID LONG/SHORT SETUP FOUND")
        print("NO TELEGRAM MESSAGE WILL BE SENT")
    else:
        # Best signals first. Deduplicate by symbol and direction.
        best: dict[tuple[str, str], Signal] = {}

        for sig in signals:
            key = (sig.symbol, sig.direction)
            old = best.get(key)

            if old is None or (
                sig.score > old.score
                or (
                    sig.score == old.score
                    and sig.candle_ts > old.candle_ts
                )
            ):
                best[key] = sig

        final_signals = sorted(
            best.values(),
            key=lambda s: (-s.score, -s.candle_ts),
        )

        print(
            f"QUALIFIED SIGNALS: {len(final_signals)}"
        )

        for sig in final_signals:
            msg = signal_message(sig)
            print(msg)
            telegram_send(msg)

    print("=" * 78)
    print(
        f"SCAN FINISHED | symbols={len(symbols)} | "
        f"signals={len(signals)}"
    )


if __name__ == "__main__":
    main()

import requests
from datetime import datetime, timezone

BASE_URL = "https://api.ompfinex.com/v2/udf/real/history"

SYMBOLS = [
    "BTCUSDT",
    "ETHUSDT",
    "XRPUSDT",
    "ADAUSDT",
    "SOLUSDT",
]

def get_candles(symbol, hours=300):
    now = int(datetime.now(timezone.utc).timestamp())
    start = now - (hours * 60 * 60)

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

    if data.get("s") != "ok":
        raise Exception(f"{symbol}: {data}")

    candles = []

    for i in range(len(data["t"])):
        candles.append({
            "time": data["t"][i],
            "open": float(data["o"][i]),
            "high": float(data["h"][i]),
            "low": float(data["l"][i]),
            "close": float(data["c"][i]),
            "volume": float(data["v"][i]),
        })

    return candles


print("=" * 60)
print("OMPFinex Crypto Scanner")
print("=" * 60)

for symbol in SYMBOLS:

    try:
        candles = get_candles(symbol)

        print()
        print(f"✓ {symbol}")
        print(f"  Candles: {len(candles)}")

        if candles:
            last = candles[-1]

            print(f"  Last close: {last['close']}")
            print(f"  Last high : {last['high']}")
            print(f"  Last low  : {last['low']}")

    except Exception as e:

        print()
        print(f"✗ {symbol}")
        print(f"  ERROR: {e}")

print()
print("=" * 60)
print("TEST FINISHED")
print("=" * 60)

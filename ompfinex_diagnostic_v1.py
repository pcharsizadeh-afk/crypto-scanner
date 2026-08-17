import requests
from datetime import datetime, timezone

BASE_URL = "https://api.ompfinex.com/v2/udf/real/history"

SYMBOLS = ["BTCUSDT", "ETHUSDT"]
RESOLUTIONS = [60, 240]

def ts_text(ts):
    if not ts:
        return "N/A"
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).isoformat()

def test(symbol, resolution):
    print()
    print("=" * 78)
    print(f"TEST SYMBOL={symbol} | RESOLUTION={resolution}")
    print("=" * 78)

    now = int(datetime.now(timezone.utc).timestamp())

    # Same basic approach that was previously known to work,
    # with a deliberately generous window.
    hours = 520 if resolution == 60 else 220 * 4
    start = now - hours * 60 * 60

    params = {
        "symbol": symbol,
        "from": start,
        "to": now,
        "resolution": resolution,
    }

    print("URL:", BASE_URL)
    print("PARAMS:", params)

    try:
        response = requests.get(
            BASE_URL,
            params=params,
            timeout=30,
        )

        print("HTTP STATUS:", response.status_code)
        print("FINAL URL:", response.url)

        response.raise_for_status()

        data = response.json()

        print("RESPONSE TYPE:", type(data).__name__)

        if not isinstance(data, dict):
            print("DIAGNOSIS: API response is not a JSON object.")
            print("RAW RESPONSE:", data)
            return

        print("STATUS (s):", data.get("s"))
        print("ERROR MESSAGE:", data.get("errmsg"))
        print("ERROR:", data.get("error"))

        for key in ["t", "o", "h", "l", "c", "v"]:
            value = data.get(key)
            if isinstance(value, list):
                print(f"{key}: COUNT={len(value)}")
                if value:
                    print(f"  {key}[0]   =", value[0])
                    print(f"  {key}[-1]  =", value[-1])
            else:
                print(f"{key}: TYPE={type(value).__name__} VALUE={value!r}")

        t = data.get("t", [])
        o = data.get("o", [])
        h = data.get("h", [])
        l = data.get("l", [])
        c = data.get("c", [])
        v = data.get("v", [])

        if data.get("s") != "ok":
            print("DIAGNOSIS: API did not return s=ok.")
            return

        lengths = {
            "t": len(t) if isinstance(t, list) else -1,
            "o": len(o) if isinstance(o, list) else -1,
            "h": len(h) if isinstance(h, list) else -1,
            "l": len(l) if isinstance(l, list) else -1,
            "c": len(c) if isinstance(c, list) else -1,
            "v": len(v) if isinstance(v, list) else -1,
        }

        print("ARRAY LENGTHS:", lengths)

        if len(set(lengths.values())) != 1:
            print("DIAGNOSIS: OHLCV arrays have different lengths.")
            return

        n = len(t)

        if n == 0:
            print("DIAGNOSIS: API says OK but returned ZERO candles.")
            return

        print("FIRST TIMESTAMP UTC:", ts_text(t[0]))
        print("LAST TIMESTAMP UTC :", ts_text(t[-1]))

        current = int(datetime.now(timezone.utc).timestamp())
        candle_seconds = resolution * 60

        if int(t[-1]) + candle_seconds > current:
            print("LAST CANDLE: CURRENT / STILL FORMING")
            closed_count = n - 1
        else:
            print("LAST CANDLE: CLOSED")
            closed_count = n

        print("TOTAL CANDLES:", n)
        print("CLOSED CANDLES:", closed_count)

        if closed_count < 220:
            print(
                "DIAGNOSIS: API works, but this request returned fewer "
                "than 220 CLOSED candles."
            )
            print(
                "THIS IS THE KEY TEST: the old scanner must NOT report "
                "'history unavailable: None' in this situation."
            )
        else:
            print(
                "DIAGNOSIS: HISTORY DEPTH IS SUFFICIENT FOR EMA200."
            )

    except requests.RequestException as exc:
        print("DIAGNOSIS: HTTP/NETWORK ERROR")
        print("EXCEPTION:", repr(exc))
    except ValueError as exc:
        print("DIAGNOSIS: RESPONSE WAS NOT VALID JSON")
        print("EXCEPTION:", repr(exc))
    except Exception as exc:
        print("DIAGNOSIS: UNEXPECTED ERROR")
        print("EXCEPTION:", repr(exc))


print("=" * 78)
print("OMPFinex HISTORY DIAGNOSTIC")
print("VERSION 1")
print("=" * 78)

for symbol in SYMBOLS:
    for resolution in RESOLUTIONS:
        test(symbol, resolution)

print()
print("=" * 78)
print("DIAGNOSTIC FINISHED")
print("=" * 78)

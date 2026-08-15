import requests

BASE_URL = "https://api.ompfinex.com"

SYMBOLS = [
    "BTCUSDT",
    "ETHUSDT",
    "XRPUSDT",
    "ADAUSDT",
    "SOLUSDT",
]

print("=" * 70)
print("OMPFinex MARKET TYPE DIAGNOSTIC")
print("=" * 70)

# ---------------------------------------------------------
# 1) بررسی لیست بازارهای OMPFinex
# ---------------------------------------------------------

try:
    url = f"{BASE_URL}/v1/market"
    response = requests.get(url, timeout=30)

    print("\n[V1 MARKET]")
    print("HTTP:", response.status_code)

    data = response.json()

    if data.get("status") == "OK":
        markets = data.get("data", [])

        print("Markets found:", len(markets))

        for market in markets:
            base = market.get("base_currency", {}).get("id")
            quote = market.get("quote_currency", {}).get("id")
            name = market.get("name")
            tv = market.get("tradingview_symbol")

            if base in ["BTC", "ETH", "XRP", "ADA", "SOL"]:
                print(
                    f"{base}/{quote} | "
                    f"name={name} | "
                    f"tradingview={tv}"
                )

    else:
        print("Unexpected response:")
        print(data)

except Exception as e:
    print("MARKET ERROR:", e)


# ---------------------------------------------------------
# 2) بررسی UDF Symbols
# ---------------------------------------------------------

print("\n" + "=" * 70)
print("[UDF SYMBOL CHECK]")
print("=" * 70)

for symbol in SYMBOLS:

    print(f"\nChecking: {symbol}")

    try:
        url = f"{BASE_URL}/v2/udf/real/symbols"

        params = {
            "symbol": symbol
        }

        response = requests.get(
            url,
            params=params,
            timeout=30
        )

        print("HTTP:", response.status_code)

        data = response.json()

        print("Response:")
        print(data)

    except Exception as e:
        print("ERROR:", e)


print("\n" + "=" * 70)
print("DIAGNOSTIC FINISHED")
print("=" * 70)

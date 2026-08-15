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
print("OMPFinex MARKET DIAGNOSTIC")
print("=" * 70)

for symbol in SYMBOLS:

    print()
    print(f"Checking: {symbol}")

    try:
        url = f"{BASE_URL}/v2/udf/real/search"
        params = {
            "query": symbol,
            "limit": 10
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

print()
print("=" * 70)
print("DIAGNOSTIC FINISHED")
print("=" * 70)

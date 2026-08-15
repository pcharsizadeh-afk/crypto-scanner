import requests

BASE_URL = "https://api.ompfinex.com"

symbols = ["BTC", "ETH", "XRP", "ADA", "SOL"]

print("=== OMPFinex API TEST ===")

# دریافت لیست بازارها
response = requests.get(
    f"{BASE_URL}/v1/market",
    timeout=20
)

print("HTTP:", response.status_code)

data = response.json()

if data.get("status") != "OK":
    print("API ERROR:")
    print(data)
    raise SystemExit(1)

markets = data.get("data", [])

print(f"Markets received: {len(markets)}")
print()

for market in markets:
    base = market.get("base_currency", {}).get("id")
    quote = market.get("quote_currency", {}).get("id")

    if base in symbols:
        print(
            f"{base}/{quote} | "
            f"Price: {market.get('last_price')} | "
            f"TV: {market.get('tradingview_symbol')}"
        )

print()
print("=== TEST COMPLETED ===")

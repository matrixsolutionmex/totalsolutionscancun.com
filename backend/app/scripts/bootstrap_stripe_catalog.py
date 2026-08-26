"""Create the sandbox plan catalog once; never print credentials."""

import os

import httpx


API = "https://api.stripe.com/v1"
CATALOG = {
    "TOTAL_SOLUTIONS_PRO": ("Total Solutions PRO", 49900),
    "TOTAL_SOLUTIONS_BUSINESS": ("Total Solutions BUSINESS", 199900),
}


def request(method: str, path: str, *, data: dict | None = None) -> dict:
    secret = os.getenv("STRIPE_SECRET_KEY", "").strip()
    if not secret:
        raise SystemExit("STRIPE_SECRET_KEY nao configurada")
    response = httpx.request(
        method,
        f"{API}{path}",
        data=data,
        headers={"Authorization": f"Bearer {secret}"},
        timeout=float(os.getenv("STRIPE_TIMEOUT_SECONDS", "10")),
    )
    if response.status_code >= 400:
        raise SystemExit(f"Stripe retornou HTTP {response.status_code}")
    return response.json()


def main():
    products = request("GET", "/products?active=true&limit=100").get("data", [])
    for internal_key, (name, amount) in CATALOG.items():
        product = next((item for item in products if (item.get("metadata") or {}).get("internal_key") == internal_key), None)
        if not product:
            product = request("POST", "/products", data={"name": name, "metadata[internal_key]": internal_key})
        prices = request("GET", f"/prices?active=true&product={product['id']}&limit=100").get("data", [])
        price = next((item for item in prices if item.get("unit_amount") == amount and (item.get("recurring") or {}).get("interval") == "month"), None)
        if not price:
            price = request("POST", "/prices", data={
                "product": product["id"], "currency": os.getenv("STRIPE_CURRENCY", "mxn"),
                "unit_amount": str(amount), "recurring[interval]": "month",
                "metadata[internal_key]": internal_key,
            })
        print(f"{internal_key} product={product['id']} price={price['id']}")


if __name__ == "__main__":
    main()

import random
import time
import uuid
import requests

BASE_URL = "http://127.0.0.1:5000"
PRODUCTS = ["clover-pendant", "minimalist-ring", "pearl-bracelet"]

while True:
    # 1. Browse product catalog
    requests.get(f"{BASE_URL}/products", timeout=3)

    # 2. Place an order (80% valid card, 20% deterministic declined card)
    token = "tok_declined" if random.random() < 0.2 else "tok_visa"
    idem_key = f"traffic_{uuid.uuid4().hex[:10]}"
    resp = requests.post(
        f"{BASE_URL}/orders",
        json={
            "product_id": random.choice(PRODUCTS),
            "quantity": 1,
            "payment_token": token,
        },
        headers={"Idempotency-Key": idem_key},
        timeout=3,
    )

    # 3. Cancel 10% of newly placed orders
    if resp.status_code == 201 and random.random() < 0.1:
        order_id = resp.json()["id"]
        requests.post(f"{BASE_URL}/orders/{order_id}/cancellations", timeout=3)

    time.sleep(2.5)
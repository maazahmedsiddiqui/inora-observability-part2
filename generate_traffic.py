"""generate_traffic.py - repeatable success and failure calls through BOTH front doors (Part C, C7).

Each round sends the SAME case once over HTTP (POST /orders) and once over gRPC (PlaceOrder), so the HTTP and
gRPC panels fill side by side. The sequence of cases is fixed (not random), so every run produces the same mix:

   6 x success, 2 x payment declined, 1 x invalid input, 1 x unknown product, 1 x out-of-stock-prone (necklace)

Usage:  python generate_traffic.py                 # 44 rounds, one every 2.5 s  (~2 minutes)
        python generate_traffic.py --rounds 110    # ~5 minutes
HTTP is limited to 5 orders / 10 s per client address, so the default pause (2.5 s) stays inside that limit.
"""
import argparse
import time
import uuid
from collections import Counter

import requests

from grpc_client import describe_error, place_order
from inora_obs import make_traceparent

HTTP = "http://127.0.0.1:5000"
GRPC = "127.0.0.1:50051"

CASES = (
    [("success", "clover-pendant", 1, "tok_visa")] * 3
    + [("declined", "clover-pendant", 1, "tok_declined")]
    + [("success", "minimalist-ring", 2, "tok_visa")] * 2
    + [("invalid input", "clover-pendant", 0, "tok_visa")]
    + [("success", "pearl-bracelet", 1, "tok_visa")]
    + [("declined", "pearl-bracelet", 1, "tok_declined")]
    + [("unknown product", "no-such-product", 1, "tok_visa")]
    + [("necklace (last one, then out of stock)", "limited-necklace", 1, "tok_visa")]
)


def one_round(i, label, product, qty, token):
    tp = make_traceparent()
    key = f"traffic-{uuid.uuid4().hex[:10]}"
    try:
        r = requests.post(f"{HTTP}/orders", json={"product_id": product, "quantity": qty, "payment_token": token},
                          headers={"Idempotency-Key": key, "traceparent": tp}, timeout=5)
        h = str(r.status_code)
    except requests.RequestException as e:
        h = type(e).__name__
    resp, err, _ = place_order(GRPC, product, qty, token, key + "-g", 5.0, tp)
    g = "OK" if resp is not None else describe_error(err)["status"]
    return h, g


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--rounds", type=int, default=44)
    ap.add_argument("--pause", type=float, default=2.5, help="seconds between rounds (keep >= 2.2 for the HTTP limit)")
    a = ap.parse_args()
    http_seen, grpc_seen = Counter(), Counter()
    for i in range(a.rounds):
        label, product, qty, token = CASES[i % len(CASES)]
        h, g = one_round(i, label, product, qty, token)
        http_seen[h] += 1
        grpc_seen[g] += 1
        print(f"round {i + 1:>3}/{a.rounds}  {label:<40} HTTP {h:<4}  gRPC {g}")
        time.sleep(a.pause)
    print("\nHTTP statuses:", dict(http_seen))
    print("gRPC statuses:", dict(grpc_seen))


if __name__ == "__main__":
    main()

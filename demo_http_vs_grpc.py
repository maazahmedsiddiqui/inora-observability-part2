"""demo_http_vs_grpc.py - Part B evidence: one success and one failure through BOTH versions.

Each request carries the same W3C trace id over HTTP (header) and gRPC (metadata), so the logs from the two
services can be joined in Kibana. Then it proves both front doors share one source of truth.

Run (HTTP API on :5000 and gRPC service on :50051 must be up):
  python demo_http_vs_grpc.py
"""
import secrets
import sys
import uuid

import requests

from grpc_client import describe_error, place_order
from inora_obs import make_traceparent

HTTP = "http://127.0.0.1:5000"
GRPC = "127.0.0.1:50051"


def via_http(body, key, tp):
    r = requests.post(f"{HTTP}/orders", json=body, headers={"Idempotency-Key": key, "traceparent": tp}, timeout=5)
    j = r.json()
    if r.status_code in (200, 201):
        return f"HTTP {r.status_code}", f"order {j['id']}" + (" (replayed)" if r.headers.get("Idempotent-Replayed") == "true" else ""), j["id"]
    return f"HTTP {r.status_code}", f"{j['code']}", None


def via_grpc(body, key, tp):
    resp, err, _ = place_order(GRPC, body["product_id"], body["quantity"], body["payment_token"], key, 5.0, tp)
    if resp is not None:
        return "gRPC OK", f"order {resp.order.id}" + (" (replayed)" if resp.replayed else ""), resp.order.id
    i = describe_error(err)
    return f"gRPC {i['status']}", i["error_code"], None


def main():
    rows = []
    cases = [
        ("success", {"product_id": "clover-pendant", "quantity": 1, "payment_token": "tok_visa"}),
        ("failure (payment declined)", {"product_id": "clover-pendant", "quantity": 1, "payment_token": "tok_declined"}),
    ]
    for name, body in cases:
        tp = make_traceparent()
        h = via_http(body, f"demo-http-{uuid.uuid4().hex[:8]}", tp)
        g = via_grpc(body, f"demo-grpc-{uuid.uuid4().hex[:8]}", tp)
        rows.append((name, h, g, tp.split("-")[1]))

    print(f"{'case':<28} {'HTTP':<44} {'gRPC':<48} trace_id")
    print("-" * 150)
    for name, h, g, trace in rows:
        print(f"{name:<28} {h[0] + ' ' + h[1]:<44} {g[0] + ' ' + g[1]:<48} {trace}")

    # same source of truth: an order created over HTTP is *replayed* when the same key arrives over gRPC
    print("\nshared source of truth: place over HTTP, repeat the same key over gRPC")
    body = cases[0][1]
    key = f"demo-shared-{secrets.token_hex(4)}"
    h = via_http(body, key, make_traceparent())
    g = via_grpc(body, key, make_traceparent())
    print(f"  HTTP : {h[0]} {h[1]}")
    print(f"  gRPC : {g[0]} {g[1]}")
    same = h[2] is not None and h[2] == g[2] and "(replayed)" in g[1]
    print("  => the gRPC call found the HTTP order in the shared database" if same else "  => NOT shared (unexpected)")
    return 0 if same else 1


if __name__ == "__main__":
    sys.exit(main())

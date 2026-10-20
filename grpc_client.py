"""grpc_client.py - call the Inora gRPC PlaceOrder directly (no browser needed).

Examples (from the project root, venv active):
  python grpc_client.py --product-id clover-pendant --quantity 1 --payment-token tok_visa
  python grpc_client.py --product-id clover-pendant --quantity 1 --payment-token tok_declined
  python grpc_client.py --product-id limited-necklace --quantity 1 --payment-token tok_visa --deadline 0.5

It sets a DEADLINE on the call (default 5 s), sends a W3C traceparent in gRPC metadata so the server's
logs carry the same trace_id, and reports either the order or the gRPC status + domain error code.
Exit code: 0 = order created/replayed, 1 = the service answered with an error, 2 = could not complete (deadline / unreachable).
"""
import argparse
import secrets
import sys

import grpc

from inora.v1 import inora_pb2, inora_pb2_grpc
from inora_obs import make_traceparent


def place_order(target, product_id, quantity, payment_token, idempotency_key="", deadline_s=5.0, traceparent=None):
    """Returns (response_or_None, grpc_error_or_None, traceparent_used)."""
    traceparent = traceparent or make_traceparent()
    request = inora_pb2.PlaceOrderRequest(product_id=product_id, quantity=quantity,
                                          payment_token=payment_token, idempotency_key=idempotency_key)
    with grpc.insecure_channel(target) as channel:
        stub = inora_pb2_grpc.OrderServiceStub(channel)        # generated client stub
        try:
            response = stub.PlaceOrder(request, timeout=deadline_s,          # timeout = deadline
                                       metadata=(("traceparent", traceparent),))
            return response, None, traceparent
        except grpc.RpcError as err:
            return None, err, traceparent


def describe_error(err):
    trailing = dict(err.trailing_metadata() or ())
    return {"status": err.code().name, "details": err.details(), "error_code": trailing.get("error-code", "-")}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--target", default="127.0.0.1:50051")
    ap.add_argument("--product-id", required=True)
    ap.add_argument("--quantity", type=int, required=True)
    ap.add_argument("--payment-token", required=True)
    ap.add_argument("--idempotency-key", default="", help="optional; makes retries safe")
    ap.add_argument("--deadline", type=float, default=5.0, help="seconds before the call is cancelled (default 5)")
    ap.add_argument("--traceparent", default=None, help="reuse an existing W3C traceparent (default: new)")
    a = ap.parse_args(argv)

    resp, err, tp = place_order(a.target, a.product_id, a.quantity, a.payment_token,
                                a.idempotency_key, a.deadline, a.traceparent)
    trace_id = tp.split("-")[1]
    if resp is not None:
        o = resp.order
        print(f"OK  order={o.id} product={o.product_id} qty={o.quantity} total_pkr={o.total_pkr} "
              f"status={o.status} replayed={resp.replayed}  trace_id={trace_id}")
        return 0
    info = describe_error(err)
    print(f"ERROR  grpc_status={info['status']}  error_code={info['error_code']}  "
          f"details={info['details']!r}  trace_id={trace_id}")
    return 2 if err.code() in (grpc.StatusCode.DEADLINE_EXCEEDED, grpc.StatusCode.UNAVAILABLE) else 1


if __name__ == "__main__":
    sys.exit(main())

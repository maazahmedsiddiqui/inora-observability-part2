"""Inora gRPC service (Assignment 2, Part B): PlaceOrder.

This is NOT a wrapper around the HTTP API. It imports the same business module (inora_core.Store) and
opens the same SQLite database, so both front doors enforce the same rules on the same data:

    HTTP:  client -> Flask route -> inora_core.Store.place_order -> SQLite
    gRPC:  client -> generated stub -> HTTP/2 -> generated dispatch -> OrderService.PlaceOrder
                                                                   -> inora_core.Store.place_order -> SQLite
"""
import logging
import os
import signal
import time
from concurrent import futures

import grpc

from inora.v1 import inora_pb2, inora_pb2_grpc
from inora_core import (
    DomainError, IdempotencyConflict, NotFound, OutOfStock, PaymentDeclined, Store, ValidationFailed,
)
from inora_obs import make_traceparent, parse_traceparent, setup_file_logging

log = logging.getLogger("inora.grpc")

# domain error code -> gRPC status. One table, mirroring the HTTP ERROR_MAP in app.py.
GRPC_STATUS_MAP = {
    ValidationFailed.code: grpc.StatusCode.INVALID_ARGUMENT,      # HTTP 400
    NotFound.code: grpc.StatusCode.NOT_FOUND,                     # HTTP 404
    PaymentDeclined.code: grpc.StatusCode.FAILED_PRECONDITION,    # HTTP 422
    OutOfStock.code: grpc.StatusCode.RESOURCE_EXHAUSTED,          # HTTP 409
    IdempotencyConflict.code: grpc.StatusCode.ALREADY_EXISTS,     # HTTP 409
}


def _order_to_message(order):
    return inora_pb2.Order(
        id=order["id"], product_id=order["product_id"], quantity=order["quantity"],
        total_pkr=order["total_pkr"], status=order["status"], created_at=order["created_at"],
    )


class OrderService(inora_pb2_grpc.OrderServiceServicer):
    def __init__(self, store, simulate_delay_s=0.0):
        self.store = store
        self.simulate_delay_s = simulate_delay_s       # demo/test switch: makes deadlines observable

    def PlaceOrder(self, request, context):
        started = time.perf_counter()
        # 1. trace correlation: take the caller's trace id from metadata, or start a new trace
        md = dict(context.invocation_metadata())
        parsed = parse_traceparent(md.get("traceparent"))
        traceparent = md["traceparent"] if parsed else make_traceparent()
        trace_id = parsed[0] if parsed else traceparent.split("-")[1]
        ctx = {"request_id": trace_id[:8], "trace_id": trace_id, "endpoint": "/inora.v1.OrderService/PlaceOrder"}
        context.send_initial_metadata((("traceparent", traceparent),))

        try:
            if self.simulate_delay_s:
                time.sleep(self.simulate_delay_s)
            # 2. the SAME business call the HTTP route makes
            order, replayed = self.store.place_order(
                product_id=request.product_id,
                quantity=request.quantity,
                payment_token=request.payment_token,
                idempotency_key=request.idempotency_key or None,
            )
        except DomainError as err:
            status = GRPC_STATUS_MAP.get(err.code, grpc.StatusCode.INVALID_ARGUMENT)
            level = logging.ERROR if isinstance(err, PaymentDeclined) else logging.WARNING
            log.log(level, f"Domain error ({err.code}): {err.message}",
                    extra={**ctx, "status_code": status.value[0]})
            context.set_trailing_metadata((("error-code", err.code), ("traceparent", traceparent)))
            context.abort(status, err.message)
        except Exception:                                  # never leak a traceback to the caller
            log.exception("Unhandled exception", extra={**ctx, "status_code": grpc.StatusCode.INTERNAL.value[0]})
            context.set_trailing_metadata((("error-code", "internal_error"), ("traceparent", traceparent)))
            context.abort(grpc.StatusCode.INTERNAL, "An unexpected error occurred.")

        log.info(f"Order {order['id']} {'replayed idempotently' if replayed else 'placed'}",
                 extra={**ctx, "status_code": 0})
        return inora_pb2.PlaceOrderResponse(order=_order_to_message(order), replayed=replayed)


def build_server(store, port=50051, max_workers=10, simulate_delay_s=0.0):
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=max_workers))
    inora_pb2_grpc.add_OrderServiceServicer_to_server(OrderService(store, simulate_delay_s), server)
    bound = server.add_insecure_port(f"[::]:{port}")
    return server, bound


def main():
    setup_file_logging("inora-grpc", os.environ.get("LOG_FILE", "logs/grpc.log"))
    store = Store(os.environ.get("INORA_DB", "data/inora.db"))
    port = int(os.environ.get("GRPC_PORT", "50051"))
    server, bound = build_server(store, port, simulate_delay_s=float(os.environ.get("GRPC_SIMULATE_DELAY", "0")))
    server.start()
    log.info(f"gRPC server listening on port {bound}", extra={"endpoint": "startup"})
    print(f"inora-grpc listening on :{bound}", flush=True)
    stop = server.wait_for_termination
    signal.signal(signal.SIGTERM, lambda *_: server.stop(grace=5))
    try:
        stop()
    except KeyboardInterrupt:
        server.stop(grace=2)


if __name__ == "__main__":
    main()

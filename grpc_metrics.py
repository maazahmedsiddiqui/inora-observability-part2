"""Prometheus metrics for the Inora gRPC service (Assignment 2, Part C).

All metrics live in their OWN registry (GRPC_REGISTRY) and are served on their own port (METRICS_PORT, 9102),
so Prometheus scrapes the gRPC container directly, next to the HTTP API's /metrics.

Where things are measured: SERVER side. The interceptor wraps the generated dispatch, so the duration is the
time spent inside the service handler (business rules + SQLite). It excludes network transfer, message
(de)serialisation and any wait for a free worker thread. The Python caller has no metrics endpoint.

Labels are bounded: grpc_service / grpc_method come from the .proto (one method today), grpc_code is one of the
17 gRPC status names. Trace ids, request ids, order ids and user input are NEVER used as labels.
"""
import time

import grpc
from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram
from prometheus_client.core import GaugeMetricFamily

GRPC_REGISTRY = CollectorRegistry()

# Same bucket edges as the HTTP latency histogram, so p50/p95 panels are directly comparable.
# 5 ms .. 5 s covers a fast SQLite commit up to the caller's 2-5 s deadlines.
GRPC_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0)

GRPC_HANDLED = Counter(
    "grpc_server_handled_total",
    "gRPC calls finished by the server, by method and final status code",
    ["grpc_service", "grpc_method", "grpc_code"], registry=GRPC_REGISTRY)
GRPC_DURATION = Histogram(
    "grpc_server_handling_seconds",
    "Server-side time spent inside the gRPC handler, in seconds",
    ["grpc_service", "grpc_method"], buckets=GRPC_BUCKETS, registry=GRPC_REGISTRY)
GRPC_IN_FLIGHT = Gauge(
    "grpc_server_in_flight",
    "gRPC calls currently being handled",
    ["grpc_service", "grpc_method"], registry=GRPC_REGISTRY)
# Same name and meaning as the HTTP API's counter: orders by business outcome (success / failed).
# Without this, orders placed over gRPC would be invisible to the business dashboard.
BUSINESS_ORDERS = Counter(
    "business_orders_total", "Total order business outcomes by status", ["status"], registry=GRPC_REGISTRY)

UNKNOWN = "unknown"      # label value for calls to methods this server does not implement (keeps labels bounded)


def seed_series(service, method, extra_codes=()):
    """Create the series at 0 so rate()/increase() see the very first call, and panels show zeros not gaps."""
    codes = {"OK", "INVALID_ARGUMENT", "NOT_FOUND", "FAILED_PRECONDITION", "RESOURCE_EXHAUSTED",
             "ALREADY_EXISTS", "INTERNAL", "CANCELLED", *extra_codes}
    for c in codes:
        GRPC_HANDLED.labels(service, method, c)
    GRPC_DURATION.labels(service, method)
    GRPC_IN_FLIGHT.labels(service, method).set(0)
    for s in ("success", "failed"):
        BUSINESS_ORDERS.labels(s)


def _split(full_method):
    # "/inora.v1.OrderService/PlaceOrder" -> ("inora.v1.OrderService", "PlaceOrder")
    _, _, rest = full_method.partition("/")
    service, _, method = rest.partition("/")
    return service, method


def _final_code(context, raised):
    """The status the client will actually get."""
    code = context.code()
    if code is not None:
        return code.name
    if raised:
        return grpc.StatusCode.UNKNOWN.name
    if not context.is_active():               # client already gave up (deadline passed / cancelled)
        return grpc.StatusCode.CANCELLED.name
    return grpc.StatusCode.OK.name


class MetricsInterceptor(grpc.ServerInterceptor):
    """Counts calls by status, times the handler and tracks in-flight calls for every unary-unary method."""

    def __init__(self, known_methods):
        self.known = set(known_methods)            # {(service, method)} - anything else is "unknown"

    def intercept_service(self, continuation, handler_call_details):
        service, method = _split(handler_call_details.method)
        handler = continuation(handler_call_details)
        if handler is None or handler.unary_unary is None or (service, method) not in self.known:
            GRPC_HANDLED.labels(UNKNOWN, UNKNOWN, grpc.StatusCode.UNIMPLEMENTED.name).inc()
            return handler
        inner = handler.unary_unary

        def timed(request, context):
            GRPC_IN_FLIGHT.labels(service, method).inc()
            started = time.perf_counter()
            raised = False
            try:
                return inner(request, context)
            except BaseException:                  # context.abort() raises; the status is read from the context
                raised = True
                raise
            finally:
                GRPC_DURATION.labels(service, method).observe(time.perf_counter() - started)
                GRPC_HANDLED.labels(service, method, _final_code(context, raised)).inc()
                GRPC_IN_FLIGHT.labels(service, method).dec()

        return grpc.unary_unary_rpc_method_handler(
            timed, request_deserializer=handler.request_deserializer,
            response_serializer=handler.response_serializer)


class StockCollector:
    """Reads stock from the shared database at scrape time, so it is always current - including orders placed
    over HTTP by the other container. product_id is a bounded label (the catalogue has 4 products)."""

    def __init__(self, store):
        self.store = store

    def collect(self):
        g = GaugeMetricFamily("inora_product_stock", "Units in stock per product (shared database)",
                              labels=["product_id"])
        for p in self.store.list_products():
            g.add_metric([p["id"]], p["stock"])
        yield g

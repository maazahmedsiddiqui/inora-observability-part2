import json
import logging
import os
import threading
import time
import uuid
from flask import Flask, Response, jsonify, request
from prometheus_client import Counter, Gauge, Histogram, Summary, generate_latest
from werkzeug.exceptions import HTTPException

from inora_obs import setup_file_logging

from inora_core import (
    DomainError,
    IdempotencyConflict,
    NotFound,
    OutOfStock,
    PaymentDeclined,
    Store,
    ValidationFailed,
)

# ---------------- Logging Setup (A1 JSON format + W3C trace_id; shared with the gRPC service) ----------------
LOG_FILE = os.environ.get("LOG_FILE", "logs/app.log")
setup_file_logging("inora-http-api", LOG_FILE)

# ---------------- Prometheus Metrics (Fixed A1 gaps + bounded labels) ----------------
HTTP_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0)

REQUEST_COUNT = Counter(
    "app_requests_total",
    "Total HTTP requests across all application endpoints",
    ["method", "endpoint", "http_status"],
)
ACTIVE_REQUESTS = Gauge(
    "app_active_requests",
    "Number of in-flight HTTP requests currently being processed",
)
REQUEST_LATENCY = Histogram(
    "app_request_latency_seconds",
    "Server-side HTTP request duration in seconds",
    ["method", "endpoint"],
    buckets=HTTP_BUCKETS,
)
ORDER_PROCESSING = Summary(
    "app_order_processing_seconds",
    "Time spent executing order placement business logic",
)
BUSINESS_ORDERS = Counter(
    "business_orders_total",
    "Total order business outcomes by status",
    ["status"],
)
RATE_LIMIT_DECISIONS = Counter(
    "http_rate_limit_decisions_total",
    "Rate limiter decisions on POST /orders",
    ["decision"],
)

# ---------------- Rate Limiter (Fixed Window per Client IP) ----------------
class FixedWindowRateLimiter:
    """Single-instance thread-safe fixed-window counter per client IP.
    Default: 5 requests per 10-second window per client IP."""

    def __init__(self, limit=5, window_seconds=10):
        self.limit = limit
        self.window_seconds = window_seconds
        self._lock = threading.Lock()
        self._windows = {}  # ip -> (window_start_epoch, count)

    def check(self, client_ip, now=None):
        now = time.time() if now is None else now
        with self._lock:
            window_start, count = self._windows.get(client_ip, (now, 0))
            if now - window_start >= self.window_seconds:
                window_start, count = now, 0
            if count >= self.limit:
                retry_after = max(1, int(round(self.window_seconds - (now - window_start))))
                return False, retry_after, 0
            count += 1
            self._windows[client_ip] = (window_start, count)
            remaining = self.limit - count
            return True, 0, remaining

    def reset(self):
        with self._lock:
            self._windows.clear()


# ---------------- Domain Error -> HTTP Status Mapping ----------------
ERROR_MAP = {
    ValidationFailed.code: (400, "Bad Request"),
    NotFound.code: (404, "Not Found"),
    OutOfStock.code: (409, "Conflict"),
    IdempotencyConflict.code: (409, "Conflict"),
    PaymentDeclined.code: (422, "Unprocessable Content"),
}


def parse_or_create_trace():
    """Extract W3C traceparent or create one. Returns (traceparent, trace_id, req_id)."""
    tp = request.headers.get("traceparent", "").strip()
    parts = tp.split("-")
    if len(parts) == 4 and len(parts[1]) == 32 and len(parts[2]) == 16:
        trace_id = parts[1]
        span_id = parts[2]
    else:
        trace_id = uuid.uuid4().hex
        span_id = uuid.uuid4().hex[:16]
        tp = f"00-{trace_id}-{span_id}-01"
    return tp, trace_id, trace_id[:8]


def problem_response(status, title, detail, code, field=None, headers=None):
    body = {
        "type": f"https://inora.example/problems/{code}",
        "title": title,
        "status": status,
        "detail": detail,
        "code": code,
        "trace_id": getattr(request, "_trace_id", "n/a"),
    }
    if field is not None:
        body["field"] = field
    resp = Response(
        json.dumps(body),
        status=status,
        mimetype="application/problem+json",
    )
    if headers:
        for k, v in headers.items():
            resp.headers[k] = str(v)
    return resp


def create_app(db_path=None, rate_limit=None, rate_window=None):
    app = Flask(__name__)
    store = Store(db_path or os.environ.get("INORA_DB", "data/inora.db"))
    limiter = FixedWindowRateLimiter(
        limit=int(rate_limit or os.environ.get("RATE_LIMIT_MAX", 5)),
        window_seconds=int(rate_window or os.environ.get("RATE_LIMIT_WINDOW", 10)),
    )
    app.config["STORE"] = store
    app.config["LIMITER"] = limiter
    # Opt-in CORS: only when CORS_ALLOW_ORIGIN is set (docker-compose sets it to the Swagger UI origin), so a
    # browser page on another port may call this API. Unset = no cross-origin access (the default).
    cors_origin = os.environ.get("CORS_ALLOW_ORIGIN", "").strip()

    @app.before_request
    def _before():
        request._start_time = time.perf_counter()
        tp, trace_id, req_id = parse_or_create_trace()
        request._traceparent = tp
        request._trace_id = trace_id
        request._req_id = req_id
        if request.path != "/metrics":
            ACTIVE_REQUESTS.inc()

    @app.after_request
    def _after(response):
        response.headers["traceparent"] = getattr(request, "_traceparent", "")
        if cors_origin:
            response.headers["Access-Control-Allow-Origin"] = cors_origin
            response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
            response.headers["Access-Control-Allow-Headers"] = "Content-Type, Idempotency-Key, traceparent"
            response.headers["Access-Control-Expose-Headers"] = (
                "Location, Idempotent-Replayed, Retry-After, X-RateLimit-Limit, X-RateLimit-Remaining, traceparent")
        if request.path != "/metrics":
            ACTIVE_REQUESTS.dec()
            # Use route template (e.g. /orders/<order_id>) so path IDs never become labels
            route_Template = request.url_rule.rule if request.url_rule else "unmatched"
            elapsed = time.perf_counter() - getattr(request, "_start_time", time.perf_counter())
            REQUEST_LATENCY.labels(request.method, route_Template).observe(elapsed)
            REQUEST_COUNT.labels(request.method, route_Template, str(response.status_code)).inc()
        return response

    @app.errorhandler(DomainError)
    def _handle_domain_error(err):
        status, title = ERROR_MAP.get(err.code, (400, "Domain Error"))
        if isinstance(err, (OutOfStock, PaymentDeclined)):
            BUSINESS_ORDERS.labels("failed").inc()
        # A declined payment is a failed order: log it at ERROR (as in Assignment 1) so Kibana's
        # `severity: ERROR` search finds it, exactly as it does for the gRPC service.
        logging.log(
            logging.ERROR if isinstance(err, PaymentDeclined) else logging.WARNING,
            f"Domain error ({err.code}): {err.message}",
            extra={
                "request_id": request._req_id,
                "trace_id": request._trace_id,
                "endpoint": request.path,
                "status_code": status,
            },
        )
        return problem_response(status, title, err.message, err.code, field=err.field)

    @app.errorhandler(HTTPException)
    def _handle_http_error(err):
        """Routing/protocol errors (unknown URL, wrong method...) use the same
        problem+json format as every other error."""
        codes = {400: "bad_request", 404: "not_found", 405: "method_not_allowed",
                 413: "payload_too_large", 415: "unsupported_media_type"}
        resp = problem_response(err.code, err.name, err.description,
                                codes.get(err.code, "http_error"))
        if err.code == 405 and getattr(err, "valid_methods", None):
            resp.headers["Allow"] = ", ".join(sorted(err.valid_methods))
        logging.warning(
            f"HTTP error {err.code} on {request.method} {request.path}",
            extra={"request_id": getattr(request, "_req_id", "n/a"),
                   "trace_id": getattr(request, "_trace_id", "n/a"),
                   "endpoint": "unmatched" if request.url_rule is None else request.url_rule.rule,
                   "status_code": err.code},
        )
        return resp

    @app.errorhandler(Exception)
    def _handle_unexpected_error(err):
        """Last resort: never leak a Python traceback or an HTML page to the caller."""
        logging.exception(
            "Unhandled exception",
            extra={"request_id": getattr(request, "_req_id", "n/a"),
                   "trace_id": getattr(request, "_trace_id", "n/a"),
                   "endpoint": request.path, "status_code": 500},
        )
        return problem_response(500, "Internal Server Error",
                                "An unexpected error occurred.", "internal_error")

    @app.route("/metrics", methods=["GET"])
    def metrics():
        return generate_latest(), 200, {"Content-Type": "text/plain; version=0.0.4"}

    @app.route("/openapi.yaml", methods=["GET"])
    def openapi_spec():
        spec_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "openapi.yaml")
        with open(spec_path, "r", encoding="utf-8") as f:
            return Response(f.read(), status=200, mimetype="application/yaml")

    @app.route("/products", methods=["GET"])
    def list_products():
        items = store.list_products()
        logging.info(
            "Listed products catalog",
            extra={
                "request_id": request._req_id,
                "trace_id": request._trace_id,
                "endpoint": "/products",
                "status_code": 200,
            },
        )
        return jsonify({"items": items}), 200

    @app.route("/products/<product_id>", methods=["GET"])
    def get_product(product_id):
        product = store.get_product(product_id)
        logging.info(
            f"Fetched product {product_id}",
            extra={
                "request_id": request._req_id,
                "trace_id": request._trace_id,
                "endpoint": "/products/<product_id>",
                "status_code": 200,
            },
        )
        return jsonify(product), 200

    @app.route("/orders", methods=["POST"])
    def place_order():
        # 1. Rate Limit Check (5 req / 10s per IP)
        client_ip = request.headers.get("X-Forwarded-For", request.remote_addr or "127.0.0.1")
        allowed, retry_after, remaining = limiter.check(client_ip)
        if not allowed:
            RATE_LIMIT_DECISIONS.labels("rejected").inc()
            logging.warning(
                f"Rate limit exceeded for IP {client_ip}",
                extra={
                    "request_id": request._req_id,
                    "trace_id": request._trace_id,
                    "endpoint": "/orders",
                    "status_code": 429,
                },
            )
            return problem_response(
                429,
                "Too Many Requests",
                f"Rate limit of {limiter.limit} requests per {limiter.window_seconds}s exceeded.",
                "rate_limit_exceeded",
                headers={
                    "Retry-After": retry_after,
                    "X-RateLimit-Limit": limiter.limit,
                    "X-RateLimit-Remaining": 0,
                },
            )
        RATE_LIMIT_DECISIONS.labels("allowed").inc()

        # 2. Controlled Fault Injection (only when triggered by caller demo or env)
        fault = request.headers.get("X-Simulate-Fault") or os.environ.get("FAULT_MODE", "")
        if fault == "503":
            BUSINESS_ORDERS.labels("failed").inc()
            logging.error(
                "Simulated upstream payment gateway outage (503)",
                extra={
                    "request_id": request._req_id,
                    "trace_id": request._trace_id,
                    "endpoint": "/orders",
                    "status_code": 503,
                },
            )
            return problem_response(
                503,
                "Service Unavailable",
                "Temporary upstream service failure; safe to retry with Idempotency-Key.",
                "service_unavailable",
            )
        if fault == "slow":
            # Longer than the caller's 2.0s timeout, shorter than timeout + first backoff (3.0s), so the
            # slow request finishes on the server BEFORE the caller's retry arrives (a clean replay).
            time.sleep(float(os.environ.get("FAULT_SLOW_SECONDS", "2.5")))

        # 3. Validate JSON Payload
        if not request.is_json:
            raise ValidationFailed("Request body must be valid application/json")
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            raise ValidationFailed("JSON body must be an object")

        idem_key = request.headers.get("Idempotency-Key")
        start_t = time.perf_counter()
        order, replayed = store.place_order(
            product_id=data.get("product_id"),
            quantity=data.get("quantity"),
            payment_token=data.get("payment_token"),
            idempotency_key=idem_key,
        )
        ORDER_PROCESSING.observe(time.perf_counter() - start_t)

        status_code = 200 if replayed else 201
        if not replayed:
            BUSINESS_ORDERS.labels("success").inc()

        logging.info(
            f"Order {order['id']} {'replayed idempotently' if replayed else 'placed'}",
            extra={
                "request_id": request._req_id,
                "trace_id": request._trace_id,
                "endpoint": "/orders",
                "status_code": status_code,
            },
        )
        resp = jsonify(order)
        resp.status_code = status_code
        resp.headers["Location"] = f"/orders/{order['id']}"
        resp.headers["Idempotent-Replayed"] = "true" if replayed else "false"
        resp.headers["X-RateLimit-Limit"] = str(limiter.limit)
        resp.headers["X-RateLimit-Remaining"] = str(remaining)
        return resp

    @app.route("/orders/<order_id>", methods=["GET"])
    def get_order(order_id):
        order = store.get_order(order_id)
        logging.info(
            f"Fetched order {order_id}",
            extra={
                "request_id": request._req_id,
                "trace_id": request._trace_id,
                "endpoint": "/orders/<order_id>",
                "status_code": 200,
            },
        )
        return jsonify(order), 200

    @app.route("/orders/<order_id>/cancellations", methods=["POST"])
    def cancel_order(order_id):
        was_placed = store.get_order(order_id)["status"] == "placed"
        order = store.cancel_order(order_id)
        if was_placed:  # repeating a cancel is safe and must not inflate the metric
            BUSINESS_ORDERS.labels("cancelled").inc()
        logging.info(
            f"Order {order_id} cancelled",
            extra={
                "request_id": request._req_id,
                "trace_id": request._trace_id,
                "endpoint": "/orders/<order_id>/cancellations",
                "status_code": 200,
            },
        )
        return jsonify(order), 200

    return app


app = create_app()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, threaded=True)
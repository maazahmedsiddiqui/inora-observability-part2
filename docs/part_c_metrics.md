# Part C - Metrics audit, additions and queries

## C1. Audit of the Assignment 1 metrics

| A1 metric | Verdict in A1 | Gap found | What was done |
|---|---|---|---|
| `app_requests_total{method,endpoint,http_status}` | Useful: request and error rate by route | `/cancel` was never counted; counts were incremented by hand inside handlers, so any path that skipped the line (exceptions, 404s) was missed | Counted once per request in an `after_request` hook for **every** application route, using the **route template** (`/orders/<order_id>`), so ids never become label values |
| `app_active_requests` (gauge) | Useful: in-flight HTTP requests | Only `/catalog` and `/order` were tracked | Incremented/decremented in `before_request`/`after_request` for all routes except `/metrics` |
| `app_request_latency_seconds` (histogram) | Useful in principle | Covered `/catalog` only, no labels (so `/order` latency was invisible), default buckets | Labelled by `method` and `endpoint`; explicit buckets 5 ms - 5 s; p50/p95 by `histogram_quantile` |
| `app_order_processing_seconds` (summary) | Partly useful: time inside order logic | A Python summary has only `_count`/`_sum` - no quantiles, cannot be aggregated across instances | Kept (average business-logic time); percentiles come from the histograms instead |
| `business_orders_total{status}` | Useful: business outcomes, separate from technical errors | `failed` came from a random 20% fault, so it did not mean a real failure; orders placed over gRPC would not be counted at all | `failed` now means a declined payment or out of stock (deterministic); a repeated cancel no longer counts twice; the gRPC service exports the same metric |
| *(none)* rate limiting | Missing | No visibility of throttling | `http_rate_limit_decisions_total{decision=allowed\|rejected}` (added in Part A) |
| *(none)* gRPC | Missing | New service had no metrics | `grpc_server_handled_total`, `grpc_server_handling_seconds`, `grpc_server_in_flight` (this part) |
| *(none)* stock | Missing | The one number a shop most needs, and the only state both front doors change | `inora_product_stock{product_id}` (this part) |

Not added, on purpose: client-side metrics (the Python caller has no metrics endpoint; the manual says it does not need one), message-size metrics (see C5), and anything labelled with trace, request, order, user or idempotency-key values (unbounded).

## C8. The new metrics

All gRPC metrics are measured on the **server**, inside the service handler: they exclude network time, message (de)serialisation and any wait for a free worker thread. They live in their own registry and are served on port 9102 of the `grpc` container; Prometheus job `inora_grpc` scrapes `grpc:9102` every 5 s.

| Metric | Purpose | Type | Unit | Labels (all bounded) | Code location | Panel query |
|---|---|---|---|---|---|---|
| `grpc_server_handled_total` | Calls and error rate by RPC method and final status | Counter | calls | `grpc_service`, `grpc_method`, `grpc_code` (17 status names; `unknown` method for unimplemented calls) | `grpc_metrics.py` `MetricsInterceptor.intercept_service` | `sum by (grpc_method, grpc_code) (rate(grpc_server_handled_total{grpc_method!="unknown"}[1m]))` and error rate `sum by (grpc_method) (rate(grpc_server_handled_total{grpc_code!="OK"}[5m])) / sum by (grpc_method) (rate(grpc_server_handled_total[5m]))` |
| `grpc_server_handling_seconds` | How long the handler takes; p50 and p95 | Histogram, buckets 5 ms, 10, 25, 50, 100, 250, 500 ms, 1, 2.5, 5 s | seconds | `grpc_service`, `grpc_method` | `grpc_metrics.py`, same interceptor | `histogram_quantile(0.95, sum by (le) (rate(grpc_server_handling_seconds_bucket{grpc_method="PlaceOrder"}[5m])))` (and `0.50`) |
| `grpc_server_in_flight` | Calls being handled right now (saturation of the 10-worker pool) | Gauge | calls | `grpc_service`, `grpc_method` | `grpc_metrics.py`, same interceptor | `sum(grpc_server_in_flight)` |
| `business_orders_total` (gRPC copy) | Orders by outcome over gRPC; same meaning as the HTTP counter, so both front doors can be summed | Counter | orders | `status` = `success` \| `failed` | `grpc_server.py` `PlaceOrder` | `sum by (status) (rate(business_orders_total[1m])) * 60` |
| `inora_product_stock` | Units in stock per product, from the shared database at scrape time | Gauge (custom collector) | units | `product_id` (4 products) | `grpc_metrics.py` `StockCollector`, registered in `grpc_server.py` `main()` | `inora_product_stock` |

HTTP side, for comparison (existing, Part A): `app_requests_total`, `app_request_latency_seconds_bucket`, `app_active_requests`, `http_rate_limit_decisions_total`.

**Windows.** Rates use a 1-minute window; error rate, p50 and p95 use a 5-minute window (about 60 scrapes at 5 s, enough samples for a stable quantile at demo traffic).

**Bucket choice.** One fast SQLite commit is a few milliseconds, a slow disk or a busy laptop tens to hundreds, the caller's timeout is 2 s and the gRPC client default deadline is 5 s. 5 ms - 5 s therefore resolves the normal range and still shows the tail. The HTTP histogram uses the same edges so the two p95 panels compare directly.

## C5. In-flight calls, not message size

`PlaceOrder` messages are a few dozen bytes with a fixed shape, so their size never changes and says nothing about health. In-flight calls shows how close the server is to its worker limit (10 threads) and is the gRPC counterpart of the HTTP `app_active_requests` gauge, so the two can be plotted together.

## C6. Another missing metric

`inora_product_stock{product_id}` - stock is the one piece of state that both front doors change, so it is the clearest evidence of the shared source of truth: an order placed over HTTP lowers the number that the gRPC service reports. It also lets the shop alert on "limited-necklace = 0" before customers hit out-of-stock errors.

## A caveat worth stating

A client deadline is recorded on the server as `CANCELLED` (the server sees the client go away), not `DEADLINE_EXCEEDED`, and the handler still finishes the work. As with HTTP timeouts, an `idempotency_key` is what makes the retry safe.

## C7. Repeatable traffic

`python generate_traffic.py` sends a fixed sequence over both front doors (6 success, 2 declined, 1 invalid input, 1 unknown product, 1 necklace), one round every 2.5 s. For a visible in-flight and p95 effect, slow the gRPC handler: set `GRPC_SIMULATE_DELAY=2` before `docker compose up -d grpc` and run `python generate_traffic.py --pause 0.5`.

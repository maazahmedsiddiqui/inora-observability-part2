# Inora - Assignment 2: HTTP API, gRPC service and metrics

Enterprise Software Development, Fall 2026. Extends the Assignment 1 observability project (Part 1 repo: `inora-observability`, unchanged).

Inora is a jewellery store. The task chosen for this assignment is **placing an order**. It is offered over two front doors that share the same business rules and the same database:

| | HTTP (Part A) | gRPC (Part B) |
|---|---|---|
| Operation | `POST /orders` | `inora.v1.OrderService/PlaceOrder` |
| Container | `inora-api` (Flask, port 5000) | `inora-grpc` (port 50051, metrics 9102) |
| Business rules | `inora_core.Store.place_order` | the same `inora_core.Store.place_order` |
| Data | one SQLite file on the shared `inora-data` volume | the same file |

Part C adds Prometheus metrics for the gRPC service and a Grafana dashboard that shows HTTP and gRPC side by side.

## Contents

1. [Prerequisites](#1-prerequisites)
2. [Start everything](#2-start-everything)
3. [Where things run](#3-where-things-run)
4. [Run and test](#4-run-and-test)
5. [API reference](#5-api-reference)
6. [Reliability and rate limiting](#6-reliability-and-rate-limiting)
7. [gRPC service](#7-grpc-service)
8. [Metrics and dashboard](#8-metrics-and-dashboard)
9. [Project layout](#9-project-layout)
10. [Demo switches](#10-demo-switches)
11. [Evidence](#11-evidence)
12. [Sources and notes](#12-sources-and-notes)

## 1. Prerequisites

* Docker Desktop (running)
* Python 3.10+ (developed on 3.14) and PowerShell (commands below are PowerShell; on bash, activate with `source venv/bin/activate`)

```powershell
python -m venv venv
.\venv\Scripts\Activate.ps1
pip install -r requirements-dev.txt
```

## 2. Start everything

```powershell
docker compose up -d --build
docker compose ps
```

Nine containers start: `inora-api`, `inora-grpc`, `swagger-ui`, `prometheus`, `grafana`, `node-exporter`, `elasticsearch`, `kibana`, `filebeat`. The first build takes a few minutes. If you change `prometheus.yml`, run `docker compose restart prometheus`.

Stop with `docker compose down` (add `-v` only to wipe the database and start from the seed data).

## 3. Where things run

| What | URL / address |
|---|---|
| HTTP API | http://localhost:5000 (spec at `/openapi.yaml`, metrics at `/metrics`) |
| Swagger UI (try the API from the spec) | http://localhost:8080 |
| gRPC service | `127.0.0.1:50051` |
| gRPC metrics | http://localhost:9102/metrics |
| Prometheus | http://localhost:9090 (targets: `/targets`) |
| Grafana | http://localhost:3000 (`admin` / `admin`) - dashboard **Inora - HTTP vs gRPC (Assignment 2)** in folder *Inora* |
| Kibana | http://localhost:5601 (data view `Filebeat Logs`) |

The Grafana data source and the Assignment 2 dashboard are provisioned from `grafana/`, so they appear automatically. The Part 1 dashboard (`grafana_dashboard.json`) can still be imported by hand.

## 4. Run and test

Unit, API, contract, gRPC and metrics tests (no containers needed):

```powershell
python -m pytest -v
```
Expected: **95 passed**. (`pytest.ini` limits collection to `tests/`, so the Part 1 experiment script `cardinality_test.py` is not run.)

| Test file | Covers |
|---|---|
| `tests/test_core.py` | business rules: valid, invalid, repeated (idempotent), declined, out of stock, parallel retries, cancel |
| `tests/test_http_api.py` | HTTP: valid request, invalid input, repeated request, `429` + `Retry-After`, `problem+json` for every error |
| `tests/test_openapi_contract.py` | uses `openapi.yaml` to check real responses (status, content type, headers, body schema) and request validation |
| `tests/test_caller.py` | timeout, retry/backoff, circuit breaker, 429 handled separately |
| `tests/test_grpc.py` | gRPC statuses, deadline, idempotency, **shared source of truth with HTTP**, trace correlation |
| `tests/test_grpc_metrics.py` | gRPC metrics: counts by status, duration histogram, in-flight, bounded labels |

### Try the HTTP API

```powershell
Invoke-RestMethod http://localhost:5000/products
Invoke-RestMethod -Method Post -Uri http://localhost:5000/orders -ContentType "application/json" `
  -Headers @{"Idempotency-Key"="demo-1"} `
  -Body '{"product_id":"clover-pendant","quantity":1,"payment_token":"tok_visa"}'
```
(`Invoke-RestMethod` raises an error on 4xx/5xx responses; Swagger UI or `caller.py` show those more clearly.)
Or open Swagger UI (http://localhost:8080), expand an operation, **Try it out**.

### Python caller (timeout, retry, circuit breaker)

```powershell
$env:RATE_LIMIT_MAX=100          # the demo makes ~8 order attempts in a few seconds
docker compose up -d api
python caller.py --scenario reliability

Remove-Item Env:RATE_LIMIT_MAX   # back to the default limit (5 per 10 s)
docker compose up -d api
python caller.py --scenario ratelimit
```

### gRPC client and the HTTP-vs-gRPC comparison

```powershell
python grpc_client.py --product-id clover-pendant --quantity 1 --payment-token tok_visa --idempotency-key demo-1
python grpc_client.py --product-id clover-pendant --quantity 1 --payment-token tok_declined
python grpc_client.py --product-id clover-pendant --quantity 1 --payment-token tok_visa --deadline 0.001
python demo_http_vs_grpc.py
```

### Fill the dashboard

```powershell
python generate_traffic.py                   # fixed mix over HTTP and gRPC, ~2 minutes
$env:GRPC_SIMULATE_DELAY=2; docker compose up -d grpc
python generate_traffic.py --pause 0.5 --rounds 40   # slow gRPC handler: in-flight and p95 rise
Remove-Item Env:GRPC_SIMULATE_DELAY; docker compose up -d grpc
```

### Regenerate the gRPC code from the `.proto`

```powershell
python -m grpc_tools.protoc -I proto --python_out=. --grpc_python_out=. proto/inora/v1/inora.proto
```

## 5. API reference

Full machine-readable description: [`openapi.yaml`](openapi.yaml).

| Method and path | Purpose | Success | Errors |
|---|---|---|---|
| `GET /products` | list products and stock | 200 | |
| `GET /products/{product_id}` | one product | 200 | 404 |
| `POST /orders` | place an order (`Idempotency-Key` optional, rate limited) | 201 created, 200 replay | 400, 404, 409, 422, 429, 500, 503 |
| `GET /orders/{order_id}` | one order | 200 | 404 |
| `POST /orders/{order_id}/cancellations` | cancel (safe to repeat) | 200 | 404 |

`/metrics` and `/openapi.yaml` are infrastructure endpoints and are not part of the reviewed API.

Request body for `POST /orders`: `{"product_id": "clover-pendant", "quantity": 1, "payment_token": "tok_visa"}`. Rules: quantity 1-10; the token must start with `tok_`; **`tok_declined` is always declined**, which makes failures repeatable.

**One error format.** Every error (including unknown URL, wrong method, malformed JSON and unexpected failures) is `application/problem+json`:

```json
{"type": "https://inora.example/problems/payment_declined", "title": "Unprocessable Content", "status": 422,
 "detail": "payment was declined", "code": "payment_declined", "field": "payment_token", "trace_id": "..."}
```

| Situation | HTTP | gRPC status | `code` / `error-code` |
|---|---|---|---|
| Created | 201 | `OK` | |
| Repeat with same key | 200 + `Idempotent-Replayed: true` | `OK`, `replayed=true` | |
| Bad input | 400 | `INVALID_ARGUMENT` | `validation_error` |
| Unknown product / order | 404 | `NOT_FOUND` | `not_found` |
| Payment declined | 422 | `FAILED_PRECONDITION` | `payment_declined` |
| Out of stock | 409 | `RESOURCE_EXHAUSTED` | `out_of_stock` |
| Key reused with a different request | 409 | `ALREADY_EXISTS` | `idempotency_conflict` |
| Too many requests | 429 + `Retry-After` | (not rate limited) | `rate_limit_exceeded` |
| Transient server failure | 503 (`service_unavailable`; demo switch only) | `UNAVAILABLE` is reported by the client channel when the service is unreachable; the service has no fault switch for it | |
| Client gave up | client timeout | `DEADLINE_EXCEEDED` (client) / `CANCELLED` (server metric) | |

## 6. Reliability and rate limiting

**Idempotency.** `POST /orders` accepts an `Idempotency-Key` (1-128 chars). The first request with a key creates the order and stores the key with a hash of the request in the database. Repeating the same key and body returns the original order (`200`, `Idempotent-Replayed: true`) and creates nothing; the same key with a different body is rejected (`409`). A failed attempt (declined payment, no stock) does not use up its key. The check and the insert run in one `BEGIN IMMEDIATE` transaction, so parallel retries (even from the gRPC container) create one order. Cancelling twice is also safe.

**Rate limit.** `POST /orders`: **5 requests per 10-second fixed window per client address** (`X-Forwarded-For` if present, otherwise the connection address; this header is trusted, which is a known simplification). The limit is **shared by everyone behind the same address** and is kept in memory in the single API process (it resets if the container restarts). Over the limit the API answers `429` with `Retry-After` (seconds until the window resets) and `X-RateLimit-*` headers; calls are allowed again once the window has reset. The gRPC service is not rate limited.

**Python caller** (`caller.py`): timeout 2.0 s per attempt; at most 3 attempts; exponential backoff 1 s then 2 s (cap 4 s, +/-10 % jitter); circuit breaker opens after 3 consecutive failures and allows one trial call after 5 s. Failures that count: timeouts, connection errors, HTTP 500/502/503/504. Client errors (4xx) are never retried. A `429` is reported separately with its `Retry-After` and never trips the breaker. A POST is only retried when it carries an `Idempotency-Key`; all attempts of one call reuse the same key and the same trace id.

## 7. gRPC service

`proto/inora/v1/inora.proto` defines one unary RPC, `PlaceOrder`. The handler in `grpc_server.py` calls `inora_core.Store.place_order` directly - the same function the Flask route calls - so both doors enforce the same rules on the same database. It is not a wrapper around the HTTP API.

```
HTTP : client -> Flask route -> inora_core.Store.place_order -> SQLite
gRPC : client -> generated stub -> HTTP/2 -> generated dispatch -> OrderService.PlaceOrder
              -> inora_core.Store.place_order -> SQLite
```

**Logs and trace correlation.** Both services write the same JSON line format (`inora_obs/logs.py`: `time, service_name, severity, message, request_id, trace_id, endpoint, status_code`) into `./logs`, which Filebeat ships to Elasticsearch. A W3C `traceparent` is accepted from an HTTP header or gRPC metadata (or created) and written as `trace_id` in every log line. In Kibana, search `trace_id : "<id>"` to see one request across `inora-http-api` and `inora-grpc`. A declined payment is logged at `ERROR` by both services. The trace id is never used as a Prometheus label.

## 8. Metrics and dashboard

Details, audit of the Assignment 1 metrics and the per-metric table: [`docs/part_c_metrics.md`](docs/part_c_metrics.md).

* Prometheus job `inora_grpc` scrapes `grpc:9102`; `inora_backend` scrapes `api:5000` (config: `prometheus.yml`).
* New gRPC metrics (server-side, bounded labels): `grpc_server_handled_total{grpc_service,grpc_method,grpc_code}`, `grpc_server_handling_seconds` (histogram, 5 ms - 5 s), `grpc_server_in_flight`; plus `business_orders_total{status}` (same meaning as the HTTP counter) and `inora_product_stock{product_id}`.
* Dashboard JSON: `grafana/dashboards/inora-http-vs-grpc.json` (provisioned). Rates use a 1-minute window; error rate, p50 and p95 use a 5-minute window.

## 9. Project layout

```
app.py                    HTTP API (Flask)           caller.py            Python caller: timeout/retry/breaker
inora_core/               shared business rules      grpc_server.py       gRPC service
  service.py  errors.py   (SQLite, idempotency)      grpc_metrics.py      gRPC metrics + interceptor
inora_obs/                shared JSON logs, traceparent  grpc_client.py    gRPC client (deadline)
proto/inora/v1/inora.proto                           demo_http_vs_grpc.py HTTP vs gRPC evidence script
inora/v1/                 generated gRPC code        generate_traffic.py  repeatable traffic, both doors
openapi.yaml              API description            traffic.py           continuous HTTP traffic (Part 1 style)
tests/  docs/  evidence/                             cardinality_test.py  Part 1 experiment (not a test)
prometheus.yml  filebeat.yml  docker-compose.yml  Dockerfile  Dockerfile.grpc  pytest.ini  requirements*.txt
grafana/provisioning/ (data source, dashboard provider)   grafana/dashboards/ (Assignment 2 dashboard)
grafana_dashboard.json    Part 1 dashboard (manual import)
```

## 10. Demo switches

| Switch | Effect |
|---|---|
| `X-Simulate-Fault: 503` / `slow` request header on `POST /orders` | forces a 503 / delays the handler (default `FAULT_SLOW_SECONDS=2.5`); used by `caller.py`. Demo only. |
| `FAULT_MODE=503\|slow` (API env) | same, for every request |
| `RATE_LIMIT_MAX`, `RATE_LIMIT_WINDOW` (API env) | change the rate limit (default 5 per 10 s) |
| `GRPC_SIMULATE_DELAY=<seconds>` (gRPC env) | delays the gRPC handler |
| `INORA_SEED_STOCK` | stock of the three regular products (compose uses 100000) |
| `CORS_ALLOW_ORIGIN` | opt-in cross-origin access for Swagger UI (compose sets `http://localhost:8080`; unset by default) |

## 11. Evidence

Captured output and screenshots are in [`evidence/`](evidence/): caller reliability and rate-limit runs, HTTP-vs-gRPC comparison, traffic run, Kibana trace correlation, Prometheus targets, Grafana dashboard (normal and slow-handler).

## 12. Sources and notes

* Course lecture material on APIs/REST/OpenAPI/rate limiting and on gRPC (`APIs.md`, `GRPC.md` and the lecture slides) for the REST constraints, `problem+json`, `Idempotency-Key`, `traceparent` and rate-limit concepts.
* Standards used: OpenAPI 3.0.3; RFC 9457 / RFC 7807 Problem Details; W3C Trace Context (`traceparent`); Protocol Buffers 3 and gRPC status codes.
* Part 1 of this project (the Flask app, ELK pipeline, Prometheus/Grafana stack and `grafana_dashboard.json`) is the student's own earlier work and is reused here.
* Data volumes (`inora-data`, Elasticsearch data) are not part of the submission.

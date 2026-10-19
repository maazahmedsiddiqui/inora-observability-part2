# Inora Observability Project

## The Problem

Inora is a jewelry store. When a customer tries to buy a piece, one of three things can happen: the order succeeds, the customer cancels partway through, or the backend silently fails (e.g. a payment error). From the outside, a cancelled order and a crashed order look identical — a checkout that didn't complete. Without visibility into which is which, Inora's team can't tell whether cart abandonment is a customer decision or a technical bug costing them sales. The team also has no way to know the site is running slow until customers complain, by which point sales are already lost.

## Intended Users

Inora's small development/ops team — the people who need to answer, at a glance, "is checkout broken right now?" and "was that failed order a real system error or a customer cancelling?"

## The Solution

This project adds an observability layer on top of a Flask backend simulating Inora's storefront:

- **A live Grafana dashboard** that separates *business* outcomes (orders placed, cancelled, or failed) from *technical* health (request latency, error rates, server load). The team can see at a glance whether a spike in incomplete orders is customers cancelling or the backend failing.
- **An ELK logging pipeline** (Filebeat → Elasticsearch → Kibana) that gives every request a unique `request_id`. If a checkout fails, the team can search that exact ID in Kibana and trace precisely what happened, instead of guessing from vague error reports.

### What works

- `GET /catalog` — lists jewelry items, with realistic response-time variation.
- `POST /order` — places an order; ~20% randomly fail (simulating real payment failures).
- `POST /cancel` — a genuine customer cancellation, tracked separately from failures.
- All requests are logged as structured JSON (time, severity, message, request ID) and shipped into Kibana.
- All requests are measured in Prometheus (request counts, in-flight requests, latency, order-processing time) and visualized in Grafana, alongside server CPU/memory via Node Exporter.

## How to Try It

### Prerequisites
- Docker Desktop
- Python 3.10+

### 1. Start the observability stack
```bash
docker compose up -d
```
This starts Prometheus (`:9090`), Grafana (`:3000`), Node Exporter (`:9100`), Elasticsearch (`:9200`), Kibana (`:5601`), and Filebeat.

### 2. Set up the Python environment (first time only)
```bash
python -m venv venv
.\venv\Scripts\activate
pip install -r requirements.txt
```

### 3. Run the app and generate traffic
Open two terminals (with the virtual environment activated in each):
```bash
python app.py
```
```bash
python traffic.py
```
`traffic.py` continuously calls `/catalog`, `/order`, and occasionally `/cancel`, so there's always live data.

### 4. View the dashboards
- **Grafana:** `http://localhost:3000` (login: `admin` / `admin`) — import `grafana_dashboard.json` if it isn't already loaded.
- **Kibana:** `http://localhost:5601` → Discover → data view `Filebeat Logs`. Search `severity: ERROR` to see failed orders, or filter by a specific `request_id` to trace one checkout end to end.
- **Prometheus:** `http://localhost:9090` — for raw PromQL queries.

### 5. Test it
- Watch the Grafana "Total Orders Placed" panel while `traffic.py` runs — you'll see `success`, `failed`, and `cancelled` as separate lines.
- Pick any `ERROR`-severity log line in Kibana, copy its `request_id`, and search for it — you'll find the exact request that failed.

### 6. Clean up safely
```bash
# Stop the app and traffic generator with Ctrl+C in their terminals, then:
docker compose down
```
This stops and removes all containers. Your data (`app.log`, Grafana dashboard export, Elasticsearch indices) is *not* deleted unless you also run `docker compose down -v` (removes volumes) — use that only if you want a fully clean slate.
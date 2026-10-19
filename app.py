import json
import uuid
import random
import time
import logging
from flask import Flask, request
from prometheus_client import Counter, Gauge, Histogram, Summary, generate_latest

class JsonFormatter(logging.Formatter):
    def format(self, record):
        return json.dumps({
            "time": self.formatTime(record),
            "service_name": "inora-backend",
            "severity": record.levelname,
            "message": record.getMessage(),
            "request_id": getattr(record, "request_id", "n/a")
        })

handler = logging.FileHandler("app.log")
handler.setFormatter(JsonFormatter())
logging.getLogger().addHandler(handler)
logging.getLogger().setLevel(logging.INFO)

app = Flask(__name__)

REQUEST_COUNT = Counter('app_requests_total', 'Total requests', ['method', 'endpoint', 'http_status'])
ACTIVE_REQUESTS = Gauge('app_active_requests', 'Active requests')
REQUEST_LATENCY = Histogram('app_request_latency_seconds', 'Request latency')
ORDER_PROCESSING = Summary('app_order_processing_seconds', 'Time spent processing orders')
BUSINESS_ORDERS = Counter('business_orders_total', 'Total orders placed', ['status'])

@app.route('/metrics')
def metrics():
    return generate_latest(), 200, {'Content-Type': 'text/plain; version=0.0.4'}

@app.route('/catalog')
@ACTIVE_REQUESTS.track_inprogress()
@REQUEST_LATENCY.time()
def catalog():
    req_id = str(uuid.uuid4())[:8]
    time.sleep(random.uniform(0.1, 0.3))
    REQUEST_COUNT.labels('GET', '/catalog', '200').inc()
    logging.info("Catalog endpoint accessed by user", extra={"request_id": req_id})
    return {"items": ["clover pendant", "minimalist ring", "pearl bracelet"]}

@app.route('/order', methods=['POST'])
@ACTIVE_REQUESTS.track_inprogress()
def order():
    req_id = str(uuid.uuid4())[:8]
    start_time = time.time()
    if random.random() < 0.2:
        REQUEST_COUNT.labels('POST', '/order', '500').inc()
        BUSINESS_ORDERS.labels('failed').inc()
        logging.error("Payment transaction failed for order attempt", extra={"request_id": req_id})
        return {"error": "payment_failed"}, 500
    
    time.sleep(random.uniform(0.2, 0.6))
    REQUEST_COUNT.labels('POST', '/order', '200').inc()
    BUSINESS_ORDERS.labels('success').inc()
    ORDER_PROCESSING.observe(time.time() - start_time)
    logging.info("Order processed successfully", extra={"request_id": req_id})
    return {"status": "success"}

@app.route('/cancel', methods=['POST'])
def cancel():
    BUSINESS_ORDERS.labels('cancelled').inc()
    logging.info("Order cancelled by user", extra={"request_id": str(uuid.uuid4())[:8]})
    return {"status": "cancelled"}

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
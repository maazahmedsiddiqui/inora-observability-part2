from prometheus_client import Counter, start_http_server
import time

demo_requests_v2 = Counter('demo_requests_v2_total', 'test counter without label')
start_http_server(8002)

for i in range(100):
    demo_requests_v2.inc()
    time.sleep(1)

print("Done — no labels used, should still be 1 series.")



# test 1:
# from prometheus_client import Counter, start_http_server
# import uuid, time

# demo_requests = Counter('demo_requests_total', 'test counter', ['request_id'])
# start_http_server(8001)

# for i in range(100):
#     demo_requests.labels(request_id=str(uuid.uuid4())).inc()
#     time.sleep(1)

# print("Done — 100 unique IDs generated. Keep this running or check Prometheus now.")
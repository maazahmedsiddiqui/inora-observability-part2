"""Part C tests: the gRPC metrics are correct, bounded, and cover the success and failure paths."""
import threading
import time

import grpc
import pytest
from prometheus_client import CollectorRegistry

import grpc_metrics as m
import grpc_server
from grpc_client import place_order
from inora_core import Store

SV, ME = "inora.v1.OrderService", "PlaceOrder"
GOOD = dict(product_id="clover-pendant", quantity=1, payment_token="tok_visa")


def handled(code):
    return m.GRPC_REGISTRY.get_sample_value(
        "grpc_server_handled_total", {"grpc_service": SV, "grpc_method": ME, "grpc_code": code}) or 0.0


def duration_count():
    return m.GRPC_REGISTRY.get_sample_value("grpc_server_handling_seconds_count",
                                            {"grpc_service": SV, "grpc_method": ME}) or 0.0


def in_flight():
    return m.GRPC_REGISTRY.get_sample_value("grpc_server_in_flight", {"grpc_service": SV, "grpc_method": ME})


def business(status):
    return m.GRPC_REGISTRY.get_sample_value("business_orders_total", {"status": status}) or 0.0


def call(target, deadline_s=5.0, **kw):
    a = {**GOOD, **kw}
    return place_order(target, a["product_id"], a["quantity"], a["payment_token"], "", deadline_s)


@pytest.fixture
def target(tmp_path):
    server, port = grpc_server.build_server(Store(str(tmp_path / "m.db")), port=0)
    server.start()
    yield f"127.0.0.1:{port}"
    server.stop(0)


@pytest.fixture
def slow_target(tmp_path):
    server, port = grpc_server.build_server(Store(str(tmp_path / "ms.db")), port=0, simulate_delay_s=0.6)
    server.start()
    yield f"127.0.0.1:{port}"
    server.stop(0)


def test_calls_are_counted_by_final_status_code(target):
    before = {c: handled(c) for c in ("OK", "INVALID_ARGUMENT", "NOT_FOUND", "FAILED_PRECONDITION", "RESOURCE_EXHAUSTED")}
    call(target)
    call(target, quantity=0)
    call(target, product_id="nope")
    call(target, payment_token="tok_declined")
    call(target, product_id="limited-necklace")
    call(target, product_id="limited-necklace")        # second one: out of stock
    after = {c: handled(c) for c in before}
    assert {c: after[c] - before[c] for c in before} == {
        "OK": 2, "INVALID_ARGUMENT": 1, "NOT_FOUND": 1, "FAILED_PRECONDITION": 1, "RESOURCE_EXHAUSTED": 1}


def test_duration_histogram_observes_every_call_success_or_failure(target):
    n = duration_count()
    s = m.GRPC_REGISTRY.get_sample_value("grpc_server_handling_seconds_sum", {"grpc_service": SV, "grpc_method": ME})
    call(target)
    call(target, quantity=0)
    assert duration_count() - n == 2
    assert m.GRPC_REGISTRY.get_sample_value("grpc_server_handling_seconds_sum",
                                            {"grpc_service": SV, "grpc_method": ME}) > s
    # buckets are cumulative and end in +Inf
    inf = m.GRPC_REGISTRY.get_sample_value("grpc_server_handling_seconds_bucket",
                                           {"grpc_service": SV, "grpc_method": ME, "le": "+Inf"})
    assert inf == duration_count()


def test_in_flight_rises_during_a_call_and_returns_to_zero(slow_target):
    seen = []
    t = threading.Thread(target=lambda: call(slow_target))
    t.start()
    for _ in range(20):
        seen.append(in_flight())
        time.sleep(0.05)
    t.join()
    assert max(seen) == 1.0 and in_flight() == 0.0


def test_client_deadline_is_recorded_server_side_as_cancelled(slow_target):
    before = handled("CANCELLED")
    _, err, _ = call(slow_target, deadline_s=0.2)
    assert err.code() == grpc.StatusCode.DEADLINE_EXCEEDED          # what the CLIENT sees
    time.sleep(0.8)                                                  # let the slow handler finish
    assert handled("CANCELLED") - before == 1                        # what the SERVER sees


def test_unknown_method_is_counted_under_a_bounded_label(target):
    before = m.GRPC_REGISTRY.get_sample_value("grpc_server_handled_total", {
        "grpc_service": m.UNKNOWN, "grpc_method": m.UNKNOWN, "grpc_code": "UNIMPLEMENTED"}) or 0.0
    with grpc.insecure_channel(target) as ch:
        with pytest.raises(grpc.RpcError):
            ch.unary_unary("/inora.v1.OrderService/Nope")(b"", timeout=2)
    after = m.GRPC_REGISTRY.get_sample_value("grpc_server_handled_total", {
        "grpc_service": m.UNKNOWN, "grpc_method": m.UNKNOWN, "grpc_code": "UNIMPLEMENTED"})
    assert after - before == 1


def test_business_outcomes_match_the_http_rules(target):
    s, f = business("success"), business("failed")
    call(target)                                                     # success
    call(target, payment_token="tok_declined")                       # failed
    call(target, quantity=0)                                         # invalid input: NOT a business failure
    assert (business("success") - s, business("failed") - f) == (1, 1)


def test_stock_collector_reads_the_shared_database(tmp_path):
    store = Store(str(tmp_path / "stock.db"))
    reg = CollectorRegistry()
    reg.register(m.StockCollector(store))
    get = lambda p: reg.get_sample_value("inora_product_stock", {"product_id": p})
    assert get("clover-pendant") == 50 and get("limited-necklace") == 1
    store.place_order("clover-pendant", 3, "tok_visa")
    assert get("clover-pendant") == 47


def test_label_names_are_bounded_no_ids():
    allowed = {"grpc_service", "grpc_method", "grpc_code", "status", "le", "product_id"}
    names = set()
    for metric in m.GRPC_REGISTRY.collect():
        for sample in metric.samples:
            names |= set(sample.labels)
    assert names <= allowed, names - allowed
    for bad in ("trace_id", "request_id", "order_id", "user", "idempotency_key"):
        assert bad not in names

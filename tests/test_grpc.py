"""Part B tests: the gRPC service and its relationship to the HTTP API.
A real gRPC server runs in-process on a free port, talking to a real SQLite file."""
import logging

import grpc
import pytest

import grpc_server
from app import create_app
from grpc_client import describe_error, place_order
from inora_core import DomainError, Store
from inora_obs import make_traceparent

GOOD = dict(product_id="clover-pendant", quantity=2, payment_token="tok_visa")


class Capture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        self.records.append(record)


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "shared.db")


@pytest.fixture
def grpc_target(db_path):
    server, port = grpc_server.build_server(Store(db_path), port=0)
    server.start()
    yield f"127.0.0.1:{port}"
    server.stop(0)


@pytest.fixture
def slow_target(db_path):
    server, port = grpc_server.build_server(Store(db_path), port=0, simulate_delay_s=1.0)
    server.start()
    yield f"127.0.0.1:{port}"
    server.stop(0)


@pytest.fixture
def logs():
    h = Capture()
    root = logging.getLogger()
    old = root.level
    root.addHandler(h)
    root.setLevel(logging.INFO)
    yield h.records
    root.removeHandler(h)
    root.setLevel(old)


def call(target, **overrides):
    args = {**GOOD, **overrides}
    return place_order(target, args["product_id"], args["quantity"], args["payment_token"],
                       args.get("idempotency_key", ""), args.get("deadline_s", 5.0), args.get("traceparent"))


# ---------- success ----------
def test_place_order_success(grpc_target):
    resp, err, _ = call(grpc_target)
    assert err is None
    assert resp.order.id.startswith("ord_") and resp.order.status == "placed"
    assert resp.order.total_pkr == 9000 and resp.replayed is False


# ---------- each failure -> its documented gRPC status + domain code in trailing metadata ----------
@pytest.mark.parametrize("overrides,status,code", [
    (dict(quantity=0), grpc.StatusCode.INVALID_ARGUMENT, "validation_error"),
    (dict(quantity=11), grpc.StatusCode.INVALID_ARGUMENT, "validation_error"),
    (dict(payment_token="visa"), grpc.StatusCode.INVALID_ARGUMENT, "validation_error"),
    (dict(product_id=""), grpc.StatusCode.INVALID_ARGUMENT, "validation_error"),
    (dict(product_id="nope"), grpc.StatusCode.NOT_FOUND, "not_found"),
    (dict(payment_token="tok_declined"), grpc.StatusCode.FAILED_PRECONDITION, "payment_declined"),
])
def test_failures_map_to_grpc_status_and_error_code(grpc_target, overrides, status, code):
    resp, err, _ = call(grpc_target, **overrides)
    assert resp is None
    info = describe_error(err)
    assert info["status"] == status.name and info["error_code"] == code


def test_out_of_stock_is_resource_exhausted(grpc_target):
    scarce = dict(product_id="limited-necklace", quantity=1)
    assert call(grpc_target, **scarce)[1] is None
    _, err, _ = call(grpc_target, **scarce)
    assert describe_error(err) == {"status": "RESOURCE_EXHAUSTED", "details": "not enough stock for this product",
                                   "error_code": "out_of_stock"}


# ---------- idempotency over gRPC ----------
def test_same_key_replays_and_conflicting_key_is_already_exists(grpc_target):
    first, _, _ = call(grpc_target, idempotency_key="k1")
    second, _, _ = call(grpc_target, idempotency_key="k1")
    assert second.order.id == first.order.id and second.replayed is True
    _, err, _ = call(grpc_target, idempotency_key="k1", quantity=3)
    assert describe_error(err)["status"] == "ALREADY_EXISTS"


# ---------- B1: same business rules and the same source of truth as HTTP ----------
def test_http_and_grpc_share_one_source_of_truth(db_path, grpc_target):
    http = create_app(db_path=db_path, rate_limit=100).test_client()
    body = {"product_id": "clover-pendant", "quantity": 2, "payment_token": "tok_visa"}

    via_http = http.post("/orders", json=body, headers={"Idempotency-Key": "shared-1"})
    assert via_http.status_code == 201
    via_grpc, err, _ = call(grpc_target, idempotency_key="shared-1")
    assert err is None and via_grpc.replayed is True                       # gRPC saw the HTTP order
    assert via_grpc.order.id == via_http.get_json()["id"]

    other, _, _ = call(grpc_target, idempotency_key="shared-2")            # gRPC order is visible to HTTP
    assert http.get(f"/orders/{other.order.id}").get_json()["total_pkr"] == 9000
    assert http.get("/products/clover-pendant").get_json()["stock"] == 50 - 2 - 2


def test_same_input_gives_equivalent_outcome_on_both_front_doors(db_path, grpc_target):
    """Every failure case maps to an HTTP status AND a gRPC status through the SAME domain code."""
    http = create_app(db_path=db_path, rate_limit=1000).test_client()
    cases = [dict(quantity=0), dict(product_id="nope"), dict(payment_token="tok_declined")]
    for overrides in cases:
        body = {**GOOD, **overrides}
        h = http.post("/orders", json=body).get_json()["code"]
        _, err, _ = call(grpc_target, **overrides)
        assert describe_error(err)["error_code"] == h


def test_every_domain_error_has_a_grpc_status():
    codes = {cls.code for cls in DomainError.__subclasses__()}
    assert codes == set(grpc_server.GRPC_STATUS_MAP)


# ---------- B3: deadline ----------
def test_deadline_exceeded_is_reported(slow_target):
    resp, err, _ = call(slow_target, deadline_s=0.2)
    assert resp is None and err.code() == grpc.StatusCode.DEADLINE_EXCEEDED


def test_generous_deadline_succeeds_on_slow_server(slow_target):
    assert call(slow_target, deadline_s=5.0)[1] is None


# ---------- B6: trace correlation across the gRPC boundary ----------
def test_trace_id_from_caller_appears_in_server_logs_success_and_failure(grpc_target, logs):
    tp = make_traceparent()
    trace_id = tp.split("-")[1]
    assert call(grpc_target, traceparent=tp)[1] is None
    assert call(grpc_target, traceparent=tp, payment_token="tok_declined")[1] is not None
    mine = [r for r in logs if getattr(r, "trace_id", None) == trace_id]
    assert len(mine) == 2
    assert {r.levelno for r in mine} == {logging.INFO, logging.ERROR}
    assert all(r.request_id == trace_id[:8] for r in mine)


def test_server_returns_traceparent_in_trailing_metadata_on_error(grpc_target):
    tp = make_traceparent()
    _, err, _ = call(grpc_target, traceparent=tp, quantity=0)
    assert dict(err.trailing_metadata())["traceparent"] == tp


def test_server_starts_a_trace_when_caller_sends_none(grpc_target, logs):
    import inora.v1.inora_pb2 as pb
    import inora.v1.inora_pb2_grpc as rpc
    with grpc.insecure_channel(grpc_target) as ch:
        rpc.OrderServiceStub(ch).PlaceOrder(pb.PlaceOrderRequest(
            product_id="clover-pendant", quantity=1, payment_token="tok_visa"))
    assert any(len(getattr(r, "trace_id", "")) == 32 for r in logs)

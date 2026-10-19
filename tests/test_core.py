import threading

import pytest

from inora_core import (
    Store, ValidationFailed, NotFound, OutOfStock, PaymentDeclined, IdempotencyConflict,
)

GOOD = dict(product_id="clover-pendant", quantity=2, payment_token="tok_visa")


@pytest.fixture
def store(tmp_path):
    return Store(str(tmp_path / "test.db"))


def stock(store, pid):
    return store.get_product(pid)["stock"]


# ---- valid request ----
def test_valid_order_is_created_and_stock_drops(store):
    order, replayed = store.place_order(**GOOD)
    assert replayed is False
    assert order["status"] == "placed"
    assert order["total_pkr"] == 9000
    assert stock(store, "clover-pendant") == 48


# ---- invalid input ----
@pytest.mark.parametrize("bad", [
    dict(product_id=""), dict(quantity=0), dict(quantity=11), dict(quantity="2"),
    dict(quantity=True), dict(payment_token="visa"), dict(payment_token="tok_"),
])
def test_invalid_input_is_rejected_and_changes_nothing(store, bad):
    with pytest.raises(ValidationFailed):
        store.place_order(**{**GOOD, **bad})
    assert stock(store, "clover-pendant") == 50


def test_unknown_product(store):
    with pytest.raises(NotFound):
        store.place_order(**{**GOOD, "product_id": "nope"})


def test_declined_payment_is_deterministic_and_keeps_stock(store):
    with pytest.raises(PaymentDeclined):
        store.place_order(**{**GOOD, "payment_token": "tok_declined"})
    assert stock(store, "clover-pendant") == 50


def test_out_of_stock(store):
    store.place_order(product_id="limited-necklace", quantity=1, payment_token="tok_visa")
    with pytest.raises(OutOfStock):
        store.place_order(product_id="limited-necklace", quantity=1, payment_token="tok_visa")


# ---- repeated request (idempotency) ----
def test_same_key_same_request_does_not_duplicate(store):
    first, r1 = store.place_order(**GOOD, idempotency_key="k1")
    second, r2 = store.place_order(**GOOD, idempotency_key="k1")
    assert (r1, r2) == (False, True)
    assert first["id"] == second["id"]
    assert stock(store, "clover-pendant") == 48          # decremented once, not twice


def test_same_key_different_request_conflicts(store):
    store.place_order(**GOOD, idempotency_key="k1")
    with pytest.raises(IdempotencyConflict):
        store.place_order(**{**GOOD, "quantity": 3}, idempotency_key="k1")


def test_failed_attempt_does_not_burn_the_key(store):
    with pytest.raises(PaymentDeclined):
        store.place_order(**{**GOOD, "payment_token": "tok_declined"}, idempotency_key="k2")
    order, replayed = store.place_order(**GOOD, idempotency_key="k2")
    assert replayed is False and order["status"] == "placed"


def test_parallel_retries_with_one_key_create_one_order(store):
    results = []

    def go():
        results.append(store.place_order(**GOOD, idempotency_key="race"))

    threads = [threading.Thread(target=go) for _ in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len({o["id"] for o, _ in results}) == 1
    assert sum(1 for _, replayed in results if not replayed) == 1
    assert stock(store, "clover-pendant") == 48


# ---- cancel ----
def test_cancel_restores_stock_and_repeat_is_safe(store):
    order, _ = store.place_order(**GOOD)
    assert store.cancel_order(order["id"])["status"] == "cancelled"
    assert stock(store, "clover-pendant") == 50
    store.cancel_order(order["id"])
    assert stock(store, "clover-pendant") == 50          # not restored twice


def test_cancel_unknown_order(store):
    with pytest.raises(NotFound):
        store.cancel_order("ord_missing")

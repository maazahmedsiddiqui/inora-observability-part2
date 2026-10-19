import re
import pytest

from app import create_app


@pytest.fixture
def client(tmp_path):
    app = create_app(
        db_path=str(tmp_path / "http_test.db"),
        rate_limit=5,
        rate_window=10,
    )
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c


# ---- A8: Valid request ----
def test_list_and_get_products(client):
    r = client.get("/products")
    assert r.status_code == 200
    items = r.get_json()["items"]
    assert len(items) == 4
    single = client.get("/products/clover-pendant")
    assert single.status_code == 200
    assert single.get_json()["price_pkr"] == 4500


def test_place_valid_order_and_fetch_it(client):
    r = client.post(
        "/orders",
        json={"product_id": "clover-pendant", "quantity": 2, "payment_token": "tok_visa"},
        headers={"Idempotency-Key": "valid-1"},
    )
    assert r.status_code == 201
    assert r.headers["Idempotent-Replayed"] == "false"
    assert "traceparent" in r.headers
    body = r.get_json()
    assert body["total_pkr"] == 9000
    assert body["status"] == "placed"

    fetched = client.get(f"/orders/{body['id']}")
    assert fetched.status_code == 200
    assert fetched.get_json()["id"] == body["id"]


# ---- A8: Invalid input & consistent application/problem+json error format ----
@pytest.mark.parametrize(
    "payload,expected_status,expected_code",
    [
        ({"product_id": "", "quantity": 1, "payment_token": "tok_visa"}, 400, "validation_error"),
        ({"product_id": "clover-pendant", "quantity": 0, "payment_token": "tok_visa"}, 400, "validation_error"),
        ({"product_id": "clover-pendant", "quantity": 99, "payment_token": "tok_visa"}, 400, "validation_error"),
        ({"product_id": "clover-pendant", "quantity": 1, "payment_token": "bad"}, 400, "validation_error"),
        ({"product_id": "unknown-item", "quantity": 1, "payment_token": "tok_visa"}, 404, "not_found"),
        ({"product_id": "clover-pendant", "quantity": 1, "payment_token": "tok_declined"}, 422, "payment_declined"),
    ],
)
def test_invalid_or_rejected_orders_return_problem_json(client, payload, expected_status, expected_code):
    r = client.post("/orders", json=payload)
    assert r.status_code == expected_status
    assert r.content_type.startswith("application/problem+json")
    prob = r.get_json()
    assert prob["status"] == expected_status
    assert prob["code"] == expected_code
    assert "detail" in prob and "trace_id" in prob


# ---- A5 & A8: Repeated request (idempotency) ----
def test_repeated_request_with_same_idempotency_key_is_safe(client):
    payload = {"product_id": "clover-pendant", "quantity": 3, "payment_token": "tok_visa"}
    r1 = client.post("/orders", json=payload, headers={"Idempotency-Key": "idem-repeat-1"})
    r2 = client.post("/orders", json=payload, headers={"Idempotency-Key": "idem-repeat-1"})
    assert r1.status_code == 201
    assert r2.status_code == 200
    assert r2.headers["Idempotent-Replayed"] == "true"
    assert r1.get_json()["id"] == r2.get_json()["id"]
    # Stock should have dropped by 3 once (50 -> 47), not 6
    prod = client.get("/products/clover-pendant").get_json()
    assert prod["stock"] == 47


def test_same_idempotency_key_with_different_body_returns_409(client):
    client.post(
        "/orders",
        json={"product_id": "clover-pendant", "quantity": 1, "payment_token": "tok_visa"},
        headers={"Idempotency-Key": "idem-conflict"},
    )
    r2 = client.post(
        "/orders",
        json={"product_id": "clover-pendant", "quantity": 2, "payment_token": "tok_visa"},
        headers={"Idempotency-Key": "idem-conflict"},
    )
    assert r2.status_code == 409
    assert r2.get_json()["code"] == "idempotency_conflict"


# ---- Subresource cancellation ----
def test_cancel_order_subresource_restores_stock_and_is_idempotent(client):
    placed = client.post(
        "/orders",
        json={"product_id": "limited-necklace", "quantity": 1, "payment_token": "tok_visa"},
    ).get_json()
    assert client.get("/products/limited-necklace").get_json()["stock"] == 0

    c1 = client.post(f"/orders/{placed['id']}/cancellations")
    c2 = client.post(f"/orders/{placed['id']}/cancellations")
    assert c1.status_code == 200 and c2.status_code == 200
    assert c2.get_json()["status"] == "cancelled"
    assert client.get("/products/limited-necklace").get_json()["stock"] == 1


# ---- A6: Rate limiting ----
def test_rate_limiter_allows_5_then_returns_429_with_retry_after(client):
    payload = {"product_id": "minimalist-ring", "quantity": 1, "payment_token": "tok_visa"}
    for i in range(5):
        r = client.post("/orders", json=payload, headers={"Idempotency-Key": f"rl-{i}"})
        assert r.status_code == 201

    blocked = client.post("/orders", json=payload, headers={"Idempotency-Key": "rl-6"})
    assert blocked.status_code == 429
    assert blocked.content_type.startswith("application/problem+json")
    assert int(blocked.headers["Retry-After"]) >= 1
    assert blocked.get_json()["code"] == "rate_limit_exceeded"

# ---- A4: OpenAPI spec matches live Flask routes ----
def test_openapi_spec_matches_registered_routes(client):
    spec_resp = client.get("/openapi.yaml")
    assert spec_resp.status_code == 200
    spec_text = spec_resp.get_data(as_text=True)
    spec_paths = {
        line.strip().rstrip(":")
        for line in spec_text.splitlines()
        if line.strip().startswith("/") and line.strip().endswith(":")
    }

    flask_paths = set()
    for rule in client.application.url_map.iter_rules():
        if rule.rule not in ("/static/<path:filename>", "/metrics", "/openapi.yaml"):
            normalized = re.sub(r"<(?:[^:>]+:)?([^>]+)>", r"{\1}", rule.rule)
            flask_paths.add(normalized)

    assert spec_paths == flask_paths


# ---- A2: consistent error format also for routing / protocol / malformed-body errors ----
def test_unknown_url_returns_problem_json(client):
    r = client.get("/no-such-page")
    assert r.status_code == 404
    assert r.content_type.startswith("application/problem+json")
    assert r.get_json()["code"] == "not_found"


def test_wrong_method_returns_problem_json_with_allow_header(client):
    r = client.put("/orders", json={})
    assert r.status_code == 405
    assert r.content_type.startswith("application/problem+json")
    assert r.get_json()["code"] == "method_not_allowed"
    assert "POST" in r.headers["Allow"]


def test_malformed_json_body_returns_400_problem_json(client):
    r = client.post("/orders", data="{not json", content_type="application/json")
    assert r.status_code == 400
    assert r.content_type.startswith("application/problem+json")
    assert r.get_json()["code"] == "validation_error"


def test_missing_json_content_type_returns_400_problem_json(client):
    r = client.post("/orders", data="product_id=x", content_type="text/plain")
    assert r.status_code == 400
    assert r.get_json()["code"] == "validation_error"


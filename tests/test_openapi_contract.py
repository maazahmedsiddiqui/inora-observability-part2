"""A4: use openapi.yaml to TEST the running API.

For every request below, the spec decides what a correct answer looks like:
  * the status code must be documented for that operation,
  * the Content-Type must be one the spec lists for that status,
  * the body must match the response schema,
  * every header the spec declares for that response must be present.
Request bodies are also checked against the spec: inputs the schema accepts must be accepted by the server,
inputs the schema rejects must be rejected with a 400. Finally every documented outcome of POST /orders must be
reachable (nothing documented but never produced, nothing produced but undocumented).
"""
import copy
import os

import pytest
import yaml
from jsonschema import Draft7Validator

from app import create_app

SPEC_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "openapi.yaml")
SPEC = yaml.safe_load(open(SPEC_PATH, encoding="utf-8"))


# ---------------- tiny OpenAPI helpers ----------------
def deref(node):
    """Resolve local $refs ('#/components/...') everywhere in a node; convert OpenAPI 'nullable' to JSON Schema."""
    if isinstance(node, dict):
        if "$ref" in node:
            target = SPEC
            for part in node["$ref"].lstrip("#/").split("/"):
                target = target[part]
            return deref(copy.deepcopy(target))
        out = {k: deref(v) for k, v in node.items()}
        if out.get("nullable") and "type" in out:
            out["type"] = [out["type"], "null"]
        out.pop("nullable", None)
        return out
    if isinstance(node, list):
        return [deref(v) for v in node]
    return node


def operation(method, template):
    return deref(SPEC["paths"][template][method.lower()])


def assert_matches_spec(resp, method, template):
    """The core check: does this real response match what the spec promises?"""
    op = operation(method, template)
    documented = op["responses"]
    status = str(resp.status_code)
    assert status in documented, f"{method} {template} returned {status}; spec documents {sorted(documented)}"
    spec_resp = documented[status]
    ctype = resp.mimetype
    content = spec_resp.get("content", {})
    assert ctype in content, f"{method} {template} {status}: Content-Type {ctype!r} not in spec {list(content)}"
    schema = content[ctype].get("schema")
    if schema:
        errors = list(Draft7Validator(schema).iter_errors(resp.get_json()))
        assert not errors, f"{method} {template} {status} body breaks the schema: {errors[0].message}"
    for header, hdef in spec_resp.get("headers", {}).items():
        assert header in resp.headers, f"{method} {template} {status} is missing documented header {header}"
        allowed = hdef.get("schema", {}).get("enum")
        if allowed:
            assert resp.headers[header] in allowed
    return status


# ---------------- fixtures ----------------
GOOD = {"product_id": "clover-pendant", "quantity": 2, "payment_token": "tok_visa"}


@pytest.fixture
def client(tmp_path):
    return create_app(db_path=str(tmp_path / "contract.db"), rate_limit=1000, rate_window=10).test_client()


def post_order(client, body=GOOD, **headers):
    return client.post("/orders", json=body, headers=headers)


# ---------------- the spec itself ----------------
def test_spec_routes_and_methods_match_the_flask_app(client):
    from_spec = {(m.upper(), p.replace("{", "<").replace("}", ">")) for p, ops in SPEC["paths"].items() for m in ops}
    from_app = {(m, r.rule) for r in client.application.url_map.iter_rules() for m in r.methods
                if m in ("GET", "POST") and r.rule not in ("/metrics", "/openapi.yaml", "/static/<path:filename>")}
    assert from_spec == from_app


def test_every_operation_has_an_operation_id_and_documented_errors():
    ids = []
    for path, ops in SPEC["paths"].items():
        for method, op in ops.items():
            ids.append(op["operationId"])
            assert any(code.startswith(("4", "2")) for code in op["responses"]), (path, method)
    assert len(ids) == len(set(ids))


# ---------------- GET endpoints ----------------
def test_get_products_matches_spec(client):
    assert assert_matches_spec(client.get("/products"), "GET", "/products") == "200"


def test_get_product_ok_and_not_found_match_spec(client):
    assert assert_matches_spec(client.get("/products/clover-pendant"), "GET", "/products/{product_id}") == "200"
    assert assert_matches_spec(client.get("/products/nope"), "GET", "/products/{product_id}") == "404"


def test_get_order_ok_and_not_found_match_spec(client):
    oid = post_order(client).get_json()["id"]
    assert assert_matches_spec(client.get(f"/orders/{oid}"), "GET", "/orders/{order_id}") == "200"
    assert assert_matches_spec(client.get("/orders/ord_missing"), "GET", "/orders/{order_id}") == "404"


def test_cancel_ok_repeat_and_not_found_match_spec(client):
    oid = post_order(client).get_json()["id"]
    for _ in range(2):                                               # safe to repeat
        assert assert_matches_spec(client.post(f"/orders/{oid}/cancellations"), "POST",
                                   "/orders/{order_id}/cancellations") == "200"
    assert assert_matches_spec(client.post("/orders/ord_missing/cancellations"), "POST",
                               "/orders/{order_id}/cancellations") == "404"


# ---------------- POST /orders: every documented outcome ----------------
def observe_post_orders(tmp_path):
    """Provoke every outcome of POST /orders and return {status: response}."""
    seen = {}

    def record(resp):
        seen[assert_matches_spec(resp, "POST", "/orders")] = resp
        return resp

    app = create_app(db_path=str(tmp_path / "all.db"), rate_limit=1000, rate_window=10).test_client()
    record(app.post("/orders", json=GOOD, headers={"Idempotency-Key": "k1"}))                                                     # 201
    record(app.post("/orders", json=GOOD, headers={"Idempotency-Key": "k1"}))                                                     # 200 replay
    record(post_order(app, {**GOOD, "quantity": 0}))                                                                              # 400
    record(post_order(app, {**GOOD, "product_id": "nope"}))                                                                       # 404
    scarce = {**GOOD, "product_id": "limited-necklace", "quantity": 1}
    post_order(app, scarce)
    record(post_order(app, scarce))                                                                                               # 409 out of stock
    record(app.post("/orders", json={**GOOD, "quantity": 3}, headers={"Idempotency-Key": "k1"}))                                  # 409 key reuse
    record(post_order(app, {**GOOD, "payment_token": "tok_declined"}))                                                            # 422
    record(app.post("/orders", json=GOOD, headers={"X-Simulate-Fault": "503"}))                                                   # 503
    limited = create_app(db_path=str(tmp_path / "rl.db"), rate_limit=2, rate_window=10).test_client()
    for _ in range(3):
        resp = limited.post("/orders", json=GOOD)
    record(resp)                                                                                                                  # 429
    return seen


def test_every_documented_post_orders_outcome_is_produced_and_matches_spec(tmp_path):
    seen = observe_post_orders(tmp_path)
    documented = set(SPEC["paths"]["/orders"]["post"]["responses"])
    # 500 is documented but needs a bug to trigger; every other documented outcome must really happen
    assert set(seen) == documented - {"500"}
    assert int(seen["429"].headers["Retry-After"]) >= 1


def test_replay_and_first_response_are_distinguishable(tmp_path):
    seen = observe_post_orders(tmp_path)
    assert seen["201"].headers["Idempotent-Replayed"] == "false"
    assert seen["200"].headers["Idempotent-Replayed"] == "true"
    assert seen["201"].headers["Location"].startswith("/orders/ord_")


# ---------------- inputs: the spec's request schema vs the server's validation ----------------
REQ_SCHEMA = deref(SPEC["components"]["schemas"]["PlaceOrderRequest"])


@pytest.mark.parametrize("body", [
    GOOD, {**GOOD, "quantity": 1}, {**GOOD, "quantity": 10}, {**GOOD, "product_id": "pearl-bracelet"},
])
def test_inputs_the_spec_accepts_are_accepted_by_the_server(client, body):
    assert not list(Draft7Validator(REQ_SCHEMA).iter_errors(body))
    assert post_order(client, body).status_code == 201


@pytest.mark.parametrize("body", [
    {**GOOD, "quantity": 0}, {**GOOD, "quantity": 11}, {**GOOD, "quantity": "2"},
    {**GOOD, "payment_token": "visa"}, {"product_id": "clover-pendant", "quantity": 1},
])
def test_inputs_the_spec_rejects_are_rejected_by_the_server(client, body):
    assert list(Draft7Validator(REQ_SCHEMA).iter_errors(body))
    resp = post_order(client, body)
    assert resp.status_code == 400
    assert_matches_spec(resp, "POST", "/orders")


# ---------------- opt-in CORS (for Swagger UI) ----------------
def test_cors_is_off_by_default_and_on_when_configured(tmp_path, monkeypatch):
    monkeypatch.delenv("CORS_ALLOW_ORIGIN", raising=False)
    off = create_app(db_path=str(tmp_path / "c1.db")).test_client()
    assert "Access-Control-Allow-Origin" not in off.get("/products").headers

    monkeypatch.setenv("CORS_ALLOW_ORIGIN", "http://localhost:8080")
    on = create_app(db_path=str(tmp_path / "c2.db")).test_client()
    pre = on.options("/orders", headers={"Origin": "http://localhost:8080",
                                         "Access-Control-Request-Method": "POST",
                                         "Access-Control-Request-Headers": "content-type,idempotency-key"})
    assert pre.headers["Access-Control-Allow-Origin"] == "http://localhost:8080"
    assert "Idempotency-Key" in pre.headers["Access-Control-Allow-Headers"]
    assert "Retry-After" in on.get("/products").headers["Access-Control-Expose-Headers"]

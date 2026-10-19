"""Tests for caller.py: timeout, limited retry with backoff, circuit breaker. No network, no real sleeping."""
import pytest
import requests

from caller import CircuitBreaker, InoraClient, Settings


class FakeResp:
    def __init__(self, status, body=None, headers=None):
        self.status_code, self._body, self.headers = status, body or {}, headers or {}

    def json(self):
        return self._body


class FakeSession:
    """Plays back a script of responses/exceptions, one per request."""
    def __init__(self, script):
        self.script, self.calls = list(script), []

    def request(self, method, url, json=None, headers=None, timeout=None):
        self.calls.append({"method": method, "url": url, "headers": headers, "timeout": timeout})
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def make(script, **kw):
    clock, sleeps = Clock(), []
    session = FakeSession(script)

    def sleep(d):
        sleeps.append(d)
        clock.t += d

    c = InoraClient(session=session, sleep=sleep, clock=clock, rng=lambda: 0.5,   # rng 0.5 -> zero jitter
                    log=lambda *_: None, settings=Settings(**kw))
    return c, session, sleeps, clock


OK = lambda: FakeResp(201, {"id": "ord_1"})
E503 = lambda: FakeResp(503, {"code": "service_unavailable"})
TIMEOUT = lambda: requests.exceptions.Timeout()


def test_success_first_try_sends_timeout_key_and_traceparent():
    c, s, sleeps, _ = make([OK()])
    r = c.place_order("clover-pendant", 1)
    assert r.ok and r.attempts == 1 and sleeps == []
    call = s.calls[0]
    assert call["timeout"] == 2.0
    assert call["headers"]["Idempotency-Key"].startswith("caller-")
    assert call["headers"]["traceparent"].startswith("00-")


def test_retry_after_timeout_then_success_reuses_same_key_and_trace():
    c, s, sleeps, _ = make([TIMEOUT(), OK()])
    r = c.place_order("clover-pendant", 1)
    assert r.ok and r.attempts == 2
    keys = {call["headers"]["Idempotency-Key"] for call in s.calls}
    traces = {call["headers"]["traceparent"].split("-")[1] for call in s.calls}
    assert len(keys) == 1 and len(traces) == 1            # a retry is the SAME logical operation
    assert sleeps == [1.0]


def test_backoff_doubles_and_attempts_are_limited():
    c, s, sleeps, _ = make([E503(), E503(), E503(), OK()], breaker_threshold=99)
    r = c.place_order("clover-pendant", 1)
    assert r.outcome == "failed" and r.attempts == 3 and len(s.calls) == 3   # 4th response never requested
    assert sleeps == [1.0, 2.0]


def test_backoff_is_capped():
    c, s, sleeps, _ = make([E503()] * 6, max_attempts=6, backoff_cap_s=3.0, breaker_threshold=99)
    c.place_order("clover-pendant", 1)
    assert sleeps == [1.0, 2.0, 3.0, 3.0, 3.0]


@pytest.mark.parametrize("status", [400, 402, 404, 409, 422])
def test_client_errors_are_not_retried_and_do_not_trip_breaker(status):
    c, s, sleeps, _ = make([FakeResp(status, {"code": "x"})])
    r = c.place_order("clover-pendant", 1)
    assert r.outcome == "client_error" and len(s.calls) == 1 and c.breaker.failures == 0


def test_429_is_reported_separately_with_retry_after_and_never_trips_breaker():
    c, s, sleeps, _ = make([FakeResp(429, {"code": "rate_limit_exceeded"}, {"Retry-After": "7"})] * 5)
    for _ in range(5):
        r = c.place_order("clover-pendant", 1)
        assert r.outcome == "rate_limited" and r.retry_after_s == 7
    assert c.breaker.state == CircuitBreaker.CLOSED and sleeps == []


def test_post_without_idempotency_key_is_never_retried():
    c, s, sleeps, _ = make([E503(), OK()])
    r = c.request("POST", "/orders", json={})            # no key: a repeat could create a duplicate
    assert r.outcome == "failed" and len(s.calls) == 1


def test_get_is_safe_to_retry_without_a_key():
    c, s, sleeps, _ = make([E503(), FakeResp(200, {"items": []})])
    assert c.request("GET", "/products").ok and len(s.calls) == 2


def test_connection_error_is_a_failure_and_is_retried():
    c, s, _, _ = make([requests.exceptions.ConnectionError(), OK()])
    assert c.place_order("clover-pendant", 1).attempts == 2


def test_breaker_opens_after_threshold_blocks_calls_then_recovers():
    c, s, sleeps, clock = make([E503(), E503(), E503()] + [OK(), OK()])
    r = c.place_order("clover-pendant", 1)                      # 3 failures -> OPEN
    assert r.outcome == "failed" and c.breaker.state == CircuitBreaker.OPEN
    sent = len(s.calls)

    blocked = c.place_order("clover-pendant", 1)                # blocked: nothing sent
    assert blocked.outcome == "circuit_open" and len(s.calls) == sent

    clock.t += 5.0                                              # recovery period passes
    trial = c.place_order("clover-pendant", 1)                  # half-open trial succeeds
    assert trial.ok and c.breaker.state == CircuitBreaker.CLOSED
    assert c.place_order("clover-pendant", 1).ok


def test_failed_half_open_trial_reopens_breaker():
    c, s, _, clock = make([E503()] * 3 + [E503()])
    c.place_order("clover-pendant", 1)
    clock.t += 5.0
    r = c.place_order("clover-pendant", 1)
    assert r.outcome == "failed" and c.breaker.state == CircuitBreaker.OPEN
    assert c.place_order("clover-pendant", 1).outcome == "circuit_open"


def test_success_resets_failure_count():
    c, s, _, _ = make([E503(), OK(), E503(), OK()])
    c.place_order("clover-pendant", 1)
    c.place_order("clover-pendant", 1)
    assert c.breaker.failures == 0 and c.breaker.state == CircuitBreaker.CLOSED

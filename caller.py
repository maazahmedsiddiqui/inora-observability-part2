"""caller.py - a small service-style caller for the Inora HTTP API (Assignment 2, Part A).

It acts like another service that places orders. It adds three protections:
  1. TIMEOUT          every request has a hard time limit.
  2. LIMITED RETRY    transient failures are retried with exponential backoff, a few times at most.
                      A POST is only retried when it carries an Idempotency-Key (so a repeat cannot
                      create a second order). The SAME key is reused for every attempt of one call.
  3. CIRCUIT BREAKER  after repeated failures it stops calling the server for a recovery period,
                      then lets one trial call through to see if the server is back.

What counts as a failure (retried, and counted by the breaker):
  timeouts, connection errors, and HTTP 500/502/503/504.
What does NOT:
  4xx answers (bad input 400, declined payment 422, out of stock 409 ...) - retrying cannot fix them.
  429 Too Many Requests - the server is healthy but throttling us. It is reported separately with its
  Retry-After value; it is never retried inside the failure loop and never trips the breaker.

Run:
  python caller.py --scenario reliability     # success, retry, breaker open, recovery
  python caller.py --scenario ratelimit       # 429 + Retry-After, shown separately
"""
import argparse
import random
import secrets
import sys
import time
import uuid
from dataclasses import dataclass, field
from typing import Callable, Optional

import requests

# ---------------------------------------------------------------- settings
@dataclass(frozen=True)
class Settings:
    timeout_s: float = 2.0            # per-attempt limit for connect + read
    max_attempts: int = 3             # 1 first try + 2 retries
    backoff_base_s: float = 1.0       # delay before retry 1; doubles each retry (1s, 2s)
    backoff_cap_s: float = 4.0        # never wait longer than this
    backoff_jitter: float = 0.1       # +/-10% random spread so many callers do not retry in lockstep
    breaker_threshold: int = 3        # consecutive failed attempts that open the breaker
    breaker_recovery_s: float = 5.0   # how long the breaker stays open before one trial call

RETRYABLE_STATUS = {500, 502, 503, 504}


# ---------------------------------------------------------------- circuit breaker
class CircuitBreaker:
    CLOSED, OPEN, HALF_OPEN = "closed", "open", "half-open"

    def __init__(self, threshold, recovery_s, clock=time.monotonic):
        self.threshold, self.recovery_s, self.clock = threshold, recovery_s, clock
        self.state = self.CLOSED
        self.failures = 0
        self.opened_at = 0.0

    def allow(self):
        """May a request go out right now?"""
        if self.state == self.OPEN:
            if self.clock() - self.opened_at >= self.recovery_s:
                self.state = self.HALF_OPEN      # recovery period over: let ONE trial request through
                return True
            return False
        return True

    def retry_in(self):
        return max(0.0, self.recovery_s - (self.clock() - self.opened_at))

    def record_success(self):
        self.state, self.failures = self.CLOSED, 0

    def record_failure(self):
        self.failures += 1
        if self.state == self.HALF_OPEN or self.failures >= self.threshold:
            self.state, self.opened_at = self.OPEN, self.clock()


# ---------------------------------------------------------------- result type
@dataclass
class Result:
    outcome: str                      # success | client_error | rate_limited | failed | circuit_open
    status: Optional[int] = None
    body: Optional[dict] = None
    attempts: int = 0
    replayed: bool = False
    retry_after_s: Optional[int] = None
    error: str = ""
    trace_id: str = ""
    idempotency_key: str = ""

    @property
    def ok(self):
        return self.outcome == "success"


# ---------------------------------------------------------------- the caller
class InoraClient:
    def __init__(self, base_url="http://127.0.0.1:5000", settings=Settings(), session=None,
                 sleep=time.sleep, clock=time.monotonic, rng=random.random, log=print):
        self.base_url = base_url.rstrip("/")
        self.s = settings
        self.session = session or requests.Session()
        self.sleep, self.rng, self.log = sleep, rng, log
        self.breaker = CircuitBreaker(settings.breaker_threshold, settings.breaker_recovery_s, clock)

    # ---- public operations
    def place_order(self, product_id, quantity, payment_token="tok_visa", idempotency_key=None,
                    attempt_headers: Optional[Callable[[int], dict]] = None, label="order"):
        key = idempotency_key or f"caller-{uuid.uuid4().hex[:12]}"
        return self.request("POST", "/orders", json={"product_id": product_id, "quantity": quantity,
                                                     "payment_token": payment_token},
                            idempotency_key=key, attempt_headers=attempt_headers, label=label)

    # ---- core
    def request(self, method, path, json=None, idempotency_key=None, attempt_headers=None, label="call"):
        headers = {"traceparent": f"00-{secrets.token_hex(16)}-{secrets.token_hex(8)}-01"}  # one trace per logical call
        trace_id = headers["traceparent"].split("-")[1]
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        safe_to_retry = method in ("GET", "HEAD") or bool(idempotency_key)
        max_attempts = self.s.max_attempts if safe_to_retry else 1
        result = Result("failed", trace_id=trace_id, idempotency_key=idempotency_key or "")

        for attempt in range(1, max_attempts + 1):
            if not self.breaker.allow():
                if attempt > 1:        # we did try and fail; the breaker opened while retrying
                    self.log(f"[{label}]   retry skipped: circuit breaker opened")
                    result.outcome = "failed"
                    self.log(f"[{label}] GAVE UP after {result.attempts} attempt(s): {result.error}")
                    return result
                result.outcome, result.attempts = "circuit_open", 0
                result.error = f"breaker open, next trial in {self.breaker.retry_in():.1f}s"
                self.log(f"[{label}] BLOCKED  circuit breaker is OPEN - no request sent "
                         f"(trial allowed in {self.breaker.retry_in():.1f}s)")
                return result

            h = dict(headers)
            if attempt_headers:
                h.update(attempt_headers(attempt))
            trial = " (half-open trial)" if self.breaker.state == CircuitBreaker.HALF_OPEN else ""
            started = time.monotonic()
            failure = None
            try:
                resp = self.session.request(method, self.base_url + path, json=json, headers=h,
                                            timeout=self.s.timeout_s)
            except requests.exceptions.Timeout:
                failure = f"timeout after {self.s.timeout_s:.1f}s"
            except requests.exceptions.ConnectionError as e:
                failure = f"connection error ({type(e).__name__})"
            else:
                result.status, result.attempts = resp.status_code, attempt
                if resp.status_code in RETRYABLE_STATUS:
                    failure = f"HTTP {resp.status_code}"
                else:
                    self.breaker.record_success()          # the server answered properly
                    result.body = self._json(resp)
                    result.replayed = resp.headers.get("Idempotent-Replayed") == "true"
                    elapsed = time.monotonic() - started
                    if resp.status_code == 429:
                        result.outcome = "rate_limited"
                        result.retry_after_s = int(resp.headers.get("Retry-After", "1"))
                        self.log(f"[{label}] attempt {attempt}/{max_attempts} -> HTTP 429 rate limited, "
                                 f"Retry-After={result.retry_after_s}s (not a failure, not retried)")
                    elif 200 <= resp.status_code < 300:
                        result.outcome = "success"
                        extra = " (replay of an earlier identical request)" if result.replayed else ""
                        self.log(f"[{label}] attempt {attempt}/{max_attempts} -> HTTP {resp.status_code} OK "
                                 f"in {elapsed:.2f}s{extra}{trial}  breaker={self.breaker.state}")
                    else:
                        result.outcome = "client_error"
                        code = (result.body or {}).get("code", "?")
                        self.log(f"[{label}] attempt {attempt}/{max_attempts} -> HTTP {resp.status_code} "
                                 f"{code} (client error, not retried)")
                    return result

            # ---- failure path
            result.attempts = attempt
            result.error = failure
            self.breaker.record_failure()
            self.log(f"[{label}] attempt {attempt}/{max_attempts} -> FAILED: {failure}{trial}  "
                     f"breaker={self.breaker.state} (consecutive failures={self.breaker.failures})")
            if attempt == max_attempts:
                break
            if not safe_to_retry:
                break
            delay = min(self.s.backoff_cap_s, self.s.backoff_base_s * 2 ** (attempt - 1))
            delay *= 1 + self.s.backoff_jitter * (2 * self.rng() - 1)
            self.log(f"[{label}]   retrying in {delay:.2f}s (same Idempotency-Key)")
            self.sleep(delay)

        result.outcome = "failed"
        self.log(f"[{label}] GAVE UP after {result.attempts} attempt(s): {result.error}")
        return result

    @staticmethod
    def _json(resp):
        try:
            return resp.json()
        except ValueError:
            return None


# ---------------------------------------------------------------- demo scenarios
def scenario_reliability(c: InoraClient):
    """One run, 8 calls: success, retry after timeout, retry after 503, breaker opens, blocked, recovery."""
    S = c.s
    print(f"settings: timeout={S.timeout_s}s  max_attempts={S.max_attempts}  backoff={S.backoff_base_s}s doubling "
          f"(cap {S.backoff_cap_s}s, +/-{int(S.backoff_jitter*100)}% jitter)  "
          f"breaker: open after {S.breaker_threshold} consecutive failures, recovery {S.breaker_recovery_s}s\n")
    P = "clover-pendant"
    results = {}
    print("-- 1. normal call")
    results[1] = c.place_order(P, 1, label="call 1")
    print("\n-- 2. server too slow: first attempt times out, retry (same key) succeeds")
    results[2] = c.place_order(P, 1, label="call 2",
                               attempt_headers=lambda a: {"X-Simulate-Fault": "slow"} if a == 1 else {})
    print("\n-- 3. server error: first attempt gets 503, retry succeeds")
    results[3] = c.place_order(P, 1, label="call 3",
                               attempt_headers=lambda a: {"X-Simulate-Fault": "503"} if a == 1 else {})
    print("\n-- 4. server down: every attempt gets 503, breaker opens")
    results[4] = c.place_order(P, 1, label="call 4", attempt_headers=lambda a: {"X-Simulate-Fault": "503"})
    print("\n-- 5/6. breaker is open: calls are blocked without touching the server")
    results[5] = c.place_order(P, 1, label="call 5")
    results[6] = c.place_order(P, 1, label="call 6")
    wait = c.breaker.retry_in() + 0.2
    print(f"\n-- waiting {wait:.1f}s for the recovery period ...")
    time.sleep(wait)
    print("\n-- 7. recovery: one trial call goes through (server is healthy again)")
    results[7] = c.place_order(P, 1, label="call 7")
    print("\n-- 8. breaker closed again: normal call")
    results[8] = c.place_order(P, 1, label="call 8")

    print("\nsummary")
    for n, r in results.items():
        print(f"  call {n}: {r.outcome:<12} attempts={r.attempts} status={r.status} "
              f"order={((r.body or {}).get('id') or '-')} trace_id={r.trace_id}")
    expected = {1: "success", 2: "success", 3: "success", 4: "failed", 5: "circuit_open",
                6: "circuit_open", 7: "success", 8: "success"}
    bad = {n: (results[n].outcome, e) for n, e in expected.items() if results[n].outcome != e}
    if bad:
        print(f"\nUNEXPECTED outcomes (got, expected): {bad}")
        print("Is the API running with a high rate limit? Start it with:  $env:RATE_LIMIT_MAX=100; docker compose up -d api")
        return 1
    print("\nall 8 calls behaved as designed")
    return 0


def scenario_ratelimit(c: InoraClient):
    """Separate from the failure demo: show accepted requests, 429 with Retry-After, then allowed again."""
    print("-- sending 7 orders in a row (server limit is 5 per 10s per client address)")
    out = [c.place_order("minimalist-ring", 1, label=f"order {i}") for i in range(1, 8)]
    accepted = [r for r in out if r.outcome == "success"]
    limited = [r for r in out if r.outcome == "rate_limited"]
    print(f"\naccepted={len(accepted)}  rate_limited={len(limited)}")
    if limited:
        wait = max(r.retry_after_s for r in limited)
        print(f"-- waiting the advertised Retry-After ({wait}s) ...")
        time.sleep(wait + 0.2)
        print("-- sending again")
        again = c.place_order("minimalist-ring", 1, label="order 8")
        print(f"\nafter waiting: {again.outcome} (HTTP {again.status})")
        return 0 if again.ok else 1
    print("no 429 seen - is the API running with a higher limit? Restart with the default: docker compose up -d api")
    return 1


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base-url", default="http://127.0.0.1:5000")   # 127.0.0.1, not localhost: avoids slow IPv6 fallback on Windows
    ap.add_argument("--scenario", choices=["reliability", "ratelimit"], required=True)
    args = ap.parse_args()
    if args.scenario == "ratelimit":
        # Keep this demo free of timeouts/retries so it shows ONLY rate limiting: a timed-out request can
        # still have been counted by the server, which would blur accepted vs rejected.
        client = InoraClient(args.base_url, Settings(timeout_s=10.0, max_attempts=1))
    else:
        client = InoraClient(args.base_url)
    return {"reliability": scenario_reliability, "ratelimit": scenario_ratelimit}[args.scenario](client)


if __name__ == "__main__":
    sys.exit(main())

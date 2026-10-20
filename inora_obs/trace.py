"""W3C trace context (`traceparent`). Carried in an HTTP header or in gRPC metadata, written into logs as
trace_id. It is NEVER used as a Prometheus label (unbounded values)."""
import secrets


def parse_traceparent(value):
    """Return (trace_id, span_id) from a well-formed traceparent, else None."""
    parts = (value or "").strip().split("-")
    if len(parts) == 4 and len(parts[1]) == 32 and len(parts[2]) == 16:
        try:
            int(parts[1], 16), int(parts[2], 16)
        except ValueError:
            return None
        if int(parts[1], 16) and int(parts[2], 16):
            return parts[1], parts[2]
    return None


def make_traceparent(trace_id=None):
    return f"00-{trace_id or secrets.token_hex(16)}-{secrets.token_hex(8)}-01"

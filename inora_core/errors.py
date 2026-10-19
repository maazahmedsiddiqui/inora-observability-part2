"""Domain errors. Each carries a stable machine-readable `code`.
The HTTP layer maps codes to HTTP statuses; the gRPC layer maps them to gRPC status codes.
Neither layer re-implements the rules - they only translate the outcome."""


class DomainError(Exception):
    code = "domain_error"

    def __init__(self, message, field=None):
        super().__init__(message)
        self.message = message
        self.field = field


class ValidationFailed(DomainError):
    code = "validation_error"


class NotFound(DomainError):
    code = "not_found"


class OutOfStock(DomainError):
    code = "out_of_stock"


class PaymentDeclined(DomainError):
    code = "payment_declined"


class IdempotencyConflict(DomainError):
    code = "idempotency_conflict"

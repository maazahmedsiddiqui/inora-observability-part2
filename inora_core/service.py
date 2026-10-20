"""Inora business rules on top of one SQLite file (the single source of truth).

Both the Flask API and the gRPC service import this module and call Store directly.
SQLite is opened with an explicit BEGIN IMMEDIATE for writes so two processes
(or two threads) sharing the file cannot both create an order for the same
Idempotency-Key or oversell stock.
"""
import hashlib
import json
import os
import sqlite3
import uuid
from datetime import datetime, timezone

from .errors import (
    IdempotencyConflict, NotFound, OutOfStock, PaymentDeclined, ValidationFailed,
)

# Business rules (documented in the README / report)
MAX_QUANTITY = 10
DECLINED_TOKEN = "tok_declined"      # deterministic payment failure for demos and tests
TOKEN_PREFIX = "tok_"
MAX_KEY_LENGTH = 128

# Stock for the three regular products. Tests use the default (50); the Docker demo sets a
# much larger INORA_SEED_STOCK so traffic.py does not sell the shop out after a few minutes.
DEFAULT_STOCK = int(os.environ.get("INORA_SEED_STOCK", "50"))

SEED_PRODUCTS = [
    # id, name, price in PKR, stock
    ("clover-pendant", "Clover pendant", 4500, DEFAULT_STOCK),
    ("minimalist-ring", "Minimalist ring", 3200, DEFAULT_STOCK),
    ("pearl-bracelet", "Pearl bracelet", 5800, DEFAULT_STOCK),
    ("limited-necklace", "Limited edition necklace", 12000, 1),   # scarce item for out-of-stock demos
]

SCHEMA = """
CREATE TABLE IF NOT EXISTS products (
    id TEXT PRIMARY KEY, name TEXT NOT NULL,
    price_pkr INTEGER NOT NULL, stock INTEGER NOT NULL CHECK (stock >= 0)
);
CREATE TABLE IF NOT EXISTS orders (
    id TEXT PRIMARY KEY, product_id TEXT NOT NULL REFERENCES products(id),
    quantity INTEGER NOT NULL, total_pkr INTEGER NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('placed','cancelled')),
    created_at TEXT NOT NULL, cancelled_at TEXT
);
CREATE TABLE IF NOT EXISTS idempotency_keys (
    key TEXT PRIMARY KEY, request_hash TEXT NOT NULL,
    order_id TEXT NOT NULL REFERENCES orders(id), created_at TEXT NOT NULL
);
"""


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Store:
    def __init__(self, path=None):
        self.path = path or os.environ.get("INORA_DB", "inora.db")
        d = os.path.dirname(self.path)
        if d:
            os.makedirs(d, exist_ok=True)
        with self._conn() as c:
            c.executescript(SCHEMA)
            if c.execute("SELECT COUNT(*) FROM products").fetchone()[0] == 0:
                c.executemany("INSERT OR IGNORE INTO products VALUES (?,?,?,?)", SEED_PRODUCTS)

    def _conn(self):
        c = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA foreign_keys = ON")
        return _Closing(c)

    def reset(self):
        """Put the data back to the seed state (used by demos and tests)."""
        with self._conn() as c:
            c.execute("DELETE FROM idempotency_keys")
            c.execute("DELETE FROM orders")
            c.execute("DELETE FROM products")
            c.executemany("INSERT INTO products VALUES (?,?,?,?)", SEED_PRODUCTS)

    # ---------- reads ----------
    def list_products(self):
        with self._conn() as c:
            return [dict(r) for r in c.execute("SELECT * FROM products ORDER BY id")]

    def get_product(self, product_id):
        with self._conn() as c:
            row = c.execute("SELECT * FROM products WHERE id = ?", (product_id,)).fetchone()
        if row is None:
            raise NotFound("product not found", field="product_id")
        return dict(row)

    def get_order(self, order_id):
        with self._conn() as c:
            row = c.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
        if row is None:
            raise NotFound("order not found", field="order_id")
        return dict(row)

    # ---------- the business operation shared by HTTP and gRPC ----------
    def place_order(self, product_id, quantity, payment_token, idempotency_key=None):
        """Returns (order_dict, replayed). replayed=True means an earlier identical
        request already created this order, so nothing new happened."""
        product_id, quantity, payment_token, idempotency_key = self._validate_place(
            product_id, quantity, payment_token, idempotency_key)
        request_hash = hashlib.sha256(json.dumps(
            [product_id, quantity, payment_token], separators=(",", ":")).encode()).hexdigest()

        with self._conn() as c:
            c.execute("BEGIN IMMEDIATE")          # one writer at a time across all processes
            try:
                if idempotency_key:
                    prior = c.execute(
                        "SELECT request_hash, order_id FROM idempotency_keys WHERE key = ?",
                        (idempotency_key,)).fetchone()
                    if prior:
                        if prior["request_hash"] != request_hash:
                            raise IdempotencyConflict(
                                "this Idempotency-Key was already used with a different request",
                                field="idempotency_key")
                        order = c.execute("SELECT * FROM orders WHERE id = ?",
                                          (prior["order_id"],)).fetchone()
                        c.execute("COMMIT")
                        return dict(order), True

                product = c.execute("SELECT * FROM products WHERE id = ?", (product_id,)).fetchone()
                if product is None:
                    raise NotFound("product not found", field="product_id")
                if payment_token == DECLINED_TOKEN:
                    raise PaymentDeclined("payment was declined", field="payment_token")
                updated = c.execute(
                    "UPDATE products SET stock = stock - ? WHERE id = ? AND stock >= ?",
                    (quantity, product_id, quantity)).rowcount
                if updated == 0:
                    raise OutOfStock("not enough stock for this product", field="quantity")

                order_id = "ord_" + uuid.uuid4().hex[:12]
                now = _now()
                total = product["price_pkr"] * quantity
                c.execute("INSERT INTO orders VALUES (?,?,?,?,?,?,NULL)",
                          (order_id, product_id, quantity, total, "placed", now))
                if idempotency_key:
                    c.execute("INSERT INTO idempotency_keys VALUES (?,?,?,?)",
                              (idempotency_key, request_hash, order_id, now))
                c.execute("COMMIT")
                return self.get_order(order_id), False
            except BaseException:
                if c.in_transaction:
                    c.execute("ROLLBACK")
                raise

    def cancel_order(self, order_id):
        """Cancelling twice is safe: the second call changes nothing."""
        with self._conn() as c:
            c.execute("BEGIN IMMEDIATE")
            try:
                order = c.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
                if order is None:
                    raise NotFound("order not found", field="order_id")
                if order["status"] == "placed":
                    c.execute("UPDATE orders SET status='cancelled', cancelled_at=? WHERE id=?",
                              (_now(), order_id))
                    c.execute("UPDATE products SET stock = stock + ? WHERE id = ?",
                              (order["quantity"], order["product_id"]))
                c.execute("COMMIT")
            except BaseException:
                if c.in_transaction:
                    c.execute("ROLLBACK")
                raise
        return self.get_order(order_id)

    @staticmethod
    def _validate_place(product_id, quantity, payment_token, idempotency_key):
        if not isinstance(product_id, str) or not product_id.strip():
            raise ValidationFailed("product_id is required", field="product_id")
        if isinstance(quantity, bool) or not isinstance(quantity, int):
            raise ValidationFailed("quantity must be an integer", field="quantity")
        if not 1 <= quantity <= MAX_QUANTITY:
            raise ValidationFailed(f"quantity must be between 1 and {MAX_QUANTITY}", field="quantity")
        if not isinstance(payment_token, str) or not payment_token.startswith(TOKEN_PREFIX) \
                or len(payment_token) <= len(TOKEN_PREFIX):
            raise ValidationFailed("payment_token must look like tok_...", field="payment_token")
        if idempotency_key is not None:
            if not isinstance(idempotency_key, str) or not idempotency_key.strip() \
                    or len(idempotency_key) > MAX_KEY_LENGTH:
                raise ValidationFailed(
                    f"idempotency key must be 1-{MAX_KEY_LENGTH} characters", field="idempotency_key")
        return product_id.strip(), quantity, payment_token, idempotency_key


class _Closing:
    """sqlite3's own context manager does not close the connection; this one does."""
    def __init__(self, conn):
        self._c = conn

    def __enter__(self):
        return self._c

    def __exit__(self, *exc):
        self._c.close()
        return False

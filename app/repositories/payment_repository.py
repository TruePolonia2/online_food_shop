"""Payment repository for SQLite database persistence."""
from datetime import datetime, timezone
import sqlite3
from typing import Optional

from app.database.connection import get_db, transaction
from app.models.payment import Payment, PaymentStatus


def _row_to_payment(row: sqlite3.Row) -> Payment:
    """Convert an SQLite row into a Payment domain entity."""
    created_at_val = row["created_at"]
    if isinstance(created_at_val, str):
        created_at = datetime.fromisoformat(created_at_val)
    elif isinstance(created_at_val, datetime):
        created_at = created_at_val
    else:
        created_at = datetime.now(timezone.utc)

    updated_at_val = row["updated_at"] if "updated_at" in row.keys() else None
    if isinstance(updated_at_val, str):
        updated_at = datetime.fromisoformat(updated_at_val)
    elif isinstance(updated_at_val, datetime):
        updated_at = updated_at_val
    else:
        updated_at = None

    return Payment(
        id=row["id"],
        order_id=row["order_id"],
        amount=row["amount"],
        payment_method=row["payment_method"],
        status=PaymentStatus(row["status"]),
        currency=row["currency"],
        stripe_session_id=row["stripe_session_id"],
        stripe_payment_intent_id=row["stripe_payment_intent_id"],
        idempotency_key=row["idempotency_key"],
        created_at=created_at,
        updated_at=updated_at,
    )


class PaymentRepository:
    """Repository handling persistence and queries for payment attempts."""

    def __init__(self, conn: Optional[sqlite3.Connection] = None) -> None:
        """Initialize repository with optional database connection."""
        self._conn = conn

    @property
    def conn(self) -> sqlite3.Connection:
        """Get the active database connection."""
        if self._conn is not None:
            return self._conn
        return get_db()

    def create(self, payment: Payment) -> Payment:
        """Persist a new payment attempt.

        Args:
            payment: Transient Payment instance.

        Returns:
            The saved Payment instance with ID populated.
        """
        created_at_str = payment.created_at.strftime("%Y-%m-%dT%H:%M:%SZ")
        with transaction(self.conn):
            cursor = self.conn.execute(
                """
                INSERT INTO payments (
                    order_id, payment_method, status, amount, currency,
                    stripe_session_id, stripe_payment_intent_id, idempotency_key, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    payment.order_id,
                    payment.payment_method,
                    payment.status.value if isinstance(payment.status, PaymentStatus) else str(payment.status),
                    payment.amount,
                    payment.currency,
                    payment.stripe_session_id,
                    payment.stripe_payment_intent_id,
                    payment.idempotency_key,
                    created_at_str,
                ),
            )
            payment.id = cursor.lastrowid
        return payment

    def get_by_id(self, payment_id: int) -> Optional[Payment]:
        """Retrieve a payment record by primary key."""
        cursor = self.conn.execute(
            """
            SELECT id, order_id, payment_method, status, amount, currency,
                   stripe_session_id, stripe_payment_intent_id, idempotency_key,
                   created_at, updated_at
            FROM payments WHERE id = ?
            """,
            (payment_id,),
        )
        row = cursor.fetchone()
        return _row_to_payment(row) if row else None

    def get_by_order_id(self, order_id: int) -> list[Payment]:
        """Retrieve all payment attempts for a specific order."""
        cursor = self.conn.execute(
            """
            SELECT id, order_id, payment_method, status, amount, currency,
                   stripe_session_id, stripe_payment_intent_id, idempotency_key,
                   created_at, updated_at
            FROM payments WHERE order_id = ? ORDER BY id ASC
            """,
            (order_id,),
        )
        return [_row_to_payment(row) for row in cursor.fetchall()]

    def get_by_stripe_session_id(self, stripe_session_id: str) -> Optional[Payment]:
        """Retrieve a payment attempt by its unique Stripe Checkout Session ID."""
        if not stripe_session_id or not stripe_session_id.strip():
            return None
        cursor = self.conn.execute(
            """
            SELECT id, order_id, payment_method, status, amount, currency,
                   stripe_session_id, stripe_payment_intent_id, idempotency_key,
                   created_at, updated_at
            FROM payments WHERE stripe_session_id = ?
            """,
            (stripe_session_id.strip(),),
        )
        row = cursor.fetchone()
        return _row_to_payment(row) if row else None

    def get_by_idempotency_key(self, idempotency_key: str) -> Optional[Payment]:
        """Retrieve a payment record by idempotency key."""
        if not idempotency_key or not idempotency_key.strip():
            return None
        cursor = self.conn.execute(
            """
            SELECT id, order_id, payment_method, status, amount, currency,
                   stripe_session_id, stripe_payment_intent_id, idempotency_key,
                   created_at, updated_at
            FROM payments WHERE idempotency_key = ?
            """,
            (idempotency_key.strip(),),
        )
        row = cursor.fetchone()
        return _row_to_payment(row) if row else None

    def update_stripe_session_id(self, payment_id: int, stripe_session_id: str) -> None:
        """Associate a Stripe Checkout Session ID with a payment attempt."""
        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        with transaction(self.conn):
            self.conn.execute(
                """
                UPDATE payments
                SET stripe_session_id = ?, updated_at = ?
                WHERE id = ?
                """,
                (stripe_session_id.strip(), now_str, payment_id),
            )

    def update_idempotency_key(self, payment_id: int, new_idempotency_key: str) -> None:
        """Update the idempotency key for a payment attempt upon controlled conflict recovery."""
        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        with transaction(self.conn):
            self.conn.execute(
                """
                UPDATE payments
                SET idempotency_key = ?, updated_at = ?
                WHERE id = ?
                """,
                (new_idempotency_key.strip(), now_str, payment_id),
            )

    def update_status(
        self,
        payment_id: int,
        status: PaymentStatus | str,
        stripe_payment_intent_id: Optional[str] = None,
    ) -> None:
        """Update payment status and optionally record the resulting payment intent ID."""
        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        status_val = status.value if isinstance(status, PaymentStatus) else str(status)

        with transaction(self.conn):
            if stripe_payment_intent_id is not None:
                self.conn.execute(
                    """
                    UPDATE payments
                    SET status = ?, stripe_payment_intent_id = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (status_val, stripe_payment_intent_id.strip(), now_str, payment_id),
                )
            else:
                self.conn.execute(
                    """
                    UPDATE payments
                    SET status = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (status_val, now_str, payment_id),
                )

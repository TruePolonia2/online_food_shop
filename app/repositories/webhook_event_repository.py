"""Repository for Stripe webhook event persistence and idempotency tracking."""
from datetime import datetime, timezone
import sqlite3
from typing import Optional

from app.database.connection import get_db, transaction
from app.models.webhook_event import StripeWebhookEvent, WebhookEventStatus


DEFAULT_STALE_PROCESSING_SECONDS = 120


def _row_to_event(row: sqlite3.Row) -> StripeWebhookEvent:
    """Convert an SQLite row into a StripeWebhookEvent domain entity."""
    created_at_val = row["created_at"]
    if isinstance(created_at_val, str):
        created_at = datetime.fromisoformat(created_at_val)
    elif isinstance(created_at_val, datetime):
        created_at = created_at_val
    else:
        created_at = datetime.now(timezone.utc)

    processed_at_val = row["processed_at"] if "processed_at" in row.keys() else None
    if isinstance(processed_at_val, str):
        processed_at = datetime.fromisoformat(processed_at_val)
    elif isinstance(processed_at_val, datetime):
        processed_at = processed_at_val
    else:
        processed_at = None

    return StripeWebhookEvent(
        id=row["id"],
        event_id=row["event_id"],
        event_type=row["event_type"],
        livemode=bool(row["livemode"]),
        status=WebhookEventStatus(row["status"]),
        order_id=row["order_id"],
        payment_id=row["payment_id"],
        payload_summary=row["payload_summary"] if "payload_summary" in row.keys() else None,
        error_message=row["error_message"] if "error_message" in row.keys() else None,
        created_at=created_at,
        processed_at=processed_at,
    )


class WebhookEventRepository:
    """Repository handling persistence, queries, and idempotency states for Stripe webhook events."""

    def __init__(self, conn: Optional[sqlite3.Connection] = None) -> None:
        """Initialize repository with optional database connection."""
        self._conn = conn

    @property
    def conn(self) -> sqlite3.Connection:
        """Get the active database connection."""
        if self._conn is not None:
            return self._conn
        return get_db()

    def create(self, event: StripeWebhookEvent) -> StripeWebhookEvent:
        """Persist a new webhook event record.

        Args:
            event: Transient StripeWebhookEvent instance.

        Returns:
            The saved StripeWebhookEvent instance with ID populated.
        """
        created_at_str = event.created_at.strftime("%Y-%m-%dT%H:%M:%SZ")
        with transaction(self.conn):
            cursor = self.conn.execute(
                """
                INSERT INTO stripe_webhook_events (
                    event_id, event_type, livemode, status,
                    order_id, payment_id, payload_summary, error_message, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event.event_id.strip(),
                    event.event_type.strip(),
                    1 if event.livemode else 0,
                    event.status.value if isinstance(event.status, WebhookEventStatus) else str(event.status),
                    event.order_id,
                    event.payment_id,
                    event.payload_summary,
                    event.error_message,
                    created_at_str,
                ),
            )
            event.id = cursor.lastrowid
        return event

    def get_by_event_id(self, event_id: str) -> Optional[StripeWebhookEvent]:
        """Retrieve a webhook event record by its unique Stripe event ID."""
        if not event_id or not event_id.strip():
            return None
        cursor = self.conn.execute(
            """
            SELECT id, event_id, event_type, livemode, status,
                   order_id, payment_id, payload_summary, error_message,
                   created_at, processed_at
            FROM stripe_webhook_events WHERE event_id = ?
            """,
            (event_id.strip(),),
        )
        row = cursor.fetchone()
        return _row_to_event(row) if row else None

    def record_processing(
        self,
        event_id: str,
        event_type: str,
        livemode: bool = False,
        order_id: Optional[int] = None,
        payment_id: Optional[int] = None,
        payload_summary: Optional[str] = None,
        stale_timeout_seconds: int = DEFAULT_STALE_PROCESSING_SECONDS,
    ) -> tuple[bool, StripeWebhookEvent]:
        """Atomically record an incoming event in 'processing' status or recover/retry if applicable.

        Args:
            event_id: Stripe event ID.
            event_type: Stripe event type name.
            livemode: Whether event is livemode.
            order_id: Optional matched order ID.
            payment_id: Optional matched payment ID.
            payload_summary: Sanitized summary string.
            stale_timeout_seconds: Seconds after which a PROCESSING event is considered stale/crashed.

        Returns:
            Tuple of (acquired, event):
                - acquired=True: Lease granted to execute processing (new event, retry from FAILED, or recovered stale lease).
                - acquired=False: Processing blocked (already PROCESSED, IGNORED, or active in-flight PROCESSING).
        """
        clean_id = event_id.strip()
        clean_type = event_type.strip()
        now = datetime.now(timezone.utc)
        now_str = now.strftime("%Y-%m-%dT%H:%M:%SZ")

        with transaction(self.conn):
            existing = self.get_by_event_id(clean_id)
            if existing is not None:
                if existing.status == WebhookEventStatus.PROCESSED:
                    return False, existing
                if existing.status == WebhookEventStatus.IGNORED:
                    return False, existing
                if existing.status == WebhookEventStatus.FAILED:
                    # Retry of previously failed processing: re-acquire lease
                    self.conn.execute(
                        """
                        UPDATE stripe_webhook_events
                        SET status = ?, error_message = NULL, created_at = ?,
                            order_id = COALESCE(?, order_id), payment_id = COALESCE(?, payment_id)
                        WHERE event_id = ?
                        """,
                        (WebhookEventStatus.PROCESSING.value, now_str, order_id, payment_id, clean_id),
                    )
                    existing.status = WebhookEventStatus.PROCESSING
                    existing.created_at = now
                    existing.error_message = None
                    if order_id is not None:
                        existing.order_id = order_id
                    if payment_id is not None:
                        existing.payment_id = payment_id
                    return True, existing
                if existing.status == WebhookEventStatus.PROCESSING:
                    # Stale PROCESSING check: crash / timeout recovery
                    age = (now - existing.created_at).total_seconds()
                    if age > stale_timeout_seconds:
                        self.conn.execute(
                            """
                            UPDATE stripe_webhook_events
                            SET status = ?, error_message = NULL, created_at = ?,
                                order_id = COALESCE(?, order_id), payment_id = COALESCE(?, payment_id)
                            WHERE event_id = ?
                            """,
                            (WebhookEventStatus.PROCESSING.value, now_str, order_id, payment_id, clean_id),
                        )
                        existing.status = WebhookEventStatus.PROCESSING
                        existing.created_at = now
                        existing.error_message = None
                        if order_id is not None:
                            existing.order_id = order_id
                        if payment_id is not None:
                            existing.payment_id = payment_id
                        return True, existing
                    else:
                        # Fresh in-flight concurrent execution: do not acquire
                        return False, existing

            cursor = self.conn.execute(
                """
                INSERT INTO stripe_webhook_events (
                    event_id, event_type, livemode, status,
                    order_id, payment_id, payload_summary, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    clean_id,
                    clean_type,
                    1 if livemode else 0,
                    WebhookEventStatus.PROCESSING.value,
                    order_id,
                    payment_id,
                    payload_summary,
                    now_str,
                ),
            )
            created_event = StripeWebhookEvent(
                id=cursor.lastrowid,
                event_id=clean_id,
                event_type=clean_type,
                livemode=livemode,
                status=WebhookEventStatus.PROCESSING,
                order_id=order_id,
                payment_id=payment_id,
                payload_summary=payload_summary,
                created_at=now,
            )
            return True, created_event

    def mark_processed(
        self,
        event_id: str,
        order_id: Optional[int] = None,
        payment_id: Optional[int] = None,
    ) -> None:
        """Mark an event as successfully processed.

        Args:
            event_id: The Stripe event ID.
            order_id: Optional associated order ID.
            payment_id: Optional associated payment ID.
        """
        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        with transaction(self.conn):
            self.conn.execute(
                """
                UPDATE stripe_webhook_events
                SET status = ?, order_id = COALESCE(?, order_id), payment_id = COALESCE(?, payment_id),
                    processed_at = ?, error_message = NULL
                WHERE event_id = ?
                """,
                (WebhookEventStatus.PROCESSED.value, order_id, payment_id, now_str, event_id.strip()),
            )

    def mark_failed(self, event_id: str, error_message: str) -> None:
        """Mark an event processing attempt as failed.

        Args:
            event_id: The Stripe event ID.
            error_message: Summary of the error encountered.
        """
        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        with transaction(self.conn):
            self.conn.execute(
                """
                UPDATE stripe_webhook_events
                SET status = ?, error_message = ?, processed_at = ?
                WHERE event_id = ?
                """,
                (WebhookEventStatus.FAILED.value, error_message[:1000] if error_message else None, now_str, event_id.strip()),
            )

    def mark_ignored(self, event_id: str, reason: Optional[str] = None) -> None:
        """Mark an event as ignored (e.g. unhandled event type).

        Args:
            event_id: The Stripe event ID.
            reason: Optional reason string.
        """
        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        with transaction(self.conn):
            self.conn.execute(
                """
                UPDATE stripe_webhook_events
                SET status = ?, error_message = ?, processed_at = ?
                WHERE event_id = ?
                """,
                (WebhookEventStatus.IGNORED.value, reason, now_str, event_id.strip()),
            )

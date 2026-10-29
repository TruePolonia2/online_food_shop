"""Account link intent repository for SQLite persistence."""
from datetime import datetime, timezone
import sqlite3
from typing import Optional

from app.database.connection import get_db, transaction
from app.models.account_link_intent import AccountLinkIntent


def _row_to_intent(row: sqlite3.Row) -> AccountLinkIntent:
    """Convert an SQLite row into an AccountLinkIntent domain model."""
    created_at_val = row["created_at"]
    if isinstance(created_at_val, str):
        created_at = datetime.fromisoformat(created_at_val)
    elif isinstance(created_at_val, datetime):
        created_at = created_at_val
    else:
        created_at = datetime.now(timezone.utc)

    expires_at_val = row["expires_at"]
    if isinstance(expires_at_val, str):
        expires_at = datetime.fromisoformat(expires_at_val)
    elif isinstance(expires_at_val, datetime):
        expires_at = expires_at_val
    else:
        raise ValueError("Invalid expires_at in intent row.")

    consumed_at_val = row["consumed_at"]
    if consumed_at_val is not None:
        if isinstance(consumed_at_val, str):
            consumed_at = datetime.fromisoformat(consumed_at_val)
        elif isinstance(consumed_at_val, datetime):
            consumed_at = consumed_at_val
        else:
            consumed_at = None
    else:
        consumed_at = None

    cancelled_at_val = row["cancelled_at"]
    if cancelled_at_val is not None:
        if isinstance(cancelled_at_val, str):
            cancelled_at = datetime.fromisoformat(cancelled_at_val)
        elif isinstance(cancelled_at_val, datetime):
            cancelled_at = cancelled_at_val
        else:
            cancelled_at = None
    else:
        cancelled_at = None

    action = row["action"] if "action" in row.keys() else "link"

    return AccountLinkIntent(
        id=row["id"],
        intent_id=row["intent_id"],
        user_id=row["user_id"],
        provider=row["provider"],
        provider_user_id=row["provider_user_id"],
        email=row["email"],
        action=action,
        next_url=row["next_url"] if "next_url" in row.keys() else None,
        created_at=created_at,
        expires_at=expires_at,
        status=row["status"],
        consumed_at=consumed_at,
        cancelled_at=cancelled_at,
    )


class AccountLinkIntentRepository:
    """Repository managing persistence for AccountLinkIntent entities."""

    def __init__(self, conn: Optional[sqlite3.Connection] = None) -> None:
        """Initialize AccountLinkIntentRepository with an optional database connection.

        Args:
            conn: Optional database connection. If omitted, uses request-scoped get_db().
        """
        self._conn = conn

    @property
    def conn(self) -> sqlite3.Connection:
        """Get the active database connection."""
        if self._conn is not None:
            return self._conn
        return get_db()

    def create(self, intent: AccountLinkIntent) -> AccountLinkIntent:
        """Insert a new account link intent.

        Args:
            intent: The AccountLinkIntent to persist.

        Returns:
            The persisted AccountLinkIntent with its generated primary key ID.
        """
        expires_at_str = intent.expires_at.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        created_at_str = intent.created_at.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        consumed_at_str = (
            intent.consumed_at.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            if intent.consumed_at
            else None
        )
        cancelled_at_str = (
            intent.cancelled_at.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            if intent.cancelled_at
            else None
        )

        with transaction(self.conn):
            cursor = self.conn.execute(
                """
                INSERT INTO account_link_intents (
                    intent_id, user_id, provider, provider_user_id, email,
                    action, next_url, created_at, expires_at, status, consumed_at, cancelled_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    intent.intent_id,
                    intent.user_id,
                    intent.provider,
                    intent.provider_user_id,
                    intent.email,
                    intent.action,
                    intent.next_url,
                    created_at_str,
                    expires_at_str,
                    intent.status,
                    consumed_at_str,
                    cancelled_at_str,
                ),
            )
            intent.id = cursor.lastrowid
            return intent

    def get_by_intent_id(self, intent_id: str) -> Optional[AccountLinkIntent]:
        """Retrieve an account link intent by its unique token.

        Args:
            intent_id: The unique intent token string.

        Returns:
            AccountLinkIntent if found, None otherwise.
        """
        if not intent_id or not isinstance(intent_id, str):
            return None

        cursor = self.conn.execute(
            """
            SELECT id, intent_id, user_id, provider, provider_user_id, email,
                   action, next_url, created_at, expires_at, status, consumed_at, cancelled_at
            FROM account_link_intents
            WHERE intent_id = ?
            """,
            (intent_id.strip(),),
        )
        row = cursor.fetchone()
        return _row_to_intent(row) if row else None

    def mark_as_consumed(
        self,
        intent_id: str,
        expected_action: Optional[str] = None,
        consumed_at: Optional[datetime] = None,
    ) -> bool:
        """Atomically mark an intent as consumed.

        Enforces atomic single-use: only succeeds if status is 'pending' and not expired.
        Optionally validates expected_action to keep deletion and linking actions isolated.

        Args:
            intent_id: The unique token of the intent.
            expected_action: Optional required action name (e.g. 'link' or 'delete_account').
            consumed_at: Optional timestamp of consumption. Defaults to now (UTC).

        Returns:
            True if successfully consumed, False if already consumed/cancelled/expired or not found.
        """
        if not intent_id or not isinstance(intent_id, str):
            return False

        dt = consumed_at if consumed_at is not None else datetime.now(timezone.utc)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        else:
            dt = dt.astimezone(timezone.utc)
        consumed_at_str = dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        with transaction(self.conn):
            if expected_action is not None:
                cursor = self.conn.execute(
                    """
                    UPDATE account_link_intents
                    SET status = 'consumed', consumed_at = ?
                    WHERE intent_id = ? AND status = 'pending' AND action = ? AND expires_at > ?
                    """,
                    (consumed_at_str, intent_id.strip(), expected_action.strip(), now_str),
                )
            else:
                cursor = self.conn.execute(
                    """
                    UPDATE account_link_intents
                    SET status = 'consumed', consumed_at = ?
                    WHERE intent_id = ? AND status = 'pending' AND expires_at > ?
                    """,
                    (consumed_at_str, intent_id.strip(), now_str),
                )
            return cursor.rowcount > 0

    def mark_as_cancelled(
        self, intent_id: str, cancelled_at: Optional[datetime] = None
    ) -> bool:
        """Atomically mark an intent as cancelled.

        Enforces cancellation: only updates if status is 'pending'.

        Args:
            intent_id: The unique token of the intent.
            cancelled_at: Optional timestamp of cancellation. Defaults to now (UTC).

        Returns:
            True if successfully cancelled, False if already consumed/cancelled or not found.
        """
        if not intent_id or not isinstance(intent_id, str):
            return False

        dt = cancelled_at if cancelled_at is not None else datetime.now(timezone.utc)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        else:
            dt = dt.astimezone(timezone.utc)
        cancelled_at_str = dt.strftime("%Y-%m-%dT%H:%M:%SZ")

        with transaction(self.conn):
            cursor = self.conn.execute(
                """
                UPDATE account_link_intents
                SET status = 'cancelled', cancelled_at = ?
                WHERE intent_id = ? AND status = 'pending'
                """,
                (cancelled_at_str, intent_id.strip()),
            )
            return cursor.rowcount > 0

    def invalidate_pending_for_user(
        self,
        user_id: int,
        provider: str = "google",
        action: Optional[str] = None,
        invalidation_time: Optional[datetime] = None,
    ) -> int:
        """Invalidate all pending unused intents for a user and provider.

        Optionally scoped to a specific action to isolate linking from deletion.

        Args:
            user_id: The target user ID.
            provider: Provider name (defaults to 'google').
            action: Optional action filter ('link' or 'delete_account').
            invalidation_time: Optional cancellation timestamp. Defaults to now (UTC).

        Returns:
            The number of intents invalidated.
        """
        if isinstance(user_id, bool) or not isinstance(user_id, int) or user_id <= 0:
            return 0

        dt = invalidation_time if invalidation_time is not None else datetime.now(timezone.utc)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        else:
            dt = dt.astimezone(timezone.utc)
        cancelled_at_str = dt.strftime("%Y-%m-%dT%H:%M:%SZ")

        with transaction(self.conn):
            if action is not None:
                cursor = self.conn.execute(
                    """
                    UPDATE account_link_intents
                    SET status = 'cancelled', cancelled_at = ?
                    WHERE user_id = ? AND provider = ? AND action = ? AND status = 'pending'
                    """,
                    (cancelled_at_str, user_id, provider.strip().lower(), action.strip()),
                )
            else:
                cursor = self.conn.execute(
                    """
                    UPDATE account_link_intents
                    SET status = 'cancelled', cancelled_at = ?
                    WHERE user_id = ? AND provider = ? AND status = 'pending'
                    """,
                    (cancelled_at_str, user_id, provider.strip().lower()),
                )
            return cursor.rowcount

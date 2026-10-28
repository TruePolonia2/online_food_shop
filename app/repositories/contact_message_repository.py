"""Contact message repository for SQLite database access."""
from datetime import datetime, timezone
from typing import Any, Optional, Union
import sqlite3

from app.database.connection import get_db, transaction
from app.models.contact_message import ContactMessage, ContactMessageStatus


def _row_to_contact_message(row: sqlite3.Row) -> ContactMessage:
    """Convert an SQLite row into a ContactMessage domain model."""
    created_at_val = row["created_at"]
    if isinstance(created_at_val, str):
        try:
            created_at = datetime.fromisoformat(created_at_val)
        except ValueError:
            created_at = datetime.now(timezone.utc)
    elif isinstance(created_at_val, datetime):
        created_at = created_at_val
    else:
        created_at = datetime.now(timezone.utc)

    updated_at_val = row["updated_at"]
    updated_at: Optional[datetime] = None
    if isinstance(updated_at_val, str) and updated_at_val.strip():
        try:
            updated_at = datetime.fromisoformat(updated_at_val)
        except ValueError:
            updated_at = None
    elif isinstance(updated_at_val, datetime):
        updated_at = updated_at_val

    return ContactMessage(
        id=row["id"],
        name=row["name"],
        email=row["email"],
        subject=row["subject"],
        message=row["message"],
        status=ContactMessageStatus(row["status"]),
        created_at=created_at,
        updated_at=updated_at,
    )


class ContactMessageRepository:
    """Repository managing persistence and queries for ContactMessage entities."""

    def __init__(self, conn: Optional[sqlite3.Connection] = None) -> None:
        """Initialize the repository.

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

    def create(
        self,
        contact_message: Optional[ContactMessage] = None,
        *,
        name: Optional[str] = None,
        email: Optional[str] = None,
        subject: Optional[str] = None,
        message: Optional[str] = None,
        status: Union[ContactMessageStatus, str] = ContactMessageStatus.NEW,
    ) -> ContactMessage:
        """Persist a new contact message in the database.

        Can be called with either a ContactMessage domain model instance or keyword arguments.

        Args:
            contact_message: Optional ContactMessage domain entity to insert.
            name: Sender name (if domain entity omitted).
            email: Sender email (if domain entity omitted).
            subject: Subject text (if domain entity omitted).
            message: Message text body (if domain entity omitted).
            status: Initial status (default: NEW).

        Returns:
            The persisted ContactMessage with its assigned primary key ID.
        """
        if contact_message is None:
            contact_message = ContactMessage(
                name=name or "",
                email=email or "",
                subject=subject or "",
                message=message or "",
                status=status,
            )

        created_at_str = (
            contact_message.created_at.strftime("%Y-%m-%dT%H:%M:%SZ")
            if isinstance(contact_message.created_at, datetime)
            else datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        )
        updated_at_str = (
            contact_message.updated_at.strftime("%Y-%m-%dT%H:%M:%SZ")
            if isinstance(contact_message.updated_at, datetime)
            else None
        )

        with transaction(self.conn):
            cursor = self.conn.execute(
                """
                INSERT INTO contact_messages (name, email, subject, message, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?);
                """,
                (
                    contact_message.name,
                    contact_message.email,
                    contact_message.subject,
                    contact_message.message,
                    contact_message.status.value,
                    created_at_str,
                    updated_at_str,
                ),
            )
            contact_message.id = cursor.lastrowid
            return contact_message

    def get_by_id(self, message_id: int) -> Optional[ContactMessage]:
        """Retrieve a single contact message by its primary key ID.

        Args:
            message_id: Integer primary key.

        Returns:
            ContactMessage if found, None otherwise.
        """
        cursor = self.conn.execute(
            """
            SELECT id, name, email, subject, message, status, created_at, updated_at
            FROM contact_messages
            WHERE id = ?;
            """,
            (message_id,),
        )
        row = cursor.fetchone()
        return _row_to_contact_message(row) if row else None

    def get_all(
        self,
        status: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[ContactMessage]:
        """Retrieve paginated contact messages, optionally filtered by status.

        Args:
            status: Optional status string ('new', 'read', 'resolved').
            limit: Maximum number of rows to return.
            offset: Number of rows to skip.

        Returns:
            List of ContactMessage domain models sorted in reverse chronological order.
        """
        if status and status.strip():
            cursor = self.conn.execute(
                """
                SELECT id, name, email, subject, message, status, created_at, updated_at
                FROM contact_messages
                WHERE status = ?
                ORDER BY created_at DESC, id DESC
                LIMIT ? OFFSET ?;
                """,
                (status.strip().lower(), limit, offset),
            )
        else:
            cursor = self.conn.execute(
                """
                SELECT id, name, email, subject, message, status, created_at, updated_at
                FROM contact_messages
                ORDER BY created_at DESC, id DESC
                LIMIT ? OFFSET ?;
                """,
                (limit, offset),
            )
        return [_row_to_contact_message(row) for row in cursor.fetchall()]

    def count(self, status: Optional[str] = None) -> int:
        """Count total messages, optionally filtered by status.

        Args:
            status: Optional status filter.

        Returns:
            Integer row count.
        """
        if status and status.strip():
            cursor = self.conn.execute(
                "SELECT COUNT(*) as cnt FROM contact_messages WHERE status = ?;",
                (status.strip().lower(),),
            )
        else:
            cursor = self.conn.execute("SELECT COUNT(*) as cnt FROM contact_messages;")
        row = cursor.fetchone()
        return row["cnt"] if row else 0

    def count_by_status(self, status: Optional[str] = None) -> Any:
        """Count messages grouped by status, or return count for a specific status.

        Args:
            status: Optional specific status name. If provided, returns integer count.
                   If omitted, returns dictionary mapping all statuses to counts plus 'total'.

        Returns:
            Integer count if status provided; otherwise dict of counts.
        """
        if status is not None:
            return self.count(status)

        cursor = self.conn.execute(
            "SELECT status, COUNT(*) as cnt FROM contact_messages GROUP BY status;"
        )
        counts = {"new": 0, "read": 0, "resolved": 0}
        total = 0
        for row in cursor.fetchall():
            st = row["status"]
            cnt = row["cnt"]
            counts[st] = cnt
            total += cnt
        counts["total"] = total
        return counts

    def update_status(
        self,
        message_id: int,
        new_status: str,
        updated_at: Optional[datetime] = None,
    ) -> bool:
        """Update the status and updated_at timestamp of a contact message.

        Args:
            message_id: Target message ID.
            new_status: New status string value.
            updated_at: Optional updated timestamp. Defaults to now UTC.

        Returns:
            True if a row was updated, False if message_id does not exist.
        """
        now = updated_at or datetime.now(timezone.utc)
        updated_at_str = now.strftime("%Y-%m-%dT%H:%M:%SZ")

        with transaction(self.conn):
            cursor = self.conn.execute(
                """
                UPDATE contact_messages
                SET status = ?, updated_at = ?
                WHERE id = ?;
                """,
                (new_status, updated_at_str, message_id),
            )
            return cursor.rowcount > 0

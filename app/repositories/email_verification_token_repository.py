"""Email verification token repository for SQLite database access."""
from datetime import datetime, timezone
from typing import Optional
import sqlite3

from app.database.connection import get_db, transaction
from app.models.email_verification_token import EmailVerificationToken


def _row_to_token(row: sqlite3.Row) -> EmailVerificationToken:
    """Convert an SQLite row into an EmailVerificationToken domain model."""
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
        raise ValueError("Invalid expires_at in token row.")

    used_at_val = row["used_at"]
    if used_at_val is not None:
        if isinstance(used_at_val, str):
            used_at = datetime.fromisoformat(used_at_val)
        elif isinstance(used_at_val, datetime):
            used_at = used_at_val
        else:
            used_at = None
    else:
        used_at = None

    return EmailVerificationToken(
        id=row["id"],
        user_id=row["user_id"],
        token_hash=row["token_hash"],
        expires_at=expires_at,
        created_at=created_at,
        used_at=used_at,
    )


class EmailVerificationTokenRepository:
    """Repository managing persistence for EmailVerificationToken entities."""

    def __init__(self, conn: Optional[sqlite3.Connection] = None) -> None:
        """Initialize EmailVerificationTokenRepository with an optional database connection.

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

    def create(self, token: EmailVerificationToken) -> EmailVerificationToken:
        """Insert a new email verification token.

        Args:
            token: The EmailVerificationToken to persist.

        Returns:
            The persisted EmailVerificationToken with its generated ID.
        """
        expires_at_str = token.expires_at.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        created_at_str = token.created_at.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        used_at_str = (
            token.used_at.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            if token.used_at
            else None
        )

        with transaction(self.conn):
            cursor = self.conn.execute(
                """
                INSERT INTO email_verification_tokens (user_id, token_hash, created_at, expires_at, used_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (token.user_id, token.token_hash, created_at_str, expires_at_str, used_at_str),
            )
            token.id = cursor.lastrowid
            return token

    def get_by_token_hash(self, token_hash: str) -> Optional[EmailVerificationToken]:
        """Retrieve a token by its SHA-256 hash.

        Args:
            token_hash: Hexadecimal SHA-256 hash string.

        Returns:
            The EmailVerificationToken entity if found, None otherwise.
        """
        if not token_hash or not isinstance(token_hash, str) or not token_hash.strip():
            return None

        cleaned_hash = token_hash.strip().lower()
        cursor = self.conn.execute(
            """
            SELECT id, user_id, token_hash, created_at, expires_at, used_at
            FROM email_verification_tokens
            WHERE token_hash = ?
            """,
            (cleaned_hash,),
        )
        row = cursor.fetchone()
        return _row_to_token(row) if row else None

    def get_latest_for_user(self, user_id: int) -> Optional[EmailVerificationToken]:
        """Retrieve the most recently created token for a user.

        Args:
            user_id: The ID of the user.

        Returns:
            The most recent EmailVerificationToken if found, None otherwise.
        """
        cursor = self.conn.execute(
            """
            SELECT id, user_id, token_hash, created_at, expires_at, used_at
            FROM email_verification_tokens
            WHERE user_id = ?
            ORDER BY created_at DESC, id DESC
            LIMIT 1
            """,
            (user_id,),
        )
        row = cursor.fetchone()
        return _row_to_token(row) if row else None

    def mark_as_used(
        self, token_id: int, used_at: Optional[datetime] = None
    ) -> bool:
        """Atomically mark a token as used.

        Enforces atomic single-use: only succeeds if used_at IS NULL.

        Args:
            token_id: The ID of the token.
            used_at: Optional timestamp of consumption. Defaults to now (UTC).

        Returns:
            True if the token was successfully marked as used, False if it was
            already used or did not exist.
        """
        dt = used_at if used_at is not None else datetime.now(timezone.utc)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        else:
            dt = dt.astimezone(timezone.utc)
        used_at_str = dt.strftime("%Y-%m-%dT%H:%M:%SZ")

        with transaction(self.conn):
            cursor = self.conn.execute(
                """
                UPDATE email_verification_tokens
                SET used_at = ?
                WHERE id = ? AND used_at IS NULL
                """,
                (used_at_str, token_id),
            )
            return cursor.rowcount > 0

    def invalidate_pending_for_user(
        self, user_id: int, invalidation_time: Optional[datetime] = None
    ) -> int:
        """Invalidate all pending unused tokens for a user.

        Args:
            user_id: The user whose pending tokens should be invalidated.
            invalidation_time: Timestamp to record as used_at. Defaults to now (UTC).

        Returns:
            The number of tokens invalidated.
        """
        dt = invalidation_time if invalidation_time is not None else datetime.now(timezone.utc)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        else:
            dt = dt.astimezone(timezone.utc)
        used_at_str = dt.strftime("%Y-%m-%dT%H:%M:%SZ")

        with transaction(self.conn):
            cursor = self.conn.execute(
                """
                UPDATE email_verification_tokens
                SET used_at = ?
                WHERE user_id = ? AND used_at IS NULL
                """,
                (used_at_str, user_id),
            )
            return cursor.rowcount

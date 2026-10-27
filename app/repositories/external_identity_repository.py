"""External identity repository for SQLite database access."""
from datetime import datetime, timezone
from typing import Optional
import sqlite3

from app.database.connection import get_db, transaction
from app.models.external_identity import ExternalIdentity


def _row_to_identity(row: sqlite3.Row) -> ExternalIdentity:
    """Convert an SQLite row into an ExternalIdentity domain model."""
    created_at_val = row["created_at"]
    if isinstance(created_at_val, str):
        created_at = datetime.fromisoformat(created_at_val.replace("Z", "+00:00"))
    elif isinstance(created_at_val, datetime):
        created_at = created_at_val
    else:
        created_at = datetime.now(timezone.utc)

    return ExternalIdentity(
        id=row["id"],
        user_id=row["user_id"],
        provider=row["provider"],
        provider_user_id=row["provider_user_id"],
        email=row["email"] if "email" in row.keys() else None,
        created_at=created_at,
    )


class ExternalIdentityRepository:
    """Repository managing persistence for ExternalIdentity entities."""

    def __init__(self, conn: Optional[sqlite3.Connection] = None) -> None:
        """Initialize with an optional database connection.

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

    def get_by_provider_sub(
        self, provider: str, provider_user_id: str
    ) -> Optional[ExternalIdentity]:
        """Retrieve an external identity by provider and provider subject ID.

        Args:
            provider: The identity provider name (e.g. 'google').
            provider_user_id: The provider's unique user identifier (sub).

        Returns:
            The ExternalIdentity if found, None otherwise.
        """
        if not provider or not provider_user_id:
            return None
        cursor = self.conn.execute(
            """
            SELECT id, user_id, provider, provider_user_id, email, created_at
            FROM user_external_identities
            WHERE provider = ? AND provider_user_id = ?
            """,
            (provider.strip().lower(), str(provider_user_id).strip()),
        )
        row = cursor.fetchone()
        return _row_to_identity(row) if row else None

    def get_by_user_id(self, user_id: int) -> list[ExternalIdentity]:
        """Retrieve all external identities linked to a user.

        Args:
            user_id: The internal user ID.

        Returns:
            List of ExternalIdentity entities linked to the user.
        """
        if isinstance(user_id, bool) or not isinstance(user_id, int) or user_id <= 0:
            return []
        cursor = self.conn.execute(
            """
            SELECT id, user_id, provider, provider_user_id, email, created_at
            FROM user_external_identities
            WHERE user_id = ?
            ORDER BY id ASC
            """,
            (user_id,),
        )
        return [_row_to_identity(row) for row in cursor.fetchall()]

    def get_by_user_and_provider(
        self, user_id: int, provider: str
    ) -> Optional[ExternalIdentity]:
        """Retrieve an external identity for a specific user and provider.

        Args:
            user_id: The internal user ID.
            provider: The identity provider name (e.g. 'google').

        Returns:
            The ExternalIdentity if found, None otherwise.
        """
        if isinstance(user_id, bool) or not isinstance(user_id, int) or user_id <= 0:
            return None
        if not provider or not isinstance(provider, str):
            return None
        cursor = self.conn.execute(
            """
            SELECT id, user_id, provider, provider_user_id, email, created_at
            FROM user_external_identities
            WHERE user_id = ? AND provider = ?
            """,
            (user_id, provider.strip().lower()),
        )
        row = cursor.fetchone()
        return _row_to_identity(row) if row else None

    def create(self, identity: ExternalIdentity) -> ExternalIdentity:
        """Persist a new external identity link in the database.

        Args:
            identity: The ExternalIdentity to insert.

        Returns:
            The persisted ExternalIdentity with its database ID set.

        Raises:
            sqlite3.IntegrityError: If uniqueness constraints are violated.
        """
        with transaction(self.conn):
            cursor = self.conn.execute(
                """
                INSERT INTO user_external_identities (
                    user_id, provider, provider_user_id, email, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    identity.user_id,
                    identity.provider,
                    identity.provider_user_id,
                    identity.email,
                    identity.created_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
                ),
            )
            identity.id = cursor.lastrowid
        return identity

    def delete_by_user_id(self, user_id: int) -> int:
        """Delete all external identities linked to a user.

        Args:
            user_id: The internal user ID.

        Returns:
            The number of rows deleted.
        """
        if isinstance(user_id, bool) or not isinstance(user_id, int) or user_id <= 0:
            return 0
        with transaction(self.conn):
            cursor = self.conn.execute(
                "DELETE FROM user_external_identities WHERE user_id = ?",
                (user_id,),
            )
            return cursor.rowcount

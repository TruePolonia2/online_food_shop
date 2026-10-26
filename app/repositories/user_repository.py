"""User repository for SQLite database access."""
from datetime import datetime, timezone
from typing import Optional
import sqlite3

from app.database.connection import get_db, transaction
from app.models.user import User, UserRole


def _row_to_user(row: sqlite3.Row) -> User:
    """Convert an SQLite row into a User domain model."""
    created_at_val = row["created_at"]
    if isinstance(created_at_val, str):
        created_at = datetime.fromisoformat(created_at_val)
    elif isinstance(created_at_val, datetime):
        created_at = created_at_val
    else:
        created_at = datetime.now(timezone.utc)

    return User(
        id=row["id"],
        email=row["email"],
        role=UserRole(row["role"]),
        is_verified=bool(row["is_verified"]),
        created_at=created_at,
    )


class UserRepository:
    """Repository managing persistence for User entities."""

    def __init__(self, conn: Optional[sqlite3.Connection] = None) -> None:
        """Initialize UserRepository with an optional database connection.

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

    def get_by_id(self, user_id: int) -> Optional[User]:
        """Retrieve a user by their primary key ID.

        Args:
            user_id: The user's ID.

        Returns:
            The User entity if found, None otherwise.
        """
        cursor = self.conn.execute(
            "SELECT id, email, role, is_verified, created_at FROM users WHERE id = ?",
            (user_id,),
        )
        row = cursor.fetchone()
        return _row_to_user(row) if row else None

    def get_by_email(self, email: str) -> Optional[User]:
        """Retrieve a user by their email address (case-insensitive).

        Args:
            email: The email address to look up.

        Returns:
            The User entity if found, None otherwise.
        """
        if not email or not isinstance(email, str) or not email.strip():
            return None

        cleaned_email = email.strip().lower()
        cursor = self.conn.execute(
            "SELECT id, email, role, is_verified, created_at FROM users WHERE email = ? COLLATE NOCASE",
            (cleaned_email,),
        )
        row = cursor.fetchone()
        return _row_to_user(row) if row else None

    def email_exists(self, email: str) -> bool:
        """Check whether an email address is already registered (case-insensitive).

        Args:
            email: The email address to check.

        Returns:
            True if the email exists, False otherwise.
        """
        if not email or not isinstance(email, str) or not email.strip():
            return False

        cleaned_email = email.strip().lower()
        cursor = self.conn.execute(
            "SELECT 1 FROM users WHERE email = ? COLLATE NOCASE LIMIT 1",
            (cleaned_email,),
        )
        return cursor.fetchone() is not None

    def get_all(self) -> list[User]:
        """Retrieve all users sorted by ID.

        Returns:
            A list of User entities.
        """
        cursor = self.conn.execute(
            "SELECT id, email, role, is_verified, created_at FROM users ORDER BY id ASC"
        )
        return [_row_to_user(row) for row in cursor.fetchall()]

    def create(self, user: User) -> User:
        """Insert a new user into the database.

        Converts uniqueness violations on email into a domain ValueError.

        Args:
            user: The User entity to persist.

        Returns:
            The persisted User entity with its generated ID.

        Raises:
            ValueError: If a user with the same email already exists.
        """
        cleaned_email = user.email.strip().lower()

        if user.created_at:
            dt = user.created_at
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            else:
                dt = dt.astimezone(timezone.utc)
            created_at_str = dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        else:
            created_at_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        role_str = user.role.value if isinstance(user.role, UserRole) else str(user.role)
        is_verified_int = 1 if user.is_verified else 0

        with transaction(self.conn):
            try:
                cursor = self.conn.execute(
                    "INSERT INTO users (email, role, is_verified, created_at) VALUES (?, ?, ?, ?)",
                    (cleaned_email, role_str, is_verified_int, created_at_str),
                )
                user.id = cursor.lastrowid
                user.email = cleaned_email
                return user
            except sqlite3.IntegrityError as err:
                err_msg = str(err).lower()
                if "unique" in err_msg and ("users.email" in err_msg or "email" in err_msg):
                    raise ValueError(f"User with email '{cleaned_email}' already exists.") from err
                raise

    def update(self, user: User) -> bool:
        """Update an existing user's information.

        Args:
            user: The User entity with modified fields and a valid ID.

        Returns:
            True if a user was updated, False if not found.

        Raises:
            ValueError: If user.id is None or email uniqueness is violated.
        """
        if user.id is None:
            raise ValueError("User ID cannot be None when updating.")

        cleaned_email = user.email.strip().lower()
        role_str = user.role.value if isinstance(user.role, UserRole) else str(user.role)
        is_verified_int = 1 if user.is_verified else 0

        with transaction(self.conn):
            try:
                cursor = self.conn.execute(
                    "UPDATE users SET email = ?, role = ?, is_verified = ? WHERE id = ?",
                    (cleaned_email, role_str, is_verified_int, user.id),
                )
                return cursor.rowcount > 0
            except sqlite3.IntegrityError as err:
                err_msg = str(err).lower()
                if "unique" in err_msg and ("users.email" in err_msg or "email" in err_msg):
                    raise ValueError(f"User with email '{cleaned_email}' already exists.") from err
                raise

    def delete(self, user_id: int) -> bool:
        """Delete a user by their primary key ID.

        Args:
            user_id: The ID of the user to delete.

        Returns:
            True if the user was deleted, False if not found.
        """
        with transaction(self.conn):
            cursor = self.conn.execute(
                "DELETE FROM users WHERE id = ?",
                (user_id,),
            )
            return cursor.rowcount > 0

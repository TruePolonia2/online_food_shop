"""User domain model."""
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional


class UserRole(str, Enum):
    """Enumeration of user authorization roles."""

    CUSTOMER = "customer"
    OWNER = "owner"


@dataclass
class User:
    """Represents a user account in the online food shop.

    Represents identity and account authorization. Authentication credentials
    (passwords, hashes, tokens) will be introduced in subsequent phases.
    """

    email: str
    id: Optional[int] = None
    role: UserRole = UserRole.CUSTOMER
    is_verified: bool = False
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    password_hash: Optional[str] = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if not self.email or not isinstance(self.email, str) or not self.email.strip() or "@" not in self.email:
            raise ValueError("A valid user email is required.")

        self.email = self.email.strip().lower()

        # Structural email check: non-empty local and domain parts
        parts = self.email.split("@")
        if len(parts) != 2 or not parts[0] or not parts[1]:
            raise ValueError("A valid user email is required.")

        # Validate role
        if isinstance(self.role, str):
            try:
                self.role = UserRole(self.role.strip().lower())
            except ValueError:
                raise ValueError(
                    f"Invalid role '{self.role}'. Supported roles are: 'customer', 'owner'."
                )
        elif not isinstance(self.role, UserRole):
            raise ValueError("Role must be a valid UserRole or string.")

        # Validate is_verified
        if isinstance(self.is_verified, bool):
            pass
        elif isinstance(self.is_verified, int) and self.is_verified in (0, 1):
            self.is_verified = bool(self.is_verified)
        else:
            raise ValueError("is_verified must be a boolean.")

        # Validate password_hash if present
        if self.password_hash is not None:
            if not isinstance(self.password_hash, str) or not self.password_hash.strip():
                raise ValueError("Password hash must be a non-empty string when provided.")

        # Ensure created_at is a timezone-aware UTC datetime
        if isinstance(self.created_at, str):
            self.created_at = datetime.fromisoformat(self.created_at)
        if self.created_at.tzinfo is None:
            self.created_at = self.created_at.replace(tzinfo=timezone.utc)
        else:
            self.created_at = self.created_at.astimezone(timezone.utc)

    @property
    def has_credentials(self) -> bool:
        """Indicate whether the user has configured password credentials."""
        return self.password_hash is not None and bool(self.password_hash.strip())

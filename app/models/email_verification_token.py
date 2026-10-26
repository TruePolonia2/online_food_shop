"""Email verification token domain model."""
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional


@dataclass
class EmailVerificationToken:
    """Represents a single-use token for email verification.

    Security Invariant: Stores only the cryptographic hash of the token,
    never the raw verification token string.
    """

    user_id: int
    token_hash: str
    expires_at: datetime
    id: Optional[int] = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    used_at: Optional[datetime] = None

    def __post_init__(self) -> None:
        if not isinstance(self.user_id, int) or isinstance(self.user_id, bool) or self.user_id <= 0:
            raise ValueError("A valid positive user_id is required.")

        if not self.token_hash or not isinstance(self.token_hash, str) or not self.token_hash.strip():
            raise ValueError("Token hash cannot be empty.")
        self.token_hash = self.token_hash.strip().lower()

        # Parse string timestamps if needed and ensure UTC timezone awareness
        if isinstance(self.expires_at, str):
            self.expires_at = datetime.fromisoformat(self.expires_at)
        if self.expires_at.tzinfo is None:
            self.expires_at = self.expires_at.replace(tzinfo=timezone.utc)
        else:
            self.expires_at = self.expires_at.astimezone(timezone.utc)

        if isinstance(self.created_at, str):
            self.created_at = datetime.fromisoformat(self.created_at)
        if self.created_at.tzinfo is None:
            self.created_at = self.created_at.replace(tzinfo=timezone.utc)
        else:
            self.created_at = self.created_at.astimezone(timezone.utc)

        if self.used_at is not None:
            if isinstance(self.used_at, str):
                self.used_at = datetime.fromisoformat(self.used_at)
            if self.used_at.tzinfo is None:
                self.used_at = self.used_at.replace(tzinfo=timezone.utc)
            else:
                self.used_at = self.used_at.astimezone(timezone.utc)

    @property
    def is_used(self) -> bool:
        """Indicate whether the token has already been consumed."""
        return self.used_at is not None

    @property
    def is_expired(self) -> bool:
        """Indicate whether the token has expired."""
        return datetime.now(timezone.utc) >= self.expires_at

    @property
    def is_valid(self) -> bool:
        """Indicate whether the token is currently valid and unconsumed."""
        return not self.is_used and not self.is_expired

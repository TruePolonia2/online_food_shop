"""Account link intent domain model for server-side single-use OAuth linking tracking."""
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional


@dataclass
class AccountLinkIntent:
    """Represents a short-lived, single-use intent to link an external identity to a user account.

    Enforces server-side replay defense against client-side signed cookie restoration.
    """

    user_id: int
    intent_id: str
    provider: str
    provider_user_id: str
    email: str
    expires_at: datetime
    id: Optional[int] = None
    next_url: Optional[str] = None
    status: str = "pending"
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    consumed_at: Optional[datetime] = None
    cancelled_at: Optional[datetime] = None

    def __post_init__(self) -> None:
        if not isinstance(self.user_id, int) or isinstance(self.user_id, bool) or self.user_id <= 0:
            raise ValueError("A valid positive user_id is required.")

        if not self.intent_id or not isinstance(self.intent_id, str) or not self.intent_id.strip():
            raise ValueError("Intent ID cannot be empty.")
        self.intent_id = self.intent_id.strip()

        if not self.provider or not isinstance(self.provider, str) or not self.provider.strip():
            raise ValueError("Provider cannot be empty.")
        self.provider = self.provider.strip().lower()

        if not self.provider_user_id or not isinstance(self.provider_user_id, str) or not self.provider_user_id.strip():
            raise ValueError("Provider user ID cannot be empty.")
        self.provider_user_id = self.provider_user_id.strip()

        if not self.email or not isinstance(self.email, str) or not self.email.strip() or "@" not in self.email:
            raise ValueError("A valid email address is required.")
        self.email = self.email.strip().lower()

        if self.status not in ("pending", "consumed", "cancelled"):
            raise ValueError(f"Invalid intent status: {self.status}")

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

        if self.consumed_at is not None:
            if isinstance(self.consumed_at, str):
                self.consumed_at = datetime.fromisoformat(self.consumed_at)
            if self.consumed_at.tzinfo is None:
                self.consumed_at = self.consumed_at.replace(tzinfo=timezone.utc)
            else:
                self.consumed_at = self.consumed_at.astimezone(timezone.utc)

        if self.cancelled_at is not None:
            if isinstance(self.cancelled_at, str):
                self.cancelled_at = datetime.fromisoformat(self.cancelled_at)
            if self.cancelled_at.tzinfo is None:
                self.cancelled_at = self.cancelled_at.replace(tzinfo=timezone.utc)
            else:
                self.cancelled_at = self.cancelled_at.astimezone(timezone.utc)

    @property
    def is_consumed(self) -> bool:
        """Indicate whether the intent has already been consumed."""
        return self.status == "consumed" or self.consumed_at is not None

    @property
    def is_cancelled(self) -> bool:
        """Indicate whether the intent has been cancelled."""
        return self.status == "cancelled" or self.cancelled_at is not None

    @property
    def is_expired(self) -> bool:
        """Indicate whether the intent has expired."""
        return datetime.now(timezone.utc) >= self.expires_at

    @property
    def is_valid(self) -> bool:
        """Indicate whether the intent is currently valid, unconsumed, and uncancelled."""
        return self.status == "pending" and not self.is_expired and not self.is_consumed and not self.is_cancelled

"""External identity domain model for federated OAuth/OpenID Connect providers."""
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional, Union


@dataclass
class ExternalIdentity:
    """Represents a linked third-party identity (e.g. Google sub) for a user account."""

    user_id: int
    provider: str
    provider_user_id: str
    id: Optional[int] = None
    email: Optional[str] = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self) -> None:
        if isinstance(self.user_id, bool) or not isinstance(self.user_id, int) or self.user_id <= 0:
            raise ValueError("user_id must be a positive integer.")

        if not self.provider or not isinstance(self.provider, str) or not self.provider.strip():
            raise ValueError("provider must be a non-empty string.")
        self.provider = self.provider.strip().lower()

        if self.provider_user_id is None or isinstance(self.provider_user_id, bool):
            raise ValueError("provider_user_id must be a non-empty string.")
        sub_str = str(self.provider_user_id).strip()
        if not sub_str:
            raise ValueError("provider_user_id must be a non-empty string.")
        self.provider_user_id = sub_str

        if self.email is not None:
            if not isinstance(self.email, str) or not self.email.strip():
                self.email = None
            else:
                self.email = self.email.strip().lower()

        if isinstance(self.created_at, str):
            try:
                self.created_at = datetime.fromisoformat(self.created_at.replace("Z", "+00:00"))
            except (ValueError, TypeError):
                self.created_at = datetime.now(timezone.utc)
        elif isinstance(self.created_at, datetime):
            if self.created_at.tzinfo is None:
                self.created_at = self.created_at.replace(tzinfo=timezone.utc)

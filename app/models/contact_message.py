"""Contact message domain model."""
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional


class ContactMessageStatus(str, Enum):
    """Enumeration of contact message lifecycle statuses."""

    NEW = "new"
    READ = "read"
    RESOLVED = "resolved"


# Allowed status transitions between distinct lifecycle states
ALLOWED_STATUS_TRANSITIONS: dict[ContactMessageStatus, set[ContactMessageStatus]] = {
    ContactMessageStatus.NEW: {ContactMessageStatus.READ, ContactMessageStatus.RESOLVED},
    ContactMessageStatus.READ: {ContactMessageStatus.NEW, ContactMessageStatus.RESOLVED},
    ContactMessageStatus.RESOLVED: {ContactMessageStatus.NEW, ContactMessageStatus.READ},
}

MAX_NAME_LENGTH = 100
MAX_EMAIL_LENGTH = 254
MAX_SUBJECT_LENGTH = 150
MAX_MESSAGE_LENGTH = 5000


@dataclass
class ContactMessage:
    """Represents a customer inquiry or support message submitted through the contact form."""

    name: str
    email: str
    subject: str
    message: str
    id: Optional[int] = None
    status: ContactMessageStatus = ContactMessageStatus.NEW
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: Optional[datetime] = None

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("Contact message name cannot be empty.")
        self.name = self.name.strip()
        if len(self.name) > MAX_NAME_LENGTH:
            raise ValueError(f"Contact message name must not exceed {MAX_NAME_LENGTH} characters.")

        if not isinstance(self.email, str) or not self.email.strip() or "@" not in self.email:
            raise ValueError("A valid email address is required.")
        self.email = self.email.strip().lower()
        if len(self.email) > MAX_EMAIL_LENGTH:
            raise ValueError(f"Email address must not exceed {MAX_EMAIL_LENGTH} characters.")

        if not isinstance(self.subject, str) or not self.subject.strip():
            raise ValueError("Contact message subject cannot be empty.")
        self.subject = self.subject.strip()
        if len(self.subject) > MAX_SUBJECT_LENGTH:
            raise ValueError(f"Contact message subject must not exceed {MAX_SUBJECT_LENGTH} characters.")

        if not isinstance(self.message, str) or not self.message.strip():
            raise ValueError("Contact message body cannot be empty.")
        self.message = self.message.strip()
        if len(self.message) > MAX_MESSAGE_LENGTH:
            raise ValueError(f"Contact message body must not exceed {MAX_MESSAGE_LENGTH} characters.")

        if isinstance(self.status, str) and not isinstance(self.status, ContactMessageStatus):
            try:
                self.status = ContactMessageStatus(self.status.strip().lower())
            except ValueError:
                raise ValueError(
                    f"Invalid status '{self.status}'. Allowed statuses: "
                    f"{', '.join(s.value for s in ContactMessageStatus)}."
                )

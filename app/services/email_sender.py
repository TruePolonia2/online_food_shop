"""Email delivery abstractions and in-memory test sender."""
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional, Protocol, runtime_checkable


@dataclass
class SentEmail:
    """Record of an email sent by an EmailSender implementation."""

    to_email: str
    raw_token: str
    sent_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    subject: str = "Verify your email address"
    email_type: str = "verification"
    reset_url: Optional[str] = None


@runtime_checkable
class EmailSender(Protocol):
    """Protocol defining the interface for sending system emails."""

    def send_verification_email(self, to_email: str, raw_token: str) -> bool:
        """Send an email verification message containing the raw token.

        Args:
            to_email: The destination email address.
            raw_token: The plaintext verification token.

        Returns:
            True if delivery succeeded, False otherwise.
        """
        ...

    def send_password_reset_email(
        self, to_email: str, raw_token: str, reset_url: Optional[str] = None
    ) -> bool:
        """Send a password reset message containing the raw token and reset URL.

        Args:
            to_email: The destination email address.
            raw_token: The plaintext reset token.
            reset_url: Optional trusted absolute URL for password reset.

        Returns:
            True if delivery succeeded, False otherwise.
        """
        ...


class InMemoryEmailSender:
    """In-memory implementation of EmailSender for tests and local development.

    Records sent emails in an internal list without network or SMTP I/O.
    """

    def __init__(self, simulate_failure: bool = False) -> None:
        """Initialize InMemoryEmailSender.

        Args:
            simulate_failure: If True, calls to send methods fail.
        """
        self.sent_messages: list[SentEmail] = []
        self.simulate_failure: bool = simulate_failure

    def send_verification_email(self, to_email: str, raw_token: str) -> bool:
        """Record the sent verification email in-memory, or simulate failure.

        Args:
            to_email: The destination email address.
            raw_token: The plaintext verification token.

        Returns:
            True if delivery succeeded, False if simulated failure.
        """
        if self.simulate_failure:
            return False

        message = SentEmail(
            to_email=to_email,
            raw_token=raw_token,
            subject="Verify your email address",
            email_type="verification",
        )
        self.sent_messages.append(message)
        return True

    def send_password_reset_email(
        self, to_email: str, raw_token: str, reset_url: Optional[str] = None
    ) -> bool:
        """Record the sent password reset email in-memory, or simulate failure.

        Args:
            to_email: The destination email address.
            raw_token: The plaintext reset token.
            reset_url: Optional trusted absolute URL for password reset.

        Returns:
            True if delivery succeeded, False if simulated failure.
        """
        if self.simulate_failure:
            return False

        message = SentEmail(
            to_email=to_email,
            raw_token=raw_token,
            subject="Reset your password",
            email_type="password_reset",
            reset_url=reset_url,
        )
        self.sent_messages.append(message)
        return True

    def clear(self) -> None:
        """Clear all recorded sent messages."""
        self.sent_messages.clear()

    @property
    def last_email(self) -> Optional[SentEmail]:
        """Return the most recently sent email, or None if none sent."""
        return self.sent_messages[-1] if self.sent_messages else None

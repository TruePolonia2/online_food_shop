"""Contact service coordinating contact message submission, rate limiting, and status lifecycle."""
import logging
import re
import sqlite3
import threading
import time
from typing import Any, Optional

from app.database.connection import get_db
from app.models.contact_message import (
    ALLOWED_STATUS_TRANSITIONS,
    MAX_EMAIL_LENGTH,
    MAX_MESSAGE_LENGTH,
    MAX_NAME_LENGTH,
    MAX_SUBJECT_LENGTH,
    ContactMessage,
    ContactMessageStatus,
)
from app.repositories.contact_message_repository import ContactMessageRepository

logger = logging.getLogger(__name__)

EMAIL_REGEX = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class ContactValidationError(ValueError):
    """Raised when contact form validation fails."""

    def __init__(self, errors: dict[str, str], form_data: Optional[dict[str, Any]] = None) -> None:
        super().__init__("; ".join(errors.values()))
        self.errors = errors
        self.form_data = form_data or {}


class ContactRateLimitExceededError(Exception):
    """Raised when contact form submission rate limit is exceeded."""

    pass


class ContactStatusTransitionError(ValueError):
    """Raised when an invalid status transition is requested."""

    pass


class ContactRateLimiter:
    """Thread-safe, bounded sliding-window rate limiter for contact form submissions.

    Limits submissions per client IP within a rolling time window.
    Automatically prunes expired records and evicts oldest keys under memory pressure.
    """

    def __init__(
        self,
        max_attempts: int = 5,
        window_seconds: int = 600,
        max_keys: int = 10000,
    ) -> None:
        self.max_attempts = max_attempts
        self.window_seconds = window_seconds
        self.max_keys = max_keys
        self._lock = threading.Lock()
        self._attempts: dict[str, list[float]] = {}

    def _prune_expired_locked(self, now: float) -> None:
        """Remove expired timestamps and empty IP keys. Must be called with lock held."""
        cutoff = now - self.window_seconds
        keys_to_delete = []
        for ip, timestamps in self._attempts.items():
            valid = [t for t in timestamps if t > cutoff]
            if not valid:
                keys_to_delete.append(ip)
            else:
                self._attempts[ip] = valid
        for ip in keys_to_delete:
            del self._attempts[ip]

    def _enforce_capacity_locked(self, now: float) -> None:
        """Enforce max_keys bound by pruning expired entries and evicting oldest non-locked keys."""
        self._prune_expired_locked(now)
        if len(self._attempts) < self.max_keys:
            return

        # Evict only non-locked keys (< max_attempts) to prevent an attacker from
        # evicting an active lockout on an IP address via memory exhaustion.
        candidates = [
            (ip, max(ts) if ts else 0.0)
            for ip, ts in self._attempts.items()
            if len(ts) < self.max_attempts
        ]
        candidates.sort(key=lambda item: item[1])
        excess = (len(self._attempts) - self.max_keys) + 1
        for ip, _ in candidates[:excess]:
            del self._attempts[ip]

    def is_rate_limited(self, ip: str) -> bool:
        """Check whether the client IP has exceeded the allowed submissions.

        If the limiter is at capacity and saturated with active lockouts, fails closed
        by throttling untracked requests to prevent flooding past capacity.
        """
        ip_clean = (ip or "").strip()
        if not ip_clean:
            return False
        now = time.time()
        with self._lock:
            self._prune_expired_locked(now)
            timestamps = self._attempts.get(ip_clean)
            if timestamps is not None:
                return len(timestamps) >= self.max_attempts

            # Key is not tracked. Check if limiter is at capacity with all stored keys locked out.
            if len(self._attempts) >= self.max_keys:
                has_evictable = any(
                    len(ts) < self.max_attempts for ts in self._attempts.values()
                )
                if not has_evictable:
                    logger.warning(
                        "ContactRateLimiter reached maximum capacity (%d) with active blocks. Throttling untracked IP.",
                        self.max_keys,
                    )
                    return True

            return False

    def record_submission(self, ip: str) -> None:
        """Record a successful or validated contact form submission from client IP."""
        ip_clean = (ip or "").strip()
        if not ip_clean:
            return
        now = time.time()
        with self._lock:
            if ip_clean in self._attempts:
                self._attempts[ip_clean].append(now)
                return

            self._enforce_capacity_locked(now)
            if len(self._attempts) < self.max_keys:
                self._attempts[ip_clean] = [now]
            else:
                logger.warning(
                    "ContactRateLimiter reached maximum capacity (%d) with active blocks.",
                    self.max_keys,
                )

    def reset(self, ip: str) -> None:
        """Reset attempts for a specific IP (e.g. for testing or administrative unblock)."""
        ip_clean = (ip or "").strip()
        with self._lock:
            self._attempts.pop(ip_clean, None)

    def clear(self) -> None:
        """Clear all rate limiter state (for tests)."""
        with self._lock:
            self._attempts.clear()


# Default singleton instance
contact_rate_limiter = ContactRateLimiter(
    max_attempts=5,
    window_seconds=600,
    max_keys=10000,
)


class ContactService:
    """Service managing contact message validation, submission, and status lifecycles."""

    def __init__(
        self,
        contact_repo: Optional[ContactMessageRepository] = None,
        rate_limiter: Optional[ContactRateLimiter] = None,
        conn: Optional[sqlite3.Connection] = None,
    ) -> None:
        self._conn = conn
        self.contact_repo = contact_repo or ContactMessageRepository(conn=conn)
        self.rate_limiter = rate_limiter or contact_rate_limiter

    @property
    def conn(self) -> sqlite3.Connection:
        """Get the active database connection."""
        if self._conn is not None:
            return self._conn
        return get_db()

    def validate_contact_data(
        self,
        name: Optional[str],
        email: Optional[str],
        subject: Optional[str],
        message: Optional[str],
    ) -> tuple[dict[str, str], dict[str, str]]:
        """Validate contact form inputs.

        Returns:
            A tuple of (errors_dict, cleaned_data_dict).
        """
        errors: dict[str, str] = {}
        cleaned_data: dict[str, str] = {}

        # Name validation
        clean_name = (name or "").strip()
        cleaned_data["name"] = clean_name
        if not clean_name:
            errors["name"] = "Name is required."
        elif len(clean_name) > MAX_NAME_LENGTH:
            errors["name"] = f"Name must not exceed {MAX_NAME_LENGTH} characters."

        # Email validation
        raw_email = (email or "").strip()
        clean_email = raw_email.lower()
        cleaned_data["email"] = clean_email
        if not clean_email:
            errors["email"] = "Email is required."
        elif len(clean_email) > MAX_EMAIL_LENGTH:
            errors["email"] = f"Email must not exceed {MAX_EMAIL_LENGTH} characters."
        elif not EMAIL_REGEX.match(clean_email):
            errors["email"] = "Please enter a valid email address (e.g., name@example.com)."

        # Subject validation
        clean_subject = (subject or "").strip()
        cleaned_data["subject"] = clean_subject
        if not clean_subject:
            errors["subject"] = "Subject is required."
        elif len(clean_subject) > MAX_SUBJECT_LENGTH:
            errors["subject"] = f"Subject must not exceed {MAX_SUBJECT_LENGTH} characters."

        # Message validation
        clean_message = (message or "").strip()
        cleaned_data["message"] = clean_message
        if not clean_message:
            errors["message"] = "Message is required."
        elif len(clean_message) > MAX_MESSAGE_LENGTH:
            errors["message"] = f"Message must not exceed {MAX_MESSAGE_LENGTH} characters."

        return errors, cleaned_data

    def submit_message(
        self,
        name: Optional[str],
        email: Optional[str],
        subject: Optional[str],
        message: Optional[str],
        client_ip: str,
    ) -> ContactMessage:
        """Validate and record a new customer contact message with IP rate limiting.

        Args:
            name: Sender full name.
            email: Sender contact email.
            subject: Inquiry subject.
            message: Message body text.
            client_ip: Direct client IP address for sliding-window rate limiting.

        Returns:
            The newly created and persisted ContactMessage.

        Raises:
            ContactRateLimitExceededError: If client IP exceeded submission limit.
            ContactValidationError: If any field fails validation constraints.
        """
        # 1. Check rate limit
        if self.rate_limiter.is_rate_limited(client_ip):
            raise ContactRateLimitExceededError(
                "Too many contact submissions. Please wait a few minutes before trying again."
            )

        # 2. Server-side validation
        errors, cleaned = self.validate_contact_data(
            name=name,
            email=email,
            subject=subject,
            message=message,
        )
        if errors:
            raise ContactValidationError(errors, cleaned)

        # 3. Persist contact message
        contact_message = self.contact_repo.create(
            name=cleaned["name"],
            email=cleaned["email"],
            subject=cleaned["subject"],
            message=cleaned["message"],
        )

        # 4. Record rate limit timestamp
        self.rate_limiter.record_submission(client_ip)

        return contact_message

    def get_message(self, message_id: int, mark_read_if_new: bool = False) -> Optional[ContactMessage]:
        """Retrieve a contact message by ID without modifying database state.

        Adheres strictly to safe GET semantics: viewing a message never mutates
        persistent database state. Transitions to 'read' or other statuses must be
        performed via explicit CSRF-protected POST actions.

        Args:
            message_id: ID of the message.
            mark_read_if_new: Unused parameter retained for signature compatibility.

        Returns:
            ContactMessage if found, None otherwise.
        """
        return self.contact_repo.get_by_id(message_id)

    def list_messages(
        self,
        status: Optional[str] = None,
        page: int = 1,
        per_page: int = 20,
    ) -> tuple[list[ContactMessage], int, int]:
        """List contact messages with optional status filter and pagination.

        Args:
            status: Optional status filter ("new", "read", "resolved", or None for all).
            page: 1-indexed page number (clamped to >= 1).
            per_page: Items per page (clamped to between 1 and 100).

        Returns:
            A tuple of (messages_list, total_count, total_pages).
        """
        valid_status: Optional[str] = None
        if status:
            status_clean = status.strip().lower()
            try:
                valid_status = ContactMessageStatus(status_clean).value
            except ValueError:
                valid_status = None

        safe_page = max(1, page)
        safe_per_page = max(1, min(100, per_page))
        offset = (safe_page - 1) * safe_per_page

        messages = self.contact_repo.get_all(
            status=valid_status,
            limit=safe_per_page,
            offset=offset,
        )
        total_count = self.contact_repo.count(status=valid_status)
        total_pages = max(1, (total_count + safe_per_page - 1) // safe_per_page)

        return messages, total_count, total_pages

    def update_status(self, message_id: int, new_status: str) -> ContactMessage:
        """Update the lifecycle status of a contact message following allowed transitions.

        Args:
            message_id: Message ID.
            new_status: Desired new status string ("new", "read", "resolved").

        Returns:
            Updated ContactMessage.

        Raises:
            ValueError: If message not found or new status is unrecognized.
            ContactStatusTransitionError: If transition is forbidden.
        """
        clean_status = (new_status or "").strip().lower()
        try:
            target_status = ContactMessageStatus(clean_status)
        except ValueError as err:
            raise ValueError(f"Invalid contact message status '{new_status}'.") from err

        current_message = self.contact_repo.get_by_id(message_id)
        if current_message is None:
            raise ValueError(f"Contact message #{message_id} not found.")

        if current_message.status == target_status:
            return current_message

        allowed = ALLOWED_STATUS_TRANSITIONS.get(current_message.status, set())
        if target_status not in allowed:
            raise ContactStatusTransitionError(
                f"Cannot transition message from '{current_message.status.value}' to '{target_status.value}'."
            )

        success = self.contact_repo.update_status(message_id, target_status.value)
        if not success:
            raise ValueError(f"Failed to update status for contact message #{message_id}.")
        updated_msg = self.contact_repo.get_by_id(message_id)
        if updated_msg is None:
            raise ValueError(f"Contact message #{message_id} not found after status update.")
        return updated_msg

    def get_status_counts(self) -> dict[str, int]:
        """Get message counts broken down by status plus total count."""
        return {
            "all": self.contact_repo.count(),
            "new": self.contact_repo.count_by_status(ContactMessageStatus.NEW.value),
            "read": self.contact_repo.count_by_status(ContactMessageStatus.READ.value),
            "resolved": self.contact_repo.count_by_status(ContactMessageStatus.RESOLVED.value),
        }

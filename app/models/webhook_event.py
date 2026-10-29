"""Stripe webhook event domain models and status enumerations."""
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional


class WebhookEventStatus(str, Enum):
    """Lifecycle statuses for Stripe webhook events."""

    PROCESSING = "processing"
    PROCESSED = "processed"
    FAILED = "failed"
    IGNORED = "ignored"


@dataclass
class StripeWebhookEvent:
    """Represents a received Stripe webhook event for idempotency and audit tracking."""

    event_id: str
    event_type: str
    id: Optional[int] = None
    livemode: bool = False
    status: WebhookEventStatus = WebhookEventStatus.PROCESSING
    order_id: Optional[int] = None
    payment_id: Optional[int] = None
    payload_summary: Optional[str] = None
    error_message: Optional[str] = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    processed_at: Optional[datetime] = None

    def __post_init__(self) -> None:
        if isinstance(self.status, str) and not isinstance(self.status, WebhookEventStatus):
            self.status = WebhookEventStatus(self.status)

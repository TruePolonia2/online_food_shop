"""Payment domain models and status enumerations."""
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional


class PaymentStatus(str, Enum):
    """Lifecycle statuses for payment attempts and orders."""

    UNPAID = "unpaid"
    PENDING = "pending"
    PAID = "paid"
    FAILED = "failed"
    CANCELLED = "cancelled"


class PaymentMethod(str, Enum):
    """Supported payment methods."""

    STANDARD = "standard"
    STRIPE = "stripe"


@dataclass
class Payment:
    """Represents a discrete payment attempt for an order."""

    order_id: int
    amount: int
    id: Optional[int] = None
    payment_method: str = "stripe"
    status: PaymentStatus = PaymentStatus.PENDING
    currency: str = "eur"
    stripe_session_id: Optional[str] = None
    stripe_payment_intent_id: Optional[str] = None
    idempotency_key: Optional[str] = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: Optional[datetime] = None

    def __post_init__(self) -> None:
        if isinstance(self.status, str) and not isinstance(self.status, PaymentStatus):
            self.status = PaymentStatus(self.status)
        if self.currency:
            self.currency = self.currency.strip().lower()
        if self.payment_method:
            self.payment_method = self.payment_method.strip().lower()

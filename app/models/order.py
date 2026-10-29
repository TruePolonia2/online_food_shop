"""Order domain model."""
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from app.models.order_item import OrderItem
from app.models.payment import PaymentStatus


class OrderStatus(str, Enum):
    """Enumeration of standard order lifecycle statuses."""

    PENDING = "pending"
    CONFIRMED = "confirmed"
    PAID = "paid"
    SHIPPED = "shipped"
    DELIVERED = "delivered"
    CANCELLED = "cancelled"


@dataclass
class Order:
    """Represents a customer order with immutable delivery snapshot data.

    Designed for persistence in SQLite with order items stored in a separate table.
    Preserves order-specific delivery details (name, email, phone, address)
    independently from the customer account profile to protect privacy and
    guarantee historical accuracy.
    """

    customer_id: int
    id: Optional[int] = None
    status: OrderStatus = OrderStatus.PENDING
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    items: list[OrderItem] = field(default_factory=list)
    customer_name: Optional[str] = None
    customer_email: Optional[str] = None
    customer_phone: Optional[str] = None
    customer_address: Optional[str] = None
    user_id: Optional[int] = None
    payment_method: str = "standard"
    payment_status: PaymentStatus = PaymentStatus.UNPAID
    stripe_session_id: Optional[str] = None
    checkout_token: Optional[str] = None

    def __post_init__(self) -> None:
        if isinstance(self.status, str) and not isinstance(self.status, OrderStatus):
            self.status = OrderStatus(self.status)
        if isinstance(self.payment_status, str) and not isinstance(
            self.payment_status, PaymentStatus
        ):
            self.payment_status = PaymentStatus(self.payment_status)
        if self.payment_method:
            self.payment_method = self.payment_method.strip().lower()

    @property
    def total(self) -> int:
        """Calculate total order amount in minor currency units (cents)."""
        return sum(item.subtotal for item in self.items)

    @property
    def delivery_name(self) -> Optional[str]:
        """Delivery recipient name snapshot."""
        return self.customer_name

    @property
    def delivery_email(self) -> Optional[str]:
        """Delivery contact email snapshot."""
        return self.customer_email

    @property
    def delivery_phone(self) -> Optional[str]:
        """Delivery contact phone snapshot."""
        return self.customer_phone

    @property
    def delivery_address(self) -> Optional[str]:
        """Delivery physical address snapshot."""
        return self.customer_address

    def add_item(self, item: OrderItem) -> None:
        """Add an item to the order."""
        self.items.append(item)

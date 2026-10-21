"""Order domain model."""
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from app.models.order_item import OrderItem


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
    """Represents a customer order.

    Designed for persistence in SQLite with order items stored in a separate table.
    """

    customer_id: int
    id: Optional[int] = None
    status: OrderStatus = OrderStatus.PENDING
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    items: list[OrderItem] = field(default_factory=list)

    def __post_init__(self) -> None:
        if isinstance(self.status, str) and not isinstance(self.status, OrderStatus):
            self.status = OrderStatus(self.status)

    @property
    def total(self) -> int:
        """Calculate total order amount in minor currency units (cents)."""
        return sum(item.subtotal for item in self.items)

    def add_item(self, item: OrderItem) -> None:
        """Add an item to the order."""
        self.items.append(item)

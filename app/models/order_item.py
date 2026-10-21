"""OrderItem domain model."""
from dataclasses import dataclass
from typing import Optional


@dataclass
class OrderItem:
    """Represents a product item within a completed or pending order.

    Preserves the unit price at purchase time in minor currency units (cents)
    to protect order history against future catalog price changes.
    """

    product_id: int
    product_name: str
    unit_price: int  # in minor currency units (cents)
    quantity: int
    id: Optional[int] = None
    order_id: Optional[int] = None

    def __post_init__(self) -> None:
        if self.unit_price < 0:
            raise ValueError("Unit price cannot be negative.")
        if self.quantity <= 0:
            raise ValueError("Order item quantity must be greater than zero.")
        if not self.product_name or not self.product_name.strip():
            raise ValueError("Product name cannot be empty.")
        self.product_name = self.product_name.strip()

    @property
    def subtotal(self) -> int:
        """Calculate order item subtotal in minor currency units (cents)."""
        return self.unit_price * self.quantity

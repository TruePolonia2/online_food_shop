"""CartItem domain model."""
from dataclasses import dataclass

from app.models.product import Product


@dataclass
class CartItem:
    """Represents a product and quantity in a shopping cart."""

    product: Product
    quantity: int = 1

    def __post_init__(self) -> None:
        if self.quantity <= 0:
            raise ValueError("Cart item quantity must be greater than zero.")

    @property
    def subtotal(self) -> int:
        """Calculate item subtotal in minor currency units (cents)."""
        return self.product.price * self.quantity

    def update_quantity(self, quantity: int) -> None:
        """Update item quantity enforcing domain validation."""
        if quantity <= 0:
            raise ValueError("Cart item quantity must be greater than zero.")
        self.quantity = quantity

"""Cart domain model."""
from dataclasses import dataclass, field
from typing import Optional

from app.models.cart_item import CartItem
from app.models.product import Product


@dataclass
class Cart:
    """Represents a customer's shopping cart.

    Designed as an in-memory domain model independent of a specific persistence mechanism.
    """

    customer_id: Optional[int] = None
    items: list[CartItem] = field(default_factory=list)

    def add_item(self, product: Product, quantity: int = 1) -> None:
        """Add a product to the cart or increment quantity if already present."""
        if (
            isinstance(quantity, bool)
            or not isinstance(quantity, int)
            or quantity <= 0
        ):
            raise ValueError("Quantity to add must be greater than zero.")

        for item in self.items:
            if item.product.id is not None and item.product.id == product.id:
                item.update_quantity(item.quantity + quantity)
                return

        self.items.append(CartItem(product=product, quantity=quantity))

    def remove_item(self, product_id: int) -> None:
        """Remove a product from the cart by its product ID."""
        self.items = [item for item in self.items if item.product.id != product_id]

    def update_quantity(self, product_id: int, quantity: int) -> None:
        """Update quantity for a product, removing it if quantity is zero or less."""
        if isinstance(quantity, bool) or not isinstance(quantity, int):
            raise ValueError("Quantity must be an integer.")

        if quantity <= 0:
            self.remove_item(product_id)
            return

        for item in self.items:
            if item.product.id == product_id:
                item.update_quantity(quantity)
                return

        raise ValueError(f"Product with ID {product_id} not found in cart.")

    def clear(self) -> None:
        """Empty all items from the cart."""
        self.items.clear()

    @property
    def total(self) -> int:
        """Calculate total amount of all items in minor currency units (cents)."""
        return sum(item.subtotal for item in self.items)

    @property
    def total_quantity(self) -> int:
        """Calculate total quantity of items in the cart."""
        return sum(item.quantity for item in self.items)

    @property
    def is_empty(self) -> bool:
        """Check whether the cart is empty."""
        return len(self.items) == 0

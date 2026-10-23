"""Product domain model."""
from dataclasses import dataclass
from typing import Optional


@dataclass
class Product:
    """Represents a product in the shop catalog.

    Price is represented in minor currency units (e.g. cents) as an integer
    to avoid binary floating-point precision issues.
    """

    name: str
    price: int  # in minor currency units (cents)
    stock_quantity: int = 0
    id: Optional[int] = None
    description: Optional[str] = None
    category_id: Optional[int] = None
    is_active: bool = True

    def __post_init__(self) -> None:
        if not self.name or not self.name.strip():
            raise ValueError("Product name cannot be empty.")
        self.name = self.name.strip()
        if isinstance(self.price, bool) or not isinstance(self.price, int) or self.price < 0:
            raise ValueError("Product price cannot be negative.")
        if (
            isinstance(self.stock_quantity, bool)
            or not isinstance(self.stock_quantity, int)
            or self.stock_quantity < 0
        ):
            raise ValueError("Stock quantity cannot be negative.")

    @property
    def is_in_stock(self) -> bool:
        """Check if product is active and has stock available."""
        return self.is_active and self.stock_quantity > 0

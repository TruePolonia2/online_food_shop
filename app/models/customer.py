"""Customer domain model."""
from dataclasses import dataclass
from typing import Optional


@dataclass
class Customer:
    """Represents a customer in the food shop."""

    name: str
    email: str
    id: Optional[int] = None
    phone: Optional[str] = None
    address: Optional[str] = None

    def __post_init__(self) -> None:
        if not self.name or not self.name.strip():
            raise ValueError("Customer name cannot be empty.")
        if not self.email or "@" not in self.email:
            raise ValueError("A valid customer email is required.")
        self.name = self.name.strip()
        self.email = self.email.strip().lower()

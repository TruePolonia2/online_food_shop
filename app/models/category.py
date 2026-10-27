"""Category domain model."""
from dataclasses import dataclass
from typing import Optional


@dataclass
class Category:
    """Represents a product category in the catalog."""

    id: Optional[int] = None
    name: str = ""
    description: Optional[str] = None
    is_active: bool = True

    def __post_init__(self) -> None:
        if not self.name or not self.name.strip():
            raise ValueError("Category name cannot be empty.")
        self.name = self.name.strip()
        if self.description is not None:
            self.description = self.description.strip()
        if not isinstance(self.is_active, bool):
            raise ValueError("Category is_active must be a boolean.")

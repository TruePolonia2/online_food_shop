"""Category service coordinating business logic and persistence for product categories."""
from app.models.category import Category
from app.repositories.category_repository import CategoryRepository


class CategoryService:
    """Service handling category validation and operations."""

    def __init__(self, category_repo: CategoryRepository | None = None) -> None:
        """Initialize CategoryService with injected repository.

        Args:
            category_repo: CategoryRepository instance. Defaults to a new instance.
        """
        self.category_repo = category_repo or CategoryRepository()

    def get_by_id(self, category_id: int) -> Category | None:
        """Retrieve a category by primary key ID.

        Args:
            category_id: The ID of the category.

        Returns:
            The Category entity if found, None otherwise.
        """
        return self.category_repo.get_by_id(category_id)

    def get_all(self, include_inactive: bool = False) -> list[Category]:
        """Retrieve all categories sorted alphabetically.

        Args:
            include_inactive: Whether to include inactive categories. Defaults to False.

        Returns:
            A list of Category entities.
        """
        return self.category_repo.get_all(include_inactive=include_inactive)

    def create_category(
        self,
        name: str,
        description: str | None = None,
        is_active: bool = True,
    ) -> Category:
        """Create a new category after validating business rules.

        Args:
            name: The category name. Must be non-empty and unique.
            description: Optional category description.
            is_active: Whether category is active. Defaults to True.

        Returns:
            The persisted Category entity with assigned ID.

        Raises:
            ValueError: If the name is empty, already in use, or invalid types.
        """
        if not name or not name.strip():
            raise ValueError("Category name cannot be empty.")

        if not isinstance(is_active, bool):
            raise ValueError("Category is_active must be a boolean.")

        cleaned_name = name.strip()
        if self.category_repo.get_by_name(cleaned_name) is not None:
            raise ValueError(f"Category with name '{cleaned_name}' already exists.")

        cleaned_desc = description.strip() if description else None
        category = Category(name=cleaned_name, description=cleaned_desc, is_active=is_active)
        return self.category_repo.create(category)

    def update_category(
        self,
        category_id: int,
        name: str,
        description: str | None = None,
        is_active: bool = True,
    ) -> Category:
        """Update an existing category after validating business rules.

        Args:
            category_id: The ID of the category to update.
            name: The new category name. Must be non-empty and unique.
            description: Optional updated description.
            is_active: Whether category is active. Defaults to True.

        Returns:
            The updated Category entity.

        Raises:
            ValueError: If the category does not exist, name is empty, or name is taken.
        """
        category = self.category_repo.get_by_id(category_id)
        if category is None:
            raise ValueError(f"Category with ID {category_id} not found.")

        if not name or not name.strip():
            raise ValueError("Category name cannot be empty.")

        if not isinstance(is_active, bool):
            raise ValueError("Category is_active must be a boolean.")

        cleaned_name = name.strip()
        existing = self.category_repo.get_by_name(cleaned_name)
        if existing is not None and existing.id != category_id:
            raise ValueError(f"Category with name '{cleaned_name}' already exists.")

        category.name = cleaned_name
        category.description = description.strip() if description else None
        category.is_active = is_active
        self.category_repo.update(category)
        return category

    def delete_category(self, category_id: int) -> bool:
        """Delete a category by its ID.

        Args:
            category_id: The ID of the category to delete.

        Returns:
            True if the category was deleted.

        Raises:
            ValueError: If the category does not exist.
        """
        category = self.category_repo.get_by_id(category_id)
        if category is None:
            raise ValueError(f"Category with ID {category_id} not found.")

        return self.category_repo.delete(category_id)

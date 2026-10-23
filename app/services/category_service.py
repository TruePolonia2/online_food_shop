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

    def get_all(self) -> list[Category]:
        """Retrieve all categories sorted alphabetically.

        Returns:
            A list of Category entities.
        """
        return self.category_repo.get_all()

    def create_category(self, name: str, description: str | None = None) -> Category:
        """Create a new category after validating business rules.

        Args:
            name: The category name. Must be non-empty and unique.
            description: Optional category description.

        Returns:
            The persisted Category entity with assigned ID.

        Raises:
            ValueError: If the name is empty or already in use.
        """
        if not name or not name.strip():
            raise ValueError("Category name cannot be empty.")

        cleaned_name = name.strip()
        if self.category_repo.get_by_name(cleaned_name) is not None:
            raise ValueError(f"Category with name '{cleaned_name}' already exists.")

        cleaned_desc = description.strip() if description else None
        category = Category(name=cleaned_name, description=cleaned_desc)
        return self.category_repo.create(category)

    def update_category(
        self, category_id: int, name: str, description: str | None = None
    ) -> Category:
        """Update an existing category after validating business rules.

        Args:
            category_id: The ID of the category to update.
            name: The new category name. Must be non-empty and unique.
            description: Optional updated description.

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

        cleaned_name = name.strip()
        existing = self.category_repo.get_by_name(cleaned_name)
        if existing is not None and existing.id != category_id:
            raise ValueError(f"Category with name '{cleaned_name}' already exists.")

        category.name = cleaned_name
        category.description = description.strip() if description else None
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

"""Category repository for SQLite database access."""
from typing import Optional
import sqlite3

from app.database.connection import get_db, transaction
from app.models.category import Category


def _row_to_category(row: sqlite3.Row) -> Category:
    """Convert an SQLite row into a Category domain model."""
    is_active = bool(row["is_active"]) if "is_active" in row.keys() else True
    return Category(
        id=row["id"],
        name=row["name"],
        description=row["description"],
        is_active=is_active,
    )


class CategoryRepository:
    """Repository managing persistence for Category entities."""

    def __init__(self, conn: Optional[sqlite3.Connection] = None) -> None:
        """Initialize the repository.

        Args:
            conn: Optional database connection. If omitted, uses request-scoped get_db().
        """
        self._conn = conn

    @property
    def conn(self) -> sqlite3.Connection:
        """Get the active database connection."""
        if self._conn is not None:
            return self._conn
        return get_db()

    def get_by_id(self, category_id: int) -> Optional[Category]:
        """Retrieve a category by its primary key ID.

        Args:
            category_id: The ID of the category.

        Returns:
            The Category entity if found, None otherwise.
        """
        cursor = self.conn.execute(
            "SELECT id, name, description, is_active FROM categories WHERE id = ?",
            (category_id,),
        )
        row = cursor.fetchone()
        return _row_to_category(row) if row else None

    def get_by_name(self, name: str) -> Optional[Category]:
        """Retrieve a category by its unique name.

        Args:
            name: The category name.

        Returns:
            The Category entity if found, None otherwise.
        """
        cursor = self.conn.execute(
            "SELECT id, name, description, is_active FROM categories WHERE name = ?",
            (name.strip(),),
        )
        row = cursor.fetchone()
        return _row_to_category(row) if row else None

    def get_all(self, include_inactive: bool = False) -> list[Category]:
        """Retrieve categories sorted alphabetically by name.

        Args:
            include_inactive: Whether to include inactive categories. Defaults to False.

        Returns:
            A list of Category entities.
        """
        if include_inactive:
            query = "SELECT id, name, description, is_active FROM categories ORDER BY name ASC"
        else:
            query = (
                "SELECT id, name, description, is_active FROM categories "
                "WHERE is_active = 1 ORDER BY name ASC"
            )
        cursor = self.conn.execute(query)
        return [_row_to_category(row) for row in cursor.fetchall()]

    def create(self, category: Category) -> Category:
        """Insert a new category into the database.

        Args:
            category: The Category entity to persist.

        Returns:
            The persisted Category entity with its assigned ID.
        """
        with transaction(self.conn):
            cursor = self.conn.execute(
                "INSERT INTO categories (name, description, is_active) VALUES (?, ?, ?)",
                (category.name, category.description, 1 if category.is_active else 0),
            )
            category.id = cursor.lastrowid
            return category

    def update(self, category: Category) -> bool:
        """Update an existing category.

        Args:
            category: The Category entity with updated attributes and a valid ID.

        Returns:
            True if a category was updated, False if no matching record was found.
        """
        if category.id is None:
            raise ValueError("Category ID cannot be None when updating.")

        with transaction(self.conn):
            cursor = self.conn.execute(
                "UPDATE categories SET name = ?, description = ?, is_active = ? WHERE id = ?",
                (
                    category.name,
                    category.description,
                    1 if category.is_active else 0,
                    category.id,
                ),
            )
            return cursor.rowcount > 0

    def delete(self, category_id: int) -> bool:
        """Delete a category by its ID.

        Note: Foreign key constraints on products will set product.category_id to NULL.

        Args:
            category_id: The ID of the category to delete.

        Returns:
            True if a category was deleted, False if not found.
        """
        with transaction(self.conn):
            cursor = self.conn.execute(
                "DELETE FROM categories WHERE id = ?",
                (category_id,),
            )
            return cursor.rowcount > 0

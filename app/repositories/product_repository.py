"""Product repository for SQLite database access."""
from typing import Optional
import sqlite3

from app.database.connection import get_db, transaction
from app.models.product import Product


def _row_to_product(row: sqlite3.Row) -> Product:
    """Convert an SQLite row into a Product domain model."""
    image_url = row["image_url"] if "image_url" in row.keys() else None
    return Product(
        id=row["id"],
        name=row["name"],
        price=row["price"],
        stock_quantity=row["stock_quantity"],
        description=row["description"],
        category_id=row["category_id"],
        is_active=bool(row["is_active"]),
        image_url=image_url,
    )


class ProductRepository:
    """Repository managing persistence for Product entities."""

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

    def get_by_id(self, product_id: int) -> Optional[Product]:
        """Retrieve a product by its primary key ID.

        Args:
            product_id: The ID of the product.

        Returns:
            The Product entity if found, None otherwise.
        """
        cursor = self.conn.execute(
            """SELECT id, name, price, stock_quantity, description, category_id, is_active, image_url
               FROM products WHERE id = ?""",
            (product_id,),
        )
        row = cursor.fetchone()
        return _row_to_product(row) if row else None

    def get_all(self, include_inactive: bool = False) -> list[Product]:
        """Retrieve products from the catalog.

        Args:
            include_inactive: When True, returns both active and inactive products.
                Defaults to False (only active products in active categories).

        Returns:
            A list of Product entities sorted alphabetically by name.
        """
        if include_inactive:
            query = """SELECT p.id, p.name, p.price, p.stock_quantity, p.description, p.category_id, p.is_active, p.image_url
                       FROM products p ORDER BY p.name ASC"""
            cursor = self.conn.execute(query)
        else:
            query = """SELECT p.id, p.name, p.price, p.stock_quantity, p.description, p.category_id, p.is_active, p.image_url
                       FROM products p
                       LEFT JOIN categories c ON p.category_id = c.id
                       WHERE p.is_active = 1 AND (p.category_id IS NULL OR c.is_active = 1)
                       ORDER BY p.name ASC"""
            cursor = self.conn.execute(query)

        return [_row_to_product(row) for row in cursor.fetchall()]

    def list_active(self) -> list[Product]:
        """Convenience method to retrieve all active catalog products.

        Returns:
            A list of active Product entities sorted by name.
        """
        return self.get_all(include_inactive=False)

    def get_by_category_id(
        self, category_id: int, include_inactive: bool = False
    ) -> list[Product]:
        """Retrieve products within a specific category.

        Args:
            category_id: The category identifier.
            include_inactive: Whether to include inactive products. Defaults to False.

        Returns:
            A list of matching Product entities sorted by name.
        """
        if include_inactive:
            query = """SELECT p.id, p.name, p.price, p.stock_quantity, p.description, p.category_id, p.is_active, p.image_url
                       FROM products p WHERE p.category_id = ? ORDER BY p.name ASC"""
            cursor = self.conn.execute(query, (category_id,))
        else:
            query = """SELECT p.id, p.name, p.price, p.stock_quantity, p.description, p.category_id, p.is_active, p.image_url
                       FROM products p
                       JOIN categories c ON p.category_id = c.id
                       WHERE p.category_id = ? AND p.is_active = 1 AND c.is_active = 1
                       ORDER BY p.name ASC"""
            cursor = self.conn.execute(query, (category_id,))

        return [_row_to_product(row) for row in cursor.fetchall()]

    def create(self, product: Product) -> Product:
        """Insert a new product into the database.

        Args:
            product: The Product entity to persist.

        Returns:
            The persisted Product entity with its newly generated ID.
        """
        with transaction(self.conn):
            cursor = self.conn.execute(
                """INSERT INTO products (name, price, stock_quantity, description, category_id, is_active, image_url)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    product.name,
                    product.price,
                    product.stock_quantity,
                    product.description,
                    product.category_id,
                    1 if product.is_active else 0,
                    product.image_url,
                ),
            )
            product.id = cursor.lastrowid
            return product

    def update(self, product: Product) -> bool:
        """Update an existing product's attributes in the database.

        Args:
            product: The Product entity with modified fields and a valid ID.

        Returns:
            True if a product was updated, False if no matching record was found.
        """
        if product.id is None:
            raise ValueError("Product ID cannot be None when updating.")

        with transaction(self.conn):
            cursor = self.conn.execute(
                """UPDATE products
                   SET name = ?, price = ?, stock_quantity = ?, description = ?,
                       category_id = ?, is_active = ?, image_url = ?
                   WHERE id = ?""",
                (
                    product.name,
                    product.price,
                    product.stock_quantity,
                    product.description,
                    product.category_id,
                    1 if product.is_active else 0,
                    product.image_url,
                    product.id,
                ),
            )
            return cursor.rowcount > 0

    def delete(self, product_id: int) -> bool:
        """Delete a product by its ID.

        Note: If the product is referenced in existing order items, a sqlite3.IntegrityError
        will be raised due to the foreign key RESTRICT constraint.

        Args:
            product_id: The ID of the product to delete.

        Returns:
            True if the product was deleted, False if not found.
        """
        with transaction(self.conn):
            cursor = self.conn.execute(
                "DELETE FROM products WHERE id = ?",
                (product_id,),
            )
            return cursor.rowcount > 0

    def get_product_stats(self) -> dict[str, int]:
        """Retrieve aggregate product counts via an optimized SQL query.

        Returns:
            Dictionary with 'total_products' and 'active_products'.
        """
        cursor = self.conn.execute(
            """SELECT COUNT(*) AS total,
                      COALESCE(SUM(CASE WHEN is_active = 1 THEN 1 ELSE 0 END), 0) AS active
               FROM products"""
        )
        row = cursor.fetchone()
        if not row:
            return {"total_products": 0, "active_products": 0}
        return {
            "total_products": int(row["total"]),
            "active_products": int(row["active"]),
        }

"""Customer repository for SQLite database access."""
from typing import Optional
import sqlite3

from app.database.connection import get_db, transaction
from app.models.customer import Customer


def _row_to_customer(row: sqlite3.Row) -> Customer:
    """Convert an SQLite row into a Customer domain model."""
    return Customer(
        id=row["id"],
        name=row["name"],
        email=row["email"],
        phone=row["phone"],
        address=row["address"],
    )


class CustomerRepository:
    """Repository managing persistence for Customer entities."""

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

    def get_by_id(self, customer_id: int) -> Optional[Customer]:
        """Retrieve a customer by their primary key ID.

        Args:
            customer_id: The customer's ID.

        Returns:
            The Customer entity if found, None otherwise.
        """
        cursor = self.conn.execute(
            "SELECT id, name, email, phone, address FROM customers WHERE id = ?",
            (customer_id,),
        )
        row = cursor.fetchone()
        return _row_to_customer(row) if row else None

    def get_by_email(self, email: str) -> Optional[Customer]:
        """Retrieve a customer by their email address (case-insensitive).

        Args:
            email: The email address to look up.

        Returns:
            The Customer entity if found, None otherwise.
        """
        cursor = self.conn.execute(
            "SELECT id, name, email, phone, address FROM customers WHERE lower(email) = lower(?)",
            (email.strip(),),
        )
        row = cursor.fetchone()
        return _row_to_customer(row) if row else None

    def get_all(self) -> list[Customer]:
        """Retrieve all customers sorted alphabetically by name.

        Returns:
            A list of Customer entities.
        """
        cursor = self.conn.execute(
            "SELECT id, name, email, phone, address FROM customers ORDER BY name ASC"
        )
        return [_row_to_customer(row) for row in cursor.fetchall()]

    def create(self, customer: Customer) -> Customer:
        """Insert a new customer into the database.

        Args:
            customer: The Customer entity to persist.

        Returns:
            The persisted Customer entity with its generated ID.
        """
        with transaction(self.conn):
            cursor = self.conn.execute(
                "INSERT INTO customers (name, email, phone, address) VALUES (?, ?, ?, ?)",
                (customer.name, customer.email, customer.phone, customer.address),
            )
            customer.id = cursor.lastrowid
            return customer

    def update(self, customer: Customer) -> bool:
        """Update an existing customer's information.

        Args:
            customer: The Customer entity with modified fields and a valid ID.

        Returns:
            True if a customer was updated, False if not found.
        """
        if customer.id is None:
            raise ValueError("Customer ID cannot be None when updating.")

        with transaction(self.conn):
            cursor = self.conn.execute(
                "UPDATE customers SET name = ?, email = ?, phone = ?, address = ? WHERE id = ?",
                (customer.name, customer.email, customer.phone, customer.address, customer.id),
            )
            return cursor.rowcount > 0

    def delete(self, customer_id: int) -> bool:
        """Delete a customer by their ID.

        Note: If the customer has existing orders, a sqlite3.IntegrityError will be raised
        due to the foreign key RESTRICT constraint on orders.

        Args:
            customer_id: The ID of the customer to delete.

        Returns:
            True if the customer was deleted, False if not found.
        """
        with transaction(self.conn):
            cursor = self.conn.execute(
                "DELETE FROM customers WHERE id = ?",
                (customer_id,),
            )
            return cursor.rowcount > 0

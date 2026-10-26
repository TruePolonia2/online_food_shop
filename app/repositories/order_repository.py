"""Order repository for SQLite database access."""
from datetime import datetime, timezone
from typing import Optional, Union
import sqlite3

from app.database.connection import get_db, transaction
from app.models.order import Order, OrderStatus
from app.models.order_item import OrderItem


def _row_to_order_item(row: sqlite3.Row) -> OrderItem:
    """Convert an SQLite row into an OrderItem domain model."""
    return OrderItem(
        id=row["id"],
        order_id=row["order_id"],
        product_id=row["product_id"],
        product_name=row["product_name"],
        unit_price=row["unit_price"],
        quantity=row["quantity"],
    )


def _row_to_order(row: sqlite3.Row, items: Optional[list[OrderItem]] = None) -> Order:
    """Convert an SQLite row into an Order domain model with its line items."""
    created_at_val = row["created_at"]
    if isinstance(created_at_val, str):
        created_at = datetime.fromisoformat(created_at_val)
    elif isinstance(created_at_val, datetime):
        created_at = created_at_val
    else:
        created_at = datetime.now(timezone.utc)

    row_keys = row.keys() if hasattr(row, "keys") else ()
    customer_name = row["customer_name"] if "customer_name" in row_keys else None
    customer_email = row["customer_email"] if "customer_email" in row_keys else None
    customer_phone = row["customer_phone"] if "customer_phone" in row_keys else None
    customer_address = row["customer_address"] if "customer_address" in row_keys else None
    user_id = row["user_id"] if "user_id" in row_keys else None

    return Order(
        id=row["id"],
        customer_id=row["customer_id"],
        status=OrderStatus(row["status"]),
        created_at=created_at,
        items=items if items is not None else [],
        customer_name=customer_name,
        customer_email=customer_email,
        customer_phone=customer_phone,
        customer_address=customer_address,
        user_id=user_id,
    )


class OrderRepository:
    """Repository managing persistence for Order and OrderItem entities."""

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

    def get_by_id(self, order_id: int) -> Optional[Order]:
        """Retrieve an order by its ID, eagerly loading its order items.

        Args:
            order_id: The primary key ID of the order.

        Returns:
            The Order entity with its items populated, or None if not found.
        """
        cursor = self.conn.execute(
            """SELECT id, customer_id, user_id, status, created_at,
                      customer_name, customer_email, customer_phone, customer_address
               FROM orders WHERE id = ?""",
            (order_id,),
        )
        order_row = cursor.fetchone()
        if not order_row:
            return None

        items_cursor = self.conn.execute(
            """SELECT id, order_id, product_id, product_name, unit_price, quantity
               FROM order_items WHERE order_id = ? ORDER BY id ASC""",
            (order_id,),
        )
        items = [_row_to_order_item(r) for r in items_cursor.fetchall()]
        return _row_to_order(order_row, items)

    def get_by_customer_id(self, customer_id: int) -> list[Order]:
        """Retrieve all orders placed by a specific customer, ordered by newest first.

        Args:
            customer_id: The ID of the customer.

        Returns:
            A list of Order entities with their items populated.
        """
        cursor = self.conn.execute(
            """SELECT id, customer_id, user_id, status, created_at,
                      customer_name, customer_email, customer_phone, customer_address
               FROM orders WHERE customer_id = ? ORDER BY created_at DESC, id DESC""",
            (customer_id,),
        )
        order_rows = cursor.fetchall()
        if not order_rows:
            return []

        return self._populate_orders_with_items(order_rows)

    def get_by_user_id(self, user_id: int) -> list[Order]:
        """Retrieve all orders placed by an authenticated user account, ordered by newest first.

        Args:
            user_id: The ID of the authenticated user.

        Returns:
            A list of Order entities with their items populated.
        """
        cursor = self.conn.execute(
            """SELECT id, customer_id, user_id, status, created_at,
                      customer_name, customer_email, customer_phone, customer_address
               FROM orders WHERE user_id = ? ORDER BY created_at DESC, id DESC""",
            (user_id,),
        )
        order_rows = cursor.fetchall()
        if not order_rows:
            return []

        return self._populate_orders_with_items(order_rows)

    def get_all(self) -> list[Order]:
        """Retrieve all orders in the system, ordered by newest first.

        Returns:
            A list of Order entities with their items populated.
        """
        cursor = self.conn.execute(
            """SELECT id, customer_id, user_id, status, created_at,
                      customer_name, customer_email, customer_phone, customer_address
               FROM orders ORDER BY created_at DESC, id DESC"""
        )
        order_rows = cursor.fetchall()
        if not order_rows:
            return []

        return self._populate_orders_with_items(order_rows)

    def _populate_orders_with_items(self, order_rows: list[sqlite3.Row]) -> list[Order]:
        """Helper to batch-load order items for multiple orders avoiding N+1 queries."""
        order_ids = [row["id"] for row in order_rows]
        placeholders = ",".join("?" for _ in order_ids)

        items_cursor = self.conn.execute(
            f"""SELECT id, order_id, product_id, product_name, unit_price, quantity
                FROM order_items WHERE order_id IN ({placeholders}) ORDER BY id ASC""",
            order_ids,
        )
        items_by_order: dict[int, list[OrderItem]] = {oid: [] for oid in order_ids}
        for item_row in items_cursor.fetchall():
            item = _row_to_order_item(item_row)
            if item.order_id in items_by_order:
                items_by_order[item.order_id].append(item)

        return [_row_to_order(row, items_by_order.get(row["id"], [])) for row in order_rows]

    def create(self, order: Order) -> Order:
        """Create an order and all its line items atomically in a single transaction.

        Preserves historical purchase unit prices snapshot in OrderItem.unit_price.
        Rolls back all changes if any insertion fails.

        Args:
            order: The Order entity to persist.

        Returns:
            The persisted Order entity with updated order and item IDs.
        """
        with transaction(self.conn):
            # Format timestamp to ISO 8601 UTC string format: 'YYYY-MM-DDTHH:MM:SSZ'
            if order.created_at:
                dt = order.created_at
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                else:
                    dt = dt.astimezone(timezone.utc)
                created_at_str = dt.strftime("%Y-%m-%dT%H:%M:%SZ")
            else:
                created_at_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

            status_str = (
                order.status.value
                if isinstance(order.status, OrderStatus)
                else str(order.status)
            )

            cursor = self.conn.execute(
                """INSERT INTO orders (
                       customer_id, user_id, status, created_at,
                       customer_name, customer_email, customer_phone, customer_address
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    order.customer_id,
                    order.user_id,
                    status_str,
                    created_at_str,
                    order.customer_name,
                    order.customer_email,
                    order.customer_phone,
                    order.customer_address,
                ),
            )
            order.id = cursor.lastrowid

            for item in order.items:
                item.order_id = order.id
                item_cursor = self.conn.execute(
                    """INSERT INTO order_items (order_id, product_id, product_name, unit_price, quantity)
                       VALUES (?, ?, ?, ?, ?)""",
                    (order.id, item.product_id, item.product_name, item.unit_price, item.quantity),
                )
                item.id = item_cursor.lastrowid

            return order

    def update_status(self, order_id: int, status: Union[OrderStatus, str]) -> bool:
        """Update the lifecycle status of an order.

        Args:
            order_id: The ID of the order to update.
            status: The new OrderStatus enum value or string representation.

        Returns:
            True if the order status was updated, False if the order was not found.

        Raises:
            ValueError: If status is not a valid OrderStatus value.
        """
        status_val = status.value if isinstance(status, OrderStatus) else str(status)
        # Validate that status is an acceptable OrderStatus enum value
        OrderStatus(status_val)

        with transaction(self.conn):
            cursor = self.conn.execute(
                "UPDATE orders SET status = ? WHERE id = ?",
                (status_val, order_id),
            )
            return cursor.rowcount > 0

    def delete(self, order_id: int) -> bool:
        """Delete an order by its ID.

        Note: Line items will be automatically cascade-deleted due to the
        foreign key ON DELETE CASCADE constraint on order_items.

        Args:
            order_id: The ID of the order to delete.

        Returns:
            True if the order was deleted, False if not found.
        """
        with transaction(self.conn):
            cursor = self.conn.execute(
                "DELETE FROM orders WHERE id = ?",
                (order_id,),
            )
            return cursor.rowcount > 0

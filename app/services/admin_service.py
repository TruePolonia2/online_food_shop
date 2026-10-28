"""Admin service coordinating dashboard metrics and administrative domain logic."""
import sqlite3
from typing import Any, Optional

from app.database.connection import get_db
from app.models.contact_message import ContactMessageStatus
from app.repositories.contact_message_repository import ContactMessageRepository
from app.repositories.order_repository import OrderRepository
from app.repositories.product_repository import ProductRepository


class AdminService:
    """Service providing aggregate metrics and administrative reporting."""

    def __init__(
        self,
        order_repo: Optional[OrderRepository] = None,
        product_repo: Optional[ProductRepository] = None,
        contact_repo: Optional[ContactMessageRepository] = None,
        conn: Optional[sqlite3.Connection] = None,
    ) -> None:
        """Initialize AdminService with repositories.

        Args:
            order_repo: Optional OrderRepository instance.
            product_repo: Optional ProductRepository instance.
            contact_repo: Optional ContactMessageRepository instance.
            conn: Optional SQLite connection.
        """
        active_conn = (
            conn
            or getattr(order_repo, "conn", None)
            or getattr(product_repo, "conn", None)
            or getattr(contact_repo, "conn", None)
        )
        self._conn = active_conn
        self.order_repo = order_repo or OrderRepository(active_conn)
        self.product_repo = product_repo or ProductRepository(active_conn)
        self.contact_repo = contact_repo or ContactMessageRepository(active_conn)

    @property
    def conn(self) -> sqlite3.Connection:
        """Get the active database connection."""
        if self._conn is not None:
            return self._conn
        return get_db()

    def get_dashboard_stats(self) -> dict[str, Any]:
        """Aggregate store statistics for the administration dashboard.

        Returns:
            Dictionary with aggregated product metrics, order metrics, status breakdown,
            and total recorded order value in integer cents.
        """
        product_stats = self.product_repo.get_product_stats()
        order_stats = self.order_repo.get_order_stats()
        new_messages_count = self.contact_repo.count_by_status(ContactMessageStatus.NEW.value)
        total_contact_messages = self.contact_repo.count()

        return {
            "total_products": product_stats["total_products"],
            "active_products": product_stats["active_products"],
            "total_orders": order_stats["total_orders"],
            "status_counts": order_stats["status_counts"],
            "total_recorded_value": order_stats["total_recorded_value"],
            "new_messages_count": new_messages_count,
            "total_contact_messages": total_contact_messages,
        }

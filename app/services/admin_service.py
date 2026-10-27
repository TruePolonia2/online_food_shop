"""Admin service coordinating dashboard metrics and administrative domain logic."""
import sqlite3
from typing import Any, Optional

from app.database.connection import get_db
from app.repositories.order_repository import OrderRepository
from app.repositories.product_repository import ProductRepository


class AdminService:
    """Service providing aggregate metrics and administrative reporting."""

    def __init__(
        self,
        order_repo: Optional[OrderRepository] = None,
        product_repo: Optional[ProductRepository] = None,
        conn: Optional[sqlite3.Connection] = None,
    ) -> None:
        """Initialize AdminService with repositories.

        Args:
            order_repo: Optional OrderRepository instance.
            product_repo: Optional ProductRepository instance.
            conn: Optional SQLite connection.
        """
        self.order_repo = order_repo or OrderRepository(conn)
        self.product_repo = product_repo or ProductRepository(conn)
        self._conn = conn

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

        return {
            "total_products": product_stats["total_products"],
            "active_products": product_stats["active_products"],
            "total_orders": order_stats["total_orders"],
            "status_counts": order_stats["status_counts"],
            "total_recorded_value": order_stats["total_recorded_value"],
        }

"""Tests for Phase 10.8: Secure Owner Order Management."""
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from app import create_app
from app.config import Config
from app.database.connection import create_connection, init_db
from app.models.cart import Cart
from app.models.category import Category
from app.models.customer import Customer
from app.models.order import Order, OrderStatus
from app.models.order_item import OrderItem
from app.models.product import Product
from app.models.user import User, UserRole
from app.repositories.category_repository import CategoryRepository
from app.repositories.customer_repository import CustomerRepository
from app.repositories.order_repository import OrderRepository
from app.repositories.product_repository import ProductRepository
from app.repositories.user_repository import UserRepository
from app.services.auth_service import AuthService
from app.services.order_service import (
    ALLOWED_ADMIN_TRANSITIONS,
    OrderConflictError,
    OrderService,
)


class BaseAdminOrderTestCase(unittest.TestCase):
    """Base setup for admin order management testing."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_admin_orders.sqlite3"

        class TestOrderAdminConfig(Config):
            DATABASE_PATH = str(self.db_path)
            TESTING = True
            SECRET_KEY = "test-order-admin-secret"
            CSRF_ENABLED = False

        self.app = create_app(TestOrderAdminConfig)
        self.client = self.app.test_client()

        with self.app.app_context():
            init_db()
            self.user_repo = UserRepository()
            self.category_repo = CategoryRepository()
            self.product_repo = ProductRepository()
            self.customer_repo = CustomerRepository()
            self.order_repo = OrderRepository()
            self.order_service = OrderService()

            pwd_hash = AuthService.hash_password("Password123!")
            self.verified_owner = self.user_repo.create(
                User(
                    email="owner@example.com",
                    password_hash=pwd_hash,
                    role=UserRole.OWNER,
                    is_verified=True,
                )
            )
            self.unverified_owner = self.user_repo.create(
                User(
                    email="unverified_owner@example.com",
                    password_hash=pwd_hash,
                    role=UserRole.OWNER,
                    is_verified=False,
                )
            )
            self.customer_user = self.user_repo.create(
                User(
                    email="customer@example.com",
                    password_hash=pwd_hash,
                    role=UserRole.CUSTOMER,
                    is_verified=True,
                )
            )
            self.other_customer = self.user_repo.create(
                User(
                    email="other@example.com",
                    password_hash=pwd_hash,
                    role=UserRole.CUSTOMER,
                    is_verified=True,
                )
            )

            # Seed sample catalog items
            self.cat_produce = self.category_repo.create(
                Category(name="Produce", description="Fresh produce")
            )
            self.prod_apple = self.product_repo.create(
                Product(
                    name="Organic Apples",
                    price=250,
                    stock_quantity=50,
                    category_id=self.cat_produce.id,
                    is_active=True,
                )
            )
            self.prod_banana = self.product_repo.create(
                Product(
                    name="Fairtrade Bananas",
                    price=180,
                    stock_quantity=40,
                    category_id=self.cat_produce.id,
                    is_active=True,
                )
            )

            # Seed customers
            self.cust_guest = self.customer_repo.create(
                Customer(
                    name="Guest Buyer",
                    email="guest@example.com",
                    phone="555-0100",
                    address="123 Guest Lane",
                )
            )
            self.cust_registered = self.customer_repo.create(
                Customer(
                    name="Registered Customer",
                    email="customer@example.com",
                    phone="555-0200",
                    address="456 Member Way",
                )
            )

            # Seed sample orders with snapshots
            # Order 1: Guest order, pending
            self.order_pending = self.order_repo.create(
                Order(
                    customer_id=self.cust_guest.id,
                    user_id=None,
                    status=OrderStatus.PENDING,
                    customer_name="Guest Buyer",
                    customer_email="guest@example.com",
                    customer_phone="555-0100",
                    customer_address="123 Guest Lane",
                    items=[
                        OrderItem(
                            product_id=self.prod_apple.id,
                            product_name="Organic Apples",
                            unit_price=250,
                            quantity=2,
                        ),
                        OrderItem(
                            product_id=self.prod_banana.id,
                            product_name="Fairtrade Bananas",
                            unit_price=180,
                            quantity=1,
                        ),
                    ],
                )
            )

            # Order 2: Registered customer order, confirmed
            self.order_confirmed = self.order_repo.create(
                Order(
                    customer_id=self.cust_registered.id,
                    user_id=self.customer_user.id,
                    status=OrderStatus.CONFIRMED,
                    customer_name="Registered Customer",
                    customer_email="customer@example.com",
                    customer_phone="555-0200",
                    customer_address="456 Member Way",
                    items=[
                        OrderItem(
                            product_id=self.prod_apple.id,
                            product_name="Organic Apples",
                            unit_price=250,
                            quantity=4,
                        ),
                    ],
                )
            )

            # Order 3: Paid order
            self.order_paid = self.order_repo.create(
                Order(
                    customer_id=self.cust_registered.id,
                    user_id=self.customer_user.id,
                    status=OrderStatus.PAID,
                    customer_name="Registered Customer",
                    customer_email="customer@example.com",
                    customer_phone="555-0200",
                    customer_address="456 Member Way",
                    items=[
                        OrderItem(
                            product_id=self.prod_banana.id,
                            product_name="Fairtrade Bananas",
                            unit_price=180,
                            quantity=3,
                        ),
                    ],
                )
            )

            # Order 4: Cancelled order
            self.order_cancelled = self.order_repo.create(
                Order(
                    customer_id=self.cust_guest.id,
                    user_id=None,
                    status=OrderStatus.CANCELLED,
                    customer_name="Guest Buyer",
                    customer_email="guest@example.com",
                    customer_phone="555-0100",
                    customer_address="123 Guest Lane",
                    items=[
                        OrderItem(
                            product_id=self.prod_apple.id,
                            product_name="Organic Apples",
                            unit_price=250,
                            quantity=1,
                        ),
                    ],
                )
            )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def login_as(self, user: User) -> None:
        """Helper to set authenticated user in session."""
        with self.client.session_transaction() as sess:
            sess["user_id"] = user.id


class TestAdminOrderAuthorization(BaseAdminOrderTestCase):
    """Authorization verification across administrative order management endpoints."""

    def test_anonymous_access_redirects_to_login(self) -> None:
        endpoints = [
            "/admin/orders",
            f"/admin/orders/{self.order_pending.id}",
        ]
        for ep in endpoints:
            res = self.client.get(ep)
            self.assertEqual(res.status_code, 302)
            self.assertIn("/login", res.headers.get("Location", ""))

        post_res = self.client.post(
            f"/admin/orders/{self.order_pending.id}/status",
            data={"current_status": "pending", "new_status": "confirmed"},
        )
        self.assertEqual(post_res.status_code, 302)
        self.assertIn("/login", post_res.headers.get("Location", ""))

    def test_customer_access_returns_403(self) -> None:
        self.login_as(self.customer_user)

        endpoints = [
            "/admin/orders",
            f"/admin/orders/{self.order_pending.id}",
        ]
        for ep in endpoints:
            res = self.client.get(ep)
            self.assertEqual(res.status_code, 403)

        post_res = self.client.post(
            f"/admin/orders/{self.order_pending.id}/status",
            data={"current_status": "pending", "new_status": "confirmed"},
        )
        self.assertEqual(post_res.status_code, 403)

    def test_unverified_owner_access_returns_403(self) -> None:
        self.login_as(self.unverified_owner)

        res = self.client.get("/admin/orders")
        self.assertEqual(res.status_code, 403)

        res_detail = self.client.get(f"/admin/orders/{self.order_pending.id}")
        self.assertEqual(res_detail.status_code, 403)

        post_res = self.client.post(
            f"/admin/orders/{self.order_pending.id}/status",
            data={"current_status": "pending", "new_status": "confirmed"},
        )
        self.assertEqual(post_res.status_code, 403)

    def test_verified_owner_access_succeeds(self) -> None:
        self.login_as(self.verified_owner)

        res = self.client.get("/admin/orders")
        self.assertEqual(res.status_code, 200)
        self.assertIn(b"Order Management", res.data)

        res_detail = self.client.get(f"/admin/orders/{self.order_pending.id}")
        self.assertEqual(res_detail.status_code, 200)
        self.assertIn(b"Order Lifecycle & Status Management", res_detail.data)

    def test_forged_session_role_tampering_denied(self) -> None:
        """Forged session claims must not grant owner privileges."""
        with self.client.session_transaction() as sess:
            sess["user_id"] = self.customer_user.id
            sess["role"] = "owner"
            sess["is_verified"] = True

        res = self.client.get("/admin/orders")
        self.assertEqual(res.status_code, 403)

    def test_deleted_or_demoted_owner_loses_access(self) -> None:
        """Owner removed from database loses access immediately."""
        with self.app.app_context():
            deleted_user = self.user_repo.create(
                User(
                    email="temp_owner@example.com",
                    password_hash=AuthService.hash_password("Password123!"),
                    role=UserRole.OWNER,
                    is_verified=True,
                )
            )
            temp_id = deleted_user.id

        self.login_as(deleted_user)
        res = self.client.get("/admin/orders")
        self.assertEqual(res.status_code, 200)

        # Delete owner
        with self.app.app_context():
            self.user_repo.delete(temp_id)

        # Stale session identity is rejected
        res_after = self.client.get("/admin/orders")
        self.assertEqual(res_after.status_code, 302)
        self.assertIn("/login", res_after.headers.get("Location", ""))

    def test_nonexistent_order_returns_404(self) -> None:
        self.login_as(self.verified_owner)

        res = self.client.get("/admin/orders/99999")
        self.assertEqual(res.status_code, 404)

        post_res = self.client.post(
            "/admin/orders/99999/status",
            data={"current_status": "pending", "new_status": "confirmed"},
        )
        self.assertEqual(post_res.status_code, 404)


class TestAdminOrderListingAndDetails(BaseAdminOrderTestCase):
    """Functional tests for administrative listing, filtering, pagination, and details."""

    def test_empty_order_list(self) -> None:
        self.login_as(self.verified_owner)

        with self.app.app_context():
            # Clear all seeded orders
            conn = create_connection(str(self.db_path))
            conn.execute("DELETE FROM order_items")
            conn.execute("DELETE FROM orders")
            conn.commit()
            conn.close()

        res = self.client.get("/admin/orders")
        self.assertEqual(res.status_code, 200)
        self.assertIn(b"No Orders Found", res.data)
        self.assertIn(b"No customer orders have been placed", res.data)

    def test_newest_first_ordering(self) -> None:
        self.login_as(self.verified_owner)

        res = self.client.get("/admin/orders")
        self.assertEqual(res.status_code, 200)

        # Order of appearance should match newest first: order_cancelled, order_paid, order_confirmed, order_pending
        content = res.data.decode("utf-8")
        idx_cancelled = content.find(f"#{self.order_cancelled.id}")
        idx_paid = content.find(f"#{self.order_paid.id}")
        idx_confirmed = content.find(f"#{self.order_confirmed.id}")
        idx_pending = content.find(f"#{self.order_pending.id}")

        self.assertTrue(idx_cancelled < idx_paid < idx_confirmed < idx_pending)

    def test_status_filtering_valid(self) -> None:
        self.login_as(self.verified_owner)

        # Filter: pending
        res_pending = self.client.get("/admin/orders?status=pending")
        self.assertEqual(res_pending.status_code, 200)
        self.assertIn(f"#{self.order_pending.id}".encode(), res_pending.data)
        self.assertNotIn(f"#{self.order_confirmed.id}".encode(), res_pending.data)
        self.assertNotIn(f"#{self.order_paid.id}".encode(), res_pending.data)
        self.assertNotIn(f"#{self.order_cancelled.id}".encode(), res_pending.data)

        # Filter: confirmed
        res_confirmed = self.client.get("/admin/orders?status=confirmed")
        self.assertEqual(res_confirmed.status_code, 200)
        self.assertIn(f"#{self.order_confirmed.id}".encode(), res_confirmed.data)
        self.assertNotIn(f"#{self.order_pending.id}".encode(), res_confirmed.data)

    def test_status_filtering_invalid(self) -> None:
        """Invalid filter parameter safely ignored without error."""
        self.login_as(self.verified_owner)

        res = self.client.get("/admin/orders?status=invalid_status_xyz")
        self.assertEqual(res.status_code, 200)
        # Displays all orders safely
        self.assertIn(f"#{self.order_pending.id}".encode(), res.data)
        self.assertIn(f"#{self.order_confirmed.id}".encode(), res.data)

    def test_pagination_boundaries(self) -> None:
        self.login_as(self.verified_owner)

        # Create additional orders to reach > 20 orders
        with self.app.app_context():
            for i in range(25):
                self.order_repo.create(
                    Order(
                        customer_id=self.cust_guest.id,
                        status=OrderStatus.PENDING,
                        customer_name=f"Bulk Buyer {i}",
                        items=[
                            OrderItem(
                                product_id=self.prod_apple.id,
                                product_name="Organic Apples",
                                unit_price=250,
                                quantity=1,
                            )
                        ],
                    )
                )

        # Page 1: 20 orders
        res_p1 = self.client.get("/admin/orders?page=1")
        self.assertEqual(res_p1.status_code, 200)
        self.assertIn(b"Page ", res_p1.data)
        self.assertIn(b">1</span> of <span class=\"font-bold text-gray-900\">2</span>", res_p1.data)
        self.assertIn(b"Next", res_p1.data)

        # Page 2: remaining orders
        res_p2 = self.client.get("/admin/orders?page=2")
        self.assertEqual(res_p2.status_code, 200)
        self.assertIn(b">2</span> of <span class=\"font-bold text-gray-900\">2</span>", res_p2.data)
        self.assertIn(b"Previous", res_p2.data)

        # Invalid page values: gracefully default to page 1
        res_neg = self.client.get("/admin/orders?page=-5")
        self.assertEqual(res_neg.status_code, 200)

        res_str = self.client.get("/admin/orders?page=abc")
        self.assertEqual(res_str.status_code, 200)

    def test_guest_and_authenticated_orders_displayed(self) -> None:
        self.login_as(self.verified_owner)

        res = self.client.get("/admin/orders")
        self.assertEqual(res.status_code, 200)
        self.assertIn(b"Guest Checkout", res.data)
        self.assertIn(f"Registered (ID #{self.customer_user.id})".encode(), res.data)

    def test_order_detail_view_and_snapshots(self) -> None:
        self.login_as(self.verified_owner)

        res = self.client.get(f"/admin/orders/{self.order_pending.id}")
        self.assertEqual(res.status_code, 200)

        # Verify historical item snapshots and calculated totals
        self.assertIn(b"Organic Apples", res.data)
        self.assertIn(b"Fairtrade Bananas", res.data)
        # Total for order_pending: 2 * 250 + 1 * 180 = 680 cents = 6,80 €
        self.assertIn(b"6,80", res.data)

        # Delivery snapshot details
        self.assertIn(b"Guest Buyer", res.data)
        self.assertIn(b"guest@example.com", res.data)
        self.assertIn(b"555-0100", res.data)
        self.assertIn(b"123 Guest Lane", res.data)

    def test_html_escaping_in_delivery_snapshot(self) -> None:
        """User-controlled delivery data must be escaped against XSS."""
        self.login_as(self.verified_owner)

        with self.app.app_context():
            xss_order = self.order_repo.create(
                Order(
                    customer_id=self.cust_guest.id,
                    status=OrderStatus.PENDING,
                    customer_name="<script>alert('xss-name')</script>",
                    customer_email="xss@example.com",
                    customer_phone="<b>555</b>",
                    customer_address="<img src=x onerror=alert(1)>",
                    items=[
                        OrderItem(
                            product_id=self.prod_apple.id,
                            product_name="<script>alert('item')</script>",
                            unit_price=100,
                            quantity=1,
                        )
                    ],
                )
            )

        res = self.client.get(f"/admin/orders/{xss_order.id}")
        self.assertEqual(res.status_code, 200)
        self.assertNotIn(b"<script>alert('xss-name')</script>", res.data)
        self.assertIn(b"&lt;script&gt;alert(&#39;xss-name&#39;)&lt;/script&gt;", res.data)
        self.assertNotIn(b"<img src=x onerror=alert(1)>", res.data)

    def test_cache_headers_set_on_admin_order_pages(self) -> None:
        self.login_as(self.verified_owner)

        res_list = self.client.get("/admin/orders")
        self.assertIn("no-store", res_list.headers.get("Cache-Control", ""))
        self.assertIn("private", res_list.headers.get("Cache-Control", ""))

        res_detail = self.client.get(f"/admin/orders/{self.order_pending.id}")
        self.assertIn("no-store", res_detail.headers.get("Cache-Control", ""))
        self.assertIn("private", res_detail.headers.get("Cache-Control", ""))

    def test_customer_order_history_remains_ownership_scoped(self) -> None:
        """Customer accessing /account/orders only sees own orders."""
        self.login_as(self.customer_user)

        res = self.client.get("/account/orders")
        self.assertEqual(res.status_code, 200)
        # Should see their orders
        self.assertIn(f"#{self.order_confirmed.id}".encode(), res.data)
        self.assertIn(f"#{self.order_paid.id}".encode(), res.data)
        # Should NOT see guest order or other orders
        self.assertNotIn(f"#{self.order_pending.id}".encode(), res.data)
        self.assertNotIn(f"#{self.order_cancelled.id}".encode(), res.data)


class TestAdminOrderStatusTransitions(BaseAdminOrderTestCase):
    """Verification of strict lifecycle rules, atomic updates, and concurrency conflict handling."""

    def test_pending_to_confirmed_succeeds(self) -> None:
        self.login_as(self.verified_owner)

        res = self.client.post(
            f"/admin/orders/{self.order_pending.id}/status",
            data={"current_status": "pending", "new_status": "confirmed"},
        )
        self.assertEqual(res.status_code, 302)
        self.assertIn(f"/admin/orders/{self.order_pending.id}", res.headers.get("Location", ""))

        # Verify persisted status
        with self.app.app_context():
            updated = self.order_repo.get_by_id(self.order_pending.id)
            self.assertEqual(updated.status, OrderStatus.CONFIRMED)

    def test_pending_to_cancelled_succeeds(self) -> None:
        self.login_as(self.verified_owner)

        res = self.client.post(
            f"/admin/orders/{self.order_pending.id}/status",
            data={"current_status": "pending", "new_status": "cancelled"},
        )
        self.assertEqual(res.status_code, 302)

        with self.app.app_context():
            updated = self.order_repo.get_by_id(self.order_pending.id)
            self.assertEqual(updated.status, OrderStatus.CANCELLED)

    def test_confirmed_to_cancelled_succeeds(self) -> None:
        self.login_as(self.verified_owner)

        res = self.client.post(
            f"/admin/orders/{self.order_confirmed.id}/status",
            data={"current_status": "confirmed", "new_status": "cancelled"},
        )
        self.assertEqual(res.status_code, 302)

        with self.app.app_context():
            updated = self.order_repo.get_by_id(self.order_confirmed.id)
            self.assertEqual(updated.status, OrderStatus.CANCELLED)

    def test_invalid_and_forbidden_transitions_rejected(self) -> None:
        """Manual transitions to paid, shipped, delivered, or backward are forbidden."""
        self.login_as(self.verified_owner)

        forbidden_cases = [
            # from pending
            (self.order_pending.id, "pending", "paid"),
            (self.order_pending.id, "pending", "shipped"),
            (self.order_pending.id, "pending", "delivered"),
            (self.order_pending.id, "pending", "arbitrary_status"),
            # from confirmed
            (self.order_confirmed.id, "confirmed", "pending"),
            (self.order_confirmed.id, "confirmed", "paid"),
            (self.order_confirmed.id, "confirmed", "shipped"),
            (self.order_confirmed.id, "confirmed", "delivered"),
            # from paid
            (self.order_paid.id, "paid", "confirmed"),
            (self.order_paid.id, "paid", "cancelled"),
            # from cancelled (terminal)
            (self.order_cancelled.id, "cancelled", "pending"),
            (self.order_cancelled.id, "cancelled", "confirmed"),
        ]

        for order_id, current_st, target_st in forbidden_cases:
            res = self.client.post(
                f"/admin/orders/{order_id}/status",
                data={"current_status": current_st, "new_status": target_st},
            )
            self.assertEqual(res.status_code, 400, f"Expected 400 for {current_st} -> {target_st}")

    def test_stale_update_returns_409(self) -> None:
        """Stale form submission where DB status already changed returns HTTP 409."""
        self.login_as(self.verified_owner)

        # Suppose admin opened form while order was pending
        stale_status = "pending"

        # Concurrently, order is confirmed
        with self.app.app_context():
            self.order_repo.update_status(self.order_pending.id, OrderStatus.CONFIRMED)

        # Admin tries to submit pending -> cancelled with stale current_status
        res = self.client.post(
            f"/admin/orders/{self.order_pending.id}/status",
            data={"current_status": stale_status, "new_status": "cancelled"},
        )
        self.assertEqual(res.status_code, 409)
        self.assertIn(b"Conflict", res.data)

    def test_status_change_does_not_modify_stock_or_items(self) -> None:
        """Administrative status changes must not modify catalog inventory stock."""
        self.login_as(self.verified_owner)

        initial_apple_stock = self.prod_apple.stock_quantity
        initial_banana_stock = self.prod_banana.stock_quantity

        # Cancel pending order
        res = self.client.post(
            f"/admin/orders/{self.order_pending.id}/status",
            data={"current_status": "pending", "new_status": "cancelled"},
        )
        self.assertEqual(res.status_code, 302)

        # Stock must remain completely unchanged
        with self.app.app_context():
            apple = self.product_repo.get_by_id(self.prod_apple.id)
            banana = self.product_repo.get_by_id(self.prod_banana.id)
            self.assertEqual(apple.stock_quantity, initial_apple_stock)
            self.assertEqual(banana.stock_quantity, initial_banana_stock)


class TestAdminOrderCSRF(unittest.TestCase):
    """CSRF protection verification on administrative order status updates."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_admin_order_csrf.sqlite3"

        class TestCSRFConfig(Config):
            DATABASE_PATH = str(self.db_path)
            TESTING = True
            SECRET_KEY = "test-csrf-secret"
            CSRF_ENABLED = True

        self.app = create_app(TestCSRFConfig)
        self.client = self.app.test_client()

        with self.app.app_context():
            init_db()
            self.user_repo = UserRepository()
            self.customer_repo = CustomerRepository()
            self.order_repo = OrderRepository()

            pwd_hash = AuthService.hash_password("Password123!")
            self.owner = self.user_repo.create(
                User(
                    email="owner@example.com",
                    password_hash=pwd_hash,
                    role=UserRole.OWNER,
                    is_verified=True,
                )
            )
            cust = self.customer_repo.create(
                Customer(name="Buyer", email="b@example.com", phone="123", address="ABC")
            )
            self.order = self.order_repo.create(
                Order(customer_id=cust.id, status=OrderStatus.PENDING)
            )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_csrf_token_required_on_status_post(self) -> None:
        with self.client.session_transaction() as sess:
            sess["user_id"] = self.owner.id

        # Missing CSRF token returns 400
        res_no_csrf = self.client.post(
            f"/admin/orders/{self.order.id}/status",
            data={"current_status": "pending", "new_status": "confirmed"},
        )
        self.assertEqual(res_no_csrf.status_code, 400)

        # With valid CSRF token in session and form
        with self.client.session_transaction() as sess:
            sess["user_id"] = self.owner.id
            sess["_csrf_token"] = "valid-test-csrf-token"

        res_with_csrf = self.client.post(
            f"/admin/orders/{self.order.id}/status",
            data={
                "current_status": "pending",
                "new_status": "confirmed",
                "csrf_token": "valid-test-csrf-token",
            },
        )
        self.assertEqual(res_with_csrf.status_code, 302)


class TestAdminOrderRegressions(BaseAdminOrderTestCase):
    """End-to-end regression verification."""

    def test_dashboard_stats_remain_accurate(self) -> None:
        self.login_as(self.verified_owner)

        res = self.client.get("/admin/")
        self.assertEqual(res.status_code, 200)
        self.assertIn(b"Store Administration", res.data)
        # Dashboard should link to orders
        self.assertIn(b'id="btn-manage-orders"', res.data)
        self.assertIn(b"/admin/orders", res.data)

    def test_guest_checkout_remains_functional(self) -> None:
        """Guest checkout continues creating unassigned orders without errors."""
        res_add = self.client.post(
            "/cart/add",
            data={"product_id": self.prod_apple.id, "quantity": 1},
        )
        self.assertEqual(res_add.status_code, 302)

        res_checkout = self.client.post(
            "/checkout",
            data={
                "name": "New Guest",
                "email": "newguest@example.com",
                "phone": "555-9999",
                "address": "999 Guest Road",
            },
        )
        self.assertEqual(res_checkout.status_code, 302)

        with self.app.app_context():
            orders = self.order_repo.get_all()
            newest = orders[0]
            self.assertIsNone(newest.user_id)
            self.assertEqual(newest.customer_name, "New Guest")

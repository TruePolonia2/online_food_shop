"""Integration and security test suite for Phase 10.6 Owner Administration Dashboard."""
from pathlib import Path
import tempfile
import unittest

from app import create_app
from app.config import Config
from app.database.connection import init_db
from app.formatting import format_euro
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


class TestAdminDashboard(unittest.TestCase):
    """Comprehensive test suite for the Owner Administration Dashboard."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_admin.sqlite3"

        class TestAdminConfig(Config):
            DATABASE_PATH = str(self.db_path)
            TESTING = True
            SECRET_KEY = "test-admin-secret-key"
            CSRF_ENABLED = False

        self.app = create_app(TestAdminConfig)
        self.client = self.app.test_client()

        with self.app.app_context():
            init_db()
            self.user_repo = UserRepository()
            self.category_repo = CategoryRepository()
            self.product_repo = ProductRepository()
            self.customer_repo = CustomerRepository()
            self.order_repo = OrderRepository()

            # Seed Users
            pwd_hash = AuthService.hash_password("Password123!")
            self.customer_user = self.user_repo.create(
                User(email="customer@example.com", password_hash=pwd_hash, role=UserRole.CUSTOMER, is_verified=True)
            )
            self.unverified_owner = self.user_repo.create(
                User(email="unverified_owner@example.com", password_hash=pwd_hash, role=UserRole.OWNER, is_verified=False)
            )
            self.verified_owner = self.user_repo.create(
                User(email="owner@example.com", password_hash=pwd_hash, role=UserRole.OWNER, is_verified=True)
            )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    # -------------------------------------------------------------------------
    # 1. Access Control Tests
    # -------------------------------------------------------------------------

    def test_anonymous_access_to_admin_redirects_to_login_with_next(self) -> None:
        resp = self.client.get("/admin")
        self.assertEqual(resp.status_code, 302)
        location = resp.headers.get("Location", "")
        self.assertIn("/login", location)
        self.assertTrue("next=/admin" in location or "next=%2Fadmin" in location)

    def test_anonymous_access_to_admin_dashboard_redirects_to_login_with_next(self) -> None:
        resp = self.client.get("/admin/dashboard")
        self.assertEqual(resp.status_code, 302)
        location = resp.headers.get("Location", "")
        self.assertIn("/login", location)
        self.assertTrue("next=/admin/dashboard" in location or "next=%2Fadmin%2Fdashboard" in location)

    def test_authenticated_customer_access_to_admin_returns_403(self) -> None:
        with self.client.session_transaction() as sess:
            sess["user_id"] = self.customer_user.id

        resp = self.client.get("/admin")
        self.assertEqual(resp.status_code, 403)

    def test_authenticated_customer_access_to_admin_dashboard_returns_403(self) -> None:
        with self.client.session_transaction() as sess:
            sess["user_id"] = self.customer_user.id

        resp = self.client.get("/admin/dashboard")
        self.assertEqual(resp.status_code, 403)

    def test_authenticated_unverified_owner_access_to_admin_returns_403(self) -> None:
        with self.client.session_transaction() as sess:
            sess["user_id"] = self.unverified_owner.id

        resp = self.client.get("/admin")
        self.assertEqual(resp.status_code, 403)

    def test_authenticated_unverified_owner_access_to_admin_dashboard_returns_403(self) -> None:
        with self.client.session_transaction() as sess:
            sess["user_id"] = self.unverified_owner.id

        resp = self.client.get("/admin/dashboard")
        self.assertEqual(resp.status_code, 403)

    def test_authenticated_verified_owner_access_to_admin_returns_200(self) -> None:
        with self.client.session_transaction() as sess:
            sess["user_id"] = self.verified_owner.id

        resp = self.client.get("/admin")
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn("Store Administration", html)
        self.assertIn("Owner Access", html)

    def test_authenticated_verified_owner_access_to_admin_dashboard_redirects_to_canonical(self) -> None:
        with self.client.session_transaction() as sess:
            sess["user_id"] = self.verified_owner.id

        resp = self.client.get("/admin/dashboard", follow_redirects=False)
        self.assertEqual(resp.status_code, 302)
        # Canonical route is /admin/
        self.assertTrue(resp.headers.get("Location", "").endswith("/admin/"))

        # Following redirect leads to successful dashboard render
        follow_resp = self.client.get("/admin/dashboard", follow_redirects=True)
        self.assertEqual(follow_resp.status_code, 200)
        self.assertIn("Store Administration", follow_resp.get_data(as_text=True))

    # -------------------------------------------------------------------------
    # 2. Tampering / Spoofing & Session Integrity Tests
    # -------------------------------------------------------------------------

    def test_forged_session_role_tampering_rejected_with_403(self) -> None:
        # User is CUSTOMER in database, but attacker injects role=owner into session cookie
        with self.client.session_transaction() as sess:
            sess["user_id"] = self.customer_user.id
            sess["role"] = "owner"
            sess["is_verified"] = True

        resp = self.client.get("/admin")
        self.assertEqual(resp.status_code, 403)

    def test_promoted_owner_gains_immediate_access_without_relogin(self) -> None:
        with self.client.session_transaction() as sess:
            sess["user_id"] = self.customer_user.id

        # Initially denied
        resp = self.client.get("/admin")
        self.assertEqual(resp.status_code, 403)

        # Promote user in database
        with self.app.app_context():
            user = self.user_repo.get_by_id(self.customer_user.id)
            user.role = UserRole.OWNER
            self.user_repo.update(user)

        # Immediate next request succeeds without logging in again
        resp = self.client.get("/admin")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Store Administration", resp.get_data(as_text=True))

    def test_deleted_user_with_persisting_session_denied_access(self) -> None:
        with self.client.session_transaction() as sess:
            sess["user_id"] = self.verified_owner.id

        # Initially succeeds
        resp = self.client.get("/admin")
        self.assertEqual(resp.status_code, 200)

        # Delete owner from database
        with self.app.app_context():
            self.user_repo.delete(self.verified_owner.id)

        # User is no longer in DB -> treated as anonymous, redirected or 401
        resp = self.client.get("/admin")
        # owner_required redirects unauthenticated browser requests to /login
        self.assertIn(resp.status_code, (302, 401))
        if resp.status_code == 302:
            self.assertIn("/login", resp.headers.get("Location", ""))

    # -------------------------------------------------------------------------
    # 3. Information Disclosure Prevention Tests
    # -------------------------------------------------------------------------

    def test_unauthorized_responses_leak_no_administrative_data(self) -> None:
        # Seed sensitive aggregate data
        with self.app.app_context():
            cat = self.category_repo.create(Category(name="Specialty"))
            p = self.product_repo.create(
                Product(name="Confidential Gold Caviar", price=99999, stock_quantity=10, category_id=cat.id)
            )
            c = self.customer_repo.create(Customer(name="VIP Customer", email="vip@example.com"))
            self.order_repo.create(
                Order(
                    customer_id=c.id,
                    status=OrderStatus.PAID,
                    items=[OrderItem(product_id=p.id, product_name=p.name, unit_price=p.price, quantity=7)],
                )
            )

        # Anonymous request
        anon_resp = self.client.get("/admin")
        anon_body = anon_resp.get_data(as_text=True)
        self.assertNotIn("Confidential Gold Caviar", anon_body)
        self.assertNotIn("99999", anon_body)
        self.assertNotIn("699,99", anon_body)
        self.assertNotIn("Recorded order total", anon_body)

        # Customer request (403)
        with self.client.session_transaction() as sess:
            sess["user_id"] = self.customer_user.id
        cust_resp = self.client.get("/admin")
        self.assertEqual(cust_resp.status_code, 403)
        cust_body = cust_resp.get_data(as_text=True)
        self.assertNotIn("Confidential Gold Caviar", cust_body)
        self.assertNotIn("99999", cust_body)
        self.assertNotIn("699,99", cust_body)
        self.assertNotIn("Recorded order total", cust_body)

        # Unverified owner request (403)
        with self.client.session_transaction() as sess:
            sess["user_id"] = self.unverified_owner.id
        unverified_resp = self.client.get("/admin")
        self.assertEqual(unverified_resp.status_code, 403)
        unverified_body = unverified_resp.get_data(as_text=True)
        self.assertNotIn("Confidential Gold Caviar", unverified_body)
        self.assertNotIn("99999", unverified_body)
        self.assertNotIn("699,99", unverified_body)
        self.assertNotIn("Recorded order total", unverified_body)

    # -------------------------------------------------------------------------
    # 4. Aggregate Correctness & Live Updating Tests
    # -------------------------------------------------------------------------

    def test_dashboard_renders_empty_database_gracefully(self) -> None:
        with self.client.session_transaction() as sess:
            sess["user_id"] = self.verified_owner.id

        resp = self.client.get("/admin")
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)

        # Metric counts render 0 gracefully
        self.assertIn('id="metric-total-products" class="text-3xl font-extrabold text-gray-900 tracking-tight">0<', html)
        self.assertIn('id="metric-active-products" class="text-3xl font-extrabold text-gray-900 tracking-tight">0<', html)
        self.assertIn('id="metric-total-orders" class="text-3xl font-extrabold text-gray-900 tracking-tight">0<', html)
        self.assertIn('id="metric-total-recorded-value" class="text-3xl font-extrabold text-gray-900 tracking-tight">0,00 €<', html)

        # Status counts render 0 gracefully
        self.assertIn('id="status-count-pending" class="text-2xl font-extrabold text-amber-900 mt-2">0<', html)
        self.assertIn('id="status-count-confirmed" class="text-2xl font-extrabold text-blue-900 mt-2">0<', html)
        self.assertIn('id="status-count-paid" class="text-2xl font-extrabold text-indigo-900 mt-2">0<', html)
        self.assertIn('id="status-count-shipped" class="text-2xl font-extrabold text-purple-900 mt-2">0<', html)
        self.assertIn('id="status-count-delivered" class="text-2xl font-extrabold text-emerald-900 mt-2">0<', html)
        self.assertIn('id="status-count-cancelled" class="text-2xl font-extrabold text-gray-800 mt-2">0<', html)

        # Explicit caption warning
        self.assertIn("Recorded order total (not verified or paid revenue)", html)

    def test_dashboard_displays_accurate_populated_metrics(self) -> None:
        with self.app.app_context():
            cat = self.category_repo.create(Category(name="Pantry"))
            p1 = self.product_repo.create(
                Product(name="Organic Olive Oil", price=1200, stock_quantity=15, is_active=True, category_id=cat.id)
            )
            p2 = self.product_repo.create(
                Product(name="Sea Salt", price=300, stock_quantity=20, is_active=True, category_id=cat.id)
            )
            p3 = self.product_repo.create(
                Product(name="Archived Spice", price=500, stock_quantity=0, is_active=False, category_id=cat.id)
            )

            cust = self.customer_repo.create(Customer(name="Gourmet Buyer", email="gourmet@example.com"))

            # Order 1: Pending, 2x p1 (2400) + 1x p2 (300) = 2700 cents
            self.order_repo.create(
                Order(
                    customer_id=cust.id,
                    status=OrderStatus.PENDING,
                    items=[
                        OrderItem(product_id=p1.id, product_name=p1.name, unit_price=p1.price, quantity=2),
                        OrderItem(product_id=p2.id, product_name=p2.name, unit_price=p2.price, quantity=1),
                    ],
                )
            )

            # Order 2: Confirmed, 3x p2 (900 cents)
            self.order_repo.create(
                Order(
                    customer_id=cust.id,
                    status=OrderStatus.CONFIRMED,
                    items=[
                        OrderItem(product_id=p2.id, product_name=p2.name, unit_price=p2.price, quantity=3),
                    ],
                )
            )

        with self.client.session_transaction() as sess:
            sess["user_id"] = self.verified_owner.id

        resp = self.client.get("/admin")
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)

        # Products: 3 total, 2 active
        self.assertIn('id="metric-total-products" class="text-3xl font-extrabold text-gray-900 tracking-tight">3<', html)
        self.assertIn('id="metric-active-products" class="text-3xl font-extrabold text-gray-900 tracking-tight">2<', html)

        # Orders: 2 total
        self.assertIn('id="metric-total-orders" class="text-3xl font-extrabold text-gray-900 tracking-tight">2<', html)

        # Total value: 2700 + 900 = 3600 cents -> 36,00 €
        self.assertIn('id="metric-total-recorded-value" class="text-3xl font-extrabold text-gray-900 tracking-tight">36,00 €<', html)

        # Status breakdown: 1 pending, 1 confirmed, 0 others
        self.assertIn('id="status-count-pending" class="text-2xl font-extrabold text-amber-900 mt-2">1<', html)
        self.assertIn('id="status-count-confirmed" class="text-2xl font-extrabold text-blue-900 mt-2">1<', html)
        self.assertIn('id="status-count-paid" class="text-2xl font-extrabold text-indigo-900 mt-2">0<', html)
        self.assertIn('id="status-count-shipped" class="text-2xl font-extrabold text-purple-900 mt-2">0<', html)
        self.assertIn('id="status-count-delivered" class="text-2xl font-extrabold text-emerald-900 mt-2">0<', html)
        self.assertIn('id="status-count-cancelled" class="text-2xl font-extrabold text-gray-800 mt-2">0<', html)

    def test_dashboard_metrics_reflect_catalog_and_order_changes(self) -> None:
        with self.app.app_context():
            cat = self.category_repo.create(Category(name="Produce"))
            p = self.product_repo.create(
                Product(name="Fresh Apples", price=250, stock_quantity=10, is_active=True, category_id=cat.id)
            )
            cust = self.customer_repo.create(Customer(name="Buyer", email="buyer@example.com"))

        with self.client.session_transaction() as sess:
            sess["user_id"] = self.verified_owner.id

        # Baseline: 1 product, 1 active, 0 orders
        r1 = self.client.get("/admin")
        self.assertIn('id="metric-total-products" class="text-3xl font-extrabold text-gray-900 tracking-tight">1<', r1.get_data(as_text=True))
        self.assertIn('id="metric-active-products" class="text-3xl font-extrabold text-gray-900 tracking-tight">1<', r1.get_data(as_text=True))
        self.assertIn('id="metric-total-orders" class="text-3xl font-extrabold text-gray-900 tracking-tight">0<', r1.get_data(as_text=True))

        # Deactivate product
        with self.app.app_context():
            p.is_active = False
            self.product_repo.update(p)

        # Deactivated: 1 product, 0 active
        r2 = self.client.get("/admin")
        self.assertIn('id="metric-total-products" class="text-3xl font-extrabold text-gray-900 tracking-tight">1<', r2.get_data(as_text=True))
        self.assertIn('id="metric-active-products" class="text-3xl font-extrabold text-gray-900 tracking-tight">0<', r2.get_data(as_text=True))

        # Create order and mark PAID
        with self.app.app_context():
            o = self.order_repo.create(
                Order(
                    customer_id=cust.id,
                    status=OrderStatus.PAID,
                    items=[OrderItem(product_id=p.id, product_name=p.name, unit_price=250, quantity=4)],
                )
            )

        # Now: 1 order, 1 paid, value 10,00 € (1000 cents)
        r3 = self.client.get("/admin")
        h3 = r3.get_data(as_text=True)
        self.assertIn('id="metric-total-orders" class="text-3xl font-extrabold text-gray-900 tracking-tight">1<', h3)
        self.assertIn('id="status-count-paid" class="text-2xl font-extrabold text-indigo-900 mt-2">1<', h3)
        self.assertIn('id="metric-total-recorded-value" class="text-3xl font-extrabold text-gray-900 tracking-tight">10,00 €<', h3)

    # -------------------------------------------------------------------------
    # 5. Navigation Visibility Tests
    # -------------------------------------------------------------------------

    def test_admin_nav_link_hidden_for_anonymous_visitor(self) -> None:
        resp = self.client.get("/")
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertNotIn('id="admin-nav-link"', html)
        self.assertNotIn("/admin", html)

    def test_admin_nav_link_hidden_for_authenticated_customer(self) -> None:
        with self.client.session_transaction() as sess:
            sess["user_id"] = self.customer_user.id

        resp = self.client.get("/")
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertNotIn('id="admin-nav-link"', html)
        self.assertNotIn("/admin", html)

    def test_admin_nav_link_hidden_for_authenticated_unverified_owner(self) -> None:
        with self.client.session_transaction() as sess:
            sess["user_id"] = self.unverified_owner.id

        resp = self.client.get("/")
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertNotIn('id="admin-nav-link"', html)
        self.assertNotIn("/admin", html)

    def test_admin_nav_link_visible_exclusively_for_verified_owner(self) -> None:
        with self.client.session_transaction() as sess:
            sess["user_id"] = self.verified_owner.id

        resp = self.client.get("/")
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn('id="admin-nav-link"', html)
        self.assertIn('href="/admin/"', html)
        self.assertIn("Admin", html)

    # -------------------------------------------------------------------------
    # 6. Administration Layout & Placeholders Tests
    # -------------------------------------------------------------------------

    def test_dashboard_placeholders_and_storefront_navigation(self) -> None:
        with self.client.session_transaction() as sess:
            sess["user_id"] = self.verified_owner.id

        resp = self.client.get("/admin")
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)

        # Storefront navigation link
        self.assertIn("Return to Storefront", html)
        self.assertIn('href="/"', html)

        # Management sections
        self.assertIn("Product Catalog Management", html)
        self.assertIn("Order Processing & Fulfillment", html)
        self.assertIn("Customer Management", html)
        self.assertIn("Coming Later", html)
        self.assertIn('href="/admin/orders"', html)

        # Disabled placeholder elements with aria-disabled
        self.assertIn('aria-disabled="true"', html)
        self.assertIn("disabled", html)

        # Verify no broken or dead links (no href="#")
        self.assertNotIn('href="#"', html)


if __name__ == "__main__":
    unittest.main()

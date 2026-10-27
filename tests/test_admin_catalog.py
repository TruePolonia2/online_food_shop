"""Comprehensive test suite for Phase 10.7 Secure Owner Catalog Management."""
from pathlib import Path
import tempfile
import unittest

from app import create_app
from app.config import Config
from app.csrf import generate_csrf_token
from app.database.connection import init_db
from app.models.cart import Cart
from app.models.category import Category
from app.models.customer import Customer
from app.models.order import Order
from app.models.order_item import OrderItem
from app.models.product import Product
from app.models.user import User, UserRole
from app.repositories.category_repository import CategoryRepository
from app.repositories.customer_repository import CustomerRepository
from app.repositories.order_repository import OrderRepository
from app.repositories.product_repository import ProductRepository
from app.repositories.user_repository import UserRepository
from app.services.auth_service import AuthService
from app.services.cart_service import CartService
from app.services.catalog_validation import (
    parse_euro_to_cents,
    parse_stock_quantity,
    validate_product_image_url,
)
from app.services.category_service import CategoryService
from app.services.order_service import OrderService
from app.services.product_service import ProductService


class TestCatalogValidationUnit(unittest.TestCase):
    """Unit tests for price, stock, and image parsing/validation."""

    def test_parse_euro_to_cents_valid(self) -> None:
        self.assertEqual(parse_euro_to_cents("12"), 1200)
        self.assertEqual(parse_euro_to_cents("12.5"), 1250)
        self.assertEqual(parse_euro_to_cents("12.50"), 1250)
        self.assertEqual(parse_euro_to_cents("12,50"), 1250)
        self.assertEqual(parse_euro_to_cents("0.99"), 99)
        self.assertEqual(parse_euro_to_cents("0,99"), 99)
        self.assertEqual(parse_euro_to_cents("0"), 0)
        self.assertEqual(parse_euro_to_cents("0.00"), 0)
        self.assertEqual(parse_euro_to_cents("  3.45  "), 345)
        self.assertEqual(parse_euro_to_cents(10), 1000)

    def test_parse_euro_to_cents_invalid(self) -> None:
        for invalid in [
            True,
            False,
            "-5",
            "-0.01",
            "12.345",  # excessive precision
            "12,345",
            "abc",
            "",
            "   ",
            "12.3.4",
            "12,50.00",
            None,
            [],
        ]:
            with self.assertRaises(ValueError):
                parse_euro_to_cents(invalid)

    def test_parse_stock_quantity_valid(self) -> None:
        self.assertEqual(parse_stock_quantity("0"), 0)
        self.assertEqual(parse_stock_quantity("15"), 15)
        self.assertEqual(parse_stock_quantity(20), 20)
        self.assertEqual(parse_stock_quantity(" 100 "), 100)

    def test_parse_stock_quantity_invalid(self) -> None:
        for invalid in [
            True,
            False,
            "-1",
            "-10",
            "10.5",
            "10,5",
            "abc",
            "",
            "   ",
            None,
        ]:
            with self.assertRaises(ValueError):
                parse_stock_quantity(invalid)

    def test_validate_product_image_url_valid(self) -> None:
        self.assertIsNone(validate_product_image_url(None))
        self.assertIsNone(validate_product_image_url(""))
        self.assertIsNone(validate_product_image_url("   "))
        self.assertEqual(
            validate_product_image_url("/static/images/apple.png"),
            "/static/images/apple.png",
        )
        self.assertEqual(
            validate_product_image_url("https://example.com/apple.jpg"),
            "https://example.com/apple.jpg",
        )
        self.assertEqual(
            validate_product_image_url("http://example.com/apple.jpg"),
            "http://example.com/apple.jpg",
        )

    def test_validate_product_image_url_dangerous_schemes(self) -> None:
        for dangerous in [
            "javascript:alert(1)",
            "data:text/html;base64,PHNjcmlwdD4=",
            "vbscript:msgbox",
            "file:///etc/passwd",
            'https://example.com/<script>',
            'https://example.com/"onerror=alert(1)',
            "ftp://example.com/image.jpg",
        ]:
            with self.assertRaises(ValueError):
                validate_product_image_url(dangerous)


class BaseAdminCatalogTestCase(unittest.TestCase):
    """Base setup for integration and route testing."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_admin_catalog.sqlite3"

        class TestCatalogConfig(Config):
            DATABASE_PATH = str(self.db_path)
            TESTING = True
            SECRET_KEY = "test-catalog-secret"
            CSRF_ENABLED = False  # Disabled by default for easy form testing; tested separately

        self.app = create_app(TestCatalogConfig)
        self.client = self.app.test_client()

        with self.app.app_context():
            init_db()
            self.user_repo = UserRepository()
            self.category_repo = CategoryRepository()
            self.product_repo = ProductRepository()
            self.customer_repo = CustomerRepository()
            self.order_repo = OrderRepository()

            pwd_hash = AuthService.hash_password("Password123!")
            self.verified_owner = self.user_repo.create(
                User(email="owner@example.com", password_hash=pwd_hash, role=UserRole.OWNER, is_verified=True)
            )
            self.unverified_owner = self.user_repo.create(
                User(email="unverified_owner@example.com", password_hash=pwd_hash, role=UserRole.OWNER, is_verified=False)
            )
            self.customer_user = self.user_repo.create(
                User(email="customer@example.com", password_hash=pwd_hash, role=UserRole.CUSTOMER, is_verified=True)
            )

            # Seed base categories and products
            self.cat_produce = self.category_repo.create(Category(name="Produce", description="Fresh produce"))
            self.cat_bakery = self.category_repo.create(Category(name="Bakery", description="Baked goods"))

            self.prod_apple = self.product_repo.create(
                Product(name="Apple", price=120, stock_quantity=20, category_id=self.cat_produce.id, is_active=True)
            )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()


class TestAdminCatalogAuthorization(BaseAdminCatalogTestCase):
    """Authorization verification across all admin catalog endpoints."""

    def test_anonymous_access_redirects_to_login(self) -> None:
        routes = [
            "/admin/categories",
            "/admin/categories/new",
            f"/admin/categories/{self.cat_produce.id}/edit",
            "/admin/products",
            "/admin/products/new",
            f"/admin/products/{self.prod_apple.id}/edit",
        ]
        for route in routes:
            resp = self.client.get(route)
            self.assertEqual(resp.status_code, 302, f"Failed on {route}")
            self.assertIn("/login", resp.headers.get("Location", ""))

    def test_customer_access_returns_403(self) -> None:
        with self.client.session_transaction() as sess:
            sess["user_id"] = self.customer_user.id

        routes = [
            "/admin/categories",
            "/admin/categories/new",
            f"/admin/categories/{self.cat_produce.id}/edit",
            "/admin/products",
            "/admin/products/new",
            f"/admin/products/{self.prod_apple.id}/edit",
        ]
        for route in routes:
            resp = self.client.get(route)
            self.assertEqual(resp.status_code, 403, f"Failed on {route}")

    def test_unverified_owner_access_returns_403(self) -> None:
        with self.client.session_transaction() as sess:
            sess["user_id"] = self.unverified_owner.id

        routes = [
            "/admin/categories",
            "/admin/categories/new",
            f"/admin/categories/{self.cat_produce.id}/edit",
            "/admin/products",
            "/admin/products/new",
            f"/admin/products/{self.prod_apple.id}/edit",
        ]
        for route in routes:
            resp = self.client.get(route)
            self.assertEqual(resp.status_code, 403, f"Failed on {route}")

    def test_verified_owner_access_succeeds(self) -> None:
        with self.client.session_transaction() as sess:
            sess["user_id"] = self.verified_owner.id

        routes = [
            "/admin/categories",
            "/admin/categories/new",
            f"/admin/categories/{self.cat_produce.id}/edit",
            "/admin/products",
            "/admin/products/new",
            f"/admin/products/{self.prod_apple.id}/edit",
        ]
        for route in routes:
            resp = self.client.get(route)
            self.assertEqual(resp.status_code, 200, f"Failed on {route}")

    def test_forged_session_role_tampering_denied(self) -> None:
        with self.client.session_transaction() as sess:
            sess["user_id"] = self.customer_user.id
            sess["role"] = "owner"
            sess["is_verified"] = True

        resp = self.client.get("/admin/products")
        self.assertEqual(resp.status_code, 403)


class TestAdminCatalogCSRF(BaseAdminCatalogTestCase):
    """CSRF enforcement verification on administrative state-changing POST routes."""

    def test_csrf_protection_enforced_when_enabled(self) -> None:
        # Recreate client with CSRF_ENABLED = True
        class CSRFConfig(Config):
            DATABASE_PATH = str(self.db_path)
            TESTING = True
            SECRET_KEY = "csrf-test-secret"
            CSRF_ENABLED = True

        csrf_app = create_app(CSRFConfig)
        client = csrf_app.test_client()

        with client.session_transaction() as sess:
            sess["user_id"] = self.verified_owner.id

        # POST without CSRF token fails with 400
        resp = client.post("/admin/categories/new", data={"name": "Beverages"})
        self.assertEqual(resp.status_code, 400)

        # POST with invalid CSRF token fails with 400
        resp_invalid = client.post(
            "/admin/categories/new",
            data={"name": "Beverages", "csrf_token": "invalid-token"},
        )
        self.assertEqual(resp_invalid.status_code, 400)

        # POST with valid session CSRF token succeeds
        with client.session_transaction() as sess:
            sess["_csrf_token"] = "valid-test-csrf-token"

        resp_valid = client.post(
            "/admin/categories/new",
            data={"name": "Beverages", "csrf_token": "valid-test-csrf-token"},
            follow_redirects=True,
        )
        self.assertEqual(resp_valid.status_code, 200)
        self.assertIn("Beverages", resp_valid.get_data(as_text=True))


class TestAdminCategoryManagement(BaseAdminCatalogTestCase):
    """Category CRUD operations and validations in administration."""

    def setUp(self) -> None:
        super().setUp()
        with self.client.session_transaction() as sess:
            sess["user_id"] = self.verified_owner.id

    def test_list_categories(self) -> None:
        resp = self.client.get("/admin/categories")
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn("Category Management", html)
        self.assertIn("Produce", html)
        self.assertIn("Bakery", html)

    def test_create_category_success(self) -> None:
        resp = self.client.post(
            "/admin/categories/new",
            data={"name": "Dairy", "description": "Milk and cheese", "is_active": "1"},
            follow_redirects=True,
        )
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn("Category &#39;Dairy&#39; created successfully.", html)
        self.assertIn("Dairy", html)

        with self.app.app_context():
            cat = self.category_repo.get_by_name("Dairy")
            self.assertIsNotNone(cat)
            self.assertEqual(cat.description, "Milk and cheese")
            self.assertTrue(cat.is_active)

    def test_create_category_duplicate_name_rejected(self) -> None:
        resp = self.client.post(
            "/admin/categories/new",
            data={"name": "Produce", "description": "Duplicate category"},
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("already exists", resp.get_data(as_text=True))

    def test_create_category_empty_name_rejected(self) -> None:
        resp = self.client.post(
            "/admin/categories/new",
            data={"name": "   ", "description": "Empty name"},
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("Category name is required", resp.get_data(as_text=True))

    def test_edit_category_success(self) -> None:
        resp = self.client.post(
            f"/admin/categories/{self.cat_produce.id}/edit",
            data={
                "name": "Fresh Organic Produce",
                "description": "Locally grown fruits and greens",
                # Omit is_active to deactivate
            },
            follow_redirects=True,
        )
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn("Fresh Organic Produce", html)

        with self.app.app_context():
            updated = self.category_repo.get_by_id(self.cat_produce.id)
            self.assertEqual(updated.name, "Fresh Organic Produce")
            self.assertFalse(updated.is_active)

    def test_edit_nonexistent_category_returns_404(self) -> None:
        resp = self.client.get("/admin/categories/99999/edit")
        self.assertEqual(resp.status_code, 404)

        resp_post = self.client.post(
            "/admin/categories/99999/edit",
            data={"name": "Ghost Category"},
        )
        self.assertEqual(resp_post.status_code, 404)


class TestAdminProductManagement(BaseAdminCatalogTestCase):
    """Product CRUD operations, pricing, and stock validations."""

    def setUp(self) -> None:
        super().setUp()
        with self.client.session_transaction() as sess:
            sess["user_id"] = self.verified_owner.id

    def test_list_products_shows_all_including_inactive(self) -> None:
        with self.app.app_context():
            self.product_repo.create(
                Product(name="Expired Yogurt", price=100, is_active=False)
            )

        resp = self.client.get("/admin/products")
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn("Apple", html)
        self.assertIn("Expired Yogurt", html)
        self.assertIn("Inactive", html)

    def test_create_product_success(self) -> None:
        resp = self.client.post(
            "/admin/products/new",
            data={
                "name": "Organic Honey",
                "price": "8.50",
                "stock_quantity": "15",
                "category_id": str(self.cat_produce.id),
                "description": "Raw local honey",
                "image_url": "/static/images/honey.png",
                "is_active": "1",
            },
            follow_redirects=True,
        )
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn("Organic Honey", html)
        self.assertIn("8,50 €", html)

        with self.app.app_context():
            prod = self.product_repo.get_all(include_inactive=True)
            created = next(p for p in prod if p.name == "Organic Honey")
            self.assertEqual(created.price, 850)
            self.assertEqual(created.stock_quantity, 15)
            self.assertEqual(created.category_id, self.cat_produce.id)
            self.assertEqual(created.image_url, "/static/images/honey.png")
            self.assertTrue(created.is_active)

    def test_create_product_comma_decimal_price_accepted(self) -> None:
        resp = self.client.post(
            "/admin/products/new",
            data={
                "name": "Sea Salt Crackers",
                "price": "3,25",
                "stock_quantity": "10",
                "is_active": "1",
            },
            follow_redirects=True,
        )
        self.assertEqual(resp.status_code, 200)

        with self.app.app_context():
            prods = self.product_repo.get_all(include_inactive=True)
            p = next(x for x in prods if x.name == "Sea Salt Crackers")
            self.assertEqual(p.price, 325)

    def test_create_product_rejects_negative_price(self) -> None:
        resp = self.client.post(
            "/admin/products/new",
            data={
                "name": "Invalid Price Product",
                "price": "-4.50",
                "stock_quantity": "5",
            },
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("negative", resp.get_data(as_text=True).lower())

    def test_create_product_rejects_excessive_decimal_precision(self) -> None:
        resp = self.client.post(
            "/admin/products/new",
            data={
                "name": "High Precision Product",
                "price": "4.555",
                "stock_quantity": "5",
            },
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("decimal places", resp.get_data(as_text=True).lower())

    def test_create_product_rejects_negative_stock(self) -> None:
        resp = self.client.post(
            "/admin/products/new",
            data={
                "name": "Negative Stock Product",
                "price": "5.00",
                "stock_quantity": "-3",
            },
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("stock", resp.get_data(as_text=True).lower())

    def test_create_product_rejects_decimal_stock(self) -> None:
        resp = self.client.post(
            "/admin/products/new",
            data={
                "name": "Decimal Stock Product",
                "price": "5.00",
                "stock_quantity": "3.5",
            },
        )
        self.assertEqual(resp.status_code, 400)

    def test_create_product_rejects_nonexistent_category(self) -> None:
        resp = self.client.post(
            "/admin/products/new",
            data={
                "name": "Bad Category Product",
                "price": "5.00",
                "stock_quantity": "5",
                "category_id": "99999",
            },
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("category", resp.get_data(as_text=True).lower())

    def test_create_product_rejects_dangerous_image_scheme(self) -> None:
        resp = self.client.post(
            "/admin/products/new",
            data={
                "name": "XSS Image Product",
                "price": "5.00",
                "stock_quantity": "5",
                "image_url": "javascript:alert(1)",
            },
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("scheme", resp.get_data(as_text=True).lower())

    def test_edit_product_success(self) -> None:
        resp = self.client.post(
            f"/admin/products/{self.prod_apple.id}/edit",
            data={
                "name": "Honeycrisp Apple",
                "price": "2.40",
                "stock_quantity": "50",
                "category_id": str(self.cat_bakery.id),
                "description": "Sweet crisp apple",
                "image_url": "/static/images/honeycrisp.png",
                # omit is_active to deactivate
            },
            follow_redirects=True,
        )
        self.assertEqual(resp.status_code, 200)

        with self.app.app_context():
            updated = self.product_repo.get_by_id(self.prod_apple.id)
            self.assertEqual(updated.name, "Honeycrisp Apple")
            self.assertEqual(updated.price, 240)
            self.assertEqual(updated.stock_quantity, 50)
            self.assertEqual(updated.category_id, self.cat_bakery.id)
            self.assertEqual(updated.image_url, "/static/images/honeycrisp.png")
            self.assertFalse(updated.is_active)

    def test_edit_nonexistent_product_returns_404(self) -> None:
        resp = self.client.get("/admin/products/99999/edit")
        self.assertEqual(resp.status_code, 404)

        resp_post = self.client.post(
            "/admin/products/99999/edit",
            data={"name": "Ghost Product", "price": "1.00", "stock_quantity": "1"},
        )
        self.assertEqual(resp_post.status_code, 404)


class TestCategoryActivationAndCatalogBehavior(BaseAdminCatalogTestCase):
    """Interaction between category activation and public catalog availability."""

    def test_inactive_category_hidden_from_storefront_navigation(self) -> None:
        # Produce is active, Bakery is active
        r1 = self.client.get("/")
        self.assertIn("Produce", r1.get_data(as_text=True))
        self.assertIn("Bakery", r1.get_data(as_text=True))

        # Deactivate Bakery category
        with self.app.app_context():
            self.cat_bakery.is_active = False
            self.category_repo.update(self.cat_bakery)

        # Storefront navigation only renders active categories
        r2 = self.client.get("/")
        html2 = r2.get_data(as_text=True)
        self.assertIn("Produce", html2)
        self.assertNotIn("Bakery", html2)

    def test_inactive_category_products_hidden_from_public_catalog(self) -> None:
        # Create a product in Bakery
        with self.app.app_context():
            croissant = self.product_repo.create(
                Product(name="Butter Croissant", price=250, stock_quantity=10, category_id=self.cat_bakery.id, is_active=True)
            )

        # Initially visible
        r1 = self.client.get("/")
        self.assertIn("Butter Croissant", r1.get_data(as_text=True))

        # Deactivate Bakery category
        with self.app.app_context():
            self.cat_bakery.is_active = False
            self.category_repo.update(self.cat_bakery)

        # Homepage does not list products of inactive category
        r2 = self.client.get("/")
        self.assertNotIn("Butter Croissant", r2.get_data(as_text=True))

        # Direct category query does not expose products
        r3 = self.client.get(f"/?category_id={self.cat_bakery.id}")
        self.assertNotIn("Butter Croissant", r3.get_data(as_text=True))

        # Direct product detail view returns 404
        r4 = self.client.get(f"/products/{croissant.id}")
        self.assertEqual(r4.status_code, 404)

    def test_cart_and_checkout_reject_products_from_inactive_category(self) -> None:
        with self.app.app_context():
            croissant = self.product_repo.create(
                Product(name="French Baguette", price=200, stock_quantity=10, category_id=self.cat_bakery.id, is_active=True)
            )
            cust = self.customer_repo.create(Customer(name="Shopper", email="shopper@example.com"))

            cart_service = CartService()
            order_service = OrderService()

            # Customer adds item while category is active
            cart = cart_service.create_cart(cust.id)
            cart_service.add_item(cart, croissant.id, 1)

            # Owner deactivates the category
            self.cat_bakery.is_active = False
            self.category_repo.update(self.cat_bakery)

            # Cart validation catches inactive category
            validation_errors = cart_service.validate_cart(cart)
            self.assertTrue(len(validation_errors) > 0)
            self.assertIn("unavailable", validation_errors[0].lower())

            # Attempting to add item to cart raises ValueError
            with self.assertRaises(ValueError):
                cart_service.add_item(cart, croissant.id, 1)

            # Attempting checkout raises ValueError and does not create an order
            with self.assertRaises(ValueError) as ctx:
                order_service.checkout(cust.id, cart)
            self.assertIn("inactive category", str(ctx.exception).lower())


class TestHistoricalOrderPreservation(BaseAdminCatalogTestCase):
    """Critical test guaranteeing historical orders are immutable when catalog items are edited."""

    def test_historical_order_snapshots_remain_intact_after_product_and_category_edits(self) -> None:
        # 1. Create a customer and place an order for the Apple (price 120 cents, name "Apple")
        with self.app.app_context():
            cust = self.customer_repo.create(Customer(name="Historical Buyer", email="history@example.com"))
            order_service = OrderService()
            cart_service = CartService()

            cart = cart_service.create_cart(cust.id)
            cart_service.add_item(cart, self.prod_apple.id, quantity=3)  # 3 * 120 = 360 cents
            order = order_service.checkout(cust.id, cart)

            initial_order_id = order.id
            self.assertEqual(order.total, 360)
            self.assertEqual(len(order.items), 1)
            self.assertEqual(order.items[0].product_name, "Apple")
            self.assertEqual(order.items[0].unit_price, 120)
            self.assertEqual(order.items[0].quantity, 3)

        # 2. Owner modifies the product in administration:
        #    Renames product, triples price, zeros out stock, changes category, and deactivates it!
        with self.client.session_transaction() as sess:
            sess["user_id"] = self.verified_owner.id

        resp = self.client.post(
            f"/admin/products/{self.prod_apple.id}/edit",
            data={
                "name": "Completely Modified Apple",
                "price": "9.99",  # 999 cents
                "stock_quantity": "0",
                "category_id": str(self.cat_bakery.id),
                "description": "Altered description",
                "image_url": "/static/images/new_apple.png",
                # Inactive
            },
            follow_redirects=True,
        )
        self.assertEqual(resp.status_code, 200)

        # 3. Owner also renames and deactivates the original category
        resp_cat = self.client.post(
            f"/admin/categories/{self.cat_produce.id}/edit",
            data={"name": "Deprecated Produce", "description": "Closed category"},
            follow_redirects=True,
        )
        self.assertEqual(resp_cat.status_code, 200)

        # 4. Fetch the historical order from the database and verify zero mutation
        with self.app.app_context():
            fetched_order = self.order_repo.get_by_id(initial_order_id)
            self.assertIsNotNone(fetched_order)
            self.assertEqual(fetched_order.total, 360)
            self.assertEqual(len(fetched_order.items), 1)

            historical_item = fetched_order.items[0]
            # Name snapshot must still be "Apple"
            self.assertEqual(historical_item.product_name, "Apple")
            # Unit price snapshot must still be 120
            self.assertEqual(historical_item.unit_price, 120)
            # Quantity must still be 3
            self.assertEqual(historical_item.quantity, 3)
            # Line subtotal must still be 360
            self.assertEqual(historical_item.subtotal, 360)


if __name__ == "__main__":
    unittest.main()

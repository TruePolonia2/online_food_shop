"""Focused presentation tests for Shopping Cart, Checkout, and Order Confirmation UI (Phase 7 Step 4)."""
from pathlib import Path
import tempfile
import unittest

from app import create_app
from app.config import Config
from app.database.connection import init_db
from app.models.category import Category
from app.models.product import Product
from app.repositories.category_repository import CategoryRepository
from app.repositories.product_repository import ProductRepository
from app.routes.cart_session import SESSION_CART_KEY


class TestCartCheckoutUIPresentation(unittest.TestCase):
    """Tests for cart, checkout, and order confirmation UI presentation, forms, and price formatting."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_cart_checkout_ui.sqlite3"

        class TestConfig(Config):
            DATABASE_PATH = str(self.db_path)
            TESTING = True
            SECRET_KEY = "test-cart-checkout-ui-secret"
            CSRF_ENABLED = True  # Keep CSRF active to verify tokens in forms

        self.app = create_app(TestConfig)
        self.client = self.app.test_client()

        with self.app.app_context():
            init_db()
            self.cat_repo = CategoryRepository()
            self.prod_repo = ProductRepository()

            cat = self.cat_repo.create(Category(name="Bakery"))
            self.bread = self.prod_repo.create(
                Product(name="Artisan Bread", price=350, stock_quantity=10, category_id=cat.id, is_active=True)
            )
            self.croissant = self.prod_repo.create(
                Product(name="Butter Croissant", price=180, stock_quantity=10, category_id=cat.id, is_active=True)
            )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_cart_empty_state_rendering(self) -> None:
        resp = self.client.get("/cart/")
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)

        self.assertIn("Your cart is empty", html)
        self.assertIn("Continue Shopping", html)
        self.assertIn('href="/"', html)

    def test_cart_non_empty_rendering_and_euro_formatting(self) -> None:
        # Pre-seed session cart: 2 breads (2 * 3.50 = 7.00) + 3 croissants (3 * 1.80 = 5.40) -> total: 12.40 €
        with self.client.session_transaction() as sess:
            sess[SESSION_CART_KEY] = {
                str(self.bread.id): 2,
                str(self.croissant.id): 3,
            }

        resp = self.client.get("/cart/")
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)

        # Products, unit prices and subtotals in Portuguese euro format
        self.assertIn("Artisan Bread", html)
        self.assertIn("3,50 €", html)
        self.assertIn("7,00 €", html)

        self.assertIn("Butter Croissant", html)
        self.assertIn("1,80 €", html)
        self.assertIn("5,40 €", html)

        # Cart total
        self.assertIn("12,40 €", html)

        # Action forms with CSRF tokens
        self.assertIn('action="/cart/update"', html)
        self.assertIn('action="/cart/remove"', html)
        self.assertIn('action="/cart/clear"', html)
        self.assertIn('name="csrf_token"', html)

        # Checkout link
        self.assertIn('href="/checkout"', html)
        self.assertIn("Proceed to Checkout", html)

    def test_checkout_page_presentation_and_order_summary(self) -> None:
        with self.client.session_transaction() as sess:
            sess[SESSION_CART_KEY] = {
                str(self.bread.id): 2,
            }

        resp = self.client.get("/checkout")
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)

        # Heading and instructions
        self.assertIn("Checkout", html)
        self.assertIn("Order Summary", html)
        self.assertIn("Customer Information", html)

        # Form fields and CSRF
        self.assertIn('action="/checkout"', html)
        self.assertIn('method="post"', html)
        self.assertIn('name="csrf_token"', html)
        self.assertIn('name="name"', html)
        self.assertIn('name="email"', html)
        self.assertIn('name="phone"', html)
        self.assertIn('name="address"', html)
        self.assertIn("Place Order", html)

        # Order summary items and total in euros
        self.assertIn("Artisan Bread", html)
        self.assertIn("7,00 €", html)
        self.assertIn('href="/cart/"', html)
        self.assertIn("Edit Cart", html)

    def test_order_confirmation_page_presentation(self) -> None:
        # Pre-seed session cart
        with self.client.session_transaction() as sess:
            sess[SESSION_CART_KEY] = {
                str(self.bread.id): 1,
            }

        # Obtain valid CSRF token from session
        with self.client:
            self.client.get("/checkout")
            with self.client.session_transaction() as sess:
                token = sess.get("_csrf_token")

            resp_post = self.client.post(
                "/checkout",
                data={
                    "name": "Maria Silva",
                    "email": "maria.silva@example.com",
                    "phone": "912345678",
                    "address": "Rua Central 42, Porto",
                    "csrf_token": token,
                },
                follow_redirects=True,
            )
            self.assertEqual(resp_post.status_code, 200)
            html = resp_post.get_data(as_text=True)

            # Reassuring confirmation banner
            self.assertIn("Order Confirmation", html)
            self.assertIn("Thank you for your order!", html)
            self.assertIn("Status: Pending", html)

            # Customer details
            self.assertIn("Maria Silva", html)
            self.assertIn("maria.silva@example.com", html)
            self.assertIn("912345678", html)
            self.assertIn("Rua Central 42, Porto", html)

            # Items ordered & formatted price
            self.assertIn("Artisan Bread", html)
            self.assertIn("3,50 €", html)

            # Link back to shop
            self.assertIn('href="/"', html)
            self.assertIn("Return to Shop", html)


if __name__ == "__main__":
    unittest.main()

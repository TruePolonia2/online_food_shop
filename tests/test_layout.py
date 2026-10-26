"""Tests for shared base layout, navigation, flash messages, and cart counter."""
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
from app.routes.cart_session import SESSION_CART_KEY, count_cart_items, get_cart_item_count


class TestCartItemCountHelper(unittest.TestCase):
    """Unit tests for count_cart_items and get_cart_item_count helpers."""

    def test_count_cart_items_valid_dict(self) -> None:
        self.assertEqual(count_cart_items({"1": 2, "2": 1}), 3)
        self.assertEqual(count_cart_items({"10": 5}), 5)
        self.assertEqual(count_cart_items({}), 0)

    def test_count_cart_items_rejects_booleans(self) -> None:
        # In Python, bool is a subclass of int; booleans must be strictly rejected
        self.assertEqual(count_cart_items({"1": True}), 0)
        self.assertEqual(count_cart_items({"1": False, "2": True}), 0)
        self.assertEqual(count_cart_items({"1": 3, "2": True, "3": False}), 3)

    def test_count_cart_items_rejects_non_integers_and_non_positives(self) -> None:
        self.assertEqual(count_cart_items({"1": "2", "2": 4.5, "3": None}), 0)
        self.assertEqual(count_cart_items({"1": 0, "2": -3, "3": -100}), 0)
        self.assertEqual(count_cart_items({"1": 4, "2": 0, "3": -1}), 4)

    def test_count_cart_items_handles_non_dict_safely(self) -> None:
        self.assertEqual(count_cart_items(None), 0)
        self.assertEqual(count_cart_items("not-a-dict"), 0)
        self.assertEqual(count_cart_items([1, 2, 3]), 0)
        self.assertEqual(count_cart_items(12345), 0)

    def test_get_cart_item_count_outside_request_context_returns_zero(self) -> None:
        # When called outside request context, it must safely return 0 without raising
        self.assertEqual(get_cart_item_count(), 0)


class TestLayoutAndNavigation(unittest.TestCase):
    """Integration tests for shared base layout, navigation, and flash message presentation."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_layout.sqlite3"

        class TestConfig(Config):
            DATABASE_PATH = str(self.db_path)
            TESTING = True
            SECRET_KEY = "test-layout-secret-key"
            CSRF_ENABLED = False

        self.app = create_app(TestConfig)
        self.client = self.app.test_client()

        with self.app.app_context():
            init_db()
            self.cat_repo = CategoryRepository()
            self.prod_repo = ProductRepository()

            cat = self.cat_repo.create(Category(name="Bakery"))
            self.product1 = self.prod_repo.create(
                Product(name="Bread", price=200, stock_quantity=10, category_id=cat.id, is_active=True)
            )
            self.product2 = self.prod_repo.create(
                Product(name="Croissant", price=150, stock_quantity=10, category_id=cat.id, is_active=True)
            )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_base_layout_structure_and_accessibility(self) -> None:
        resp = self.client.get("/")
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)

        # Accessibility landmarks
        self.assertIn('href="#main-content"', html)
        self.assertIn("Skip to main content", html)
        self.assertIn("<header", html)
        self.assertIn('<nav class="flex items-center', html)
        self.assertIn('aria-label="Main navigation"', html)
        self.assertIn('<main id="main-content"', html)
        self.assertIn("<footer", html)

        # Brand link
        self.assertIn("Online Food Shop", html)

        # Navigation links
        self.assertIn("Products", html)
        self.assertIn("Cart", html)
        self.assertIn('id="cart-counter"', html)

    def test_cart_counter_displays_zero_when_cart_is_empty(self) -> None:
        resp = self.client.get("/")
        html = resp.get_data(as_text=True)
        self.assertIn('id="cart-counter"', html)
        self.assertIn('data-count="0"', html)

    def test_cart_counter_reflects_session_cart_items_sum(self) -> None:
        # Pre-seed session with {"1": 2, "2": 1}
        with self.client.session_transaction() as sess:
            sess[SESSION_CART_KEY] = {
                str(self.product1.id): 2,
                str(self.product2.id): 1,
            }

        resp = self.client.get("/")
        html = resp.get_data(as_text=True)
        self.assertIn('data-count="3"', html)
        self.assertIn('Shopping Cart, 3 items', html)

    def test_cart_counter_updates_through_cart_actions(self) -> None:
        # 1. Add product1 (quantity 2)
        resp = self.client.post(
            "/cart/add",
            data={"product_id": str(self.product1.id), "quantity": "2"},
            follow_redirects=True,
        )
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn('data-count="2"', html)

        # 2. Add product2 (quantity 3) -> sum should be 5
        resp = self.client.post(
            "/cart/add",
            data={"product_id": str(self.product2.id), "quantity": "3"},
            follow_redirects=True,
        )
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn('data-count="5"', html)

        # 3. Update product1 to 1 -> sum should be 1 + 3 = 4
        resp = self.client.post(
            "/cart/update",
            data={"product_id": str(self.product1.id), "quantity": "1"},
            follow_redirects=True,
        )
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn('data-count="4"', html)

        # 4. Remove product2 -> sum should be 1
        resp = self.client.post(
            "/cart/remove",
            data={"product_id": str(self.product2.id)},
            follow_redirects=True,
        )
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn('data-count="1"', html)

        # 5. Clear cart -> sum should be 0
        resp = self.client.post("/cart/clear", follow_redirects=True)
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn('data-count="0"', html)

    def test_flash_messages_presentation_and_accessibility(self) -> None:
        # Trigger an error flash message via invalid product id in cart add
        resp_err = self.client.post(
            "/cart/add",
            data={"product_id": "invalid", "quantity": "1"},
            follow_redirects=True,
        )
        html_err = resp_err.get_data(as_text=True)
        self.assertIn("flash flash-error", html_err)
        self.assertIn('role="alert"', html_err)
        self.assertIn('aria-live="assertive"', html_err)
        self.assertIn("Error:", html_err)
        self.assertIn("Invalid product ID.", html_err)

        # Trigger a success flash message via valid cart add
        resp_succ = self.client.post(
            "/cart/add",
            data={"product_id": str(self.product1.id), "quantity": "1"},
            follow_redirects=True,
        )
        html_succ = resp_succ.get_data(as_text=True)
        self.assertIn("flash flash-success", html_succ)
        self.assertIn('role="status"', html_succ)
        self.assertIn("Success:", html_succ)
        self.assertIn("Item added to cart.", html_succ)

        # Trigger a warning flash message via checkout with empty cart
        self.client.post("/cart/clear")
        resp_warn = self.client.get("/checkout", follow_redirects=True)
        html_warn = resp_warn.get_data(as_text=True)
        self.assertIn("flash flash-warning", html_warn)
        self.assertIn('role="status"', html_warn)
        self.assertIn("Warning:", html_warn)
        self.assertIn("Your cart is empty.", html_warn)


if __name__ == "__main__":
    unittest.main()

"""Integration tests for Phase 6 Flask routes and session management."""
from pathlib import Path
import tempfile
import unittest

from app import create_app
from app.config import Config
from app.database.connection import get_db, init_db
from app.models.category import Category
from app.models.customer import Customer
from app.models.product import Product
from app.repositories.category_repository import CategoryRepository
from app.repositories.customer_repository import CustomerRepository
from app.repositories.order_repository import OrderRepository
from app.repositories.product_repository import ProductRepository


class BaseRouteTestCase(unittest.TestCase):
    """Base test case initializing Flask test client with an isolated temporary SQLite database."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_routes.sqlite3"

        class TestConfig(Config):
            DATABASE_PATH = str(self.db_path)
            TESTING = True
            SECRET_KEY = "test-route-secret-key"
            CSRF_ENABLED = False

        self.app = create_app(TestConfig)
        self.client = self.app.test_client()

        with self.app.app_context():
            init_db()
            self.category_repo = CategoryRepository()
            self.product_repo = ProductRepository()
            self.customer_repo = CustomerRepository()
            self.order_repo = OrderRepository()

            # Seed test data
            self.cat_bakery = self.category_repo.create(
                Category(name="Bakery", description="Fresh baked goods")
            )
            self.cat_produce = self.category_repo.create(
                Category(name="Produce", description="Fresh fruits and vegetables")
            )

            self.bread = self.product_repo.create(
                Product(
                    name="Sourdough Bread",
                    price=450,
                    stock_quantity=10,
                    description="Artisan loaf",
                    category_id=self.cat_bakery.id,
                    is_active=True,
                )
            )
            self.apple = self.product_repo.create(
                Product(
                    name="Gala Apple",
                    price=120,
                    stock_quantity=25,
                    description="Sweet red apple",
                    category_id=self.cat_produce.id,
                    is_active=True,
                )
            )
            self.inactive_item = self.product_repo.create(
                Product(
                    name="Seasonal Berries",
                    price=600,
                    stock_quantity=5,
                    description="Out of season",
                    category_id=self.cat_produce.id,
                    is_active=False,
                )
            )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()


class TestCatalogRoutes(BaseRouteTestCase):
    """Tests for catalog browsing, category filtering, and product details."""

    def test_index_displays_active_products(self) -> None:
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)

        self.assertIn("Sourdough Bread", html)
        self.assertIn("Gala Apple", html)
        # Inactive products should not be displayed
        self.assertNotIn("Seasonal Berries", html)
        # Categories should be listed
        self.assertIn("Bakery", html)
        self.assertIn("Produce", html)

    def test_index_filter_by_category(self) -> None:
        # Filter for Bakery
        response = self.client.get(f"/?category_id={self.cat_bakery.id}")
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)

        self.assertIn("Sourdough Bread", html)
        self.assertNotIn("Gala Apple", html)

        # Filter with non-existent category falls back to all active products
        response_fallback = self.client.get("/?category_id=99999")
        self.assertEqual(response_fallback.status_code, 200)
        fallback_html = response_fallback.get_data(as_text=True)
        self.assertIn("Sourdough Bread", fallback_html)
        self.assertIn("Gala Apple", fallback_html)

    def test_product_detail_success(self) -> None:
        response = self.client.get(f"/products/{self.bread.id}")
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)

        self.assertIn("Sourdough Bread", html)
        self.assertIn("Artisan loaf", html)
        self.assertIn("4,50 €", html)
        self.assertIn("Bakery", html)

    def test_product_detail_not_found(self) -> None:
        response = self.client.get("/products/99999")
        self.assertEqual(response.status_code, 404)

    def test_product_detail_inactive_returns_404(self) -> None:
        response = self.client.get(f"/products/{self.inactive_item.id}")
        self.assertEqual(response.status_code, 404)


class TestCartRoutes(BaseRouteTestCase):
    """Tests for shopping cart operations and session storage."""

    def test_view_cart_empty(self) -> None:
        response = self.client.get("/cart/")
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn("Your cart is empty", html)

    def test_add_to_cart_success(self) -> None:
        response = self.client.post(
            "/cart/add",
            data={"product_id": str(self.bread.id), "quantity": "2"},
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)

        self.assertIn("Item added to cart", html)
        self.assertIn("Sourdough Bread", html)
        self.assertIn("9,00 €", html)  # 2 * 450

        # Verify session contains expected data
        with self.client.session_transaction() as sess:
            self.assertEqual(sess.get("cart"), {str(self.bread.id): 2})

    def test_add_to_cart_invalid_product_or_quantity(self) -> None:
        # Invalid product ID
        resp1 = self.client.post(
            "/cart/add",
            data={"product_id": "abc", "quantity": "1"},
            follow_redirects=True,
        )
        self.assertIn("Invalid product ID", resp1.get_data(as_text=True))

        # Non-existent product ID
        resp2 = self.client.post(
            "/cart/add",
            data={"product_id": "99999", "quantity": "1"},
            follow_redirects=True,
        )
        self.assertIn("not found", resp2.get_data(as_text=True))

        # Non-positive quantity
        resp3 = self.client.post(
            "/cart/add",
            data={"product_id": str(self.bread.id), "quantity": "0"},
            follow_redirects=True,
        )
        self.assertIn("Quantity must be a positive integer", resp3.get_data(as_text=True))

        # Inactive product
        resp4 = self.client.post(
            "/cart/add",
            data={"product_id": str(self.inactive_item.id), "quantity": "1"},
            follow_redirects=True,
        )
        self.assertIn("currently unavailable", resp4.get_data(as_text=True))

        # Exceeding stock
        resp5 = self.client.post(
            "/cart/add",
            data={"product_id": str(self.bread.id), "quantity": "100"},
            follow_redirects=True,
        )
        self.assertIn("exceeds available stock", resp5.get_data(as_text=True))

    def test_update_cart_quantity(self) -> None:
        # First add 2 items
        self.client.post(
            "/cart/add",
            data={"product_id": str(self.bread.id), "quantity": "2"},
        )

        # Update to 5 items
        response = self.client.post(
            "/cart/update",
            data={"product_id": str(self.bread.id), "quantity": "5"},
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn("Cart updated successfully", html)
        self.assertIn("22,50 €", html)  # 5 * 450

        with self.client.session_transaction() as sess:
            self.assertEqual(sess.get("cart"), {str(self.bread.id): 5})

    def test_update_cart_quantity_zero_removes_item(self) -> None:
        self.client.post(
            "/cart/add",
            data={"product_id": str(self.bread.id), "quantity": "2"},
        )
        response = self.client.post(
            "/cart/update",
            data={"product_id": str(self.bread.id), "quantity": "0"},
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn("Your cart is empty", html)

        with self.client.session_transaction() as sess:
            self.assertEqual(sess.get("cart"), {})

    def test_remove_from_cart(self) -> None:
        self.client.post(
            "/cart/add",
            data={"product_id": str(self.bread.id), "quantity": "1"},
        )
        response = self.client.post(
            "/cart/remove",
            data={"product_id": str(self.bread.id)},
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn("Item removed from cart", html)

        with self.client.session_transaction() as sess:
            self.assertEqual(sess.get("cart"), {})

    def test_clear_cart(self) -> None:
        self.client.post(
            "/cart/add",
            data={"product_id": str(self.bread.id), "quantity": "1"},
        )
        self.client.post(
            "/cart/add",
            data={"product_id": str(self.apple.id), "quantity": "3"},
        )
        response = self.client.post("/cart/clear", follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn("Cart cleared", html)
        self.assertIn("Your cart is empty", html)

        with self.client.session_transaction() as sess:
            self.assertNotIn("cart", sess)

    def test_cart_session_drops_stale_or_invalid_items_on_load(self) -> None:
        # Simulate session containing a non-existent product ID and an inactive product ID
        with self.client.session_transaction() as sess:
            sess["cart"] = {
                str(self.bread.id): 2,
                "99999": 1,
                str(self.inactive_item.id): 1,
                "invalid_pid": "bad_qty",
            }

        response = self.client.get("/cart/")
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn("Sourdough Bread", html)

        # Stale/invalid entries must have been pruned from session
        with self.client.session_transaction() as sess:
            self.assertEqual(sess.get("cart"), {str(self.bread.id): 2})


class TestCheckoutAndOrderRoutes(BaseRouteTestCase):
    """Tests for checkout submission, customer reuse, transactional checkout, and confirmation access control."""

    def test_checkout_get_empty_cart_redirects_to_cart(self) -> None:
        response = self.client.get("/checkout", follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn("Your cart is empty", html)

    def test_checkout_get_with_items_renders_form(self) -> None:
        self.client.post(
            "/cart/add",
            data={"product_id": str(self.bread.id), "quantity": "1"},
        )
        response = self.client.get("/checkout")
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn("Checkout", html)
        self.assertIn("Sourdough Bread", html)
        self.assertIn("Full Name", html)

    def test_checkout_post_missing_fields_redirects_with_error(self) -> None:
        self.client.post(
            "/cart/add",
            data={"product_id": str(self.bread.id), "quantity": "1"},
        )

        # Missing name
        resp1 = self.client.post(
            "/checkout",
            data={"name": "", "email": "alice@example.com"},
            follow_redirects=True,
        )
        self.assertIn("Customer name is required", resp1.get_data(as_text=True))

        # Invalid email
        resp2 = self.client.post(
            "/checkout",
            data={"name": "Alice", "email": "invalid-email"},
            follow_redirects=True,
        )
        self.assertIn("valid email address is required", resp2.get_data(as_text=True))

    def test_checkout_post_new_customer_success(self) -> None:
        self.client.post(
            "/cart/add",
            data={"product_id": str(self.bread.id), "quantity": "2"},  # 2 * 450 = 900
        )
        self.client.post(
            "/cart/add",
            data={"product_id": str(self.apple.id), "quantity": "1"},  # 1 * 120 = 120
        )

        response = self.client.post(
            "/checkout",
            data={
                "name": "Bob Builder",
                "email": "bob@example.com",
                "phone": "555-9876",
                "address": "123 Main St",
            },
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)

        self.assertIn("Order Confirmation", html)
        self.assertIn("Bob Builder", html)
        self.assertIn("bob@example.com", html)
        self.assertIn("Sourdough Bread", html)
        self.assertIn("Gala Apple", html)
        self.assertIn("10,20 €", html)  # 900 + 120

        # Verify cart session was cleared
        with self.client.session_transaction() as sess:
            self.assertNotIn("cart", sess)
            order_id = sess.get("last_order_id")
            self.assertIsNotNone(order_id)

        # Verify stock was decremented in database
        with self.app.app_context():
            updated_bread = self.product_repo.get_by_id(self.bread.id)
            updated_apple = self.product_repo.get_by_id(self.apple.id)
            self.assertEqual(updated_bread.stock_quantity, 8)  # 10 - 2
            self.assertEqual(updated_apple.stock_quantity, 24)  # 25 - 1

    def test_checkout_post_existing_customer_reuses_id_without_modifying_profile(self) -> None:
        # Pre-register customer with original details
        with self.app.app_context():
            orig_customer = self.customer_repo.create(
                Customer(
                    name="Charlie Original",
                    email="charlie@example.com",
                    phone="111-2222",
                    address="Original Address 100",
                )
            )

        self.client.post(
            "/cart/add",
            data={"product_id": str(self.apple.id), "quantity": "2"},
        )

        # Checkout submitting different name, phone, and address for same email
        response = self.client.post(
            "/checkout",
            data={
                "name": "Charlie Updated Name",
                "email": "charlie@example.com",
                "phone": "999-9999",
                "address": "New Address 200",
            },
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)

        with self.app.app_context():
            # Customer ID must match
            customer_after = self.customer_repo.get_by_email("charlie@example.com")
            self.assertIsNotNone(customer_after)
            self.assertEqual(customer_after.id, orig_customer.id)
            # Stored profile must remain strictly unmodified as per decision 2
            self.assertEqual(customer_after.name, "Charlie Original")
            self.assertEqual(customer_after.phone, "111-2222")
            self.assertEqual(customer_after.address, "Original Address 100")

    def test_checkout_post_stock_failure_preserves_cart(self) -> None:
        self.client.post(
            "/cart/add",
            data={"product_id": str(self.bread.id), "quantity": "5"},
        )

        # Simulate another buyer exhausting bread stock right before checkout
        with self.app.app_context():
            b = self.product_repo.get_by_id(self.bread.id)
            b.stock_quantity = 2  # Only 2 left, but user requested 5
            self.product_repo.update(b)

        response = self.client.post(
            "/checkout",
            data={
                "name": "Dana",
                "email": "dana@example.com",
            },
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)

        self.assertIn("exceeds available stock", html)

        # Cart must remain preserved in session
        with self.client.session_transaction() as sess:
            self.assertEqual(sess.get("cart"), {str(self.bread.id): 5})
            self.assertNotIn("last_order_id", sess)

    def test_checkout_post_order_service_error_preserves_cart(self) -> None:
        from unittest.mock import patch

        self.client.post(
            "/cart/add",
            data={"product_id": str(self.bread.id), "quantity": "1"},
        )
        with patch(
            "app.services.order_service.OrderService.checkout",
            side_effect=ValueError("Simulated checkout failure"),
        ):
            response = self.client.post(
                "/checkout",
                data={"name": "Dana", "email": "dana@example.com"},
                follow_redirects=True,
            )
            self.assertEqual(response.status_code, 200)
            html = response.get_data(as_text=True)
            self.assertIn("Checkout could not be completed", html)
            self.assertIn("Simulated checkout failure", html)

            with self.client.session_transaction() as sess:
                self.assertEqual(sess.get("cart"), {str(self.bread.id): 1})
                self.assertNotIn("last_order_id", sess)

    def test_order_confirmation_access_control(self) -> None:
        # Place a valid order
        self.client.post(
            "/cart/add",
            data={"product_id": str(self.apple.id), "quantity": "1"},
        )
        checkout_resp = self.client.post(
            "/checkout",
            data={"name": "Evan", "email": "evan@example.com"},
        )
        self.assertEqual(checkout_resp.status_code, 302)

        with self.client.session_transaction() as sess:
            valid_order_id = sess.get("last_order_id")

        # 1. Access with the same session returns 200
        resp_allowed = self.client.get(f"/orders/{valid_order_id}")
        self.assertEqual(resp_allowed.status_code, 200)
        self.assertIn(f"Order #{valid_order_id}", resp_allowed.get_data(as_text=True))

        # 2. Access with another client (or session without matching last_order_id) returns 404
        other_client = self.app.test_client()
        resp_forbidden = other_client.get(f"/orders/{valid_order_id}")
        self.assertEqual(resp_forbidden.status_code, 404)

        # 3. Access with tampered last_order_id returns 404
        with self.client.session_transaction() as sess:
            sess["last_order_id"] = 99999
        resp_tampered = self.client.get(f"/orders/{valid_order_id}")
        self.assertEqual(resp_tampered.status_code, 404)

    def test_order_confirmation_nonexistent_order_returns_404(self) -> None:
        with self.client.session_transaction() as sess:
            sess["last_order_id"] = 99999

        response = self.client.get("/orders/99999")
        self.assertEqual(response.status_code, 404)


if __name__ == "__main__":
    unittest.main()

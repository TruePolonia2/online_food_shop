"""End-to-End User Journey integration tests for the online food shop application (Phase 8 Step 3).

Verifies realistic multi-step user workflows through the entire application stack:
HTTP routing, Jinja2 template rendering, CSRF protection, session management,
business services, repository layer, and SQLite database transactions.
"""
import os
from pathlib import Path
import re
import secrets
import tempfile
import unittest

from app import create_app
from app.config import Config, ProductionConfig
from app.database.connection import close_db, init_db
from app.models.category import Category
from app.models.product import Product
from app.repositories.category_repository import CategoryRepository
from app.repositories.customer_repository import CustomerRepository
from app.repositories.order_repository import OrderRepository
from app.repositories.product_repository import ProductRepository
from app.routes.cart_session import SESSION_CART_KEY


class BaseE2ETestCase(unittest.TestCase):
    """Base test case providing an isolated temporary SQLite database, persistent client,

    and catalog seed data for End-to-End testing.
    """

    def setUp(self) -> None:
        self._original_env = dict(os.environ)

        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "e2e_journey.sqlite3"

        class E2ETestConfig(Config):
            DATABASE_PATH = str(self.db_path)
            TESTING = True
            SECRET_KEY = "test-e2e-persistent-secret-key-32-bytes"
            CSRF_ENABLED = True  # Explicitly keep CSRF active

        self.app = create_app(E2ETestConfig)
        self.client = self.app.test_client()

        with self.app.app_context():
            init_db()
            self.cat_repo = CategoryRepository()
            self.prod_repo = ProductRepository()
            self.cust_repo = CustomerRepository()
            self.order_repo = OrderRepository()

            # Seed standard catalog categories
            self.cat_bakery = self.cat_repo.create(Category(name="Bakery", description="Fresh baked goods"))
            self.cat_produce = self.cat_repo.create(Category(name="Produce", description="Fresh fruits and veg"))
            self.cat_pantry = self.cat_repo.create(Category(name="Pantry", description="Pantry staples"))

            # Seed standard catalog products
            self.bread = self.prod_repo.create(
                Product(
                    name="Sourdough Bread",
                    price=350,  # 3,50 €
                    stock_quantity=10,
                    description="Artisan sourdough loaf",
                    category_id=self.cat_bakery.id,
                    is_active=True,
                )
            )
            self.apple = self.prod_repo.create(
                Product(
                    name="Gala Apple",
                    price=120,  # 1,20 €
                    stock_quantity=20,
                    description="Crisp red apple",
                    category_id=self.cat_produce.id,
                    is_active=True,
                )
            )
            self.olive_oil = self.prod_repo.create(
                Product(
                    name="Extra Virgin Olive Oil",
                    price=850,  # 8,50 €
                    stock_quantity=5,
                    description="Cold pressed olive oil 500ml",
                    category_id=self.cat_pantry.id,
                    is_active=True,
                )
            )
            self.seasonal_berries = self.prod_repo.create(
                Product(
                    name="Seasonal Berries",
                    price=500,  # 5,00 €
                    stock_quantity=5,
                    description="Unavailable berry box",
                    category_id=self.cat_produce.id,
                    is_active=False,  # Inactive product
                )
            )

    def tearDown(self) -> None:
        with self.app.app_context():
            close_db()
        self.temp_dir.cleanup()
        os.environ.clear()
        os.environ.update(self._original_env)

    def extract_csrf_token(self, html: str) -> str:
        """Extract the CSRF token from the first rendered form in HTML."""
        match = re.search(r'name="csrf_token"\s+value="([a-f0-9]{64})"', html)
        if not match:
            match = re.search(r'value="([a-f0-9]{64})"\s+name="csrf_token"', html)
        self.assertIsNotNone(match, "Expected a valid CSRF token in the rendered HTML form.")
        return match.group(1)  # type: ignore[union-attr]


class TestEndToEndUserJourneys(BaseE2ETestCase):
    """End-to-End user journey test suite covering complete purchase flows,

    cart modifications, stock depletion, CSRF validation, access control, and production settings.
    """

    def test_complete_purchase_journey_with_csrf_enabled(self) -> None:
        """Scenario 1: Complete happy-path purchase journey with one persistent client

        and CSRF enabled: catalog -> add products -> cart -> checkout -> confirmation.
        """
        # Step 1: Browse catalog homepage
        resp_catalog = self.client.get("/")
        self.assertEqual(resp_catalog.status_code, 200)
        catalog_html = resp_catalog.get_data(as_text=True)
        self.assertIn("Sourdough Bread", catalog_html)
        self.assertIn("3,50 €", catalog_html)
        self.assertIn("Gala Apple", catalog_html)
        self.assertNotIn("Seasonal Berries", catalog_html)

        token_catalog = self.extract_csrf_token(catalog_html)

        # Step 2: Add Sourdough Bread (quantity 2) from catalog
        resp_add1 = self.client.post(
            "/cart/add",
            data={
                "product_id": str(self.bread.id),
                "quantity": "2",
                "csrf_token": token_catalog,
            },
            follow_redirects=True,
        )
        self.assertEqual(resp_add1.status_code, 200)
        cart_html1 = resp_add1.get_data(as_text=True)
        self.assertIn("Item added to cart", cart_html1)
        self.assertIn("Sourdough Bread", cart_html1)
        self.assertIn("7,00 €", cart_html1)  # 2 * 3.50 €

        # Step 3: Visit Gala Apple detail page
        resp_detail = self.client.get(f"/products/{self.apple.id}")
        self.assertEqual(resp_detail.status_code, 200)
        detail_html = resp_detail.get_data(as_text=True)
        self.assertIn("Gala Apple", detail_html)
        self.assertIn("1,20 €", detail_html)

        token_detail = self.extract_csrf_token(detail_html)

        # Step 4: Add Gala Apple (quantity 3) from detail page
        resp_add2 = self.client.post(
            "/cart/add",
            data={
                "product_id": str(self.apple.id),
                "quantity": "3",
                "csrf_token": token_detail,
            },
            follow_redirects=True,
        )
        self.assertEqual(resp_add2.status_code, 200)
        cart_html2 = resp_add2.get_data(as_text=True)
        self.assertIn("Item added to cart", cart_html2)
        self.assertIn("Sourdough Bread", cart_html2)
        self.assertIn("7,00 €", cart_html2)
        self.assertIn("Gala Apple", cart_html2)
        self.assertIn("3,60 €", cart_html2)  # 3 * 1.20 €
        self.assertIn("10,60 €", cart_html2)  # Subtotal / Total: 7.00 + 3.60

        # Step 5: Proceed to checkout form
        resp_checkout_form = self.client.get("/checkout")
        self.assertEqual(resp_checkout_form.status_code, 200)
        checkout_form_html = resp_checkout_form.get_data(as_text=True)
        self.assertIn("Checkout", checkout_form_html)
        self.assertIn("Order Summary", checkout_form_html)
        self.assertIn("10,60 €", checkout_form_html)

        token_checkout = self.extract_csrf_token(checkout_form_html)

        # Step 6: Submit valid checkout form
        resp_checkout_post = self.client.post(
            "/checkout",
            data={
                "name": "Maria Silva",
                "email": "maria.silva@example.pt",
                "phone": "912345678",
                "address": "Rua das Flores 123, Porto",
                "csrf_token": token_checkout,
            },
            follow_redirects=True,
        )
        self.assertEqual(resp_checkout_post.status_code, 200)
        confirmation_html = resp_checkout_post.get_data(as_text=True)

        # Step 7: Verify order confirmation view
        self.assertIn("Order Confirmation", confirmation_html)
        self.assertIn("Your purchase has been placed successfully", confirmation_html)
        self.assertIn("Status: Pending", confirmation_html)
        self.assertIn("Maria Silva", confirmation_html)
        self.assertIn("maria.silva@example.pt", confirmation_html)
        self.assertIn("912345678", confirmation_html)
        self.assertIn("Rua das Flores 123, Porto", confirmation_html)
        self.assertIn("Sourdough Bread", confirmation_html)
        self.assertIn("7,00 €", confirmation_html)
        self.assertIn("Gala Apple", confirmation_html)
        self.assertIn("3,60 €", confirmation_html)
        self.assertIn("10,60 €", confirmation_html)

        # Step 8: Verify database records and stock decrements
        with self.app.app_context():
            # Customer record created
            customer = self.cust_repo.get_by_email("maria.silva@example.pt")
            self.assertIsNotNone(customer)
            self.assertEqual(customer.name, "Maria Silva")

            # Order record created
            orders = self.order_repo.get_by_customer_id(customer.id)
            self.assertEqual(len(orders), 1)
            order = orders[0]
            self.assertEqual(order.total, 1060)  # 10,60 € in integer cents
            self.assertEqual(len(order.items), 2)

            # OrderItem snapshot prices preserved
            items_by_pid = {item.product_id: item for item in order.items}
            self.assertEqual(items_by_pid[self.bread.id].unit_price, 350)
            self.assertEqual(items_by_pid[self.bread.id].quantity, 2)
            self.assertEqual(items_by_pid[self.apple.id].unit_price, 120)
            self.assertEqual(items_by_pid[self.apple.id].quantity, 3)

            # Product stock decremented
            updated_bread = self.prod_repo.get_by_id(self.bread.id)
            updated_apple = self.prod_repo.get_by_id(self.apple.id)
            self.assertEqual(updated_bread.stock_quantity, 8)  # 10 - 2
            self.assertEqual(updated_apple.stock_quantity, 17)  # 20 - 3

        # Step 9: Verify session cart cleared and confirmation page refreshable
        with self.client.session_transaction() as sess:
            self.assertNotIn(SESSION_CART_KEY, sess)
            order_id = sess.get("last_order_id")
            self.assertEqual(order_id, order.id)

        # Refreshing confirmation in same session succeeds
        resp_refresh = self.client.get(f"/orders/{order.id}")
        self.assertEqual(resp_refresh.status_code, 200)
        self.assertIn("Order Confirmation", resp_refresh.get_data(as_text=True))

    def test_cart_modifications_flow_and_final_checkout(self) -> None:
        """Scenario 2: Multiple cart modifications (update quantity, remove item,

        add new item) followed by checkout, verifying final totals and exact stock deductions.
        """
        # Step 1: Add Bread (qty=2) and Olive Oil (qty=4)
        resp_catalog = self.client.get("/")
        token = self.extract_csrf_token(resp_catalog.get_data(as_text=True))

        self.client.post(
            "/cart/add",
            data={"product_id": str(self.bread.id), "quantity": "2", "csrf_token": token},
        )
        self.client.post(
            "/cart/add",
            data={"product_id": str(self.olive_oil.id), "quantity": "4", "csrf_token": token},
        )

        # Step 2: Visit cart page
        resp_cart = self.client.get("/cart/")
        token_cart = self.extract_csrf_token(resp_cart.get_data(as_text=True))

        # Step 3: Update Bread quantity to 4
        resp_update = self.client.post(
            "/cart/update",
            data={"product_id": str(self.bread.id), "quantity": "4", "csrf_token": token_cart},
            follow_redirects=True,
        )
        self.assertEqual(resp_update.status_code, 200)
        self.assertIn("Cart updated successfully", resp_update.get_data(as_text=True))

        # Step 4: Remove Olive Oil from cart
        token_cart2 = self.extract_csrf_token(resp_update.get_data(as_text=True))
        resp_remove = self.client.post(
            "/cart/remove",
            data={"product_id": str(self.olive_oil.id), "csrf_token": token_cart2},
            follow_redirects=True,
        )
        self.assertEqual(resp_remove.status_code, 200)
        self.assertIn("Item removed from cart", resp_remove.get_data(as_text=True))
        self.assertNotIn("Extra Virgin Olive Oil", resp_remove.get_data(as_text=True))

        # Step 5: Add Apple (quantity 5)
        resp_detail = self.client.get(f"/products/{self.apple.id}")
        token_apple = self.extract_csrf_token(resp_detail.get_data(as_text=True))
        self.client.post(
            "/cart/add",
            data={"product_id": str(self.apple.id), "quantity": "5", "csrf_token": token_apple},
            follow_redirects=True,
        )

        # Step 6: Checkout and submit
        # Expected cart: 4 Breads (4 * 3.50 = 14.00 €) + 5 Apples (5 * 1.20 = 6.00 €) = 20,00 €
        resp_checkout_form = self.client.get("/checkout")
        token_checkout = self.extract_csrf_token(resp_checkout_form.get_data(as_text=True))

        resp_order = self.client.post(
            "/checkout",
            data={
                "name": "Carlos Santos",
                "email": "carlos@example.pt",
                "csrf_token": token_checkout,
            },
            follow_redirects=True,
        )
        self.assertEqual(resp_order.status_code, 200)
        order_html = resp_order.get_data(as_text=True)
        self.assertIn("20,00 €", order_html)

        # Step 7: Verify database stock levels
        with self.app.app_context():
            updated_bread = self.prod_repo.get_by_id(self.bread.id)
            updated_apple = self.prod_repo.get_by_id(self.apple.id)
            updated_oil = self.prod_repo.get_by_id(self.olive_oil.id)

            # Bread: 10 - 4 = 6
            self.assertEqual(updated_bread.stock_quantity, 6)
            # Apple: 20 - 5 = 15
            self.assertEqual(updated_apple.stock_quantity, 15)
            # Olive Oil: NOT purchased, stock remains untouched at 5
            self.assertEqual(updated_oil.stock_quantity, 5)

    def test_mid_session_stock_depletion_fails_gracefully_and_preserves_cart(self) -> None:
        """Scenario 3: Stock depletion occurs between adding an item and checkout submission.

        Verifies atomic rollback, error message display, and session cart preservation.
        """
        # User adds 4 bottles of Olive Oil (stock was 5)
        resp_cat = self.client.get("/")
        token = self.extract_csrf_token(resp_cat.get_data(as_text=True))
        self.client.post(
            "/cart/add",
            data={"product_id": str(self.olive_oil.id), "quantity": "4", "csrf_token": token},
        )

        # User views checkout page while stock is available
        resp_checkout = self.client.get("/checkout")
        self.assertEqual(resp_checkout.status_code, 200)
        token_checkout = self.extract_csrf_token(resp_checkout.get_data(as_text=True))

        # Deterministic concurrent purchase: Another process reduces available stock to 2
        with self.app.app_context():
            oil = self.prod_repo.get_by_id(self.olive_oil.id)
            oil.stock_quantity = 2
            self.prod_repo.update(oil)

        # User attempts to submit checkout with token obtained prior to depletion
        resp_submit = self.client.post(
            "/checkout",
            data={
                "name": "David Costa",
                "email": "david@example.pt",
                "csrf_token": token_checkout,
            },
            follow_redirects=True,
        )
        self.assertEqual(resp_submit.status_code, 200)
        error_html = resp_submit.get_data(as_text=True)
        self.assertIn("exceeds available stock", error_html)

        # Direct navigation to /checkout while depleted also safely redirects to /cart
        resp_direct_checkout = self.client.get("/checkout", follow_redirects=True)
        self.assertEqual(resp_direct_checkout.status_code, 200)
        self.assertIn("exceeds available stock", resp_direct_checkout.get_data(as_text=True))

        # Verify atomic rollback: No order created and stock remains 2
        with self.app.app_context():
            orders = self.order_repo.get_all()
            self.assertEqual(len(orders), 0)
            oil_after = self.prod_repo.get_by_id(self.olive_oil.id)
            self.assertEqual(oil_after.stock_quantity, 2)

        # Verify session cart remains preserved so user does not lose items
        with self.client.session_transaction() as sess:
            self.assertEqual(sess.get(SESSION_CART_KEY), {str(self.olive_oil.id): 4})
            self.assertNotIn("last_order_id", sess)

    def test_product_deactivation_during_active_session_purges_item(self) -> None:
        """Scenario 4: Product is deactivated by merchant while in a user's active session.

        Verifies that stale inactive products are automatically purged and only valid items proceed.
        """
        # User adds Bread (qty=1) and Apple (qty=2)
        resp_cat = self.client.get("/")
        token = self.extract_csrf_token(resp_cat.get_data(as_text=True))
        self.client.post(
            "/cart/add",
            data={"product_id": str(self.bread.id), "quantity": "1", "csrf_token": token},
        )
        self.client.post(
            "/cart/add",
            data={"product_id": str(self.apple.id), "quantity": "2", "csrf_token": token},
        )

        # Merchant deactivates Bread in the database
        with self.app.app_context():
            bread_db = self.prod_repo.get_by_id(self.bread.id)
            bread_db.is_active = False
            self.prod_repo.update(bread_db)

        # User visits cart: Bread should be purged from view and session
        resp_cart = self.client.get("/cart/")
        cart_html = resp_cart.get_data(as_text=True)
        self.assertNotIn("Sourdough Bread", cart_html)
        self.assertIn("Gala Apple", cart_html)
        self.assertIn("2,40 €", cart_html)  # 2 * 1.20 €

        # User proceeds to checkout
        token_cart = self.extract_csrf_token(cart_html)
        resp_order = self.client.post(
            "/checkout",
            data={
                "name": "Elena Ramos",
                "email": "elena@example.pt",
                "csrf_token": token_cart,
            },
            follow_redirects=True,
        )
        self.assertEqual(resp_order.status_code, 200)
        order_html = resp_order.get_data(as_text=True)
        self.assertIn("Order Confirmation", order_html)
        self.assertIn("2,40 €", order_html)

        # Verify database: Only Apple was ordered and stock decremented
        with self.app.app_context():
            apple_after = self.prod_repo.get_by_id(self.apple.id)
            bread_after = self.prod_repo.get_by_id(self.bread.id)
            self.assertEqual(apple_after.stock_quantity, 18)  # 20 - 2
            self.assertEqual(bread_after.stock_quantity, 10)  # Inactive bread unchanged

    def test_csrf_rejection_at_checkout_preserves_cart_and_stock(self) -> None:
        """Scenario 5: CSRF token missing or forged at checkout is rejected with HTTP 400

        without losing cart contents or altering inventory.
        """
        resp_cat = self.client.get("/")
        token = self.extract_csrf_token(resp_cat.get_data(as_text=True))
        self.client.post(
            "/cart/add",
            data={"product_id": str(self.bread.id), "quantity": "2", "csrf_token": token},
        )

        # Submitting without token is rejected with 400
        resp_missing = self.client.post(
            "/checkout",
            data={"name": "Attacker", "email": "attacker@example.com"},
        )
        self.assertEqual(resp_missing.status_code, 400)
        self.assertIn("Invalid or missing CSRF token", resp_missing.get_data(as_text=True))

        # Submitting with forged token is rejected with 400
        resp_forged = self.client.post(
            "/checkout",
            data={
                "name": "Attacker",
                "email": "attacker@example.com",
                "csrf_token": "a" * 64,
            },
        )
        self.assertEqual(resp_forged.status_code, 400)

        # Verify no orders created and stock is untouched
        with self.app.app_context():
            self.assertEqual(len(self.order_repo.get_all()), 0)
            self.assertEqual(self.prod_repo.get_by_id(self.bread.id).stock_quantity, 10)

        # Verify cart session is preserved
        with self.client.session_transaction() as sess:
            self.assertEqual(sess.get(SESSION_CART_KEY), {str(self.bread.id): 2})

        # Recovery: Legitimate checkout succeeds
        resp_form = self.client.get("/checkout")
        legit_token = self.extract_csrf_token(resp_form.get_data(as_text=True))
        resp_success = self.client.post(
            "/checkout",
            data={
                "name": "Legitimate User",
                "email": "legit@example.pt",
                "csrf_token": legit_token,
            },
            follow_redirects=True,
        )
        self.assertEqual(resp_success.status_code, 200)
        self.assertIn("Order Confirmation", resp_success.get_data(as_text=True))

    def test_order_confirmation_access_control_and_refresh(self) -> None:
        """Scenario 6: Order confirmation access isolation (IDOR protection) and refresh tolerance."""
        # Client 1 places order #1
        resp_cat = self.client.get("/")
        token = self.extract_csrf_token(resp_cat.get_data(as_text=True))
        self.client.post(
            "/cart/add",
            data={"product_id": str(self.apple.id), "quantity": "1", "csrf_token": token},
        )

        resp_form = self.client.get("/checkout")
        token_chk = self.extract_csrf_token(resp_form.get_data(as_text=True))
        resp_order1 = self.client.post(
            "/checkout",
            data={"name": "Client One", "email": "client1@example.pt", "csrf_token": token_chk},
            follow_redirects=True,
        )
        self.assertEqual(resp_order1.status_code, 200)

        with self.client.session_transaction() as sess:
            order1_id = sess.get("last_order_id")

        # Authorized client can view and refresh order confirmation
        resp_view1 = self.client.get(f"/orders/{order1_id}")
        self.assertEqual(resp_view1.status_code, 200)
        self.assertIn(f"Order #{order1_id}", resp_view1.get_data(as_text=True))

        # Different client (separate session) attempting to view order #1 is rejected with 404
        other_client = self.app.test_client()
        resp_unauthorized = other_client.get(f"/orders/{order1_id}")
        self.assertEqual(resp_unauthorized.status_code, 404)

        # Client 1 places order #2: last_order_id advances to order #2
        resp_cat2 = self.client.get("/")
        token2 = self.extract_csrf_token(resp_cat2.get_data(as_text=True))
        self.client.post(
            "/cart/add",
            data={"product_id": str(self.bread.id), "quantity": "1", "csrf_token": token2},
        )
        resp_form2 = self.client.get("/checkout")
        token_chk2 = self.extract_csrf_token(resp_form2.get_data(as_text=True))
        resp_order2 = self.client.post(
            "/checkout",
            data={"name": "Client One", "email": "client1@example.pt", "csrf_token": token_chk2},
            follow_redirects=True,
        )
        self.assertEqual(resp_order2.status_code, 200)

        with self.client.session_transaction() as sess:
            order2_id = sess.get("last_order_id")

        self.assertNotEqual(order1_id, order2_id)

        # Client 1 can access order #2, but access to previous order #1 is now 404
        self.assertEqual(self.client.get(f"/orders/{order2_id}").status_code, 200)
        self.assertEqual(self.client.get(f"/orders/{order1_id}").status_code, 404)

        # Accessing non-existent order ID returns 404
        self.assertEqual(self.client.get("/orders/99999").status_code, 404)

    def test_e2e_journey_under_production_config(self) -> None:
        """Scenario 7: Full purchase journey running under ProductionConfig with HTTPS simulation

        confirming strict cookie flags (Secure, HttpOnly, SameSite=Lax) work end-to-end.
        """
        # Create ProductionConfig test application with isolated DB and valid secret key
        os.environ["SECRET_KEY"] = secrets.token_hex(32)

        class ProdE2EConfig(ProductionConfig):
            DATABASE_PATH = str(self.db_path)
            TESTING = True

        prod_app = create_app(ProdE2EConfig)
        self.assertTrue(prod_app.config["SESSION_COOKIE_SECURE"])
        self.assertTrue(prod_app.config["SESSION_COOKIE_HTTPONLY"])
        self.assertEqual(prod_app.config["SESSION_COOKIE_SAMESITE"], "Lax")

        # Test client simulating HTTPS requests
        https_client = prod_app.test_client()

        # Step 1: Browse catalog over HTTPS
        resp_cat = https_client.get("/", base_url="https://localhost")
        self.assertEqual(resp_cat.status_code, 200)

        # Verify session cookie has Secure attribute
        session_cookie = https_client.get_cookie("session")
        if session_cookie:
            self.assertTrue(session_cookie.secure)

        token = self.extract_csrf_token(resp_cat.get_data(as_text=True))

        # Step 2: Add item to cart over HTTPS
        resp_add = https_client.post(
            "/cart/add",
            data={"product_id": str(self.bread.id), "quantity": "1", "csrf_token": token},
            base_url="https://localhost",
            follow_redirects=True,
        )
        self.assertEqual(resp_add.status_code, 200)
        self.assertIn("Item added to cart", resp_add.get_data(as_text=True))

        # Step 3: Checkout and confirmation over HTTPS
        resp_form = https_client.get("/checkout", base_url="https://localhost")
        token_chk = self.extract_csrf_token(resp_form.get_data(as_text=True))

        resp_order = https_client.post(
            "/checkout",
            data={
                "name": "Fernando Pessoa",
                "email": "pessoa@example.pt",
                "csrf_token": token_chk,
            },
            base_url="https://localhost",
            follow_redirects=True,
        )
        self.assertEqual(resp_order.status_code, 200)
        self.assertIn("Order Confirmation", resp_order.get_data(as_text=True))
        self.assertIn("Fernando Pessoa", resp_order.get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()

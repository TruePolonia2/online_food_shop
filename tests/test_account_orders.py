"""Tests for authenticated checkout ownership, private order history, and confirmation access control."""
from pathlib import Path
import tempfile
import unittest

from app import create_app
from app.config import Config
from app.database.connection import init_db
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


class TestAccountOrders(unittest.TestCase):
    """Integration and security test suite for Phase 10.5."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_account_orders.sqlite3"

        class TestAccountConfig(Config):
            DATABASE_PATH = str(self.db_path)
            TESTING = True
            SECRET_KEY = "test-account-secret-key"
            CSRF_ENABLED = False

        self.app = create_app(TestAccountConfig)
        self.client = self.app.test_client()

        with self.app.app_context():
            init_db()
            self.user_repo = UserRepository()
            self.category_repo = CategoryRepository()
            self.product_repo = ProductRepository()
            self.customer_repo = CustomerRepository()
            self.order_repo = OrderRepository()

            # Seed Category and Products
            cat = self.category_repo.create(Category(name="Bakery"))
            self.bread = self.product_repo.create(
                Product(name="Artisan Bread", price=450, stock_quantity=10, category_id=cat.id)
            )
            self.croissant = self.product_repo.create(
                Product(name="Butter Croissant", price=250, stock_quantity=15, category_id=cat.id)
            )

            # Seed Users
            pwd_hash = AuthService.hash_password("Password123!")
            self.user_a = self.user_repo.create(
                User(email="user_a@example.com", password_hash=pwd_hash, role=UserRole.CUSTOMER, is_verified=True)
            )
            self.user_b = self.user_repo.create(
                User(email="user_b@example.com", password_hash=pwd_hash, role=UserRole.CUSTOMER, is_verified=True)
            )
            self.unverified_user = self.user_repo.create(
                User(email="unverified@example.com", password_hash=pwd_hash, role=UserRole.CUSTOMER, is_verified=False)
            )
            self.owner_user = self.user_repo.create(
                User(email="owner@example.com", password_hash=pwd_hash, role=UserRole.OWNER, is_verified=True)
            )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    # -------------------------------------------------------------------------
    # 1. Authenticated Checkout Tests
    # -------------------------------------------------------------------------

    def test_authenticated_customer_checkout_assigns_order_user_id(self) -> None:
        # User A logs in
        with self.client.session_transaction() as sess:
            sess["user_id"] = self.user_a.id

        self.client.post("/cart/add", data={"product_id": str(self.bread.id), "quantity": "2"})

        resp = self.client.post(
            "/checkout",
            data={
                "name": "User A Delivery",
                "email": "user_a@example.com",
                "phone": "911-111-111",
                "address": "Street A, Lisboa",
            },
            follow_redirects=True,
        )
        self.assertEqual(resp.status_code, 200)

        with self.app.app_context():
            orders = self.order_repo.get_by_user_id(self.user_a.id)
            self.assertEqual(len(orders), 1)
            order = orders[0]
            self.assertEqual(order.user_id, self.user_a.id)
            self.assertEqual(order.customer_name, "User A Delivery")
            self.assertEqual(order.total, 900)  # 2 * 450

    def test_guest_checkout_continues_to_produce_null_user_id(self) -> None:
        # Anonymous client checkout
        self.client.post("/cart/add", data={"product_id": str(self.croissant.id), "quantity": "1"})

        resp = self.client.post(
            "/checkout",
            data={
                "name": "Guest Shopper",
                "email": "guest@example.com",
                "phone": "922-222-222",
                "address": "Guest Ave 10, Porto",
            },
            follow_redirects=True,
        )
        self.assertEqual(resp.status_code, 200)

        with self.app.app_context():
            orders = self.order_repo.get_all()
            self.assertEqual(len(orders), 1)
            order = orders[0]
            self.assertIsNone(order.user_id)
            self.assertEqual(order.customer_name, "Guest Shopper")

    def test_unverified_authenticated_customer_checkout_works(self) -> None:
        with self.client.session_transaction() as sess:
            sess["user_id"] = self.unverified_user.id

        self.client.post("/cart/add", data={"product_id": str(self.bread.id), "quantity": "1"})

        resp = self.client.post(
            "/checkout",
            data={
                "name": "Unverified Buyer",
                "email": "unverified@example.com",
            },
            follow_redirects=True,
        )
        self.assertEqual(resp.status_code, 200)

        with self.app.app_context():
            orders = self.order_repo.get_by_user_id(self.unverified_user.id)
            self.assertEqual(len(orders), 1)
            self.assertEqual(orders[0].user_id, self.unverified_user.id)

    def test_form_submitted_forged_user_id_is_ignored(self) -> None:
        with self.client.session_transaction() as sess:
            sess["user_id"] = self.user_a.id

        self.client.post("/cart/add", data={"product_id": str(self.bread.id), "quantity": "1"})

        # Attacker tries to inject user_id for User B in the form payload
        resp = self.client.post(
            "/checkout",
            data={
                "name": "Sneaky Buyer",
                "email": "user_a@example.com",
                "user_id": str(self.user_b.id),
                "role": "owner",
            },
            follow_redirects=True,
        )
        self.assertEqual(resp.status_code, 200)

        with self.app.app_context():
            # Must belong strictly to User A (from session), never User B (from form)
            user_b_orders = self.order_repo.get_by_user_id(self.user_b.id)
            self.assertEqual(len(user_b_orders), 0)

            user_a_orders = self.order_repo.get_by_user_id(self.user_a.id)
            self.assertEqual(len(user_a_orders), 1)
            self.assertEqual(user_a_orders[0].user_id, self.user_a.id)

    def test_deleted_or_invalid_session_identity_does_not_grant_order_ownership(self) -> None:
        with self.client.session_transaction() as sess:
            sess["user_id"] = 999999  # Nonexistent/deleted user

        self.client.post("/cart/add", data={"product_id": str(self.bread.id), "quantity": "1"})

        resp = self.client.post(
            "/checkout",
            data={"name": "Ghost Buyer", "email": "ghost@example.com"},
            follow_redirects=True,
        )
        self.assertEqual(resp.status_code, 200)

        with self.app.app_context():
            orders = self.order_repo.get_all()
            self.assertEqual(len(orders), 1)
            # Must remain unclaimed guest order
            self.assertIsNone(orders[0].user_id)

        # Stale user_id popped from session
        with self.client.session_transaction() as sess:
            self.assertNotIn("user_id", sess)

    def test_historical_guest_orders_are_never_auto_claimed(self) -> None:
        # Step 1: Guest places order using "new_user@example.com"
        self.client.post("/cart/add", data={"product_id": str(self.croissant.id), "quantity": "1"})
        self.client.post(
            "/checkout",
            data={"name": "Prior Guest", "email": "new_user@example.com"},
            follow_redirects=True,
        )

        with self.app.app_context():
            orders = self.order_repo.get_all()
            self.assertEqual(len(orders), 1)
            guest_order_id = orders[0].id
            self.assertIsNone(orders[0].user_id)

        # Step 2: User registers with same email and logs in
        resp_reg = self.client.post(
            "/register",
            data={
                "email": "new_user@example.com",
                "password": "Password123!",
                "confirm_password": "Password123!",
            },
        )
        self.assertEqual(resp_reg.status_code, 302)

        # Log in
        self.client.post(
            "/login",
            data={"email": "new_user@example.com", "password": "Password123!"},
        )

        # Step 3: Check order history
        resp_hist = self.client.get("/account/orders")
        self.assertEqual(resp_hist.status_code, 200)
        html = resp_hist.get_data(as_text=True)

        # Historical guest order must NOT appear in account order history
        self.assertNotIn(f"#{guest_order_id}", html)
        self.assertIn("No orders yet", html)

    def test_matching_email_alone_never_grants_order_ownership(self) -> None:
        # Guest checks out with User A's email without being authenticated
        self.client.post("/cart/add", data={"product_id": str(self.bread.id), "quantity": "1"})
        resp = self.client.post(
            "/checkout",
            data={"name": "Imposter", "email": "user_a@example.com"},
            follow_redirects=True,
        )
        self.assertEqual(resp.status_code, 200)

        with self.app.app_context():
            # User A's order history must NOT contain this order
            user_a_orders = self.order_repo.get_by_user_id(self.user_a.id)
            self.assertEqual(len(user_a_orders), 0)

            all_orders = self.order_repo.get_all()
            self.assertEqual(len(all_orders), 1)
            self.assertIsNone(all_orders[0].user_id)

    def test_checkout_failures_roll_back_consistently(self) -> None:
        with self.client.session_transaction() as sess:
            sess["user_id"] = self.user_a.id

        # Add 2 loaves of bread
        self.client.post("/cart/add", data={"product_id": str(self.bread.id), "quantity": "2"})

        from unittest.mock import patch
        from app.services.order_service import OrderService

        with patch.object(OrderService, "checkout", side_effect=ValueError("Simulated checkout failure")):
            resp = self.client.post(
                "/checkout",
                data={"name": "User A", "email": "user_a@example.com"},
                follow_redirects=True,
            )
            self.assertEqual(resp.status_code, 200)
            self.assertIn("Simulated checkout failure", resp.get_data(as_text=True))

        # Cart contents preserved in session
        with self.client.session_transaction() as sess:
            self.assertIn("cart", sess)

        # Zero orders created
        with self.app.app_context():
            self.assertEqual(len(self.order_repo.get_all()), 0)
            self.assertEqual(self.product_repo.get_by_id(self.bread.id).stock_quantity, 10)

    # -------------------------------------------------------------------------
    # 2. Order History Routes Tests
    # -------------------------------------------------------------------------

    def test_anonymous_history_access_redirects_to_login(self) -> None:
        resp = self.client.get("/account/orders")
        self.assertEqual(resp.status_code, 302)
        location = resp.headers.get("Location", "")
        self.assertIn("/login", location)
        self.assertIn("next=", location)
        self.assertIn("/account/orders", location)

    def test_anonymous_detail_access_redirects_to_login(self) -> None:
        resp = self.client.get("/account/orders/42")
        self.assertEqual(resp.status_code, 302)
        location = resp.headers.get("Location", "")
        self.assertIn("/login", location)
        self.assertIn("next=", location)
        self.assertIn("/account/orders/42", location)

    def test_authenticated_customer_sees_only_own_orders(self) -> None:
        with self.app.app_context():
            cust = self.customer_repo.create(Customer(name="Cust A", email="cust_a@example.com"))
            # User A order
            o_a = self.order_repo.create(
                Order(customer_id=cust.id, user_id=self.user_a.id, customer_name="User A Snapshot")
            )
            # User B order
            o_b = self.order_repo.create(
                Order(customer_id=cust.id, user_id=self.user_b.id, customer_name="User B Snapshot")
            )

        with self.client.session_transaction() as sess:
            sess["user_id"] = self.user_a.id

        resp = self.client.get("/account/orders")
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)

        self.assertIn(f"#{o_a.id}", html)
        self.assertNotIn(f"#{o_b.id}", html)

    def test_user_with_no_orders_sees_empty_state(self) -> None:
        with self.client.session_transaction() as sess:
            sess["user_id"] = self.user_a.id

        resp = self.client.get("/account/orders")
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)

        self.assertIn("No orders yet", html)
        self.assertIn("Browse Products", html)

    def test_direct_access_to_another_users_order_returns_404(self) -> None:
        with self.app.app_context():
            cust = self.customer_repo.create(Customer(name="Cust B", email="cust_b@example.com"))
            o_b = self.order_repo.create(Order(customer_id=cust.id, user_id=self.user_b.id))

        with self.client.session_transaction() as sess:
            sess["user_id"] = self.user_a.id

        # User A attempts to view User B's order
        resp = self.client.get(f"/account/orders/{o_b.id}")
        self.assertEqual(resp.status_code, 404)

    def test_guest_order_id_returns_404_through_account_details(self) -> None:
        with self.app.app_context():
            cust = self.customer_repo.create(Customer(name="Guest Cust", email="guest_cust@example.com"))
            o_guest = self.order_repo.create(Order(customer_id=cust.id, user_id=None))

        with self.client.session_transaction() as sess:
            sess["user_id"] = self.user_a.id

        resp = self.client.get(f"/account/orders/{o_guest.id}")
        self.assertEqual(resp.status_code, 404)

    def test_nonexistent_order_id_returns_404(self) -> None:
        with self.client.session_transaction() as sess:
            sess["user_id"] = self.user_a.id

        resp = self.client.get("/account/orders/999999")
        self.assertEqual(resp.status_code, 404)

    def test_owner_account_cannot_bypass_ownership_via_account_history(self) -> None:
        with self.app.app_context():
            cust = self.customer_repo.create(Customer(name="Cust A", email="cust_a@example.com"))
            o_a = self.order_repo.create(Order(customer_id=cust.id, user_id=self.user_a.id))

        # Owner logs in
        with self.client.session_transaction() as sess:
            sess["user_id"] = self.owner_user.id

        # Owner hits customer order history route: sees 0 orders, cannot see User A's order
        resp_list = self.client.get("/account/orders")
        self.assertEqual(resp_list.status_code, 200)
        self.assertNotIn(f"#{o_a.id}", resp_list.get_data(as_text=True))

        # Owner hits specific customer order detail via customer route: 404
        resp_detail = self.client.get(f"/account/orders/{o_a.id}")
        self.assertEqual(resp_detail.status_code, 404)

    def test_order_item_prices_and_delivery_details_come_from_snapshots(self) -> None:
        with self.client.session_transaction() as sess:
            sess["user_id"] = self.user_a.id

        # Bread is currently 450 cents (4,50 €)
        self.client.post("/cart/add", data={"product_id": str(self.bread.id), "quantity": "2"})
        self.client.post(
            "/checkout",
            data={
                "name": "Snapshot Recipient",
                "email": "user_a@example.com",
                "phone": "933-333-333",
                "address": "Snapshot Street 12, Coimbra",
            },
            follow_redirects=True,
        )

        with self.app.app_context():
            orders = self.order_repo.get_by_user_id(self.user_a.id)
            order_id = orders[0].id

            # Update catalog product price dramatically to 9999 cents (99,99 €)
            b = self.product_repo.get_by_id(self.bread.id)
            b.price = 9999
            self.product_repo.update(b)

        # View order details
        resp = self.client.get(f"/account/orders/{order_id}")
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)

        # Must display original snapshot price 4,50 € and subtotal 9,00 €, NOT 99,99 €
        self.assertIn("4,50 €", html)
        self.assertIn("9,00 €", html)
        self.assertNotIn("99,99 €", html)
        # Must display snapshot delivery details
        self.assertIn("Snapshot Recipient", html)
        self.assertIn("933-333-333", html)
        self.assertIn("Snapshot Street 12, Coimbra", html)

    # -------------------------------------------------------------------------
    # 3. Order Confirmation Authorization Tests
    # -------------------------------------------------------------------------

    def test_guest_confirmation_protection_remains_intact(self) -> None:
        self.client.post("/cart/add", data={"product_id": str(self.bread.id), "quantity": "1"})
        resp = self.client.post(
            "/checkout",
            data={"name": "Guest", "email": "guest@example.com"},
            follow_redirects=True,
        )
        self.assertEqual(resp.status_code, 200)

        with self.app.app_context():
            orders = self.order_repo.get_all()
            order_id = orders[0].id

        # Purchaser session can view confirmation
        resp_confirm = self.client.get(f"/orders/{order_id}")
        self.assertEqual(resp_confirm.status_code, 200)

        # Other client cannot view confirmation
        other_client = self.app.test_client()
        resp_other = other_client.get(f"/orders/{order_id}")
        self.assertEqual(resp_other.status_code, 404)

    def test_authenticated_purchaser_can_view_their_confirmation(self) -> None:
        with self.client.session_transaction() as sess:
            sess["user_id"] = self.user_a.id

        self.client.post("/cart/add", data={"product_id": str(self.croissant.id), "quantity": "1"})
        resp = self.client.post(
            "/checkout",
            data={"name": "User A", "email": "user_a@example.com"},
            follow_redirects=True,
        )
        self.assertEqual(resp.status_code, 200)

        with self.app.app_context():
            orders = self.order_repo.get_by_user_id(self.user_a.id)
            order_id = orders[0].id

        resp_confirm = self.client.get(f"/orders/{order_id}")
        self.assertEqual(resp_confirm.status_code, 200)
        self.assertIn("Order Confirmation", resp_confirm.get_data(as_text=True))

    def test_different_authenticated_user_cannot_view_confirmation(self) -> None:
        with self.client.session_transaction() as sess:
            sess["user_id"] = self.user_a.id

        self.client.post("/cart/add", data={"product_id": str(self.croissant.id), "quantity": "1"})
        self.client.post(
            "/checkout",
            data={"name": "User A", "email": "user_a@example.com"},
            follow_redirects=True,
        )

        with self.app.app_context():
            orders = self.order_repo.get_by_user_id(self.user_a.id)
            order_id = orders[0].id

        # User B attempts to access confirmation for User A's order
        client_b = self.app.test_client()
        with client_b.session_transaction() as sess:
            sess["user_id"] = self.user_b.id
            sess["last_order_id"] = order_id  # Even if session has order_id forged

        resp_b = client_b.get(f"/orders/{order_id}")
        self.assertEqual(resp_b.status_code, 404)

    def test_logging_out_clears_confirmation_access(self) -> None:
        with self.client.session_transaction() as sess:
            sess["user_id"] = self.user_a.id

        self.client.post("/cart/add", data={"product_id": str(self.bread.id), "quantity": "1"})
        self.client.post(
            "/checkout",
            data={"name": "User A", "email": "user_a@example.com"},
            follow_redirects=True,
        )

        with self.app.app_context():
            orders = self.order_repo.get_by_user_id(self.user_a.id)
            order_id = orders[0].id

        # Confirm purchaser can view
        self.assertEqual(self.client.get(f"/orders/{order_id}").status_code, 200)

        # Logout clears session
        self.client.post("/logout")

        # Confirmation route now returns 404
        self.assertEqual(self.client.get(f"/orders/{order_id}").status_code, 404)

    def test_logging_in_as_different_user_does_not_expose_previous_confirmation(self) -> None:
        with self.client.session_transaction() as sess:
            sess["user_id"] = self.user_a.id

        self.client.post("/cart/add", data={"product_id": str(self.bread.id), "quantity": "1"})
        self.client.post(
            "/checkout",
            data={"name": "User A", "email": "user_a@example.com"},
            follow_redirects=True,
        )

        with self.app.app_context():
            orders = self.order_repo.get_by_user_id(self.user_a.id)
            order_id = orders[0].id

        # Purchaser logs out
        self.client.post("/logout")

        # Log in as User B
        self.client.post(
            "/login",
            data={"email": "user_b@example.com", "password": "Password123!"},
        )

        # User B cannot view User A's confirmation
        self.assertEqual(self.client.get(f"/orders/{order_id}").status_code, 404)

        # Even if session last_order_id was somehow preserved or injected, User B cannot view User A's order confirmation
        with self.client.session_transaction() as sess:
            sess["last_order_id"] = order_id
        self.assertEqual(self.client.get(f"/orders/{order_id}").status_code, 404)

    # -------------------------------------------------------------------------
    # 4. Navigation & UI Tests
    # -------------------------------------------------------------------------

    def test_my_orders_navigation_link_visible_only_for_authenticated_users(self) -> None:
        # Anonymous visitor: "My Orders" link NOT present
        resp_anon = self.client.get("/")
        self.assertEqual(resp_anon.status_code, 200)
        self.assertNotIn("My Orders", resp_anon.get_data(as_text=True))

        # Authenticated user: "My Orders" link IS present
        with self.client.session_transaction() as sess:
            sess["user_id"] = self.user_a.id

        resp_auth = self.client.get("/")
        self.assertEqual(resp_auth.status_code, 200)
        self.assertIn("My Orders", resp_auth.get_data(as_text=True))
        self.assertIn("/account/orders", resp_auth.get_data(as_text=True))

    def test_html_escaping_in_order_templates(self) -> None:
        with self.client.session_transaction() as sess:
            sess["user_id"] = self.user_a.id

        self.client.post("/cart/add", data={"product_id": str(self.bread.id), "quantity": "1"})
        malicious_name = "<script>alert('xss')</script>"
        malicious_addr = "<b>Dangerous Address</b>"
        self.client.post(
            "/checkout",
            data={
                "name": malicious_name,
                "email": "user_a@example.com",
                "address": malicious_addr,
            },
            follow_redirects=True,
        )

        with self.app.app_context():
            order_id = self.order_repo.get_by_user_id(self.user_a.id)[0].id

        resp = self.client.get(f"/account/orders/{order_id}")
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)

        # Must be escaped, not rendered as raw tags
        self.assertNotIn("<script>", html)
        self.assertIn("&lt;script&gt;alert(&#39;xss&#39;)&lt;/script&gt;", html)
        self.assertNotIn("<b>Dangerous Address</b>", html)
        self.assertIn("&lt;b&gt;Dangerous Address&lt;/b&gt;", html)


if __name__ == "__main__":
    unittest.main()

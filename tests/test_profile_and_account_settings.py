"""Tests for authenticated profile dropdown navigation and account settings UI (Phase 11.0.3)."""
from pathlib import Path
import tempfile
import unittest
from werkzeug.security import generate_password_hash

from app import create_app
from app.config import Config
from app.database.connection import init_db
from app.models.category import Category
from app.models.customer import Customer
from app.models.external_identity import ExternalIdentity
from app.models.order import Order, OrderStatus
from app.models.order_item import OrderItem
from app.models.product import Product
from app.models.user import User, UserRole
from app.repositories.category_repository import CategoryRepository
from app.repositories.customer_repository import CustomerRepository
from app.repositories.external_identity_repository import ExternalIdentityRepository
from app.repositories.order_repository import OrderRepository
from app.repositories.product_repository import ProductRepository
from app.repositories.user_repository import UserRepository


class TestProfileDropdownAndAccountSettings(unittest.TestCase):
    """Test suite covering the profile dropdown and account settings page."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_settings.sqlite3"

        class TestSettingsConfig(Config):
            DATABASE_PATH = str(self.db_path)
            TESTING = True
            SECRET_KEY = "test-settings-secret-key"
            WTF_CSRF_ENABLED = False

        self.app = create_app(TestSettingsConfig)
        self.client = self.app.test_client()

        with self.app.app_context():
            init_db()
            self.user_repo = UserRepository()
            self.customer_repo = CustomerRepository()
            self.identity_repo = ExternalIdentityRepository()
            self.order_repo = OrderRepository()
            self.category_repo = CategoryRepository()
            self.product_repo = ProductRepository()

            cat = self.category_repo.create(Category(name="Pantry"))
            self.product = self.product_repo.create(
                Product(name="Fresh Olive Oil", price=1750, stock_quantity=10, category_id=cat.id)
            )

            # Seed a verified customer
            self.customer_user = self.user_repo.create(
                User(
                    email="customer@example.com",
                    role=UserRole.CUSTOMER,
                    is_verified=True,
                    password_hash=generate_password_hash("ValidPass123!"),
                )
            )

            # Seed an unverified customer
            self.unverified_user = self.user_repo.create(
                User(
                    email="unverified@example.com",
                    role=UserRole.CUSTOMER,
                    is_verified=False,
                    password_hash=generate_password_hash("ValidPass123!"),
                )
            )

            # Seed a Google-only customer (no password hash)
            self.google_user = self.user_repo.create(
                User(
                    email="google_user@example.com",
                    role=UserRole.CUSTOMER,
                    is_verified=True,
                    password_hash=None,
                )
            )
            self.identity_repo.create(
                ExternalIdentity(
                    user_id=self.google_user.id,
                    provider="google",
                    provider_user_id="google-sub-12345",
                    email="google_user@example.com",
                )
            )

            # Seed a verified store owner
            self.owner_user = self.user_repo.create(
                User(
                    email="store_owner@example.com",
                    role=UserRole.OWNER,
                    is_verified=True,
                    password_hash=generate_password_hash("OwnerPass123!"),
                )
            )

            # Seed an unverified store owner
            self.unverified_owner = self.user_repo.create(
                User(
                    email="unverified_owner@example.com",
                    role=UserRole.OWNER,
                    is_verified=False,
                    password_hash=generate_password_hash("OwnerPass123!"),
                )
            )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    # -------------------------------------------------------------------------
    # 1. Profile Dropdown Navigation Tests
    # -------------------------------------------------------------------------

    def test_unauthenticated_visitor_nav_bar(self) -> None:
        """Signed-out visitors see Login and Register, not profile dropdown."""
        res = self.client.get("/")
        self.assertEqual(res.status_code, 200)
        html = res.data.decode("utf-8")

        self.assertIn("Login", html)
        self.assertIn("Register", html)
        self.assertNotIn('id="profile-dropdown-button"', html)
        self.assertNotIn('id="profile-dropdown-menu"', html)
        self.assertNotIn('id="admin-nav-link"', html)

    def test_customer_profile_dropdown_markup_and_accessibility(self) -> None:
        """Signed-in customers see accessible profile dropdown with safe label."""
        with self.client.session_transaction() as sess:
            sess["user_id"] = self.customer_user.id
            sess["role"] = "customer"
            sess["is_verified"] = True

        res = self.client.get("/")
        self.assertEqual(res.status_code, 200)
        html = res.data.decode("utf-8")

        # Dropdown trigger button exists with ARIA attributes
        self.assertIn('id="profile-dropdown-button"', html)
        self.assertIn('aria-haspopup="true"', html)
        self.assertIn('aria-expanded="false"', html)
        self.assertIn('aria-controls="profile-dropdown-menu"', html)

        # Trigger label is a friendly label, not exposing raw full email
        self.assertIn("My Account", html)

        # Dropdown panel exists
        self.assertIn('id="profile-dropdown-menu"', html)
        self.assertIn('role="menu"', html)

        # Inside dropdown: user badge with email is present for backwards compatibility
        self.assertIn('id="user-badge"', html)
        self.assertIn("customer@example.com", html)

        # Dropdown contains Account Settings link
        self.assertIn("/account/settings", html)
        self.assertIn("Account Settings", html)

        # Dropdown contains My Orders link
        self.assertIn('id="my-orders-link"', html)
        self.assertIn("/account/orders", html)

        # Customers must NOT see Admin link
        self.assertNotIn('id="admin-nav-link"', html)
        self.assertNotIn("Admin Dashboard", html)

        # Dropdown contains Logout button with POST form
        self.assertIn('id="logout-button"', html)
        self.assertIn('/logout', html)

        # Mobile menu also includes Account Settings and My Orders
        self.assertIn('id="mobile-menu"', html)

    def test_owner_profile_dropdown_includes_admin_link(self) -> None:
        """Verified store owners see the Admin Dashboard link inside profile dropdown."""
        with self.client.session_transaction() as sess:
            sess["user_id"] = self.owner_user.id
            sess["role"] = "owner"
            sess["is_verified"] = True

        res = self.client.get("/")
        self.assertEqual(res.status_code, 200)
        html = res.data.decode("utf-8")

        self.assertIn('id="profile-dropdown-button"', html)
        self.assertIn('id="profile-dropdown-menu"', html)
        self.assertIn('id="user-badge"', html)
        self.assertIn("store_owner@example.com", html)
        self.assertIn("Store Owner", html)

        # Owner must see Admin Dashboard link
        self.assertIn('id="admin-nav-link"', html)
        self.assertIn("/admin", html)
        self.assertIn("Admin Dashboard", html)

    def test_unverified_owner_does_not_see_admin_nav_link(self) -> None:
        """Unverified store owners must NOT see the Admin Dashboard link."""
        with self.client.session_transaction() as sess:
            sess["user_id"] = self.unverified_owner.id
            sess["role"] = "owner"
            sess["is_verified"] = False

        res = self.client.get("/")
        self.assertEqual(res.status_code, 200)
        html = res.data.decode("utf-8")

        self.assertNotIn('id="admin-nav-link"', html)

    def test_profile_dropdown_portuguese_localization(self) -> None:
        """Profile dropdown displays European Portuguese labels when pt_PT is active."""
        with self.client.session_transaction() as sess:
            sess["user_id"] = self.customer_user.id
            sess["role"] = "customer"
            sess["is_verified"] = True
            sess["locale"] = "pt_PT"

        res = self.client.get("/")
        self.assertEqual(res.status_code, 200)
        html = res.data.decode("utf-8")

        self.assertIn("A Minha Conta", html)
        self.assertIn("Definições da Conta", html)
        self.assertIn("As Minhas Encomendas", html)
        self.assertIn("Terminar Sessão", html)

    # -------------------------------------------------------------------------
    # 2. Account Settings Page Tests
    # -------------------------------------------------------------------------

    def test_account_settings_requires_authentication(self) -> None:
        """Unauthenticated GET /account and /account/settings must redirect to login."""
        res_settings = self.client.get("/account/settings")
        self.assertEqual(res_settings.status_code, 302)
        self.assertIn("/login", res_settings.headers["Location"])

        res_account = self.client.get("/account")
        self.assertEqual(res_account.status_code, 302)
        self.assertIn("/login", res_account.headers["Location"])

    def test_customer_account_settings_rendering(self) -> None:
        """Authenticated customer views account settings with structured sections."""
        with self.client.session_transaction() as sess:
            sess["user_id"] = self.customer_user.id
            sess["role"] = "customer"
            sess["is_verified"] = True

        res = self.client.get("/account/settings")
        self.assertEqual(res.status_code, 200)
        html = res.data.decode("utf-8")

        # Headings and breadcrumbs
        self.assertIn("Account Settings", html)
        self.assertIn("Profile Information", html)
        self.assertIn("Password", html)
        self.assertIn("Credentials", html)
        self.assertIn("Linked Google Account", html)
        self.assertIn("Order History", html)

        # Profile info section
        self.assertIn("customer@example.com", html)
        self.assertIn("Member Since", html)
        self.assertIn("Profile editing is not currently supported in this version", html)

        # Password status
        self.assertIn("Password Configured", html)
        self.assertIn("Request Password Reset via Email", html)
        self.assertIn("/forgot-password", html)

        # Google status
        self.assertIn("Not Linked", html)

        # Account deletion destructive section for customers
        self.assertIn("Delete Account", html)
        self.assertIn("Proceed to Account Deletion", html)
        self.assertIn("/account/delete", html)

        # Owner protection notice must NOT appear for customers
        self.assertNotIn("Store Owner Account Protection", html)

    def test_customer_with_linked_customer_profile(self) -> None:
        """When a customer profile exists, customer details are shown on settings page."""
        with self.app.app_context():
            self.customer_repo.create(
                Customer(
                    name="Maria Santos",
                    email="customer@example.com",
                    phone="+351 912 345 678",
                    address="Rua das Flores 123, Lisboa",
                )
            )

        with self.client.session_transaction() as sess:
            sess["user_id"] = self.customer_user.id
            sess["role"] = "customer"
            sess["is_verified"] = True

        res = self.client.get("/account/settings")
        self.assertEqual(res.status_code, 200)
        html = res.data.decode("utf-8")

        self.assertIn("Maria Santos", html)
        self.assertIn("+351 912 345 678", html)
        self.assertIn("Rua das Flores 123, Lisboa", html)

    def test_customer_google_sso_only_account(self) -> None:
        """Customer who signed up via Google has 'No Password Configured' and 'Connected' Google status."""
        with self.client.session_transaction() as sess:
            sess["user_id"] = self.google_user.id
            sess["role"] = "customer"
            sess["is_verified"] = True

        res = self.client.get("/account/settings")
        self.assertEqual(res.status_code, 200)
        html = res.data.decode("utf-8")

        self.assertIn("No Password Configured (Google SSO Only)", html)
        self.assertIn("Connected", html)
        self.assertNotIn("Request Password Reset via Email", html)

    def test_owner_account_settings_rendering(self) -> None:
        """Store owners see store owner protection instead of self-service account deletion."""
        with self.client.session_transaction() as sess:
            sess["user_id"] = self.owner_user.id
            sess["role"] = "owner"
            sess["is_verified"] = True

        res = self.client.get("/account/settings")
        self.assertEqual(res.status_code, 200)
        html = res.data.decode("utf-8")

        self.assertIn("store_owner@example.com", html)
        self.assertIn("Store Owner", html)

        # Owner must see owner protection and NOT see deletion action link
        self.assertIn("Store Owner Account Protection", html)
        self.assertNotIn("Proceed to Account Deletion", html)

    def test_account_settings_with_orders(self) -> None:
        """Settings page displays order summary and links to orders."""
        with self.app.app_context():
            cust = self.customer_repo.create(
                Customer(
                    name="Customer User",
                    email="customer@example.com",
                )
            )
            order = self.order_repo.create(
                Order(
                    customer_id=cust.id,
                    user_id=self.customer_user.id,
                    customer_name="Customer User",
                    customer_email="customer@example.com",
                    status=OrderStatus.CONFIRMED,
                    items=[
                        OrderItem(
                            product_id=self.product.id,
                            product_name="Fresh Olive Oil",
                            quantity=2,
                            unit_price=1750,
                        )
                    ],
                )
            )

        with self.client.session_transaction() as sess:
            sess["user_id"] = self.customer_user.id
            sess["role"] = "customer"
            sess["is_verified"] = True

        res = self.client.get("/account/settings")
        self.assertEqual(res.status_code, 200)
        html = res.data.decode("utf-8")

        self.assertIn(f"#{order.id}", html)
        self.assertIn("35,00", html)
        self.assertIn("1 order(s)", html)
        self.assertIn("/account/orders", html)

    def test_account_settings_portuguese_localization(self) -> None:
        """Settings page renders in European Portuguese when locale is pt_PT."""
        with self.client.session_transaction() as sess:
            sess["user_id"] = self.customer_user.id
            sess["role"] = "customer"
            sess["is_verified"] = True
            sess["locale"] = "pt_PT"

        res = self.client.get("/account/settings")
        self.assertEqual(res.status_code, 200)
        html = res.data.decode("utf-8")

        self.assertIn("Definições da Conta", html)
        self.assertIn("Informações de Perfil", html)
        self.assertIn("Palavra-passe e Credenciais", html)
        self.assertIn("Conta Google Associada", html)
        self.assertIn("Eliminar Conta", html)
        self.assertIn("Prosseguir para a Eliminação de Conta", html)

    def test_account_settings_no_sensitive_secrets_leaked(self) -> None:
        """Account settings page must never disclose password hashes or secret tokens."""
        with self.client.session_transaction() as sess:
            sess["user_id"] = self.customer_user.id
            sess["role"] = "customer"
            sess["is_verified"] = True

        res = self.client.get("/account/settings")
        self.assertEqual(res.status_code, 200)
        html = res.data.decode("utf-8")

        # Password hash must never appear
        self.assertNotIn("pbkdf2:sha256", html)
        self.assertNotIn("ValidPass123!", html)
        self.assertNotIn("password_hash", html)

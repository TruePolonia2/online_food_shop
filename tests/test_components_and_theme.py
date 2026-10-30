"""Tests for reusable Jinja components, responsive UI, branding, and light/dark theme."""

import tempfile
from pathlib import Path
import unittest
from flask import render_template_string
from app import create_app
from app.config import Config
from app.models.user import User, UserRole
from app.database import init_db
from app.repositories.user_repository import UserRepository
from app.services.auth_service import AuthService


class TestComponentsAndTheme(unittest.TestCase):
    """Test suite verifying component templates, dark theme toggles, and responsive navigation."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_theme.sqlite3"

        class TestConfig(Config):
            TESTING = True
            SECRET_KEY = "test-components-theme-secret"
            DATABASE_PATH = str(self.db_path)
            CSRF_ENABLED = False

        self.app = create_app(TestConfig)
        self.client = self.app.test_client()

        with self.app.app_context():
            init_db()
            self.user_repo = UserRepository()
            pwd = AuthService.hash_password("Password123!")
            self.customer = self.user_repo.create(
                User(email="shopper@example.com", password_hash=pwd, role=UserRole.CUSTOMER, is_verified=True)
            )
            self.owner = self.user_repo.create(
                User(email="boss@example.com", password_hash=pwd, role=UserRole.OWNER, is_verified=True)
            )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_theme_toggle_button_and_accessibility(self) -> None:
        """Theme toggle button must be present with accessible labeling and dual icons."""
        res = self.client.get("/")
        self.assertEqual(res.status_code, 200)
        html = res.data.decode("utf-8")

        self.assertIn('id="theme-toggle"', html)
        self.assertIn('aria-label="Toggle light and dark color theme"', html)
        self.assertIn('theme-toggle-dark-icon', html)
        self.assertIn('theme-toggle-light-icon', html)
        self.assertIn('onclick="toggleTheme()"', html)

    def test_fouc_script_present_in_head(self) -> None:
        """Synchronous FOUC prevention script must be present inside <head> before stylesheets."""
        res = self.client.get("/")
        self.assertEqual(res.status_code, 200)
        html = res.data.decode("utf-8")

        head_idx = html.find("<head>")
        script_idx = html.find("localStorage.getItem('theme')")
        body_idx = html.find("<body")

        self.assertNotEqual(head_idx, -1)
        self.assertNotEqual(script_idx, -1)
        self.assertTrue(head_idx < script_idx < body_idx)
        self.assertIn("document.documentElement.classList.add('dark')", html)
        self.assertIn("document.documentElement.classList.remove('dark')", html)

    def test_brand_logo_emblem_rendering(self) -> None:
        """Brand emblem component must render circular vector emblem with dual brand coloring."""
        res = self.client.get("/")
        self.assertEqual(res.status_code, 200)
        html = res.data.decode("utf-8")

        self.assertIn("Online Food Shop", html)
        # Emblem SVG attributes
        self.assertIn('viewBox="0 0 32 32"', html)
        self.assertIn('fill="#ea580c"', html)
        self.assertIn('fill="#059669"', html)

    def test_mobile_menu_drawer_and_toggle(self) -> None:
        """Mobile navigation toggle button and collapsible drawer must be present with ARIA attributes."""
        res = self.client.get("/")
        self.assertEqual(res.status_code, 200)
        html = res.data.decode("utf-8")

        self.assertIn('id="mobile-menu-button"', html)
        self.assertIn('aria-controls="mobile-menu"', html)
        self.assertIn('aria-expanded="false"', html)
        self.assertIn('id="mobile-menu"', html)
        self.assertIn('class="hidden md:hidden border-t', html)

    def test_navigation_unauthenticated_state(self) -> None:
        """Unauthenticated visitors must see login, register, and empty cart in desktop and mobile."""
        res = self.client.get("/")
        self.assertEqual(res.status_code, 200)
        html = res.data.decode("utf-8")

        self.assertIn('id="cart-counter"', html)
        self.assertIn("Login", html)
        self.assertIn("Register", html)
        self.assertNotIn('id="admin-nav-link"', html)
        self.assertNotIn('id="my-orders-link"', html)

    def test_navigation_customer_authenticated_state(self) -> None:
        """Authenticated customers must see user badge, orders link, and logout button."""
        with self.client.session_transaction() as sess:
            sess["user_id"] = self.customer.id
            sess["role"] = "customer"
            sess["is_verified"] = True

        res = self.client.get("/")
        self.assertEqual(res.status_code, 200)
        html = res.data.decode("utf-8")

        self.assertIn('id="user-badge"', html)
        self.assertIn("shopper@example.com", html)
        self.assertIn('id="my-orders-link"', html)
        self.assertIn('id="logout-button"', html)
        self.assertNotIn('id="admin-nav-link"', html)

    def test_navigation_owner_authenticated_state(self) -> None:
        """Verified owner must see the admin nav link alongside account controls."""
        with self.client.session_transaction() as sess:
            sess["user_id"] = self.owner.id
            sess["role"] = "owner"
            sess["is_verified"] = True

        res = self.client.get("/")
        self.assertEqual(res.status_code, 200)
        html = res.data.decode("utf-8")

        self.assertIn('id="admin-nav-link"', html)
        self.assertIn('id="user-badge"', html)
        self.assertIn("boss@example.com", html)

    def test_status_badge_macro_rendering(self) -> None:
        """status_badge macro must output valid status classes with light/dark styles."""
        with self.app.app_context():
            tmpl = """
            {% from "components/badge.html" import status_badge %}
            {{ status_badge('pending', 'order') }}
            {{ status_badge('confirmed', 'order') }}
            {{ status_badge('paid', 'order') }}
            {{ status_badge('shipped', 'order') }}
            {{ status_badge('delivered', 'order') }}
            {{ status_badge('cancelled', 'order') }}
            {{ status_badge('paid', 'payment') }}
            {{ status_badge('failed', 'payment') }}
            {{ status_badge('in stock', 'catalog') }}
            {{ status_badge('out of stock', 'catalog') }}
            """
            rendered = render_template_string(tmpl)

            # Check that expected status texts appear inside badges
            self.assertIn("Pending", rendered)
            self.assertIn("Confirmed", rendered)
            self.assertIn("Paid", rendered)
            self.assertIn("Shipped", rendered)
            self.assertIn("Delivered", rendered)
            self.assertIn("Cancelled", rendered)
            self.assertIn("Failed", rendered)
            self.assertIn("In stock", rendered)
            self.assertIn("Out of stock", rendered)

            # Verify dark mode classes are included in badges
            self.assertIn("dark:bg-amber-950/60", rendered)
            self.assertIn("dark:bg-emerald-950/60", rendered)
            self.assertIn("dark:bg-rose-950/60", rendered)

    def test_empty_state_macro_rendering(self) -> None:
        """empty_state macro must render illustrations, titles, and CTA buttons."""
        with self.app.app_context():
            tmpl = """
            {% from "components/empty_state.html" import empty_state %}
            {{ empty_state(
                title="Your Cart is Empty",
                description="Explore fresh groceries and add delicious items to your basket.",
                action_url="/",
                action_label="Browse Products",
                icon="cart"
            ) }}
            """
            rendered = render_template_string(tmpl)

            self.assertIn("Your Cart is Empty", rendered)
            self.assertIn("Explore fresh groceries", rendered)
            self.assertIn('href="/"', rendered)
            self.assertIn("Browse Products", rendered)
            self.assertIn("dark:bg-slate-800", rendered)

    def test_alert_flash_messages_macro(self) -> None:
        """render_flash_messages macro must preserve semantic accessibility roles and classes."""
        with self.app.test_request_context():
            from flask import flash
            flash("Item added to cart successfully", "success")
            flash("Stock limit reached", "warning")
            flash("Invalid checkout parameters", "error")

            tmpl = """
            {% from "components/alert.html" import render_flash_messages %}
            {{ render_flash_messages() }}
            """
            rendered = render_template_string(tmpl)

            self.assertIn("flash flash-success", rendered)
            self.assertIn("flash flash-warning", rendered)
            self.assertIn("flash flash-error", rendered)
            self.assertIn('role="status"', rendered)
            self.assertIn('role="alert"', rendered)
            self.assertIn("Item added to cart successfully", rendered)
            self.assertIn("Stock limit reached", rendered)
            self.assertIn("Invalid checkout parameters", rendered)

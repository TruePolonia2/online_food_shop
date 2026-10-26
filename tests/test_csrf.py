"""Tests for CSRF protection and token validation."""
from pathlib import Path
import tempfile
import unittest

from app import create_app
from app.config import Config
from app.csrf import SESSION_CSRF_KEY, generate_csrf_token, validate_csrf_token
from app.database.connection import init_db
from app.models.category import Category
from app.models.product import Product
from app.repositories.category_repository import CategoryRepository
from app.repositories.product_repository import ProductRepository


class TestCsrfProtection(unittest.TestCase):
    """Tests confirming CSRF is enabled by default, rejects bad tokens, and accepts valid tokens."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_csrf.sqlite3"

        # Explicitly keep CSRF_ENABLED active even in test configuration
        class CsrfActiveConfig(Config):
            DATABASE_PATH = str(self.db_path)
            TESTING = True
            SECRET_KEY = "csrf-secret-key-for-tests"
            # Note: CSRF_ENABLED is NOT set to False, so it remains True by default

        self.app = create_app(CsrfActiveConfig)
        self.client = self.app.test_client()

        with self.app.app_context():
            init_db()
            self.cat_repo = CategoryRepository()
            self.prod_repo = ProductRepository()

            cat = self.cat_repo.create(Category(name="Bakery"))
            self.product = self.prod_repo.create(
                Product(
                    name="Crusty Bread",
                    price=250,
                    stock_quantity=10,
                    category_id=cat.id,
                    is_active=True,
                )
            )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _get_valid_token(self) -> str:
        """Helper to obtain a legitimate CSRF token bound to the test client session."""
        with self.client:
            self.client.get("/")
            with self.client.session_transaction() as sess:
                token = sess.get(SESSION_CSRF_KEY)
                if not token:
                    # Trigger token generation in session context
                    token = generate_csrf_token()
                    sess[SESSION_CSRF_KEY] = token
            return token

    def test_csrf_enabled_by_default(self) -> None:
        # Default config has CSRF enabled
        self.assertTrue(self.app.config.get("CSRF_ENABLED"))

    def test_safe_get_requests_not_blocked(self) -> None:
        # GET requests must never be blocked by CSRF protection
        resp = self.client.get("/")
        self.assertEqual(resp.status_code, 200)

        resp_cart = self.client.get("/cart/")
        self.assertEqual(resp_cart.status_code, 200)

        resp_product = self.client.get(f"/products/{self.product.id}")
        self.assertEqual(resp_product.status_code, 200)

    def test_post_without_token_rejected_with_400(self) -> None:
        # Attempting state-changing POST without CSRF token must return 400 Bad Request
        resp = self.client.post(
            "/cart/add",
            data={"product_id": str(self.product.id), "quantity": "1"},
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("CSRF token", resp.get_data(as_text=True))

    def test_post_with_forged_token_rejected_with_400(self) -> None:
        # First initialize session
        self._get_valid_token()

        resp = self.client.post(
            "/cart/add",
            data={
                "product_id": str(self.product.id),
                "quantity": "1",
                "csrf_token": "forged_malicious_token_12345",
            },
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("CSRF token", resp.get_data(as_text=True))

    def test_post_with_valid_form_token_accepted(self) -> None:
        token = self._get_valid_token()

        resp = self.client.post(
            "/cart/add",
            data={
                "product_id": str(self.product.id),
                "quantity": "2",
                "csrf_token": token,
            },
            follow_redirects=False,
        )
        # Legitimate state-changing request proceeds (302 redirect to cart view)
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/cart/", resp.headers.get("Location", ""))

    def test_post_with_valid_csrf_header_accepted(self) -> None:
        token = self._get_valid_token()

        # Non-form or API-style requests can pass token in X-CSRFToken header
        resp = self.client.post(
            "/cart/add",
            data={"product_id": str(self.product.id), "quantity": "1"},
            headers={"X-CSRFToken": token},
            follow_redirects=False,
        )
        self.assertEqual(resp.status_code, 302)

    def test_explicitly_disabled_csrf_allows_post_without_token(self) -> None:
        class DisabledCsrfConfig(Config):
            DATABASE_PATH = str(self.db_path)
            TESTING = True
            SECRET_KEY = "test-key"
            CSRF_ENABLED = False

        disabled_app = create_app(DisabledCsrfConfig)
        disabled_client = disabled_app.test_client()

        resp = disabled_client.post(
            "/cart/add",
            data={"product_id": str(self.product.id), "quantity": "1"},
            follow_redirects=False,
        )
        # Bypassed only when explicitly disabled
        self.assertEqual(resp.status_code, 302)

    def test_forms_in_templates_render_csrf_token(self) -> None:
        # Catalog page contains CSRF input
        resp_index = self.client.get("/")
        self.assertEqual(resp_index.status_code, 200)
        self.assertIn('name="csrf_token"', resp_index.get_data(as_text=True))

        # Product detail contains CSRF input
        resp_detail = self.client.get(f"/products/{self.product.id}")
        self.assertEqual(resp_detail.status_code, 200)
        self.assertIn('name="csrf_token"', resp_detail.get_data(as_text=True))

    def test_validate_csrf_token_constant_time_comparison(self) -> None:
        with self.app.test_request_context():
            token = generate_csrf_token()
            self.assertTrue(validate_csrf_token(token))
            self.assertFalse(validate_csrf_token(None))
            self.assertFalse(validate_csrf_token(""))
            self.assertFalse(validate_csrf_token("invalid_token"))


if __name__ == "__main__":
    unittest.main()

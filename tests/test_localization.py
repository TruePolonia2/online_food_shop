"""Automated test suite for Phase 11.0.2: English and European Portuguese Localization."""

from pathlib import Path
import tempfile
import unittest

from app import create_app
from app.config import Config
from app.csrf import SESSION_CSRF_KEY, generate_csrf_token
from app.database.connection import init_db
from app.i18n import normalize_locale
from app.models.category import Category
from app.models.product import Product
from app.models.user import User, UserRole
from app.repositories.category_repository import CategoryRepository
from app.repositories.product_repository import ProductRepository
from app.repositories.user_repository import UserRepository
from app.services.auth_service import AuthService


class TestLocalization(unittest.TestCase):
    """Test suite covering locale negotiation, switching, persistence, CSRF, and rendered Portuguese translations."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_i18n.sqlite3"

        class TestI18nConfig(Config):
            TESTING = True
            SECRET_KEY = "test-localization-secret-key"
            DATABASE_PATH = str(self.db_path)
            CSRF_ENABLED = True
            BABEL_DEFAULT_LOCALE = "en"
            BABEL_SUPPORTED_LOCALES = ["en", "pt_PT"]

        self.app = create_app(TestI18nConfig)
        self.client = self.app.test_client()

        with self.app.app_context():
            init_db()
            self.user_repo = UserRepository()
            self.cat_repo = CategoryRepository()
            self.prod_repo = ProductRepository()

            pwd = AuthService.hash_password("Password123!")
            self.user = self.user_repo.create(
                User(email="testuser@example.com", password_hash=pwd, role=UserRole.CUSTOMER, is_verified=True)
            )
            self.cat = self.cat_repo.create(Category(name="Bakery", is_active=True))
            self.product = self.prod_repo.create(
                Product(name="Fresh Sourdough", price=450, stock_quantity=10, category_id=self.cat.id, is_active=True)
            )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _get_csrf_token(self) -> str:
        """Obtain a valid CSRF token bound to the current client session."""
        self.client.get("/")
        with self.client.session_transaction() as sess:
            token = sess.get(SESSION_CSRF_KEY)
            if not token:
                token = generate_csrf_token()
                sess[SESSION_CSRF_KEY] = token
            return token

    def test_default_locale_is_english(self) -> None:
        """Verify unconfigured client defaults to English with proper lang attribute and English UI copy."""
        res = self.client.get("/")
        self.assertEqual(res.status_code, 200)
        html = res.get_data(as_text=True)

        self.assertIn('<html lang="en"', html)
        self.assertIn("Products", html)
        self.assertIn("Contact", html)
        self.assertIn("Cart", html)
        self.assertIn("Login", html)
        self.assertIn("Browse Catalog", html)

    def test_set_locale_endpoint_rejects_missing_csrf(self) -> None:
        """POST /set-locale must enforce CSRF protection and reject requests lacking a token."""
        res = self.client.post("/set-locale", data={"locale": "pt_PT", "next": "/"})
        self.assertEqual(res.status_code, 400)

    def test_set_locale_switches_to_portuguese_with_valid_csrf(self) -> None:
        """POST /set-locale with valid token should update session and set persistent cookie."""
        token = self._get_csrf_token()
        res = self.client.post("/set-locale", data={"locale": "pt_PT", "csrf_token": token, "next": "/"})
        self.assertEqual(res.status_code, 302)
        self.assertEqual(res.headers.get("Location"), "/")

        # Check session
        with self.client.session_transaction() as sess:
            self.assertEqual(sess.get("locale"), "pt_PT")

        # Check cookie
        cookie_header = res.headers.get("Set-Cookie", "")
        self.assertIn("locale=pt_PT", cookie_header)

    def test_locale_normalization_logic(self) -> None:
        """Verify normalize_locale correctly maps PT variants and rejects invalid or unsupported locales."""
        self.assertEqual(normalize_locale("pt"), "pt_PT")
        self.assertEqual(normalize_locale("pt-PT"), "pt_PT")
        self.assertEqual(normalize_locale("pt_PT"), "pt_PT")
        self.assertEqual(normalize_locale("PT-PT"), "pt_PT")
        self.assertEqual(normalize_locale("en"), "en")
        self.assertEqual(normalize_locale("en-US"), "en")

        # Unsupported or malicious locales safely fallback to default 'en'
        self.assertEqual(normalize_locale("pt-BR"), "en")
        self.assertEqual(normalize_locale("fr"), "en")
        self.assertEqual(normalize_locale("es"), "en")
        self.assertEqual(normalize_locale("../../../etc/passwd"), "en")
        self.assertEqual(normalize_locale("<script>alert(1)</script>"), "en")
        self.assertEqual(normalize_locale(""), "en")
        self.assertEqual(normalize_locale(None), "en")

    def test_open_redirect_protection_on_set_locale(self) -> None:
        """Ensure external or protocol-relative redirect URLs are rejected and default to index."""
        token = self._get_csrf_token()

        # External host
        res = self.client.post(
            "/set-locale",
            data={"locale": "pt_PT", "csrf_token": token, "next": "https://attacker.example.com/steal"},
        )
        self.assertEqual(res.status_code, 302)
        self.assertEqual(res.headers.get("Location"), "/")

        # Protocol-relative URL
        token = self._get_csrf_token()
        res = self.client.post(
            "/set-locale",
            data={"locale": "pt_PT", "csrf_token": token, "next": "//attacker.example.com"},
        )
        self.assertEqual(res.status_code, 302)
        self.assertEqual(res.headers.get("Location"), "/")

        # Valid internal next path
        token = self._get_csrf_token()
        res = self.client.post(
            "/set-locale",
            data={"locale": "pt_PT", "csrf_token": token, "next": "/contact"},
        )
        self.assertEqual(res.status_code, 302)
        self.assertEqual(res.headers.get("Location"), "/contact")

    def test_cookie_persistence_when_session_is_wiped(self) -> None:
        """When session is cleared, cookie preference should restore the selected locale."""
        # Set cookie directly
        self.client.set_cookie("locale", "pt_PT")
        res = self.client.get("/")
        self.assertEqual(res.status_code, 200)
        html = res.get_data(as_text=True)

        self.assertIn('<html lang="pt-PT"', html)
        self.assertIn("Produtos", html)
        self.assertIn("Carrinho", html)

    def test_preference_preserved_across_logout(self) -> None:
        """User language preference must survive session logout."""
        token = self._get_csrf_token()
        self.client.post(
            "/login",
            data={"email": "testuser@example.com", "password": "Password123!", "csrf_token": token},
        )
        # Switch to pt_PT
        token = self._get_csrf_token()
        self.client.post("/set-locale", data={"locale": "pt_PT", "csrf_token": token, "next": "/"})

        # Logout
        self.client.get("/logout", follow_redirects=True)

        # Check subsequent request retains Portuguese
        res = self.client.get("/")
        html = res.get_data(as_text=True)
        self.assertIn('<html lang="pt-PT"', html)
        self.assertIn("Produtos", html)

    def test_rendered_catalog_in_portuguese(self) -> None:
        """Verify catalog page renders Portugal Portuguese terminology."""
        with self.client.session_transaction() as sess:
            sess["locale"] = "pt_PT"

        res = self.client.get("/")
        self.assertEqual(res.status_code, 200)
        html = res.get_data(as_text=True)

        self.assertIn('<html lang="pt-PT"', html)
        self.assertIn("Explorar Catálogo", html)
        self.assertIn("100% Qualidade Garantida", html)
        self.assertIn("Adicionar ao Carrinho", html)
        self.assertIn("Em Stock", html)

    def test_rendered_cart_in_portuguese(self) -> None:
        """Verify cart page renders European Portuguese terminology."""
        with self.client.session_transaction() as sess:
            sess["locale"] = "pt_PT"

        res = self.client.get("/cart/", follow_redirects=True)
        self.assertEqual(res.status_code, 200)
        html = res.get_data(as_text=True)

        self.assertIn("O Seu Carrinho de Compras", html)
        self.assertIn("O seu carrinho está vazio", html)
        self.assertIn("Continuar a Comprar", html)

    def test_rendered_auth_pages_in_portuguese(self) -> None:
        """Verify login and registration pages render European Portuguese terminology (e.g. Palavra-passe)."""
        with self.client.session_transaction() as sess:
            sess["locale"] = "pt_PT"

        # Login
        res_login = self.client.get("/login")
        html_login = res_login.get_data(as_text=True)
        self.assertIn("Iniciar Sessão", html_login)
        self.assertIn("Palavra-passe", html_login)
        self.assertIn("Registe-se aqui", html_login)

        # Register
        res_register = self.client.get("/register")
        html_register = res_register.get_data(as_text=True)
        self.assertIn("Criar Conta", html_register)
        self.assertIn("Confirmar palavra-passe", html_register)

    def test_rendered_contact_page_in_portuguese(self) -> None:
        """Verify contact form renders European Portuguese labels and buttons."""
        with self.client.session_transaction() as sess:
            sess["locale"] = "pt_PT"

        res = self.client.get("/contact")
        html = res.get_data(as_text=True)

        self.assertIn("Contactar Apoio ao Cliente", html)
        self.assertIn("O Seu Nome", html)
        self.assertIn("Endereço de Email", html)
        self.assertIn("Assunto", html)
        self.assertIn("Mensagem", html)
        self.assertIn("Enviar Mensagem", html)

    def test_status_badge_rendering_in_both_locales(self) -> None:
        """Verify badge component renders localized order and payment status values."""
        from flask import render_template_string, session

        template = """
        {% from 'components/badge.html' import status_badge %}
        {{ status_badge('pending', 'order') }}
        {{ status_badge('paid', 'payment', prefix='Payment: ') }}
        {{ status_badge('shipped', 'order') }}
        """

        # In English
        with self.app.test_request_context("/"):
            session["locale"] = "en"
            rendered_en = render_template_string(template)
            self.assertIn("Pending", rendered_en)
            self.assertIn("Payment: Paid", rendered_en)
            self.assertIn("Shipped", rendered_en)

        # In Portuguese
        with self.app.test_request_context("/"):
            session["locale"] = "pt_PT"
            rendered_pt = render_template_string(template)
            self.assertIn("Pendente", rendered_pt)
            self.assertIn("Pagamento: Pago", rendered_pt)
            self.assertIn("Enviada", rendered_pt)

    def test_navbar_language_selector_ui(self) -> None:
        """Navbar should include the language selector form with accessible labeling and current indicator."""
        res = self.client.get("/")
        html = res.get_data(as_text=True)

        self.assertIn('action="/set-locale"', html)
        self.assertIn('name="locale" value="en"', html)
        self.assertIn('name="locale" value="pt_PT"', html)
        self.assertIn('aria-label="Language Selector"', html)

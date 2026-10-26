"""Integration and route tests for Phase 10.3 customer authentication and session management."""
from datetime import datetime, timedelta, timezone
import hashlib
from pathlib import Path
import tempfile
import threading
import time
import unittest

from app import create_app
from app.config import Config
from app.csrf import SESSION_CSRF_KEY, generate_csrf_token
from app.database.connection import get_db, init_db
from app.models.customer import Customer
from app.models.email_verification_token import EmailVerificationToken
from app.models.order import Order
from app.models.product import Product
from app.models.user import User, UserRole
from app.repositories.customer_repository import CustomerRepository
from app.repositories.email_verification_token_repository import EmailVerificationTokenRepository
from app.repositories.order_repository import OrderRepository
from app.repositories.product_repository import ProductRepository
from app.repositories.user_repository import UserRepository
from app.routes.auth import LoginRateLimiter, is_safe_redirect_url, login_rate_limiter
from app.routes.cart_session import SESSION_CART_KEY, extract_sanitized_cart_data
from app.services.auth_service import AuthService
from app.services.email_sender import InMemoryEmailSender


class BaseAuthRouteTestCase(unittest.TestCase):
    """Base test case initializing Flask test client with an isolated temporary database."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_auth_routes.sqlite3"

        class TestRouteConfig(Config):
            DATABASE_PATH = str(self.db_path)
            TESTING = True
            SECRET_KEY = "test-auth-route-secret-key"
            CSRF_ENABLED = False

        self.app = create_app(TestRouteConfig)
        self.client = self.app.test_client()
        login_rate_limiter.clear()

        with self.app.app_context():
            init_db()
            self.user_repo = UserRepository()
            self.token_repo = EmailVerificationTokenRepository()
            self.product_repo = ProductRepository()
            self.customer_repo = CustomerRepository()
            self.order_repo = OrderRepository()

            # Create a test catalog product for cart preservation tests
            self.sample_product = self.product_repo.create(
                Product(name="Fresh Apples", price=350, stock_quantity=20, is_active=True)
            )

    def tearDown(self) -> None:
        login_rate_limiter.clear()
        self.temp_dir.cleanup()

    def register_and_create_user(
        self,
        email: str = "customer@example.com",
        password: str = "SecurePass123!",
        is_verified: bool = False,
    ) -> tuple[User, str]:
        """Helper to create a user and token via AuthService."""
        with self.app.app_context():
            auth_service = AuthService()
            user, raw_token = auth_service.register_customer(
                email=email,
                password=password,
                confirm_password=password,
            )
            if is_verified:
                auth_service.verify_email(raw_token)
                user = self.user_repo.get_by_id(user.id)
            return user, raw_token


class TestRegistrationRoutes(BaseAuthRouteTestCase):
    """Tests for GET and POST /register."""

    def test_get_register_form(self) -> None:
        resp = self.client.get("/register")
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn("Create an Account", html)
        self.assertIn('name="email"', html)
        self.assertIn('name="password"', html)
        self.assertIn('name="confirm_password"', html)

    def test_get_register_when_already_logged_in_redirects(self) -> None:
        user, _ = self.register_and_create_user("logged_in@example.com", "Password123!")
        with self.client.session_transaction() as sess:
            sess["user_id"] = user.id

        resp = self.client.get("/register")
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/", resp.headers.get("Location", ""))

    def test_post_registration_success(self) -> None:
        resp = self.client.post(
            "/register",
            data={
                "email": "newbie@example.com",
                "password": "ValidPassword123!",
                "confirm_password": "ValidPassword123!",
            },
            follow_redirects=False,
        )
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/login", resp.headers.get("Location", ""))

        with self.app.app_context():
            user = self.user_repo.get_by_email("newbie@example.com")
            self.assertIsNotNone(user)
            self.assertEqual(user.role, UserRole.CUSTOMER)
            self.assertFalse(user.is_verified)
            self.assertTrue(user.has_credentials)

            # Security invariant: Raw token is not stored in DB, only hash
            latest_token = self.token_repo.get_latest_for_user(user.id)
            self.assertIsNotNone(latest_token)
            self.assertFalse(latest_token.is_used)

        # User is not automatically logged in
        with self.client.session_transaction() as sess:
            self.assertNotIn("user_id", sess)

    def test_post_registration_message_explains_in_memory_simulation_honestly(self) -> None:
        resp = self.client.post(
            "/register",
            data={
                "email": "honesty@example.com",
                "password": "ValidPassword123!",
                "confirm_password": "ValidPassword123!",
            },
            follow_redirects=True,
        )
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True).lower()
        self.assertIn("email delivery is simulated and no message is sent to your inbox", html)
        self.assertNotIn("application logs", html)
        self.assertNotIn("has been sent to your email", html)

    def test_post_registration_invalid_email(self) -> None:
        resp = self.client.post(
            "/register",
            data={
                "email": "invalid-email-no-at",
                "password": "ValidPassword123!",
                "confirm_password": "ValidPassword123!",
            },
        )
        self.assertEqual(resp.status_code, 400)
        html = resp.get_data(as_text=True)
        self.assertIn("valid user email is required", html.lower())
        # Passwords must not be repopulated
        self.assertNotIn('value="ValidPassword123!"', html)

    def test_post_registration_short_password(self) -> None:
        resp = self.client.post(
            "/register",
            data={
                "email": "shortpwd@example.com",
                "password": "short",
                "confirm_password": "short",
            },
        )
        self.assertEqual(resp.status_code, 400)
        html = resp.get_data(as_text=True)
        self.assertIn("at least 8 characters", html.lower())
        self.assertNotIn('value="short"', html)

    def test_post_registration_password_mismatch(self) -> None:
        resp = self.client.post(
            "/register",
            data={
                "email": "mismatch@example.com",
                "password": "Password123!",
                "confirm_password": "DifferentPassword456!",
            },
        )
        self.assertEqual(resp.status_code, 400)
        html = resp.get_data(as_text=True)
        self.assertIn("passwords do not match", html.lower())
        self.assertNotIn('value="Password123!"', html)
        self.assertNotIn('value="DifferentPassword456!"', html)

    def test_post_registration_duplicate_email(self) -> None:
        self.register_and_create_user("duplicate@example.com", "Password123!")

        resp = self.client.post(
            "/register",
            data={
                "email": "DUPLICATE@EXAMPLE.COM",
                "password": "AnotherPassword123!",
                "confirm_password": "AnotherPassword123!",
            },
        )
        self.assertEqual(resp.status_code, 400)
        html = resp.get_data(as_text=True)
        self.assertIn("already exists", html.lower())

    def test_registration_does_not_link_existing_guest_orders(self) -> None:
        with self.app.app_context():
            cust = self.customer_repo.create(Customer(name="Guest Buyer", email="guest@example.com"))
            order = self.order_repo.create(Order(customer_id=cust.id, user_id=None))
            self.assertIsNone(order.user_id)

        # Register account with same email
        resp = self.client.post(
            "/register",
            data={
                "email": "guest@example.com",
                "password": "Password123!",
                "confirm_password": "Password123!",
            },
            follow_redirects=False,
        )
        self.assertEqual(resp.status_code, 302)

        with self.app.app_context():
            order_after = self.order_repo.get_by_id(order.id)
            # Invariant: guest order user_id remains strictly None
            self.assertIsNone(order_after.user_id)

    def test_registration_form_ignores_untrusted_role_and_verification_flags(self) -> None:
        resp = self.client.post(
            "/register",
            data={
                "email": "hacker@example.com",
                "password": "ValidPassword123!",
                "confirm_password": "ValidPassword123!",
                "role": "owner",
                "is_verified": "1",
                "is_verified_bool": "true",
            },
            follow_redirects=False,
        )
        self.assertEqual(resp.status_code, 302)

        with self.app.app_context():
            user = self.user_repo.get_by_email("hacker@example.com")
            self.assertIsNotNone(user)
            # Invariant: role must strictly be customer and is_verified must be False
            self.assertEqual(user.role, UserRole.CUSTOMER)
            self.assertFalse(user.is_verified)


class TestLoginRoutes(BaseAuthRouteTestCase):
    """Tests for GET and POST /login."""

    def test_get_login_form(self) -> None:
        resp = self.client.get("/login")
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn("Sign In", html)
        self.assertIn('name="email"', html)
        self.assertIn('name="password"', html)

    def test_get_login_when_already_logged_in_redirects(self) -> None:
        user, _ = self.register_and_create_user("logged_in_user@example.com", "Password123!")
        with self.client.session_transaction() as sess:
            sess["user_id"] = user.id

        resp = self.client.get("/login")
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/", resp.headers.get("Location", ""))

    def test_post_login_success(self) -> None:
        self.register_and_create_user("user1@example.com", "Password123!", is_verified=True)

        resp = self.client.post(
            "/login",
            data={"email": "USER1@EXAMPLE.COM", "password": "Password123!"},
            follow_redirects=False,
        )
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/", resp.headers.get("Location", ""))

        with self.client.session_transaction() as sess:
            self.assertIsNotNone(sess.get("user_id"))

    def test_post_login_unverified_account_displays_warning(self) -> None:
        self.register_and_create_user("unverified@example.com", "Password123!", is_verified=False)

        resp = self.client.post(
            "/login",
            data={"email": "unverified@example.com", "password": "Password123!"},
            follow_redirects=True,
        )
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        # Warning flash and verification banner rendered
        self.assertIn("not yet verified", html.lower())
        self.assertIn("unverified-email-banner", html)

    def test_post_login_wrong_password_returns_401_generic_error(self) -> None:
        self.register_and_create_user("target@example.com", "CorrectPassword123!")

        resp = self.client.post(
            "/login",
            data={"email": "target@example.com", "password": "WrongPassword999!"},
        )
        self.assertEqual(resp.status_code, 401)
        html = resp.get_data(as_text=True)
        self.assertIn("invalid email or password", html.lower())
        # Password must not be repopulated
        self.assertNotIn('value="WrongPassword999!"', html)

        with self.client.session_transaction() as sess:
            self.assertNotIn("user_id", sess)

    def test_post_login_nonexistent_email_returns_401_generic_error(self) -> None:
        resp = self.client.post(
            "/login",
            data={"email": "nobody@example.com", "password": "Password123!"},
        )
        self.assertEqual(resp.status_code, 401)
        html = resp.get_data(as_text=True)
        self.assertIn("invalid email or password", html.lower())

    def test_post_login_missing_or_corrupted_hash_returns_401(self) -> None:
        with self.app.app_context():
            # Account with corrupted hash
            self.user_repo.create(User(email="corrupted@example.com", password_hash="not_a_valid_hash"))

        resp = self.client.post(
            "/login",
            data={"email": "corrupted@example.com", "password": "Password123!"},
        )
        self.assertEqual(resp.status_code, 401)
        self.assertIn("invalid email or password", resp.get_data(as_text=True).lower())

    def test_post_login_preserves_cart_and_prevents_session_fixation(self) -> None:
        # Prepopulate guest cart in session
        with self.client.session_transaction() as sess:
            sess[SESSION_CART_KEY] = {str(self.sample_product.id): 3}
            sess["pre_login_tracker"] = "malicious_fixation_value"

        self.register_and_create_user("cart_owner@example.com", "Password123!", is_verified=True)

        resp = self.client.post(
            "/login",
            data={"email": "cart_owner@example.com", "password": "Password123!"},
            follow_redirects=False,
        )
        self.assertEqual(resp.status_code, 302)

        with self.client.session_transaction() as sess:
            # Pre-login custom data purged
            self.assertNotIn("pre_login_tracker", sess)
            # Authenticated user ID set
            self.assertIsNotNone(sess.get("user_id"))
            # Shopping cart preserved
            self.assertEqual(sess.get(SESSION_CART_KEY), {str(self.sample_product.id): 3})

    def test_post_login_sanitizes_cart_data_rejecting_booleans_and_malformed_keys(self) -> None:
        self.register_and_create_user("sanitized_login@example.com", "Password123!", is_verified=True)

        with self.client.session_transaction() as sess:
            sess[SESSION_CART_KEY] = {
                str(self.sample_product.id): 2,
                "bad_str": 5,
                "-1": 3,
                "0": 1,
                "999": True,
                "998": 0,
                "997": -4,
                "100": 4,
            }

        resp = self.client.post(
            "/login",
            data={"email": "sanitized_login@example.com", "password": "Password123!"},
            follow_redirects=False,
        )
        self.assertEqual(resp.status_code, 302)

        with self.client.session_transaction() as sess:
            cart = sess.get(SESSION_CART_KEY)
            self.assertIsInstance(cart, dict)
            # Only valid positive int quantities with positive int keys preserved
            self.assertEqual(cart, {str(self.sample_product.id): 2, "100": 4})


class TestLogoutRoutes(BaseAuthRouteTestCase):
    """Tests for POST /logout."""

    def test_post_logout_clears_auth_and_preserves_cart(self) -> None:
        user, _ = self.register_and_create_user("user_logout@example.com", "Password123!")
        with self.client.session_transaction() as sess:
            sess["user_id"] = user.id
            sess[SESSION_CART_KEY] = {str(self.sample_product.id): 2}

        resp = self.client.post("/logout", follow_redirects=False)
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/", resp.headers.get("Location", ""))

        with self.client.session_transaction() as sess:
            self.assertNotIn("user_id", sess)
            # Cart remains intact for guest shopping
            self.assertEqual(sess.get(SESSION_CART_KEY), {str(self.sample_product.id): 2})

    def test_post_logout_clears_stale_session_and_rotates_csrf_and_preserves_sanitized_cart(self) -> None:
        user, _ = self.register_and_create_user("stale_test@example.com", "Password123!")
        with self.client.session_transaction() as sess:
            sess["user_id"] = user.id
            sess["last_order_id"] = 12345
            sess["stale_custom_state"] = "attacker_controlled_value"
            sess[SESSION_CSRF_KEY] = "old-csrf-token-12345"
            sess[SESSION_CART_KEY] = {str(self.sample_product.id): 4}

        resp = self.client.post("/logout", follow_redirects=False)
        self.assertEqual(resp.status_code, 302)

        with self.client.session_transaction() as sess:
            # All previous authentication and order confirmation state wiped
            self.assertNotIn("user_id", sess)
            self.assertNotIn("last_order_id", sess)
            self.assertNotIn("stale_custom_state", sess)
            # Fresh CSRF token generated (rotated)
            self.assertIsNotNone(sess.get(SESSION_CSRF_KEY))
            self.assertNotEqual(sess.get(SESSION_CSRF_KEY), "old-csrf-token-12345")
            # Only validated cart preserved
            self.assertEqual(sess.get(SESSION_CART_KEY), {str(self.sample_product.id): 4})

    def test_post_logout_sanitizes_malformed_cart_data(self) -> None:
        user, _ = self.register_and_create_user("logout_malformed@example.com", "Password123!")
        with self.client.session_transaction() as sess:
            sess["user_id"] = user.id
            sess[SESSION_CART_KEY] = {
                str(self.sample_product.id): 2,
                "bad_item": 1,
                "50": True,
                "60": 0,
                "-5": 3,
            }

        resp = self.client.post("/logout", follow_redirects=False)
        self.assertEqual(resp.status_code, 302)

        with self.client.session_transaction() as sess:
            cart = sess.get(SESSION_CART_KEY)
            self.assertEqual(cart, {str(self.sample_product.id): 2})

    def test_get_logout_does_not_change_state(self) -> None:
        user, _ = self.register_and_create_user("no_get_logout@example.com", "Password123!")
        with self.client.session_transaction() as sess:
            sess["user_id"] = user.id

        # GET request to /logout must NOT terminate session
        resp = self.client.get("/logout")
        self.assertEqual(resp.status_code, 302)

        with self.client.session_transaction() as sess:
            self.assertEqual(sess.get("user_id"), user.id)


class TestEmailVerificationRoutes(BaseAuthRouteTestCase):
    """Tests for GET /verify-email/<token> and POST /verify-email."""

    def test_get_verify_email_confirmation_page_does_not_consume_token(self) -> None:
        user, raw_token = self.register_and_create_user("verify_page@example.com", "Password123!")

        resp = self.client.get(f"/verify-email/{raw_token}")
        self.assertEqual(resp.status_code, 200)
        # Security response headers
        self.assertEqual(resp.headers.get("Referrer-Policy"), "no-referrer")
        self.assertIn("no-store", resp.headers.get("Cache-Control", ""))
        self.assertIn("private", resp.headers.get("Cache-Control", ""))

        html = resp.get_data(as_text=True)
        self.assertIn('content="no-referrer"', html)
        self.assertIn("Confirm Verification", html)
        # Third-party scripts suppressed to protect sensitive token from DOM access
        self.assertNotIn("cdn.tailwindcss.com", html)
        # Accessible fallback styling present
        self.assertIn(".verification-card", html)

        # Invariant: Token has NOT been consumed by GET
        with self.app.app_context():
            token_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
            token_in_db = self.token_repo.get_by_token_hash(token_hash)
            self.assertFalse(token_in_db.is_used)
            user_in_db = self.user_repo.get_by_id(user.id)
            self.assertFalse(user_in_db.is_verified)

    def test_post_verify_email_success(self) -> None:
        user, raw_token = self.register_and_create_user("active_verify@example.com", "Password123!")

        resp = self.client.post(
            "/verify-email",
            data={"token": raw_token},
            follow_redirects=False,
        )
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/login", resp.headers.get("Location", ""))

        with self.app.app_context():
            user_in_db = self.user_repo.get_by_id(user.id)
            self.assertTrue(user_in_db.is_verified)

            token_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
            token_in_db = self.token_repo.get_by_token_hash(token_hash)
            self.assertTrue(token_in_db.is_used)

    def test_post_verify_email_reuse_rejected(self) -> None:
        user, raw_token = self.register_and_create_user("reuse@example.com", "Password123!")

        # First verification succeeds
        self.client.post("/verify-email", data={"token": raw_token})

        # Second verification attempt fails
        resp = self.client.post(
            "/verify-email",
            data={"token": raw_token},
            follow_redirects=False,
        )
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/verify-email", resp.headers.get("Location", ""))

    def test_post_verify_email_expired_token_rejected(self) -> None:
        with self.app.app_context():
            user = self.user_repo.create(User(email="expired_route@example.com"))
            raw_token = "expired_raw_token_xyz"
            token_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
            past = datetime.now(timezone.utc) - timedelta(hours=2)
            self.token_repo.create(
                EmailVerificationToken(
                    user_id=user.id,
                    token_hash=token_hash,
                    created_at=past - timedelta(hours=24),
                    expires_at=past,
                )
            )

        resp = self.client.post(
            "/verify-email",
            data={"token": raw_token},
            follow_redirects=False,
        )
        self.assertEqual(resp.status_code, 302)

        with self.app.app_context():
            user_after = self.user_repo.get_by_id(user.id)
            self.assertFalse(user_after.is_verified)

    def test_post_verify_email_tampered_token_fails_safely(self) -> None:
        resp = self.client.post(
            "/verify-email",
            data={"token": "tampered_nonexistent_token_value"},
            follow_redirects=False,
        )
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/verify-email", resp.headers.get("Location", ""))

    def test_verification_warning_banner_disappears_after_successful_verification(self) -> None:
        # Create unverified user and log them in
        user, raw_token = self.register_and_create_user("banner_test@example.com", "Password123!", is_verified=False)
        with self.client.session_transaction() as sess:
            sess["user_id"] = user.id

        # Initial view: unverified banner is displayed
        resp_before = self.client.get("/")
        self.assertEqual(resp_before.status_code, 200)
        self.assertIn("unverified-email-banner", resp_before.get_data(as_text=True))

        # Perform token verification
        resp_verify = self.client.post("/verify-email", data={"token": raw_token}, follow_redirects=True)
        self.assertEqual(resp_verify.status_code, 200)

        # Subsequent view: user is now verified in DB, banner is completely gone
        resp_after = self.client.get("/")
        self.assertEqual(resp_after.status_code, 200)
        self.assertNotIn("unverified-email-banner", resp_after.get_data(as_text=True))


class TestVerificationResendRoutes(BaseAuthRouteTestCase):
    """Tests for GET and POST /verify-email/resend."""

    def test_get_resend_form(self) -> None:
        resp = self.client.get("/verify-email/resend")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Resend Verification Link", resp.get_data(as_text=True))

        resp_alias = self.client.get("/verify-email")
        self.assertEqual(resp_alias.status_code, 200)

    def test_post_resend_generic_response_anti_enumeration(self) -> None:
        # Existing unverified user
        self.register_and_create_user("resend_unverified@example.com", "Password123!", is_verified=False)
        # Existing verified user
        self.register_and_create_user("resend_verified@example.com", "Password123!", is_verified=True)

        test_cases = [
            "resend_unverified@example.com",
            "resend_verified@example.com",
            "completely_unknown_user@example.com",
        ]
        for email in test_cases:
            with self.subTest(email=email):
                resp = self.client.post(
                    "/verify-email/resend",
                    data={"email": email},
                    follow_redirects=True,
                )
                self.assertEqual(resp.status_code, 200)
                html = resp.get_data(as_text=True).lower()
                # Public response is identical and generic across all cases
                self.assertIn("verification link has been generated", html)
                self.assertIn("email delivery is simulated and no message is sent to your inbox", html)

    def test_post_resend_cooldown_enforced_server_side(self) -> None:
        user, _ = self.register_and_create_user("cooldown_user@example.com", "Password123!", is_verified=False)

        with self.app.app_context():
            token_count_before = get_db().execute("SELECT COUNT(*) as c FROM email_verification_tokens").fetchone()["c"]

        # Immediate resend attempt (< 60s) triggers cooldown on backend
        resp = self.client.post(
            "/verify-email/resend",
            data={"email": "cooldown_user@example.com"},
            follow_redirects=True,
        )
        self.assertEqual(resp.status_code, 200)
        # Public response remains generic to prevent enumeration
        self.assertIn("verification link has been generated", resp.get_data(as_text=True).lower())

        with self.app.app_context():
            token_count_after = get_db().execute("SELECT COUNT(*) as c FROM email_verification_tokens").fetchone()["c"]
            # Invariant: No second token was created due to cooldown enforcement
            self.assertEqual(token_count_after, token_count_before)


class TestSafeRedirects(BaseAuthRouteTestCase):
    """Tests for safe local redirect validation and open redirect prevention."""

    def test_is_safe_redirect_url_helper(self) -> None:
        with self.app.test_request_context("/login"):
            self.assertTrue(is_safe_redirect_url("/catalog"))
            self.assertTrue(is_safe_redirect_url("/cart"))
            self.assertTrue(is_safe_redirect_url("/cart/checkout"))
            self.assertTrue(is_safe_redirect_url("/catalog?category=fruit"))

            # Disallow external URLs and protocol-relative URLs
            self.assertFalse(is_safe_redirect_url("http://evil.com"))
            self.assertFalse(is_safe_redirect_url("https://evil.com"))
            self.assertFalse(is_safe_redirect_url("//evil.com"))
            self.assertFalse(is_safe_redirect_url(r"\evil.com"))
            self.assertFalse(is_safe_redirect_url(r"\\evil.com"))
            self.assertFalse(is_safe_redirect_url(r"/\evil.com"))
            self.assertFalse(is_safe_redirect_url(r"/path\evil.com"))
            self.assertFalse(is_safe_redirect_url("/%5cevil.com"))
            self.assertFalse(is_safe_redirect_url("/%5Cevil.com"))
            self.assertFalse(is_safe_redirect_url("/path/%5cevil.com"))
            self.assertFalse(is_safe_redirect_url("/%2f%2fevil.com"))
            self.assertFalse(is_safe_redirect_url("javascript:alert(1)"))
            self.assertFalse(is_safe_redirect_url("/test\r\nSet-Cookie:bad"))
            self.assertFalse(is_safe_redirect_url(""))
            self.assertFalse(is_safe_redirect_url(None))

    def test_post_login_with_safe_next_url(self) -> None:
        self.register_and_create_user("next_user@example.com", "Password123!", is_verified=True)

        resp = self.client.post(
            "/login",
            data={
                "email": "next_user@example.com",
                "password": "Password123!",
                "next": "/cart",
            },
            follow_redirects=False,
        )
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp.headers.get("Location"), "/cart")

    def test_post_login_with_malicious_next_url_falls_back_to_index(self) -> None:
        self.register_and_create_user("safe_user@example.com", "Password123!", is_verified=True)

        malicious_urls = [
            "http://evil.com",
            "//evil.com",
            r"\evil.com",
            r"/\evil.com",
            r"/catalog\evil.com",
            "/%5cevil.com",
            "/%5Cevil.com",
            "javascript:alert(1)",
        ]
        for bad_url in malicious_urls:
            with self.subTest(bad_url=bad_url):
                resp = self.client.post(
                    "/login",
                    data={
                        "email": "safe_user@example.com",
                        "password": "Password123!",
                        "next": bad_url,
                    },
                    follow_redirects=False,
                )
                self.assertEqual(resp.status_code, 302)
                self.assertIn("/", resp.headers.get("Location", ""))
                self.assertNotIn("evil.com", resp.headers.get("Location", ""))


class TestLoginRateLimiting(BaseAuthRouteTestCase):
    """Tests for bounded server-side login rate limiting."""

    def test_consecutive_failed_attempts_trigger_429(self) -> None:
        self.register_and_create_user("ratelimit@example.com", "CorrectPassword123!")

        # 5 failed attempts
        for _ in range(5):
            resp = self.client.post(
                "/login",
                data={"email": "ratelimit@example.com", "password": "WrongPassword!"},
            )
            self.assertEqual(resp.status_code, 401)

        # 6th attempt triggers 429 Too Many Requests
        resp_blocked = self.client.post(
            "/login",
            data={"email": "ratelimit@example.com", "password": "WrongPassword!"},
        )
        self.assertEqual(resp_blocked.status_code, 429)
        self.assertIn("too many failed login attempts", resp_blocked.get_data(as_text=True).lower())

        # Resetting rate limiter allows attempting again
        login_rate_limiter.clear()
        resp_allowed = self.client.post(
            "/login",
            data={"email": "ratelimit@example.com", "password": "CorrectPassword123!"},
        )
        self.assertEqual(resp_allowed.status_code, 302)

    def test_rate_limiter_prunes_expired_timestamps_and_empty_keys(self) -> None:
        # Create a test limiter with a 0.1s window
        limiter = LoginRateLimiter(max_attempts=3, window_seconds=1, max_keys=10)
        limiter.record_failure("127.0.0.1", "shortlived@example.com")
        self.assertIn("127.0.0.1:shortlived@example.com", limiter._attempts)

        # Wait for the entry to expire
        time.sleep(1.05)

        # Calling is_rate_limited or record_failure prunes expired entries and deletes the key
        self.assertFalse(limiter.is_rate_limited("127.0.0.1", "shortlived@example.com"))
        self.assertNotIn("127.0.0.1:shortlived@example.com", limiter._attempts)

    def test_rate_limiter_capacity_eviction_protects_locked_entries(self) -> None:
        # Limiter with capacity for only 2 keys, 2 max attempts
        limiter = LoginRateLimiter(max_attempts=2, window_seconds=300, max_keys=2)

        # Key 1 reaches lockout threshold
        limiter.record_failure("10.0.0.1", "locked@example.com")
        limiter.record_failure("10.0.0.1", "locked@example.com")
        self.assertTrue(limiter.is_rate_limited("10.0.0.1", "locked@example.com"))

        # Key 2 receives a single non-locked attempt
        limiter.record_failure("10.0.0.2", "normal@example.com")

        # Now record key 3, exceeding capacity of 2
        limiter.record_failure("10.0.0.3", "newcomer@example.com")

        # Key 1 MUST NOT be evicted because it is actively locked out
        self.assertTrue(limiter.is_rate_limited("10.0.0.1", "locked@example.com"))
        self.assertIn("10.0.0.1:locked@example.com", limiter._attempts)

        # Key 2 was non-locked and oldest, so it was evicted to maintain capacity
        self.assertNotIn("10.0.0.2:normal@example.com", limiter._attempts)

    def test_rate_limiter_thread_safety(self) -> None:
        limiter = LoginRateLimiter(max_attempts=50, window_seconds=300, max_keys=100)
        threads = []
        errors = []

        def worker(idx: int) -> None:
            try:
                for _ in range(10):
                    limiter.record_failure(f"192.168.1.{idx % 5}", f"user{idx % 3}@example.com")
            except Exception as e:
                errors.append(e)

        for i in range(10):
            t = threading.Thread(target=worker, args=(i,))
            threads.append(t)
            t.start()

        for t in threads:
            t.join()

        self.assertEqual(len(errors), 0)
        self.assertLessEqual(len(limiter._attempts), 100)

    def test_rate_limiter_saturated_with_active_blocks_fails_closed_with_429(self) -> None:
        # Create a limiter with capacity 2 and 2 max attempts
        limiter = LoginRateLimiter(max_attempts=2, window_seconds=300, max_keys=2)

        # Fill capacity with 2 actively locked-out accounts
        limiter.record_failure("10.0.0.1", "locked1@example.com")
        limiter.record_failure("10.0.0.1", "locked1@example.com")
        limiter.record_failure("10.0.0.2", "locked2@example.com")
        limiter.record_failure("10.0.0.2", "locked2@example.com")

        self.assertTrue(limiter.is_rate_limited("10.0.0.1", "locked1@example.com"))
        self.assertTrue(limiter.is_rate_limited("10.0.0.2", "locked2@example.com"))
        self.assertEqual(len(limiter._attempts), 2)

        # A new untracked account arrives when the limiter is completely saturated with active blocks
        is_blocked = limiter.is_rate_limited("10.0.0.3", "new_attacker@example.com")
        # Invariant: Fails closed to avoid infinite unthrottled brute-force attacks
        self.assertTrue(is_blocked)

        # Invariant: Capacity is strictly preserved (never exceeds max_keys)
        self.assertLessEqual(len(limiter._attempts), 2)

        # Invariant: Existing locked accounts were NOT evicted
        self.assertIn("10.0.0.1:locked1@example.com", limiter._attempts)
        self.assertIn("10.0.0.2:locked2@example.com", limiter._attempts)

    def test_login_route_when_rate_limiter_saturated_returns_429(self) -> None:
        # Saturate global login_rate_limiter with active blocks up to max_keys
        original_max_keys = login_rate_limiter.max_keys
        original_max_attempts = login_rate_limiter.max_attempts
        try:
            login_rate_limiter.clear()
            login_rate_limiter.max_keys = 2
            login_rate_limiter.max_attempts = 2

            # Lock out 2 accounts
            login_rate_limiter.record_failure("10.0.0.1", "user1@example.com")
            login_rate_limiter.record_failure("10.0.0.1", "user1@example.com")
            login_rate_limiter.record_failure("10.0.0.2", "user2@example.com")
            login_rate_limiter.record_failure("10.0.0.2", "user2@example.com")

            # Any new login attempt from untracked account must receive 429 without HTTP 500
            resp = self.client.post(
                "/login",
                data={"email": "fresh_user@example.com", "password": "AnyPassword123!"},
            )
            self.assertEqual(resp.status_code, 429)
            self.assertIn("too many failed login attempts", resp.get_data(as_text=True).lower())
            self.assertLessEqual(len(login_rate_limiter._attempts), 2)
        finally:
            login_rate_limiter.max_keys = original_max_keys
            login_rate_limiter.max_attempts = original_max_attempts
            login_rate_limiter.clear()


class TestAuthCsrfProtection(unittest.TestCase):
    """Tests verifying CSRF protection is enforced on all authentication state-changing POST routes."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_auth_csrf.sqlite3"

        # Explicitly keep CSRF_ENABLED active
        class CsrfActiveConfig(Config):
            DATABASE_PATH = str(self.db_path)
            TESTING = True
            SECRET_KEY = "csrf-secret-key-tests"
            # CSRF_ENABLED is True by default

        self.app = create_app(CsrfActiveConfig)
        self.client = self.app.test_client()
        login_rate_limiter.clear()

        with self.app.app_context():
            init_db()

    def tearDown(self) -> None:
        login_rate_limiter.clear()
        self.temp_dir.cleanup()

    def _get_csrf_token(self) -> str:
        """Obtain a valid CSRF token bound to the current client session."""
        with self.client:
            self.client.get("/")
            with self.client.session_transaction() as sess:
                token = sess.get(SESSION_CSRF_KEY)
                if not token:
                    token = generate_csrf_token()
                    sess[SESSION_CSRF_KEY] = token
            return token

    def test_post_register_without_csrf_rejected_with_400(self) -> None:
        resp = self.client.post(
            "/register",
            data={"email": "no_csrf@example.com", "password": "Password123!", "confirm_password": "Password123!"},
        )
        self.assertEqual(resp.status_code, 400)

    def test_post_login_without_csrf_rejected_with_400(self) -> None:
        resp = self.client.post(
            "/login",
            data={"email": "no_csrf@example.com", "password": "Password123!"},
        )
        self.assertEqual(resp.status_code, 400)

    def test_post_logout_without_csrf_rejected_with_400(self) -> None:
        resp = self.client.post("/logout")
        self.assertEqual(resp.status_code, 400)

    def test_post_verify_email_without_csrf_rejected_with_400(self) -> None:
        resp = self.client.post("/verify-email", data={"token": "some_token"})
        self.assertEqual(resp.status_code, 400)

    def test_post_resend_without_csrf_rejected_with_400(self) -> None:
        resp = self.client.post("/verify-email/resend", data={"email": "test@example.com"})
        self.assertEqual(resp.status_code, 400)

    def test_post_register_with_valid_csrf_accepted(self) -> None:
        token = self._get_csrf_token()
        resp = self.client.post(
            "/register",
            data={
                "email": "with_csrf@example.com",
                "password": "ValidPassword123!",
                "confirm_password": "ValidPassword123!",
                "csrf_token": token,
            },
            follow_redirects=False,
        )
        self.assertEqual(resp.status_code, 302)


if __name__ == "__main__":
    unittest.main()

"""Tests for Google OAuth 2.0 / OpenID Connect authentication (Phase 10.9)."""
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch

from werkzeug.security import generate_password_hash

from app import create_app
from app.config import Config
from app.database.connection import init_db
from app.models.external_identity import ExternalIdentity
from app.models.product import Product
from app.models.user import User, UserRole
from app.oauth import is_google_auth_configured, oauth
from app.repositories.external_identity_repository import ExternalIdentityRepository
from app.repositories.product_repository import ProductRepository
from app.repositories.user_repository import UserRepository
from app.routes.auth import account_link_rate_limiter, login_rate_limiter
from app.routes.cart_session import SESSION_CART_KEY
from app.services.auth_service import AuthService


class BaseGoogleAuthTestCase(unittest.TestCase):
    """Base test case providing isolated temporary SQLite database and app instance."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_google_auth.sqlite3"

        class TestRouteConfig(Config):
            DATABASE_PATH = str(self.db_path)
            TESTING = True
            SECRET_KEY = "test-google-auth-secret-key"
            CSRF_ENABLED = False
            GOOGLE_CLIENT_ID = "mock-google-client-id"
            GOOGLE_CLIENT_SECRET = "mock-google-client-secret"

        self.config_cls = TestRouteConfig
        self.app = create_app(TestRouteConfig)
        self.client = self.app.test_client()

        with self.app.app_context():
            init_db()
            self.user_repo = UserRepository()
            self.identity_repo = ExternalIdentityRepository()
            self.product_repo = ProductRepository()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()


class TestGoogleAuthConfig(unittest.TestCase):
    """Tests for configuration handling and disabled vs configured state."""

    def test_app_starts_without_google_credentials(self):
        with tempfile.TemporaryDirectory() as td:
            db_path = str(Path(td) / "unconfigured.sqlite3")

            class UnconfiguredConfig(Config):
                DATABASE_PATH = db_path
                TESTING = True
                CSRF_ENABLED = False
                GOOGLE_CLIENT_ID = None
                GOOGLE_CLIENT_SECRET = None

            app = create_app(UnconfiguredConfig)
            with app.app_context():
                init_db()
                self.assertFalse(is_google_auth_configured())

            client = app.test_client()
            resp = client.get("/login")
            self.assertEqual(resp.status_code, 200)
            self.assertNotIn(b"Continue with Google", resp.data)

    def test_password_login_remains_available_without_google_credentials(self):
        with tempfile.TemporaryDirectory() as td:
            db_path = str(Path(td) / "unconfigured.sqlite3")

            class UnconfiguredConfig(Config):
                DATABASE_PATH = db_path
                TESTING = True
                CSRF_ENABLED = False
                GOOGLE_CLIENT_ID = None
                GOOGLE_CLIENT_SECRET = None

            app = create_app(UnconfiguredConfig)
            with app.app_context():
                init_db()
                auth_service = AuthService()
                user, _ = auth_service.register_customer(
                    "password_user@example.com", "Password123!", "Password123!"
                )
                auth_service.user_repo.set_verified(user.id, True)

            client = app.test_client()
            resp = client.post(
                "/login",
                data={"email": "password_user@example.com", "password": "Password123!"},
                follow_redirects=False,
            )
            self.assertEqual(resp.status_code, 302)
            with client.session_transaction() as sess:
                self.assertEqual(sess.get("user_id"), 1)

    def test_google_signin_rejected_when_unconfigured(self):
        with tempfile.TemporaryDirectory() as td:
            db_path = str(Path(td) / "unconfigured.sqlite3")

            class UnconfiguredConfig(Config):
                DATABASE_PATH = db_path
                TESTING = True
                GOOGLE_CLIENT_ID = ""
                GOOGLE_CLIENT_SECRET = ""

            app = create_app(UnconfiguredConfig)
            with app.app_context():
                init_db()

            client = app.test_client()
            resp1 = client.get("/auth/google", follow_redirects=True)
            self.assertIn(b"Google sign-in is not currently configured.", resp1.data)

            resp2 = client.get("/auth/google/callback", follow_redirects=True)
            self.assertIn(b"Google sign-in is not currently configured.", resp2.data)

    def test_configured_mode_renders_google_login_action(self):
        with tempfile.TemporaryDirectory() as td:
            db_path = str(Path(td) / "configured.sqlite3")

            class ConfiguredConfig(Config):
                DATABASE_PATH = db_path
                TESTING = True
                GOOGLE_CLIENT_ID = "valid_client_id"
                GOOGLE_CLIENT_SECRET = "valid_client_secret"

            app = create_app(ConfiguredConfig)
            with app.app_context():
                init_db()
                self.assertTrue(is_google_auth_configured())

            client = app.test_client()
            resp_login = client.get("/login")
            self.assertEqual(resp_login.status_code, 200)
            self.assertIn(b"Continue with Google", resp_login.data)

            resp_register = client.get("/register")
            self.assertEqual(resp_register.status_code, 200)
            self.assertIn(b"Continue with Google", resp_register.data)


class TestGoogleAuthRoutes(BaseGoogleAuthTestCase):
    """Tests for OAuth authorization initiation and callback handling."""

    def test_authorization_initiation_sets_state_nonce_and_pkce(self):
        resp = self.client.get("/auth/google?next=/orders")
        self.assertEqual(resp.status_code, 302)
        location = resp.headers.get("Location")
        self.assertIn("https://accounts.google.com/o/oauth2/v2/auth", location)
        self.assertIn("client_id=mock-google-client-id", location)
        self.assertIn("state=", location)
        self.assertIn("nonce=", location)
        self.assertIn("scope=openid+email+profile", location)
        # PKCE S256 verification
        self.assertIn("code_challenge=", location)
        self.assertIn("code_challenge_method=S256", location)
        # Google account selection
        self.assertIn("prompt=select_account", location)
        # Canonical redirect URI
        self.assertIn("redirect_uri=http%3A%2F%2F127.0.0.1%3A5000%2Fauth%2Fgoogle%2Fcallback", location)

        with self.client.session_transaction() as sess:
            state_keys = [k for k in sess.keys() if k.startswith("_state_google_")]
            self.assertEqual(len(state_keys), 1)
            state_data = sess[state_keys[0]]["data"]
            self.assertIn("code_verifier", state_data)
            self.assertIn("nonce", state_data)
            self.assertEqual(state_data.get("next_url"), "/orders")

    def test_authorization_initiation_ignores_unsafe_next(self):
        for unsafe in ["//evil.com", "https://attacker.com", "/\\evil"]:
            self.client.get(f"/auth/google?next={unsafe}")
            with self.client.session_transaction() as sess:
                state_keys = [k for k in sess.keys() if k.startswith("_state_google_")]
                state_data = sess[state_keys[-1]]["data"]
                self.assertIsNone(state_data.get("next_url"))

    def test_canonical_oauth_redirect_uri_ignores_untrusted_host_headers(self):
        """Verify redirect_uri sent to Google is derived from trusted config, ignoring Host headers."""
        resp = self.client.get(
            "/auth/google",
            headers={"Host": "attacker.com:8080", "X-Forwarded-Host": "evil.example.com"},
        )
        self.assertEqual(resp.status_code, 302)
        location = resp.headers.get("Location")
        self.assertIn("redirect_uri=http%3A%2F%2F127.0.0.1%3A5000%2Fauth%2Fgoogle%2Fcallback", location)
        self.assertNotIn("attacker.com", location)
        self.assertNotIn("evil.example.com", location)

    def test_canonical_oauth_redirect_uri_with_custom_app_base_url(self):
        """Verify redirect_uri respects explicit APP_BASE_URL override."""
        self.app.config["APP_BASE_URL"] = "https://shop.example.com"
        resp = self.client.get("/auth/google")
        self.assertEqual(resp.status_code, 302)
        location = resp.headers.get("Location")
        self.assertIn("redirect_uri=https%3A%2F%2Fshop.example.com%2Fauth%2Fgoogle%2Fcallback", location)

    def test_canonical_oauth_redirect_uri_helper_direct(self):
        """Unit test for get_canonical_oauth_redirect_uri helper."""
        from app.routes.auth import get_canonical_oauth_redirect_uri
        with self.app.test_request_context():
            # 1. Configured APP_BASE_URL
            self.app.config["APP_BASE_URL"] = "http://localhost:5000"
            self.assertEqual(get_canonical_oauth_redirect_uri(), "http://localhost:5000/auth/google/callback")

            # 2. Configured SERVER_NAME fallback
            self.app.config["APP_BASE_URL"] = None
            self.app.config["SERVER_NAME"] = "foodshop.local:5000"
            self.app.config["PREFERRED_URL_SCHEME"] = "https"
            self.assertEqual(get_canonical_oauth_redirect_uri(), "https://foodshop.local:5000/auth/google/callback")

    def test_pkce_code_verifier_passed_during_token_exchange(self):
        resp = self.client.get("/auth/google")
        state = resp.headers.get("Location").split("state=")[1].split("&")[0]

        with self.client.session_transaction() as sess:
            state_data = oauth.google.framework.get_state_data(sess, state)
            code_verifier = state_data.get("code_verifier")
            self.assertIsNotNone(code_verifier)

        with patch.object(oauth.google, "fetch_access_token") as mock_fetch:
            mock_fetch.return_value = {
                "access_token": "mock_tok",
                "id_token": "mock_id_tok",
                "userinfo": {"sub": "sub1", "email": "u@example.com", "email_verified": True},
            }
            with patch.object(oauth.google, "parse_id_token", return_value={"sub": "sub1", "email": "u@example.com", "email_verified": True}):
                self.client.get(f"/auth/google/callback?code=mock_code&state={state}")
                mock_fetch.assert_called_once()
                _, kwargs = mock_fetch.call_args
                self.assertEqual(kwargs.get("code_verifier"), code_verifier)

    def test_multi_tab_distinct_destinations_tab1_then_tab2(self):
        # Tab 1 starts with /cart
        r1 = self.client.get("/auth/google?next=/cart")
        state1 = r1.headers.get("Location").split("state=")[1].split("&")[0]

        # Tab 2 starts with /orders
        r2 = self.client.get("/auth/google?next=/orders")
        state2 = r2.headers.get("Location").split("state=")[1].split("&")[0]

        self.assertNotEqual(state1, state2)

        # Tab 1 completes first
        with patch.object(oauth.google, "authorize_access_token") as mock_token:
            mock_token.return_value = {
                "access_token": "tok1",
                "userinfo": {"sub": "user1_sub", "email": "user1@example.com", "email_verified": True},
            }
            cb1 = self.client.get(f"/auth/google/callback?state={state1}")
            self.assertEqual(cb1.status_code, 302)
            self.assertEqual(cb1.headers.get("Location"), "/cart")

        # Tab 2 completes second (user is already authenticated from tab 1)
        cb2 = self.client.get(f"/auth/google/callback?state={state2}")
        self.assertEqual(cb2.status_code, 302)
        self.assertEqual(cb2.headers.get("Location"), "/orders")

    def test_multi_tab_distinct_destinations_tab2_then_tab1(self):
        # Tab 1 starts with /cart
        r1 = self.client.get("/auth/google?next=/cart")
        state1 = r1.headers.get("Location").split("state=")[1].split("&")[0]

        # Tab 2 starts with /orders
        r2 = self.client.get("/auth/google?next=/orders")
        state2 = r2.headers.get("Location").split("state=")[1].split("&")[0]

        # Tab 2 completes first
        with patch.object(oauth.google, "authorize_access_token") as mock_token:
            mock_token.return_value = {
                "access_token": "tok2",
                "userinfo": {"sub": "user2_sub", "email": "user2@example.com", "email_verified": True},
            }
            cb2 = self.client.get(f"/auth/google/callback?state={state2}")
            self.assertEqual(cb2.status_code, 302)
            self.assertEqual(cb2.headers.get("Location"), "/orders")

        # Tab 1 completes second
        cb1 = self.client.get(f"/auth/google/callback?state={state1}")
        self.assertEqual(cb1.status_code, 302)
        self.assertEqual(cb1.headers.get("Location"), "/cart")

    def test_invalid_or_missing_state_rejected(self):
        # Missing state
        r1 = self.client.get("/auth/google/callback?code=mock_code", follow_redirects=True)
        self.assertIn(b"Google sign-in failed. Please try again", r1.data)
        with self.client.session_transaction() as sess:
            self.assertIsNone(sess.get("user_id"))

        # Forged/invalid state
        r2 = self.client.get("/auth/google/callback?code=mock_code&state=forged_state", follow_redirects=True)
        self.assertIn(b"Google sign-in failed. Please try again", r2.data)
        with self.client.session_transaction() as sess:
            self.assertIsNone(sess.get("user_id"))

    def test_state_replay_rejected(self):
        resp = self.client.get("/auth/google?next=/cart")
        state = resp.headers.get("Location").split("state=")[1].split("&")[0]

        with patch.object(oauth.google, "authorize_access_token") as mock_token:
            mock_token.return_value = {
                "access_token": "tok",
                "userinfo": {"sub": "replay_sub", "email": "replay@example.com", "email_verified": True},
            }
            cb1 = self.client.get(f"/auth/google/callback?state={state}")
            self.assertEqual(cb1.status_code, 302)

        # Logout
        self.client.post("/logout")

        # Second attempt using replayed state fails because state was consumed and purged
        cb2 = self.client.get(f"/auth/google/callback?state={state}", follow_redirects=True)
        self.assertIn(b"Google sign-in failed. Please try again", cb2.data)
        with self.client.session_transaction() as sess:
            self.assertIsNone(sess.get("user_id"))

    def test_authenticated_user_visiting_google_endpoints_redirected(self):
        with self.app.app_context():
            user = self.user_repo.create(
                User(email="already_in@example.com", role=UserRole.CUSTOMER, is_verified=True)
            )
            uid = user.id

        with self.client.session_transaction() as sess:
            sess["user_id"] = uid

        r1 = self.client.get("/auth/google")
        self.assertEqual(r1.status_code, 302)
        self.assertEqual(r1.headers.get("Location"), "/")

        r2 = self.client.get("/auth/google/callback")
        self.assertEqual(r2.status_code, 302)
        self.assertEqual(r2.headers.get("Location"), "/")

    def test_provider_cancellation_handled_safely(self):
        resp = self.client.get("/auth/google/callback?error=access_denied", follow_redirects=True)
        self.assertIn(b"Google sign-in was cancelled.", resp.data)
        with self.client.session_transaction() as sess:
            self.assertIsNone(sess.get("user_id"))

    def test_provider_server_error_handled_safely(self):
        resp = self.client.get("/auth/google/callback?error=server_error", follow_redirects=True)
        self.assertIn(b"Google sign-in failed. Please try again", resp.data)
        with self.client.session_transaction() as sess:
            self.assertIsNone(sess.get("user_id"))

    def test_token_exchange_exception_handled_safely(self):
        with patch.object(oauth.google, "authorize_access_token", side_effect=Exception("OAuth fail")):
            resp = self.client.get("/auth/google/callback", follow_redirects=True)
            self.assertIn(b"Google sign-in failed. Please try again", resp.data)
            with self.client.session_transaction() as sess:
                self.assertIsNone(sess.get("user_id"))

    def test_missing_or_invalid_claims_rejected(self):
        cases = [
            # No userinfo
            {"access_token": "tok", "userinfo": None},
            # Missing sub
            {"access_token": "tok", "userinfo": {"email": "u@example.com", "email_verified": True}},
            # Empty sub
            {"access_token": "tok", "userinfo": {"sub": "  ", "email": "u@example.com", "email_verified": True}},
            # Missing email
            {"access_token": "tok", "userinfo": {"sub": "sub123", "email_verified": True}},
            # Empty email
            {"access_token": "tok", "userinfo": {"sub": "sub123", "email": "", "email_verified": True}},
            # Unverified email
            {"access_token": "tok", "userinfo": {"sub": "sub123", "email": "u@example.com", "email_verified": False}},
        ]
        for token_data in cases:
            with patch.object(oauth.google, "authorize_access_token", return_value=token_data):
                resp = self.client.get("/auth/google/callback", follow_redirects=True)
                with self.client.session_transaction() as sess:
                    self.assertIsNone(sess.get("user_id"))


class TestGoogleAuthIdentityAssociation(BaseGoogleAuthTestCase):
    """Tests for Case A, Case B, Case C, Case D and password defenses."""

    def test_case_b_new_customer_registration_success(self):
        with patch.object(oauth.google, "authorize_access_token") as mock_token:
            mock_token.return_value = {
                "access_token": "token123",
                "id_token": "jwt123",
                "userinfo": {
                    "sub": "google_sub_case_b",
                    "email": "case_b@example.com",
                    "email_verified": True,
                    "name": "Case B Customer",
                },
            }
            resp = self.client.get("/auth/google/callback", follow_redirects=True)
            self.assertIn(b"Welcome back, case_b@example.com!", resp.data)

        with self.client.session_transaction() as sess:
            user_id = sess.get("user_id")
            self.assertIsNotNone(user_id)
            # Verify no OAuth tokens are stored in the session
            self.assertNotIn("access_token", sess)
            self.assertNotIn("id_token", sess)

        with self.app.app_context():
            user = self.user_repo.get_by_id(user_id)
            self.assertEqual(user.email, "case_b@example.com")
            self.assertEqual(user.role, UserRole.CUSTOMER)
            self.assertTrue(user.is_verified)
            self.assertIsNone(user.password_hash)

            identity = self.identity_repo.get_by_provider_sub("google", "google_sub_case_b")
            self.assertIsNotNone(identity)
            self.assertEqual(identity.user_id, user.id)

    def test_case_a_existing_linked_google_user_login(self):
        with self.app.app_context():
            user = self.user_repo.create(
                User(email="case_a@example.com", role=UserRole.CUSTOMER, is_verified=True)
            )
            self.identity_repo.create(
                ExternalIdentity(
                    user_id=user.id,
                    provider="google",
                    provider_user_id="google_sub_case_a",
                    email=user.email,
                )
            )
            target_uid = user.id

        with patch.object(oauth.google, "authorize_access_token") as mock_token:
            mock_token.return_value = {
                "access_token": "token_case_a",
                "userinfo": {
                    "sub": "google_sub_case_a",
                    "email": "case_a@example.com",
                    "email_verified": True,
                },
            }
            resp = self.client.get("/auth/google/callback", follow_redirects=True)
            self.assertIn(b"Welcome back, case_a@example.com!", resp.data)

        with self.client.session_transaction() as sess:
            self.assertEqual(sess.get("user_id"), target_uid)

    def test_case_c_email_collision_with_local_customer_initiates_linking(self):
        with self.app.app_context():
            # Locally registered customer with password
            local_user = self.user_repo.create(
                User(
                    email="collision@example.com",
                    password_hash="scrypt:hashedpassword",
                    role=UserRole.CUSTOMER,
                    is_verified=True,
                )
            )
            local_id = local_user.id

        # Sign-in attempt with unlinked Google sub matching local verified email
        with patch.object(oauth.google, "authorize_access_token") as mock_token:
            mock_token.return_value = {
                "access_token": "tok",
                "userinfo": {
                    "sub": "unlinked_new_google_sub",
                    "email": "collision@example.com",
                    "email_verified": True,
                },
            }
            resp = self.client.get("/auth/google/callback", follow_redirects=False)
            self.assertEqual(resp.status_code, 302)
            self.assertIn("/auth/google/link", resp.headers.get("Location"))

            # Follow redirect to linking confirmation page
            resp_page = self.client.get(resp.headers.get("Location"))
            self.assertEqual(resp_page.status_code, 200)
            self.assertIn(b"Confirm Password to Link Account", resp_page.data)
            self.assertIn(b"collision@example.com", resp_page.data)

        with self.client.session_transaction() as sess:
            # User must NOT be logged in yet
            self.assertIsNone(sess.get("user_id"))
            self.assertIn("_pending_google_link", sess)

        # Verify no external identity was auto-linked without password confirmation
        with self.app.app_context():
            identities = self.identity_repo.get_by_user_id(local_id)
            self.assertEqual(len(identities), 0)

    def test_case_c_unverified_local_customer_collision_strictly_rejected(self):
        with self.app.app_context():
            unverified_user = self.user_repo.create(
                User(
                    email="unverified_col@example.com",
                    password_hash="scrypt:hashedpassword",
                    role=UserRole.CUSTOMER,
                    is_verified=False,
                )
            )
            uid = unverified_user.id

        with patch.object(oauth.google, "authorize_access_token") as mock_token:
            mock_token.return_value = {
                "access_token": "tok",
                "userinfo": {
                    "sub": "unverified_google_sub",
                    "email": "unverified_col@example.com",
                    "email_verified": True,
                },
            }
            resp = self.client.get("/auth/google/callback", follow_redirects=True)
            self.assertIn(b"has not been verified yet", resp.data)

        with self.client.session_transaction() as sess:
            self.assertIsNone(sess.get("user_id"))
            self.assertNotIn("_pending_google_link", sess)

        with self.app.app_context():
            identities = self.identity_repo.get_by_user_id(uid)
            self.assertEqual(len(identities), 0)

    def test_case_c_owner_account_collision_protected(self):
        with self.app.app_context():
            owner = self.user_repo.create(
                User(
                    email="owner@example.com",
                    password_hash="ownerpasshash",
                    role=UserRole.OWNER,
                    is_verified=True,
                )
            )
            owner_id = owner.id

        with patch.object(oauth.google, "authorize_access_token") as mock_token:
            mock_token.return_value = {
                "access_token": "tok",
                "userinfo": {
                    "sub": "fake_owner_sub",
                    "email": "owner@example.com",
                    "email_verified": True,
                },
            }
            resp = self.client.get("/auth/google/callback", follow_redirects=True)
            self.assertIn(b"An account with this email address already exists", resp.data)

        with self.client.session_transaction() as sess:
            self.assertIsNone(sess.get("user_id"))

        with self.app.app_context():
            identities = self.identity_repo.get_by_user_id(owner_id)
            self.assertEqual(len(identities), 0)

    def test_case_d_linked_user_with_changed_google_email_remains_stable(self):
        with self.app.app_context():
            user = self.user_repo.create(
                User(email="original_email@example.com", role=UserRole.CUSTOMER, is_verified=True)
            )
            self.identity_repo.create(
                ExternalIdentity(
                    user_id=user.id,
                    provider="google",
                    provider_user_id="stable_google_sub",
                    email=user.email,
                )
            )
            uid = user.id

        # Google returns changed email, but same sub
        with patch.object(oauth.google, "authorize_access_token") as mock_token:
            mock_token.return_value = {
                "access_token": "tok",
                "userinfo": {
                    "sub": "stable_google_sub",
                    "email": "new_google_email@example.com",
                    "email_verified": True,
                },
            }
            resp = self.client.get("/auth/google/callback", follow_redirects=True)
            self.assertIn(b"Welcome back, original_email@example.com!", resp.data)

        with self.client.session_transaction() as sess:
            self.assertEqual(sess.get("user_id"), uid)

        # Confirm database email was NOT overwritten
        with self.app.app_context():
            db_user = self.user_repo.get_by_id(uid)
            self.assertEqual(db_user.email, "original_email@example.com")

    def test_google_only_user_cannot_login_with_password(self):
        with patch.object(oauth.google, "authorize_access_token") as mock_token:
            mock_token.return_value = {
                "access_token": "tok",
                "userinfo": {
                    "sub": "pw_test_sub",
                    "email": "nopassword@example.com",
                    "email_verified": True,
                },
            }
            self.client.get("/auth/google/callback")

        self.client.post("/logout")

        for pw in ["", "   ", "password123", "None"]:
            resp = self.client.post(
                "/login",
                data={"email": "nopassword@example.com", "password": pw},
                follow_redirects=True,
            )
            self.assertIn(b"Invalid email or password.", resp.data)
            with self.client.session_transaction() as sess:
                self.assertIsNone(sess.get("user_id"))

    def test_google_user_resend_verification_rejected(self):
        with patch.object(oauth.google, "authorize_access_token") as mock_token:
            mock_token.return_value = {
                "access_token": "tok",
                "userinfo": {
                    "sub": "verified_sub",
                    "email": "already_verified@example.com",
                    "email_verified": True,
                },
            }
            self.client.get("/auth/google/callback")

        with self.app.app_context():
            auth_service = AuthService()
            # Verified account returns False for resend request
            result = auth_service.request_verification_resend("already_verified@example.com")
            self.assertFalse(result)


class TestGoogleAuthSessionAndCartSecurity(BaseGoogleAuthTestCase):
    """Tests for session fixation defense, cart preservation, and safe redirects."""

    def test_session_fixation_clears_old_session_and_rotates_csrf(self):
        with self.client.session_transaction() as sess:
            sess["old_secret_value"] = "attack_payload"
            sess["_csrf_token"] = "stale_csrf_token"

        with patch.object(oauth.google, "authorize_access_token") as mock_token:
            mock_token.return_value = {
                "access_token": "tok",
                "userinfo": {
                    "sub": "session_sub",
                    "email": "session_user@example.com",
                    "email_verified": True,
                },
            }
            self.client.get("/auth/google/callback")

        with self.client.session_transaction() as sess:
            self.assertNotIn("old_secret_value", sess)
            self.assertNotEqual(sess.get("_csrf_token"), "stale_csrf_token")
            self.assertIsNotNone(sess.get("user_id"))

    def test_cart_preserved_on_google_login(self):
        with self.client.session_transaction() as sess:
            sess[SESSION_CART_KEY] = {"1": 2, "2": 4}

        with patch.object(oauth.google, "authorize_access_token") as mock_token:
            mock_token.return_value = {
                "access_token": "tok",
                "userinfo": {
                    "sub": "cart_sub",
                    "email": "cart_user@example.com",
                    "email_verified": True,
                },
            }
            self.client.get("/auth/google/callback")

        with self.client.session_transaction() as sess:
            self.assertEqual(sess.get(SESSION_CART_KEY), {"1": 2, "2": 4})
            self.assertIsNotNone(sess.get("user_id"))

    def test_safe_redirect_after_google_login(self):
        resp_init = self.client.get("/auth/google?next=/cart")
        state = resp_init.headers.get("Location").split("state=")[1].split("&")[0]

        with patch.object(oauth.google, "authorize_access_token") as mock_token:
            mock_token.return_value = {
                "access_token": "tok",
                "userinfo": {
                    "sub": "redirect_sub",
                    "email": "redirect_user@example.com",
                    "email_verified": True,
                },
            }
            resp = self.client.get(f"/auth/google/callback?state={state}")
            self.assertEqual(resp.status_code, 302)
            self.assertEqual(resp.headers.get("Location"), "/cart")

    def test_unsafe_next_redirect_falls_back_to_storefront(self):
        resp_init = self.client.get("/auth/google?next=//evil.com")
        state = resp_init.headers.get("Location").split("state=")[1].split("&")[0]

        with patch.object(oauth.google, "authorize_access_token") as mock_token:
            mock_token.return_value = {
                "access_token": "tok",
                "userinfo": {
                    "sub": "evil_sub",
                    "email": "evil_redirect@example.com",
                    "email_verified": True,
                },
            }
            resp = self.client.get(f"/auth/google/callback?state={state}")
            self.assertEqual(resp.status_code, 302)
            self.assertEqual(resp.headers.get("Location"), "/")


class TestGoogleAccountLinkingFlow(BaseGoogleAuthTestCase):
    """Integration tests for Phase 10.11.3: Google Account Linking flow."""

    def setUp(self) -> None:
        super().setUp()
        account_link_rate_limiter.clear()
        login_rate_limiter.clear()
        with self.app.app_context():
            # Create a verified local customer with known password
            self.user = self.user_repo.create(
                User(
                    email="localcustomer@example.com",
                    password_hash=generate_password_hash("ValidStorePass123!"),
                    role=UserRole.CUSTOMER,
                    is_verified=True,
                )
            )

    def tearDown(self) -> None:
        account_link_rate_limiter.clear()
        login_rate_limiter.clear()
        super().tearDown()

    def _initiate_linking(self, sub="google_sub_link_101", email="localcustomer@example.com"):
        """Helper to initiate a linking intent session via Google callback collision."""
        with patch.object(oauth.google, "authorize_access_token") as mock_token:
            mock_token.return_value = {
                "access_token": "tok_linking",
                "userinfo": {
                    "sub": sub,
                    "email": email,
                    "email_verified": True,
                },
            }
            resp = self.client.get("/auth/google/callback", follow_redirects=False)
            return resp

    def test_successful_linking_flow_with_password(self):
        # Cart preservation check
        with self.client.session_transaction() as sess:
            sess[SESSION_CART_KEY] = {"1": 5, "2": 3}

        # 1. Google callback triggers collision and redirects to linking page
        resp = self._initiate_linking(sub="google_sub_first_link")
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/auth/google/link", resp.headers.get("Location"))

        # 2. View linking confirmation page
        resp_page = self.client.get("/auth/google/link")
        self.assertEqual(resp_page.status_code, 200)
        self.assertIn(b"Confirm Password to Link Account", resp_page.data)
        self.assertIn(b"localcustomer@example.com", resp_page.data)

        # 3. Submit valid store password
        resp_post = self.client.post(
            "/auth/google/link",
            data={"password": "ValidStorePass123!"},
            follow_redirects=False,
        )
        self.assertEqual(resp_post.status_code, 302)
        self.assertEqual(resp_post.headers.get("Location"), "/")

        # 4. Verify user is now authenticated and pending intent cleared
        with self.client.session_transaction() as sess:
            self.assertEqual(sess.get("user_id"), self.user.id)
            self.assertNotIn("_pending_google_link", sess)
            # Cart is preserved
            self.assertEqual(sess.get(SESSION_CART_KEY), {"1": 5, "2": 3})

        # 5. Verify external identity record was created in DB
        with self.app.app_context():
            identities = self.identity_repo.get_by_user_id(self.user.id)
            self.assertEqual(len(identities), 1)
            self.assertEqual(identities[0].provider, "google")
            self.assertEqual(identities[0].provider_user_id, "google_sub_first_link")

        # 6. Verify local password login STILL works
        self.client.post("/logout")
        with self.client.session_transaction() as sess:
            self.assertIsNone(sess.get("user_id"))

        resp_pw_login = self.client.post(
            "/login",
            data={"email": "localcustomer@example.com", "password": "ValidStorePass123!"},
            follow_redirects=True,
        )
        self.assertIn(b"Welcome back", resp_pw_login.data)
        with self.client.session_transaction() as sess:
            self.assertEqual(sess.get("user_id"), self.user.id)

        # 7. Verify subsequent Google login is 1-click without password prompt
        self.client.post("/logout")
        with patch.object(oauth.google, "authorize_access_token") as mock_token:
            mock_token.return_value = {
                "access_token": "tok_subsequent",
                "userinfo": {
                    "sub": "google_sub_first_link",
                    "email": "localcustomer@example.com",
                    "email_verified": True,
                },
            }
            resp_subsequent = self.client.get("/auth/google/callback", follow_redirects=True)
            self.assertIn(b"Welcome back, localcustomer@example.com!", resp_subsequent.data)
            with self.client.session_transaction() as sess:
                self.assertEqual(sess.get("user_id"), self.user.id)

    def test_linking_with_incorrect_password(self):
        self._initiate_linking(sub="google_sub_wrong_pw")

        resp_post = self.client.post(
            "/auth/google/link",
            data={"password": "TotallyWrongPassword!"},
        )
        self.assertEqual(resp_post.status_code, 401)
        self.assertIn(b"Incorrect password.", resp_post.data)

        # Confirm user is NOT logged in and no identity linked
        with self.client.session_transaction() as sess:
            self.assertIsNone(sess.get("user_id"))
            self.assertIn("_pending_google_link", sess)

        with self.app.app_context():
            identities = self.identity_repo.get_by_user_id(self.user.id)
            self.assertEqual(len(identities), 0)

    def test_linking_with_empty_or_whitespace_password(self):
        self._initiate_linking(sub="google_sub_empty_pw")

        resp_post = self.client.post(
            "/auth/google/link",
            data={"password": "   "},
        )
        self.assertEqual(resp_post.status_code, 400)
        self.assertIn(b"Please enter your store password to confirm linking.", resp_post.data)

        with self.client.session_transaction() as sess:
            self.assertIsNone(sess.get("user_id"))

    def test_rate_limiting_blocks_after_5_failed_attempts(self):
        self._initiate_linking(sub="google_sub_ratelimit")

        # 5 failed attempts
        for i in range(5):
            resp = self.client.post(
                "/auth/google/link",
                data={"password": f"WrongAttempt{i}"},
            )
            self.assertEqual(resp.status_code, 401)

        # 6th attempt is throttled with 429
        resp6 = self.client.post(
            "/auth/google/link",
            data={"password": "ValidStorePass123!"},
        )
        self.assertEqual(resp6.status_code, 429)
        self.assertIn(b"Too many failed attempts", resp6.data)

        # Confirm still not linked or logged in
        with self.client.session_transaction() as sess:
            self.assertIsNone(sess.get("user_id"))

    def test_expired_linking_intent_rejected(self):
        self._initiate_linking(sub="google_sub_expired")

        # Expire session timestamp manually
        with self.client.session_transaction() as sess:
            pending = dict(sess["_pending_google_link"])
            pending["expires_at"] = time.time() - 30
            sess["_pending_google_link"] = pending
            sess.modified = True

        # GET request clears intent and redirects to login
        resp_get = self.client.get("/auth/google/link", follow_redirects=True)
        self.assertIn(b"account linking request has expired", resp_get.data)
        with self.client.session_transaction() as sess:
            self.assertNotIn("_pending_google_link", sess)

    def test_expired_intent_on_post_rejected(self):
        self._initiate_linking(sub="google_sub_expired_post")

        with self.client.session_transaction() as sess:
            pending = dict(sess["_pending_google_link"])
            pending["expires_at"] = time.time() - 30
            sess["_pending_google_link"] = pending
            sess.modified = True

        resp_post = self.client.post(
            "/auth/google/link",
            data={"password": "ValidStorePass123!"},
            follow_redirects=True,
        )
        self.assertIn(b"account linking request has expired", resp_post.data)
        with self.client.session_transaction() as sess:
            self.assertIsNone(sess.get("user_id"))
            self.assertNotIn("_pending_google_link", sess)

    def test_missing_pending_link_intent_redirects_to_login(self):
        resp_get = self.client.get("/auth/google/link", follow_redirects=True)
        self.assertEqual(resp_get.status_code, 200)
        self.assertIn(b"Sign In", resp_get.data)

        resp_post = self.client.post(
            "/auth/google/link",
            data={"password": "ValidStorePass123!"},
            follow_redirects=True,
        )
        self.assertEqual(resp_post.status_code, 200)
        self.assertIn(b"Sign In", resp_post.data)

    def test_cancel_google_link_clears_session(self):
        self._initiate_linking(sub="google_sub_cancel")

        resp = self.client.post("/auth/google/link/cancel", follow_redirects=True)
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"Google account linking was cancelled.", resp.data)

        with self.client.session_transaction() as sess:
            self.assertNotIn("_pending_google_link", sess)
            self.assertIsNone(sess.get("user_id"))

    def test_get_cancel_endpoint_does_not_mutate_state(self):
        self._initiate_linking(sub="google_sub_cancel_get")

        # GET request to /cancel must be safe and idempotent
        resp = self.client.get("/auth/google/link/cancel", follow_redirects=False)
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp.headers.get("Location"), "/login")

        # Session must NOT be cleared by GET
        with self.client.session_transaction() as sess:
            self.assertIn("_pending_google_link", sess)

    def test_replaying_cancelled_session_cookie_strictly_rejected(self):
        self._initiate_linking(sub="google_sub_replay_cancel")

        # Capture the pending link session state
        with self.client.session_transaction() as sess:
            saved_pending = dict(sess["_pending_google_link"])

        # Cancel the linking intent via POST
        self.client.post("/auth/google/link/cancel", follow_redirects=True)

        # Restore the older session cookie containing the cancelled intent
        with self.client.session_transaction() as sess:
            sess["_pending_google_link"] = saved_pending

        # GET request checks server-side SQLite state and rejects replayed cookie
        resp_get = self.client.get("/auth/google/link", follow_redirects=True)
        self.assertIn(b"linking request was cancelled", resp_get.data)
        with self.client.session_transaction() as sess:
            self.assertNotIn("_pending_google_link", sess)

        # Re-inject the replayed cookie and attempt POST confirm
        with self.client.session_transaction() as sess:
            sess["_pending_google_link"] = saved_pending

        resp_post = self.client.post(
            "/auth/google/link",
            data={"password": "ValidStorePass123!"},
            follow_redirects=True,
        )
        self.assertIn(b"linking request was cancelled", resp_post.data)
        with self.client.session_transaction() as sess:
            self.assertIsNone(sess.get("user_id"))

        # Verify no external identity was created
        with self.app.app_context():
            identities = self.identity_repo.get_by_user_id(self.user.id)
            self.assertEqual(len(identities), 0)

    def test_replaying_consumed_session_cookie_strictly_rejected(self):
        self._initiate_linking(sub="google_sub_replay_consumed")

        with self.client.session_transaction() as sess:
            saved_pending = dict(sess["_pending_google_link"])

        # Successfully confirm linking once
        resp_confirm = self.client.post(
            "/auth/google/link",
            data={"password": "ValidStorePass123!"},
            follow_redirects=False,
        )
        self.assertEqual(resp_confirm.status_code, 302)

        # Log out
        self.client.post("/logout")

        # Restore the earlier signed session cookie before intent was consumed
        with self.client.session_transaction() as sess:
            sess["_pending_google_link"] = saved_pending

        # GET request detects intent is already consumed in SQLite
        resp_get = self.client.get("/auth/google/link", follow_redirects=True)
        self.assertIn(b"already been used", resp_get.data)

        # POST confirm with replayed cookie is rejected
        with self.client.session_transaction() as sess:
            sess["_pending_google_link"] = saved_pending

        resp_post = self.client.post(
            "/auth/google/link",
            data={"password": "ValidStorePass123!"},
            follow_redirects=True,
        )
        self.assertIn(b"already been used", resp_post.data)
        with self.client.session_transaction() as sess:
            self.assertIsNone(sess.get("user_id"))

    def test_server_side_database_intent_expiration_enforced(self):
        self._initiate_linking(sub="google_sub_db_expired")

        with self.client.session_transaction() as sess:
            intent_id = sess["_pending_google_link"]["intent_id"]
            # Keep client session cookie timestamp in the future (+3600)
            sess["_pending_google_link"]["expires_at"] = time.time() + 3600
            sess.modified = True

        # Directly expire the intent in the SQLite database
        with self.app.app_context():
            import sqlite3
            conn = self.user_repo.conn
            conn.execute(
                "UPDATE account_link_intents SET expires_at = '2020-01-01T00:00:00Z' WHERE intent_id = ?",
                (intent_id,),
            )
            conn.commit()

        # Server-side validation catches the expired intent despite client cookie timestamp
        resp_get = self.client.get("/auth/google/link", follow_redirects=True)
        self.assertIn(b"account linking request has expired", resp_get.data)

    def test_authenticated_user_accessing_link_endpoint_redirected(self):
        with self.client.session_transaction() as sess:
            sess["user_id"] = self.user.id

        resp = self.client.get("/auth/google/link", follow_redirects=False)
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp.headers.get("Location"), "/")

    def test_next_redirect_preserved_through_linking(self):
        # Start OAuth initiation with next=/cart
        resp_init = self.client.get("/auth/google?next=/cart")
        state = resp_init.headers.get("Location").split("state=")[1].split("&")[0]

        with patch.object(oauth.google, "authorize_access_token") as mock_token:
            mock_token.return_value = {
                "access_token": "tok_next",
                "userinfo": {
                    "sub": "sub_next_link",
                    "email": "localcustomer@example.com",
                    "email_verified": True,
                },
            }
            resp_cb = self.client.get(f"/auth/google/callback?state={state}", follow_redirects=False)
            self.assertEqual(resp_cb.status_code, 302)
            self.assertIn("/auth/google/link", resp_cb.headers.get("Location"))

        # Confirm linking
        resp_link = self.client.post(
            "/auth/google/link",
            data={"password": "ValidStorePass123!"},
            follow_redirects=False,
        )
        self.assertEqual(resp_link.status_code, 302)
        self.assertEqual(resp_link.headers.get("Location"), "/cart")


class TestGoogleAccountLinkingCsrf(unittest.TestCase):
    """Verify CSRF protection is strictly enforced on Google account linking POST."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_csrf_link.sqlite3"

        class CsrfActiveConfig(Config):
            DATABASE_PATH = str(self.db_path)
            TESTING = True
            SECRET_KEY = "csrf-secret-key-link"
            CSRF_ENABLED = True
            GOOGLE_CLIENT_ID = "mock-client-id"
            GOOGLE_CLIENT_SECRET = "mock-client-secret"

        self.app = create_app(CsrfActiveConfig)
        self.client = self.app.test_client()

        with self.app.app_context():
            init_db()
            self.user_repo = UserRepository()
            self.user = self.user_repo.create(
                User(
                    email="csrflink@example.com",
                    password_hash=generate_password_hash("ValidPass123!"),
                    role=UserRole.CUSTOMER,
                    is_verified=True,
                )
            )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_post_without_csrf_token_rejected_with_400(self):
        with self.client.session_transaction() as sess:
            sess["_pending_google_link"] = {
                "user_id": self.user.id,
                "email": "csrflink@example.com",
                "sub": "csrf_sub_123",
                "name": "CSRF Link",
                "next_url": None,
                "created_at": time.time(),
                "expires_at": time.time() + 600,
            }

        resp = self.client.post(
            "/auth/google/link",
            data={"password": "ValidPass123!"},
        )
        self.assertEqual(resp.status_code, 400)

    def test_cancel_post_without_csrf_token_rejected_with_400(self):
        with self.client.session_transaction() as sess:
            sess["_pending_google_link"] = {
                "intent_id": "test_intent_csrf",
                "user_id": self.user.id,
                "email": "csrflink@example.com",
                "sub": "csrf_sub_123",
                "expires_at": time.time() + 600,
            }

        resp = self.client.post("/auth/google/link/cancel")
        self.assertEqual(resp.status_code, 400)


if __name__ == "__main__":
    unittest.main()

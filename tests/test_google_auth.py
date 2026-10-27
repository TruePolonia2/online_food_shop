"""Tests for Google OAuth 2.0 / OpenID Connect authentication (Phase 10.9)."""
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import MagicMock, patch

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

    def test_case_c_email_collision_with_local_customer_strictly_rejected(self):
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

        # Malicious or accidental sign-in attempt with new unlinked Google sub
        with patch.object(oauth.google, "authorize_access_token") as mock_token:
            mock_token.return_value = {
                "access_token": "tok",
                "userinfo": {
                    "sub": "unlinked_new_google_sub",
                    "email": "collision@example.com",
                    "email_verified": True,
                },
            }
            resp = self.client.get("/auth/google/callback", follow_redirects=True)
            self.assertIn(
                b"An account with this email address already exists. Please log in using your password.",
                resp.data,
            )

        with self.client.session_transaction() as sess:
            self.assertIsNone(sess.get("user_id"))

        # Verify no external identity was auto-linked
        with self.app.app_context():
            identities = self.identity_repo.get_by_user_id(local_id)
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


if __name__ == "__main__":
    unittest.main()

"""Tests for role-based authorization decorators and access control primitives."""
from pathlib import Path
import tempfile
import unittest

from flask import jsonify

from app import create_app
from app.authorization import login_required, owner_required, role_required
from app.config import Config
from app.database.connection import init_db
from app.models.user import User, UserRole
from app.repositories.user_repository import UserRepository


class TestAuthorizationDecorators(unittest.TestCase):
    """Test suite for login_required, owner_required, and role_required decorators."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_authz.sqlite3"

        class TestAuthzConfig(Config):
            DATABASE_PATH = str(self.db_path)
            TESTING = True
            SECRET_KEY = "test-authz-secret-key"
            CSRF_ENABLED = False

        self.app = create_app(TestAuthzConfig)

        # Register test-only protected routes to avoid creating unnecessary production routes
        @self.app.route("/test-login-required")
        @login_required
        def view_login_required():
            return jsonify({"status": "authenticated"})

        @self.app.route("/api/test-login-required")
        @login_required
        def view_api_login_required():
            return jsonify({"status": "authenticated"})

        @self.app.route("/test-owner-required")
        @owner_required
        def view_owner_required():
            return jsonify({"status": "owner_access_granted"})

        @self.app.route("/api/test-owner-required")
        @owner_required
        def view_api_owner_required():
            return jsonify({"status": "owner_access_granted"})

        @self.app.route("/test-custom-role-required")
        @role_required(UserRole.CUSTOMER)
        def view_customer_only():
            return jsonify({"status": "customer_access_granted"})

        self.client = self.app.test_client()

        with self.app.app_context():
            init_db()
            self.user_repo = UserRepository()

            self.customer_user = self.user_repo.create(
                User(email="customer@example.com", role=UserRole.CUSTOMER, is_verified=True)
            )
            self.unverified_customer = self.user_repo.create(
                User(email="unverified_customer@example.com", role=UserRole.CUSTOMER, is_verified=False)
            )
            self.verified_owner = self.user_repo.create(
                User(email="owner@example.com", role=UserRole.OWNER, is_verified=True)
            )
            self.unverified_owner = self.user_repo.create(
                User(email="unverified_owner@example.com", role=UserRole.OWNER, is_verified=False)
            )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_anonymous_access_to_login_required_browser_redirects_with_safe_next(self) -> None:
        resp = self.client.get("/test-login-required?tab=details")
        self.assertEqual(resp.status_code, 302)
        location = resp.headers.get("Location", "")
        self.assertIn("/login", location)
        self.assertIn("next=", location)
        self.assertIn("/test-login-required", location)

    def test_anonymous_access_to_login_required_api_returns_401(self) -> None:
        # JSON endpoint via path prefix
        resp = self.client.get("/api/test-login-required")
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(resp.get_json(), {"error": "Authentication required."})

        # JSON endpoint via Accept header
        resp_accept = self.client.get("/test-login-required", headers={"Accept": "application/json"})
        self.assertEqual(resp_accept.status_code, 401)
        self.assertEqual(resp_accept.get_json(), {"error": "Authentication required."})

    def test_authenticated_customer_access_to_login_required_succeeds(self) -> None:
        with self.client.session_transaction() as sess:
            sess["user_id"] = self.customer_user.id

        resp = self.client.get("/test-login-required")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json(), {"status": "authenticated"})

    def test_authenticated_unverified_customer_access_to_login_required_succeeds(self) -> None:
        # Verification is not required for general login-required views
        with self.client.session_transaction() as sess:
            sess["user_id"] = self.unverified_customer.id

        resp = self.client.get("/test-login-required")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json(), {"status": "authenticated"})

    def test_anonymous_access_to_owner_required_browser_redirects_with_safe_next(self) -> None:
        resp = self.client.get("/test-owner-required")
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/login", resp.headers.get("Location", ""))

    def test_anonymous_access_to_owner_required_api_returns_401(self) -> None:
        resp = self.client.get("/api/test-owner-required")
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(resp.get_json(), {"error": "Authentication required."})

    def test_authenticated_customer_access_to_owner_required_returns_403(self) -> None:
        with self.client.session_transaction() as sess:
            sess["user_id"] = self.customer_user.id

        # Browser route returns 403 Forbidden
        resp = self.client.get("/test-owner-required")
        self.assertEqual(resp.status_code, 403)

        # API route returns 403 Forbidden JSON
        resp_api = self.client.get("/api/test-owner-required")
        self.assertEqual(resp_api.status_code, 403)
        self.assertIn("forbidden", resp_api.get_json()["error"].lower())

    def test_authenticated_verified_owner_access_to_owner_required_succeeds(self) -> None:
        with self.client.session_transaction() as sess:
            sess["user_id"] = self.verified_owner.id

        resp = self.client.get("/test-owner-required")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json(), {"status": "owner_access_granted"})

    def test_authenticated_unverified_owner_access_to_owner_required_returns_403(self) -> None:
        # Policy: owner access strictly requires a verified email address
        with self.client.session_transaction() as sess:
            sess["user_id"] = self.unverified_owner.id

        resp = self.client.get("/test-owner-required")
        self.assertEqual(resp.status_code, 403)

        resp_api = self.client.get("/api/test-owner-required")
        self.assertEqual(resp_api.status_code, 403)
        self.assertIn("verified email required", resp_api.get_json()["error"].lower())

    def test_forged_session_role_cannot_grant_owner_access(self) -> None:
        with self.client.session_transaction() as sess:
            sess["user_id"] = self.customer_user.id
            # Attacker attempts to forge session role
            sess["role"] = "owner"
            sess["is_verified"] = True

        resp = self.client.get("/test-owner-required")
        # Invariant: Database entity is authoritative, client-supplied session role is ignored
        self.assertEqual(resp.status_code, 403)

    def test_deleted_user_or_invalid_session_user_id_denies_access(self) -> None:
        with self.client.session_transaction() as sess:
            sess["user_id"] = 999999  # Nonexistent user

        resp = self.client.get("/test-login-required")
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/login", resp.headers.get("Location", ""))

        # Stale session user_id is automatically purged
        with self.client.session_transaction() as sess:
            self.assertNotIn("user_id", sess)

    def test_role_change_in_database_takes_immediate_effect(self) -> None:
        # User starts as customer and logs in
        with self.client.session_transaction() as sess:
            sess["user_id"] = self.customer_user.id

        # Accessing owner route is forbidden
        resp_before = self.client.get("/test-owner-required")
        self.assertEqual(resp_before.status_code, 403)

        # Database record is updated to owner
        with self.app.app_context():
            self.user_repo.set_role(self.customer_user.id, UserRole.OWNER)

        # Subsequent request in same session succeeds immediately without re-login
        resp_after = self.client.get("/test-owner-required")
        self.assertEqual(resp_after.status_code, 200)
        self.assertEqual(resp_after.get_json(), {"status": "owner_access_granted"})


if __name__ == "__main__":
    unittest.main()

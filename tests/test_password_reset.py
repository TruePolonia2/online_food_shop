"""Offline tests for Phase 10.10: Secure Password Reset and Account Recovery."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

from werkzeug.security import check_password_hash

from app import create_app
from app.config import Config
from app.csrf import SESSION_CSRF_KEY, generate_csrf_token
from app.database.connection import create_connection, get_db, init_db
from app.models.external_identity import ExternalIdentity
from app.models.password_reset_token import PasswordResetToken
from app.models.user import User, UserRole
from app.repositories.external_identity_repository import ExternalIdentityRepository
from app.repositories.password_reset_token_repository import PasswordResetTokenRepository
from app.repositories.user_repository import UserRepository
from app.routes.auth import (
    PasswordResetRateLimiter,
    build_absolute_reset_url,
    forgot_password_rate_limiter,
    login_rate_limiter,
)
from app.services.auth_service import AuthService
from app.services.email_sender import InMemoryEmailSender


class PasswordResetModelTestCase(unittest.TestCase):
    """Unit tests for the PasswordResetToken model."""

    def test_token_creation_and_attributes(self):
        now = datetime.now(timezone.utc)
        expires = now + timedelta(minutes=30)
        token = PasswordResetToken(
            id=1,
            user_id=42,
            token_hash="fakehash",
            created_at=now,
            expires_at=expires,
            used_at=None,
        )
        self.assertEqual(token.id, 1)
        self.assertEqual(token.user_id, 42)
        self.assertEqual(token.token_hash, "fakehash")
        self.assertFalse(token.is_used)
        self.assertFalse(token.is_expired)
        self.assertTrue(token.is_valid)

    def test_token_expired(self):
        now = datetime.now(timezone.utc)
        past = now - timedelta(minutes=5)
        token = PasswordResetToken(
            user_id=42,
            token_hash="fakehash",
            created_at=past - timedelta(minutes=30),
            expires_at=past,
        )
        self.assertTrue(token.is_expired)
        self.assertFalse(token.is_used)
        self.assertFalse(token.is_valid)

    def test_token_used(self):
        now = datetime.now(timezone.utc)
        token = PasswordResetToken(
            user_id=42,
            token_hash="fakehash",
            created_at=now - timedelta(minutes=10),
            expires_at=now + timedelta(minutes=20),
            used_at=now,
        )
        self.assertFalse(token.is_expired)
        self.assertTrue(token.is_used)
        self.assertFalse(token.is_valid)


class PasswordResetRepositoryTestCase(unittest.TestCase):
    """Integration tests for PasswordResetTokenRepository with SQLite."""

    def setUp(self):
        self.conn = create_connection(":memory:")
        init_db(self.conn)
        self.user_repo = UserRepository(self.conn)
        self.token_repo = PasswordResetTokenRepository(self.conn)

        user = User(
            email="test@example.com",
            password_hash="scrypt:fakehash",
            role=UserRole.CUSTOMER,
            is_verified=True,
        )
        self.user = self.user_repo.create(user)

    def tearDown(self):
        self.conn.close()

    def test_create_and_get_by_token_hash(self):
        now = datetime.now(timezone.utc)
        expires = now + timedelta(minutes=30)
        token = PasswordResetToken(
            user_id=self.user.id,
            token_hash="abc123hash",
            created_at=now,
            expires_at=expires,
        )
        persisted = self.token_repo.create(token)
        self.assertIsNotNone(persisted.id)
        self.assertEqual(persisted.user_id, self.user.id)
        self.assertEqual(persisted.token_hash, "abc123hash")

        retrieved = self.token_repo.get_by_token_hash("abc123hash")
        self.assertIsNotNone(retrieved)
        self.assertEqual(retrieved.id, persisted.id)
        self.assertEqual(retrieved.user_id, self.user.id)
        self.assertTrue(retrieved.is_valid)

    def test_mark_as_used_atomic(self):
        now = datetime.now(timezone.utc)
        token = self.token_repo.create(
            PasswordResetToken(
                user_id=self.user.id,
                token_hash="atomic_test_hash",
                created_at=now,
                expires_at=now + timedelta(minutes=30),
            )
        )
        # First call succeeds
        success_first = self.token_repo.mark_as_used(token.id, used_at=now)
        self.assertTrue(success_first)

        # Subsequent call on already used token returns False
        success_second = self.token_repo.mark_as_used(token.id, used_at=now)
        self.assertFalse(success_second)

        # Token is now used
        retrieved = self.token_repo.get_by_id(token.id)
        self.assertIsNotNone(retrieved)
        self.assertTrue(retrieved.is_used)
        self.assertFalse(retrieved.is_valid)

    def test_invalidate_pending_for_user(self):
        now = datetime.now(timezone.utc)
        t1 = self.token_repo.create(
            PasswordResetToken(
                user_id=self.user.id,
                token_hash="hash1",
                created_at=now,
                expires_at=now + timedelta(minutes=30),
            )
        )
        t2 = self.token_repo.create(
            PasswordResetToken(
                user_id=self.user.id,
                token_hash="hash2",
                created_at=now,
                expires_at=now + timedelta(minutes=30),
            )
        )
        # Mark t1 as already used
        self.token_repo.mark_as_used(t1.id, used_at=now)

        # Invalidate remaining pending tokens
        count = self.token_repo.invalidate_pending_for_user(self.user.id, invalidation_time=now)
        self.assertEqual(count, 1)  # only t2 was pending

        r2 = self.token_repo.get_by_id(t2.id)
        self.assertIsNotNone(r2)
        self.assertTrue(r2.is_used)
        self.assertFalse(r2.is_valid)

    def test_foreign_key_cascade_deletion(self):
        now = datetime.now(timezone.utc)
        token = self.token_repo.create(
            PasswordResetToken(
                user_id=self.user.id,
                token_hash="cascade_hash",
                created_at=now,
                expires_at=now + timedelta(minutes=30),
            )
        )
        self.assertIsNotNone(self.token_repo.get_by_id(token.id))

        # Delete user
        self.conn.execute("DELETE FROM users WHERE id = ?", (self.user.id,))
        self.conn.commit()

        # Reset token should be deleted via cascade
        self.assertIsNone(self.token_repo.get_by_id(token.id))


class PasswordResetAuthServiceTestCase(unittest.TestCase):
    """Service layer tests for password reset functionality."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_auth_service_pwd.sqlite3"
        self.conn = create_connection(str(self.db_path))
        init_db(self.conn)
        self.email_sender = InMemoryEmailSender()
        self.service = AuthService(conn=self.conn, email_sender=self.email_sender)
        self.user_repo = UserRepository(self.conn)
        self.external_identity_repo = ExternalIdentityRepository(self.conn)

        # Create standard local user
        self.user, _ = self.service.register_customer(
            email="local@example.com",
            password="InitialPassword123!",
            confirm_password="InitialPassword123!",
        )
        self.email_sender.clear()

    def tearDown(self):
        self.conn.close()
        self.temp_dir.cleanup()

    def test_request_password_reset_success(self):
        sent = self.service.request_password_reset(
            "local@example.com",
            reset_url_builder=lambda tok: f"https://example.com/reset/{tok}",
        )
        self.assertTrue(sent)
        pwd_messages = [
            m for m in self.email_sender.sent_messages if m.email_type == "password_reset"
        ]
        self.assertEqual(len(pwd_messages), 1)

        msg = pwd_messages[0]
        self.assertEqual(msg.to_email, "local@example.com")
        self.assertEqual(msg.email_type, "password_reset")
        self.assertTrue(msg.reset_url.startswith("https://example.com/reset/"))

    def test_request_password_reset_nonexistent_email(self):
        sent = self.service.request_password_reset("nonexistent@example.com")
        self.assertFalse(sent)
        pwd_messages = [
            m for m in self.email_sender.sent_messages if m.email_type == "password_reset"
        ]
        self.assertEqual(len(pwd_messages), 0)

    def test_request_password_reset_google_only_account(self):
        # Register a Google-only user with password_hash=None
        google_user, _ = self.service.authenticate_or_register_google_user(
            sub="google-sub-12345",
            email="googleonly@example.com",
            name="Google User",
        )
        self.assertIsNone(google_user.password_hash)

        sent = self.service.request_password_reset("googleonly@example.com")
        self.assertFalse(sent)
        pwd_messages = [
            m for m in self.email_sender.sent_messages if m.email_type == "password_reset"
        ]
        self.assertEqual(len(pwd_messages), 0)

    def test_request_password_reset_invalidates_prior_tokens(self):
        # First request
        self.service.request_password_reset("local@example.com")
        tok1 = [
            m for m in self.email_sender.sent_messages if m.email_type == "password_reset"
        ][0].raw_token

        # Second request
        self.service.request_password_reset("local@example.com")
        tok2 = [
            m for m in self.email_sender.sent_messages if m.email_type == "password_reset"
        ][1].raw_token

        self.assertNotEqual(tok1, tok2)

        # First token should now be invalid / consumed
        self.assertIsNone(self.service.get_valid_password_reset_token(tok1))
        # Second token is valid
        self.assertIsNotNone(self.service.get_valid_password_reset_token(tok2))

    def test_reset_password_success(self):
        self.service.request_password_reset("local@example.com")
        token = [
            m for m in self.email_sender.sent_messages if m.email_type == "password_reset"
        ][0].raw_token

        updated_user = self.service.reset_password(
            raw_token=token,
            new_password="NewSecurePassword456!",
            confirm_password="NewSecurePassword456!",
        )
        self.assertIsNotNone(updated_user)
        self.assertEqual(updated_user.id, self.user.id)
        self.assertTrue(check_password_hash(updated_user.password_hash, "NewSecurePassword456!"))

        # Old password rejected
        self.assertIsNone(self.service.authenticate("local@example.com", "InitialPassword123!"))
        # New password authenticated
        auth_user = self.service.authenticate("local@example.com", "NewSecurePassword456!")
        self.assertIsNotNone(auth_user)

        # Token cannot be reused (replay rejected)
        self.assertIsNone(
            self.service.reset_password(
                raw_token=token,
                new_password="AnotherPassword789!",
                confirm_password="AnotherPassword789!",
            )
        )

    def test_reset_password_validation_rules(self):
        self.service.request_password_reset("local@example.com")
        token = [
            m for m in self.email_sender.sent_messages if m.email_type == "password_reset"
        ][0].raw_token

        # Password too short (<8)
        with self.assertRaises(ValueError):
            self.service.reset_password(token, "short", "short")

        # Password mismatch
        with self.assertRaises(ValueError):
            self.service.reset_password(token, "ValidPass123!", "MismatchPass123!")

        # Password whitespace only
        with self.assertRaises(ValueError):
            self.service.reset_password(token, "        ", "        ")

        # Password exceeds 128 chars
        with self.assertRaises(ValueError):
            self.service.reset_password(token, "A" * 129, "A" * 129)

        # Token was NOT consumed by failed validations
        self.assertIsNotNone(self.service.get_valid_password_reset_token(token))

    def test_reset_password_fails_if_expired_or_invalid(self):
        self.assertIsNone(
            self.service.reset_password("invalid_token", "ValidPass123!", "ValidPass123!")
        )

    def test_reset_password_preserves_user_attributes(self):
        # Create an owner user
        owner_now = datetime.now(timezone.utc)
        owner = self.user_repo.create(
            User(
                email="owner@example.com",
                password_hash=self.service.hash_password("OwnerPassword123!"),
                role=UserRole.OWNER,
                is_verified=True,
                created_at=owner_now,
            )
        )
        self.service.request_password_reset("owner@example.com")
        token = [
            m for m in self.email_sender.sent_messages if m.email_type == "password_reset"
        ][0].raw_token

        updated_owner = self.service.reset_password(token, "NewOwnerPass123!", "NewOwnerPass123!")
        self.assertIsNotNone(updated_owner)
        self.assertEqual(updated_owner.role, UserRole.OWNER)
        self.assertTrue(updated_owner.is_verified)
        self.assertEqual(updated_owner.email, "owner@example.com")

    def test_concurrent_token_redemption(self):
        self.service.request_password_reset("local@example.com")
        token = [
            m for m in self.email_sender.sent_messages if m.email_type == "password_reset"
        ][0].raw_token

        results = []
        barrier = threading.Barrier(2)

        def worker(pwd):
            thread_conn = create_connection(str(self.db_path))
            try:
                thread_service = AuthService(conn=thread_conn)
                barrier.wait()
                res = thread_service.reset_password(token, pwd, pwd)
                results.append(res)
            finally:
                thread_conn.close()

        t1 = threading.Thread(target=worker, args=("ConcurrentPass1!",))
        t2 = threading.Thread(target=worker, args=("ConcurrentPass2!",))
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        successes = [r for r in results if r is not None]
        failures = [r for r in results if r is None]
        self.assertEqual(len(successes), 1)
        self.assertEqual(len(failures), 1)

    def test_reset_password_rollback_on_failed_password_update(self):
        self.service.request_password_reset("local@example.com")
        token = [
            m for m in self.email_sender.sent_messages if m.email_type == "password_reset"
        ][0].raw_token

        # Force UserRepository.set_password_hash to return False (simulating unexpected update failure)
        with patch.object(UserRepository, "set_password_hash", return_value=False):
            result = self.service.reset_password(
                raw_token=token,
                new_password="NewSecurePassword456!",
                confirm_password="NewSecurePassword456!",
            )
            self.assertIsNone(result)

        # Verify token consumption was completely rolled back in the database
        valid_token = self.service.get_valid_password_reset_token(token)
        self.assertIsNotNone(valid_token)
        self.assertFalse(valid_token.is_used)
        self.assertIsNone(valid_token.used_at)

        # Verify old password remains completely valid
        old_auth = self.service.authenticate("local@example.com", "InitialPassword123!")
        self.assertIsNotNone(old_auth)
        self.assertIsNone(self.service.authenticate("local@example.com", "NewSecurePassword456!"))

        # Verify token remains available for a legitimate retry
        retry_result = self.service.reset_password(
            raw_token=token,
            new_password="NewSecurePassword456!",
            confirm_password="NewSecurePassword456!",
        )
        self.assertIsNotNone(retry_result)
        self.assertIsNotNone(self.service.authenticate("local@example.com", "NewSecurePassword456!"))

    def test_reset_password_rollback_on_invalidation_exception(self):
        self.service.request_password_reset("local@example.com")
        token = [
            m for m in self.email_sender.sent_messages if m.email_type == "password_reset"
        ][0].raw_token

        # Force PasswordResetTokenRepository.invalidate_pending_for_user to raise an exception
        with patch.object(
            PasswordResetTokenRepository,
            "invalidate_pending_for_user",
            side_effect=sqlite3.OperationalError("disk I/O error"),
        ):
            with self.assertRaises(sqlite3.OperationalError):
                self.service.reset_password(
                    raw_token=token,
                    new_password="NewSecurePassword456!",
                    confirm_password="NewSecurePassword456!",
                )

        # Verify token consumption was rolled back
        valid_token = self.service.get_valid_password_reset_token(token)
        self.assertIsNotNone(valid_token)
        self.assertFalse(valid_token.is_used)

        # Verify old password remains valid
        self.assertIsNotNone(self.service.authenticate("local@example.com", "InitialPassword123!"))
        self.assertIsNone(self.service.authenticate("local@example.com", "NewSecurePassword456!"))


class PasswordResetRouteTestCase(unittest.TestCase):
    """Route, anti-enumeration, rate-limiting, and header tests."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_pwd_reset_routes.sqlite3"

        class TestRouteConfig(Config):
            DATABASE_PATH = str(self.db_path)
            TESTING = True
            SECRET_KEY = "test-pwd-reset-secret-key"
            CSRF_ENABLED = False
            APP_BASE_URL = "https://shop.example.com"

        self.app = create_app(TestRouteConfig)
        self.client = self.app.test_client()
        forgot_password_rate_limiter.clear()
        login_rate_limiter.clear()

        with self.app.app_context():
            init_db()
            auth_service = AuthService()
            self.user, _ = auth_service.register_customer(
                email="user@example.com",
                password="OldPassword123!",
                confirm_password="OldPassword123!",
            )

    def tearDown(self):
        self.temp_dir.cleanup()
        forgot_password_rate_limiter.clear()
        login_rate_limiter.clear()

    def request_reset_for(self, email: str) -> str:
        """Helper to trigger password reset in-app and return the dispatched raw token."""
        captured = []
        original_send = InMemoryEmailSender.send_password_reset_email

        def mock_send(sender_self, to_email, raw_token, reset_url=None):
            captured.append(raw_token)
            return original_send(sender_self, to_email, raw_token, reset_url)

        with patch.object(
            InMemoryEmailSender,
            "send_password_reset_email",
            side_effect=mock_send,
            autospec=True,
        ):
            with self.app.app_context():
                service = AuthService()
                service.request_password_reset(email, reset_url_builder=build_absolute_reset_url)

        return captured[0] if captured else ""

    def test_forgot_password_get_renders_form(self):
        for endpoint in ("/auth/forgot-password", "/forgot-password"):
            response = self.client.get(endpoint)
            self.assertEqual(response.status_code, 200)
            self.assertIn(b"Forgot Password", response.data)
            self.assertIn(b"Send Reset Link", response.data)

    def test_forgot_password_post_valid_email(self):
        with patch.object(
            InMemoryEmailSender, "send_password_reset_email", autospec=True
        ) as mock_send:
            mock_send.return_value = True
            response = self.client.post(
                "/auth/forgot-password",
                data={"email": "user@example.com"},
                follow_redirects=True,
            )
            self.assertEqual(response.status_code, 200)
            self.assertIn(b"If an eligible account exists", response.data)
            self.assertEqual(mock_send.call_count, 1)

            # Verify reset URL derives strictly from APP_BASE_URL
            call_args = mock_send.call_args
            to_email = call_args.kwargs.get("to_email") or (call_args.args[1] if len(call_args.args) > 1 else None)
            reset_url = call_args.kwargs.get("reset_url")
            self.assertEqual(to_email, "user@example.com")
            self.assertIsNotNone(reset_url)
            self.assertTrue(
                reset_url.startswith("https://shop.example.com/auth/reset-password/")
            )

        # Verify token created in database
        with self.app.app_context():
            token = PasswordResetTokenRepository().get_latest_for_user(self.user.id)
            self.assertIsNotNone(token)
            self.assertTrue(token.is_valid)

    def test_forgot_password_post_nonexistent_email_anti_enumeration(self):
        with patch.object(
            InMemoryEmailSender, "send_password_reset_email", autospec=True
        ) as mock_send:
            response = self.client.post(
                "/auth/forgot-password",
                data={"email": "nonexistent@example.com"},
                follow_redirects=True,
            )
            self.assertEqual(response.status_code, 200)
            # Identical public feedback
            self.assertIn(b"If an eligible account exists", response.data)
            self.assertEqual(mock_send.call_count, 0)

        with self.app.app_context():
            conn = get_db()
            cursor = conn.execute("SELECT COUNT(*) as c FROM password_reset_tokens")
            self.assertEqual(cursor.fetchone()["c"], 0)

    def test_forgot_password_post_google_user_anti_enumeration(self):
        # Create Google-only user
        with self.app.app_context():
            auth_service = AuthService()
            auth_service.authenticate_or_register_google_user(
                sub="google-sub-999",
                email="googleonly@example.com",
            )

        with patch.object(
            InMemoryEmailSender, "send_password_reset_email", autospec=True
        ) as mock_send:
            response = self.client.post(
                "/auth/forgot-password",
                data={"email": "googleonly@example.com"},
                follow_redirects=True,
            )
            self.assertEqual(response.status_code, 200)
            self.assertIn(b"If an eligible account exists", response.data)
            self.assertEqual(mock_send.call_count, 0)

    def test_forgot_password_rate_limiting(self):
        for _ in range(5):
            res = self.client.post("/auth/forgot-password", data={"email": "user@example.com"})
            self.assertEqual(res.status_code, 302)

        # 6th attempt is rate-limited (HTTP 429)
        res_blocked = self.client.post(
            "/auth/forgot-password", data={"email": "user@example.com"}
        )
        self.assertEqual(res_blocked.status_code, 429)
        self.assertIn(b"Too many password reset requests", res_blocked.data)

    def test_rate_limiting_single_ip_multiple_emails(self):
        # A single client IP (198.51.100.10) submits 15 requests for 15 distinct email addresses
        client_env = {"REMOTE_ADDR": "198.51.100.10"}
        for i in range(1, 16):
            res = self.client.post(
                "/auth/forgot-password",
                data={"email": f"victim{i}@example.com"},
                environ_overrides=client_env,
            )
            self.assertEqual(res.status_code, 302)

        # 16th request from the same IP for yet another email is rate-limited (HTTP 429)
        res_blocked = self.client.post(
            "/auth/forgot-password",
            data={"email": "victim16@example.com"},
            environ_overrides=client_env,
        )
        self.assertEqual(res_blocked.status_code, 429)
        self.assertIn(b"Too many password reset requests", res_blocked.data)

    def test_rate_limiting_multiple_ips_single_email(self):
        # 5 distinct IPs target the same normalized email
        target_email = "targeted_account@example.com"
        for i in range(1, 6):
            res = self.client.post(
                "/auth/forgot-password",
                data={"email": target_email},
                environ_overrides={"REMOTE_ADDR": f"203.0.113.{i}"},
            )
            self.assertEqual(res.status_code, 302)

        # 6th request from a completely new IP targeting the same email is rate-limited (HTTP 429)
        res_blocked = self.client.post(
            "/auth/forgot-password",
            data={"email": target_email},
            environ_overrides={"REMOTE_ADDR": "203.0.113.99"},
        )
        self.assertEqual(res_blocked.status_code, 429)
        self.assertIn(b"Too many password reset requests", res_blocked.data)

    def test_reset_password_get_valid_token_headers(self):
        raw_token = self.request_reset_for("user@example.com")

        for endpoint in (f"/auth/reset-password/{raw_token}", f"/reset-password/{raw_token}"):
            response = self.client.get(endpoint)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers.get("Referrer-Policy"), "no-referrer")
            self.assertEqual(response.headers.get("Cache-Control"), "no-store, private")
            self.assertIn(b"Set New Password", response.data)

    def test_reset_password_get_invalid_token(self):
        response = self.client.get("/auth/reset-password/invalid_token", follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn(
            b"The password reset link is invalid, expired, or has already been used",
            response.data,
        )

    def test_reset_password_post_success_and_login_flow(self):
        raw_token = self.request_reset_for("user@example.com")

        response = self.client.post(
            f"/auth/reset-password/{raw_token}",
            data={
                "password": "BrandNewPassword123!",
                "confirm_password": "BrandNewPassword123!",
            },
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Your password has been reset successfully", response.data)

        # Confirm user is NOT auto-logged in
        with self.client.session_transaction() as sess:
            self.assertIsNone(sess.get("user_id"))

        # User cannot log in with old password
        login_res_old = self.client.post(
            "/login",
            data={"email": "user@example.com", "password": "OldPassword123!"},
            follow_redirects=True,
        )
        self.assertIn(b"Invalid email or password", login_res_old.data)

        # User CAN log in with new password
        login_res_new = self.client.post(
            "/login",
            data={"email": "user@example.com", "password": "BrandNewPassword123!"},
            follow_redirects=True,
        )
        self.assertIn(b"Welcome back, user@example.com!", login_res_new.data)

    def test_reset_password_post_validation_error_preserves_token(self):
        raw_token = self.request_reset_for("user@example.com")

        # Password mismatch
        response = self.client.post(
            f"/auth/reset-password/{raw_token}",
            data={
                "password": "ValidPassword123!",
                "confirm_password": "MismatchPassword123!",
            },
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.headers.get("Referrer-Policy"), "no-referrer")
        self.assertEqual(response.headers.get("Cache-Control"), "no-store, private")
        self.assertIn(b"Passwords do not match", response.data)

        # Token still valid for retry
        with self.app.app_context():
            self.assertIsNotNone(AuthService().get_valid_password_reset_token(raw_token))

    def test_reset_password_post_replay_rejected(self):
        raw_token = self.request_reset_for("user@example.com")

        # First reset succeeds
        res1 = self.client.post(
            f"/auth/reset-password/{raw_token}",
            data={
                "password": "PasswordOne123!",
                "confirm_password": "PasswordOne123!",
            },
            follow_redirects=True,
        )
        self.assertIn(b"Your password has been reset successfully", res1.data)

        # Second reset with same token fails
        res2 = self.client.post(
            f"/auth/reset-password/{raw_token}",
            data={
                "password": "PasswordTwo123!",
                "confirm_password": "PasswordTwo123!",
            },
            follow_redirects=True,
        )
        self.assertIn(
            b"The password reset link is invalid, expired, or has already been used",
            res2.data,
        )

    def test_host_header_poisoning_mitigated(self):
        with self.app.test_request_context(
            "/auth/forgot-password",
            headers={"Host": "attacker-controlled-host.com"},
        ):
            url = build_absolute_reset_url("fake_raw_token")
            # Must strictly use configured APP_BASE_URL, never the untrusted Host header
            self.assertTrue(url.startswith("https://shop.example.com/auth/reset-password/"))
            self.assertNotIn("attacker-controlled-host.com", url)


class PasswordResetCsrfTestCase(unittest.TestCase):
    """CSRF protection enforcement tests for password reset endpoints."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_pwd_reset_csrf.sqlite3"

        class TestCsrfConfig(Config):
            DATABASE_PATH = str(self.db_path)
            TESTING = True
            SECRET_KEY = "test-pwd-reset-csrf-key"
            CSRF_ENABLED = True

        self.app = create_app(TestCsrfConfig)
        self.client = self.app.test_client()

        with self.app.app_context():
            init_db()

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_forgot_password_post_without_csrf_rejected(self):
        res = self.client.post("/auth/forgot-password", data={"email": "any@example.com"})
        self.assertEqual(res.status_code, 400)

    def test_reset_password_post_without_csrf_rejected(self):
        res = self.client.post(
            "/auth/reset-password/sometoken",
            data={"password": "Password123!", "confirm_password": "Password123!"},
        )
        self.assertEqual(res.status_code, 400)


class PasswordResetMigrationTestCase(unittest.TestCase):
    """Test idempotent schema migration of password_reset_tokens table."""

    def test_migration_on_pre_existing_database(self):
        temp_dir = tempfile.TemporaryDirectory()
        db_path = Path(temp_dir.name) / "migration_test.sqlite3"

        # Initialize base tables except password_reset_tokens
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA foreign_keys = ON;")
        conn.execute(
            """
            CREATE TABLE users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT NOT NULL UNIQUE COLLATE NOCASE,
                password_hash TEXT,
                role TEXT NOT NULL DEFAULT 'customer',
                is_verified INTEGER NOT NULL DEFAULT 0,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            """
        )
        conn.execute(
            "INSERT INTO users (email, password_hash, role, is_verified) VALUES (?, ?, ?, ?)",
            ("preexisting@example.com", "hash", "customer", 1),
        )
        conn.commit()
        conn.close()

        # Run init_db which includes schema migration
        conn = create_connection(str(db_path))
        init_db(conn)

        # Verify password_reset_tokens table exists
        cursor = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='password_reset_tokens'"
        )
        self.assertIsNotNone(cursor.fetchone())

        # Verify index exists
        cursor = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_password_reset_tokens_user_id'"
        )
        self.assertIsNotNone(cursor.fetchone())

        # Verify existing data preserved
        cursor = conn.execute("SELECT email FROM users WHERE email = 'preexisting@example.com'")
        row = cursor.fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["email"], "preexisting@example.com")

        # Running init_db again is idempotent and does not error
        init_db(conn)
        conn.close()
        temp_dir.cleanup()


class PasswordResetRateLimiterTestCase(unittest.TestCase):
    """Unit tests for the bounded PasswordResetRateLimiter."""

    def setUp(self):
        self.limiter = PasswordResetRateLimiter(
            max_per_ip_email=3,
            max_per_email=4,
            max_per_ip=6,
            window_seconds=10,
            max_keys=10,
        )

    def test_combo_limit(self):
        for _ in range(3):
            self.assertFalse(self.limiter.is_rate_limited("10.0.0.1", "user@test.com"))
            self.limiter.record_attempt("10.0.0.1", "user@test.com")
        self.assertTrue(self.limiter.is_rate_limited("10.0.0.1", "user@test.com"))

    def test_per_email_distributed_limit(self):
        for i in range(4):
            ip = f"10.0.0.{i+1}"
            self.assertFalse(self.limiter.is_rate_limited(ip, "shared@test.com"))
            self.limiter.record_attempt(ip, "shared@test.com")
        # 5th attempt from a new IP is blocked because email reached 4
        self.assertTrue(self.limiter.is_rate_limited("10.0.0.99", "shared@test.com"))

    def test_per_ip_aggregate_limit(self):
        ip = "10.0.0.50"
        for i in range(6):
            email = f"user{i}@test.com"
            self.assertFalse(self.limiter.is_rate_limited(ip, email))
            self.limiter.record_attempt(ip, email)
        # 7th attempt from same IP with different email is blocked
        self.assertTrue(self.limiter.is_rate_limited(ip, "another@test.com"))

    def test_reset_clears_account_and_combo_keys(self):
        self.limiter.record_attempt("10.0.0.1", "user@test.com")
        self.limiter.record_attempt("10.0.0.1", "user@test.com")
        self.limiter.record_attempt("10.0.0.1", "user@test.com")
        self.assertTrue(self.limiter.is_rate_limited("10.0.0.1", "user@test.com"))

        # Reset user
        self.limiter.reset("10.0.0.1", "user@test.com")
        self.assertFalse(self.limiter.is_rate_limited("10.0.0.1", "user@test.com"))


if __name__ == "__main__":
    unittest.main()

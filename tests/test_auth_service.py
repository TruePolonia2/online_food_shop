"""Unit and integration tests for AuthService, password security, and email verification."""
from datetime import datetime, timedelta, timezone
import hashlib
import sqlite3
import unittest
from unittest.mock import MagicMock

from app.database.connection import create_connection, init_db
from app.models.email_verification_token import EmailVerificationToken
from app.models.user import User, UserRole
from app.repositories.email_verification_token_repository import EmailVerificationTokenRepository
from app.repositories.user_repository import UserRepository
from app.services.auth_service import AuthService
from app.services.email_sender import InMemoryEmailSender, SentEmail


class BaseAuthServiceTestCase(unittest.TestCase):
    """Base fixture setting up an isolated in-memory SQLite database and AuthService."""

    def setUp(self) -> None:
        self.conn = create_connection(":memory:")
        init_db(self.conn)
        self.user_repo = UserRepository(self.conn)
        self.token_repo = EmailVerificationTokenRepository(self.conn)
        self.email_sender = InMemoryEmailSender()
        self.auth_service = AuthService(
            user_repo=self.user_repo,
            token_repo=self.token_repo,
            email_sender=self.email_sender,
            conn=self.conn,
            resend_cooldown_seconds=60,
        )

    def tearDown(self) -> None:
        self.conn.close()


class TestPasswordValidationAndHashing(unittest.TestCase):
    """Tests for password strength validation, scrypt hashing, and verification."""

    def test_validate_password_length(self) -> None:
        # Too short (< 8 chars)
        with self.assertRaises(ValueError) as ctx:
            AuthService.validate_password("1234567")
        self.assertIn("at least 8 characters", str(ctx.exception))

        # Minimum valid length (8 chars)
        AuthService.validate_password("12345678")

        # Too long (> 128 chars)
        with self.assertRaises(ValueError) as ctx:
            AuthService.validate_password("a" * 129)
        self.assertIn("must not exceed 128", str(ctx.exception))

        # Maximum valid length (128 chars)
        AuthService.validate_password("a" * 128)

    def test_validate_password_whitespace_and_type(self) -> None:
        # Solely whitespace
        with self.assertRaises(ValueError) as ctx:
            AuthService.validate_password("        ")
        self.assertIn("whitespace", str(ctx.exception))

        # Non-string input
        for bad_val in [12345678, None, [], {}]:
            with self.subTest(bad_val=bad_val):
                with self.assertRaises(ValueError):
                    AuthService.validate_password(bad_val)  # type: ignore[arg-type]

    def test_validate_password_confirmation_matching(self) -> None:
        # Matching confirmation
        AuthService.validate_password("ValidPassword123!", confirm_password="ValidPassword123!")

        # Mismatched confirmation
        with self.assertRaises(ValueError) as ctx:
            AuthService.validate_password("ValidPassword123!", confirm_password="DifferentPassword456!")
        self.assertIn("do not match", str(ctx.exception))

    def test_hash_password_and_verify_password(self) -> None:
        plaintext = "StrongPass123!"
        hashed = AuthService.hash_password(plaintext)

        # Scrypt prefix check
        self.assertTrue(hashed.startswith("scrypt:"))

        # Verification succeeds with matching password
        self.assertTrue(AuthService.verify_password(plaintext, hashed))

        # Verification fails with incorrect password
        self.assertFalse(AuthService.verify_password("WrongPass123!", hashed))

        # Verification fails with invalid inputs
        self.assertFalse(AuthService.verify_password("", hashed))
        self.assertFalse(AuthService.verify_password(plaintext, None))
        self.assertFalse(AuthService.verify_password(plaintext, ""))

        # Salting invariant: hashing same password twice produces distinct salt/hash strings
        hashed2 = AuthService.hash_password(plaintext)
        self.assertNotEqual(hashed, hashed2)
        self.assertTrue(AuthService.verify_password(plaintext, hashed2))


class TestCustomerRegistration(BaseAuthServiceTestCase):
    """Tests for customer registration, atomic database transactions, and verification token generation."""

    def test_register_customer_success(self) -> None:
        user, raw_token = self.auth_service.register_customer(
            email="newcustomer@example.com",
            password="SecurePassword2026!",
            confirm_password="SecurePassword2026!",
        )

        self.assertIsNotNone(user.id)
        self.assertEqual(user.email, "newcustomer@example.com")
        self.assertEqual(user.role, UserRole.CUSTOMER)
        self.assertFalse(user.is_verified)
        self.assertTrue(user.has_credentials)
        self.assertTrue(user.password_hash.startswith("scrypt:"))

        # Security invariant: Raw token is not equal to hash stored in DB
        expected_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
        token_in_db = self.token_repo.get_by_token_hash(expected_hash)
        self.assertIsNotNone(token_in_db)
        self.assertEqual(token_in_db.user_id, user.id)
        self.assertFalse(token_in_db.is_used)
        self.assertFalse(token_in_db.is_expired)

        # Token was dispatched via email sender
        self.assertEqual(len(self.email_sender.sent_messages), 1)
        sent = self.email_sender.sent_messages[0]
        self.assertEqual(sent.to_email, "newcustomer@example.com")
        self.assertEqual(sent.raw_token, raw_token)

    def test_register_customer_invalid_email_raises_value_error(self) -> None:
        invalid_emails = ["", "   ", "not-an-email", "@nodomain.com", "nouser@", None]
        for bad_email in invalid_emails:
            with self.subTest(email=bad_email):
                with self.assertRaises(ValueError):
                    self.auth_service.register_customer(
                        email=bad_email,  # type: ignore[arg-type]
                        password="ValidPassword123!",
                    )

    def test_register_customer_invalid_password_raises_value_error(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            self.auth_service.register_customer(
                email="valid@example.com",
                password="short",
            )
        self.assertIn("at least 8 characters", str(ctx.exception))

    def test_register_customer_duplicate_email_raises_value_error(self) -> None:
        self.auth_service.register_customer(
            email="existing@example.com",
            password="InitialPassword123!",
        )

        # Duplicate email registration attempt (case-insensitive)
        with self.assertRaises(ValueError) as ctx:
            self.auth_service.register_customer(
                email="EXISTING@EXAMPLE.COM",
                password="AnotherPassword123!",
            )
        self.assertIn("already exists", str(ctx.exception))

    def test_register_customer_email_delivery_failure_resilience(self) -> None:
        # Simulate an email service error
        self.email_sender.simulate_failure = True

        user, raw_token = self.auth_service.register_customer(
            email="resilience@example.com",
            password="ResilientPassword123!",
        )

        # User and token must be persisted despite email delivery failure
        self.assertIsNotNone(user.id)
        fetched_user = self.user_repo.get_by_id(user.id)
        self.assertIsNotNone(fetched_user)

        expected_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
        token_in_db = self.token_repo.get_by_token_hash(expected_hash)
        self.assertIsNotNone(token_in_db)

    def test_register_customer_token_failure_rolls_back_user(self) -> None:
        # Simulate token persistence failure (e.g. database error)
        with unittest.mock.patch.object(
            self.token_repo, "create", side_effect=sqlite3.OperationalError("Simulated token persistence failure")
        ):
            with self.assertRaises(sqlite3.OperationalError):
                self.auth_service.register_customer(
                    email="rollback_user@example.com",
                    password="ValidPassword123!",
                )

        # Invariant 1: Assert no partially created user remains in the database
        user_in_db = self.user_repo.get_by_email("rollback_user@example.com")
        self.assertIsNone(user_in_db)

        # Invariant 2: Assert no verification token remains
        count_tokens = self.conn.execute(
            "SELECT COUNT(*) as c FROM email_verification_tokens"
        ).fetchone()["c"]
        self.assertEqual(count_tokens, 0)

        # Invariant 3: Assert no verification email is sent
        self.assertEqual(len(self.email_sender.sent_messages), 0)


class TestAuthentication(BaseAuthServiceTestCase):
    """Tests for user credential verification and account lookup."""

    def setUp(self) -> None:
        super().setUp()
        self.registered_user, _ = self.auth_service.register_customer(
            email="auth_user@example.com",
            password="CorrectPassword123!",
        )

    def test_authenticate_success(self) -> None:
        user = self.auth_service.authenticate("auth_user@example.com", "CorrectPassword123!")
        self.assertIsNotNone(user)
        self.assertEqual(user.id, self.registered_user.id)
        self.assertEqual(user.email, "auth_user@example.com")

    def test_authenticate_case_insensitive_email(self) -> None:
        user = self.auth_service.authenticate("  AUTH_USER@EXAMPLE.COM  ", "CorrectPassword123!")
        self.assertIsNotNone(user)
        self.assertEqual(user.id, self.registered_user.id)

    def test_authenticate_wrong_password_returns_none(self) -> None:
        user = self.auth_service.authenticate("auth_user@example.com", "WrongPassword999!")
        self.assertIsNone(user)

    def test_authenticate_unknown_user_returns_none(self) -> None:
        user = self.auth_service.authenticate("nonexistent@example.com", "CorrectPassword123!")
        self.assertIsNone(user)

    def test_authenticate_empty_or_invalid_inputs_return_none(self) -> None:
        self.assertIsNone(self.auth_service.authenticate("", "pass"))
        self.assertIsNone(self.auth_service.authenticate("   ", "pass"))
        self.assertIsNone(self.auth_service.authenticate("auth_user@example.com", ""))
        self.assertIsNone(self.auth_service.authenticate(None, "pass"))  # type: ignore[arg-type]
        self.assertIsNone(self.auth_service.authenticate("auth_user@example.com", None))  # type: ignore[arg-type]

    def test_authenticate_user_without_credentials_returns_none(self) -> None:
        # Create an account without credentials (e.g. legacy row with NULL password_hash)
        legacy_user = self.user_repo.create(User(email="legacy@example.com", password_hash=None))
        self.assertIsNone(legacy_user.password_hash)

        # Attempting to authenticate with any password returns None
        self.assertIsNone(self.auth_service.authenticate("legacy@example.com", "any_password"))
        self.assertIsNone(self.auth_service.authenticate("legacy@example.com", ""))

    def test_authenticate_corrupted_hash_returns_none(self) -> None:
        # Create a user with a malformed / corrupted stored password hash
        user = self.user_repo.create(
            User(email="corrupted@example.com", password_hash="invalid_corrupted_hash_string")
        )
        self.assertIsNotNone(user.id)

        # Verify authentication fails safely without unhandled exception
        result = self.auth_service.authenticate("corrupted@example.com", "AnyPassword123!")
        self.assertIsNone(result)


class TestEmailVerification(BaseAuthServiceTestCase):
    """Tests for token verification, single-use enforcement, and race condition defense."""

    def test_verify_email_success(self) -> None:
        user, raw_token = self.auth_service.register_customer(
            email="verify_me@example.com",
            password="Password123!",
        )
        self.assertFalse(user.is_verified)

        verified_user = self.auth_service.verify_email(raw_token)
        self.assertIsNotNone(verified_user)
        self.assertTrue(verified_user.is_verified)

        # Database state updated
        fetched_user = self.user_repo.get_by_id(user.id)
        self.assertTrue(fetched_user.is_verified)

        # Token marked as used
        token_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
        token_in_db = self.token_repo.get_by_token_hash(token_hash)
        self.assertTrue(token_in_db.is_used)

    def test_verify_email_single_use_rejection(self) -> None:
        user, raw_token = self.auth_service.register_customer(
            email="single_use@example.com",
            password="Password123!",
        )

        first_verification = self.auth_service.verify_email(raw_token)
        self.assertIsNotNone(first_verification)

        # Re-submitting consumed token must fail
        second_verification = self.auth_service.verify_email(raw_token)
        self.assertIsNone(second_verification)

    def test_verify_email_expired_token_rejection(self) -> None:
        user = self.user_repo.create(User(email="expired@example.com"))
        raw_token = "expired_raw_token_value_12345"
        token_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()

        # Create token that expired 1 hour ago
        self.token_repo.create(
            EmailVerificationToken(
                user_id=user.id,
                token_hash=token_hash,
                created_at=datetime.now(timezone.utc) - timedelta(hours=25),
                expires_at=datetime.now(timezone.utc) - timedelta(hours=1),
            )
        )

        verified = self.auth_service.verify_email(raw_token)
        self.assertIsNone(verified)

        fetched_user = self.user_repo.get_by_id(user.id)
        self.assertFalse(fetched_user.is_verified)

    def test_verify_email_invalid_or_empty_token_returns_none(self) -> None:
        self.assertIsNone(self.auth_service.verify_email("completely_bogus_token"))
        self.assertIsNone(self.auth_service.verify_email(""))
        self.assertIsNone(self.auth_service.verify_email("   "))
        self.assertIsNone(self.auth_service.verify_email(None))  # type: ignore[arg-type]

    def test_verify_email_atomic_race_condition_defense(self) -> None:
        user, raw_token = self.auth_service.register_customer(
            email="race@example.com",
            password="Password123!",
        )

        # Mock token_repo.mark_as_used to simulate another concurrent request consuming token
        with unittest.mock.patch.object(self.token_repo, "mark_as_used", return_value=False):
            result = self.auth_service.verify_email(raw_token)
            self.assertIsNone(result)

        # User remains unverified since consumption failed
        fetched = self.user_repo.get_by_id(user.id)
        self.assertFalse(fetched.is_verified)


class TestVerificationResend(BaseAuthServiceTestCase):
    """Tests for requesting email verification resend, rate limiting cooldown, and token invalidation."""

    def test_request_verification_resend_cooldown_enforcement(self) -> None:
        user, _ = self.auth_service.register_customer(
            email="cooldown@example.com",
            password="Password123!",
        )

        # Immediate resend attempt triggers cooldown ValueError
        with self.assertRaises(ValueError) as ctx:
            self.auth_service.request_verification_resend("cooldown@example.com")
        self.assertIn("Please wait", str(ctx.exception))

    def test_request_verification_resend_after_cooldown_success(self) -> None:
        user, original_token = self.auth_service.register_customer(
            email="resend_ok@example.com",
            password="Password123!",
        )
        self.email_sender.clear()

        # Simulate that the initial token was created 120 seconds ago (cooldown expired)
        orig_hash = hashlib.sha256(original_token.encode("utf-8")).hexdigest()
        old_token = self.token_repo.get_by_token_hash(orig_hash)
        old_created = datetime.now(timezone.utc) - timedelta(seconds=120)
        self.conn.execute(
            "UPDATE email_verification_tokens SET created_at = ? WHERE id = ?",
            (old_created.strftime("%Y-%m-%dT%H:%M:%SZ"), old_token.id),
        )

        resend_result = self.auth_service.request_verification_resend("resend_ok@example.com")
        self.assertTrue(resend_result)

        # Check that old token was marked as used/invalidated
        updated_old = self.token_repo.get_by_token_hash(orig_hash)
        self.assertTrue(updated_old.is_used)

        # Check that a new token was dispatched via email
        self.assertEqual(len(self.email_sender.sent_messages), 1)
        new_sent = self.email_sender.sent_messages[0]
        self.assertEqual(new_sent.to_email, "resend_ok@example.com")
        self.assertNotEqual(new_sent.raw_token, original_token)

        # New token can be used to verify the user
        verified = self.auth_service.verify_email(new_sent.raw_token)
        self.assertIsNotNone(verified)
        self.assertTrue(verified.is_verified)

    def test_request_verification_resend_already_verified_returns_false(self) -> None:
        user, raw_token = self.auth_service.register_customer(
            email="already_verified@example.com",
            password="Password123!",
        )
        self.auth_service.verify_email(raw_token)
        self.email_sender.clear()

        resend_result = self.auth_service.request_verification_resend("already_verified@example.com")
        self.assertFalse(resend_result)
        self.assertEqual(len(self.email_sender.sent_messages), 0)

    def test_request_verification_resend_unknown_user_returns_false(self) -> None:
        resend_result = self.auth_service.request_verification_resend("nobody@example.com")
        self.assertFalse(resend_result)
        self.assertEqual(len(self.email_sender.sent_messages), 0)

    def test_request_verification_resend_invalid_inputs(self) -> None:
        self.assertFalse(self.auth_service.request_verification_resend(""))
        self.assertFalse(self.auth_service.request_verification_resend("   "))
        self.assertFalse(self.auth_service.request_verification_resend(None))  # type: ignore[arg-type]

    def test_request_verification_resend_concurrent_requests_cannot_both_succeed(self) -> None:
        import os
        import tempfile
        import threading

        # Setup temporary shared database file for concurrent threads
        temp_db = tempfile.NamedTemporaryFile(delete=False)
        temp_db.close()
        try:
            init_conn = create_connection(temp_db.name)
            init_db(init_conn)
            u_repo = UserRepository(init_conn)
            u_repo.create(User(email="concurrent_resend@example.com"))
            init_conn.close()

            results: list[tuple[int, str]] = []
            exceptions: list[tuple[int, str]] = []
            shared_sender = InMemoryEmailSender()

            def resend_task(tid: int) -> None:
                thread_conn = create_connection(temp_db.name)
                service = AuthService(
                    user_repo=UserRepository(thread_conn),
                    token_repo=EmailVerificationTokenRepository(thread_conn),
                    email_sender=shared_sender,
                    conn=thread_conn,
                    resend_cooldown_seconds=60,
                )
                try:
                    success = service.request_verification_resend("concurrent_resend@example.com")
                    if success:
                        results.append((tid, "success"))
                except ValueError as ve:
                    results.append((tid, f"cooldown: {ve}"))
                except Exception as ex:
                    exceptions.append((tid, f"error: {type(ex).__name__}: {ex}"))
                finally:
                    thread_conn.close()

            t1 = threading.Thread(target=resend_task, args=(1,))
            t2 = threading.Thread(target=resend_task, args=(2,))
            t1.start()
            t2.start()
            t1.join()
            t2.join()

            self.assertEqual(exceptions, [])
            successes = [r for r in results if r[1] == "success"]
            rejections = [r for r in results if "cooldown" in r[1]]
            self.assertEqual(len(successes), 1)
            self.assertEqual(len(rejections), 1)
            self.assertEqual(len(shared_sender.sent_messages), 1)
        finally:
            try:
                os.remove(temp_db.name)
            except OSError:
                pass


class TestInMemoryEmailSender(unittest.TestCase):
    """Tests for InMemoryEmailSender behavior and failure simulation."""

    def test_sender_records_and_clears_messages(self) -> None:
        sender = InMemoryEmailSender()
        self.assertIsNone(sender.last_email)

        success = sender.send_verification_email("alice@example.com", "token_abc")
        self.assertTrue(success)
        self.assertEqual(len(sender.sent_messages), 1)
        self.assertIsNotNone(sender.last_email)
        self.assertEqual(sender.last_email.to_email, "alice@example.com")
        self.assertEqual(sender.last_email.raw_token, "token_abc")

        sender.clear()
        self.assertEqual(len(sender.sent_messages), 0)
        self.assertIsNone(sender.last_email)

    def test_sender_simulate_failure(self) -> None:
        sender = InMemoryEmailSender(simulate_failure=True)
        success = sender.send_verification_email("bob@example.com", "token_xyz")
        self.assertFalse(success)
        self.assertEqual(len(sender.sent_messages), 0)


class TestAuthServiceGoogleUser(BaseAuthServiceTestCase):
    """Tests for AuthService.authenticate_or_register_google_user."""

    def test_case_b_register_new_google_user(self):
        user, err = self.auth_service.authenticate_or_register_google_user(
            sub="sub_100", email="new_google@example.com", name="Google User"
        )
        self.assertIsNone(err)
        self.assertIsNotNone(user)
        self.assertEqual(user.email, "new_google@example.com")
        self.assertEqual(user.role, UserRole.CUSTOMER)
        self.assertTrue(user.is_verified)
        self.assertIsNone(user.password_hash)

        # External identity link exists
        identities = self.auth_service.external_identity_repo.get_by_user_id(user.id)
        self.assertEqual(len(identities), 1)
        self.assertEqual(identities[0].provider_user_id, "sub_100")

    def test_case_a_authenticate_existing_google_user(self):
        created_user, _ = self.auth_service.authenticate_or_register_google_user(
            sub="sub_200", email="linked@example.com"
        )
        user, err = self.auth_service.authenticate_or_register_google_user(
            sub="sub_200", email="linked@example.com"
        )
        self.assertIsNone(err)
        self.assertEqual(user.id, created_user.id)

    def test_case_c_collision_with_existing_local_user(self):
        # Register customer locally with password
        local_user, _ = self.auth_service.register_customer(
            email="collision@example.com", password="Password123!", confirm_password="Password123!"
        )

        user, err = self.auth_service.authenticate_or_register_google_user(
            sub="different_sub_300", email="collision@example.com"
        )
        self.assertIsNone(user)
        self.assertEqual(err, "collision")

    def test_case_d_linked_user_with_changed_email_maintains_local_email(self):
        created_user, _ = self.auth_service.authenticate_or_register_google_user(
            sub="sub_400", email="old_email@example.com"
        )
        # Google provides new email for same sub
        user, err = self.auth_service.authenticate_or_register_google_user(
            sub="sub_400", email="new_email@example.com"
        )
        self.assertIsNone(err)
        self.assertEqual(user.id, created_user.id)
        # Verify local email in DB was NOT changed
        reloaded = self.user_repo.get_by_id(created_user.id)
        self.assertEqual(reloaded.email, "old_email@example.com")

    def test_invalid_sub_and_email_rejected(self):
        for invalid_sub in [None, "", "   ", True, False]:
            user, err = self.auth_service.authenticate_or_register_google_user(
                sub=invalid_sub, email="test@example.com"
            )
            self.assertIsNone(user)
            self.assertEqual(err, "invalid_sub")

        for invalid_email in [None, "", "   ", "notanemail", 123]:
            user, err = self.auth_service.authenticate_or_register_google_user(
                sub="sub_valid", email=invalid_email
            )
            self.assertIsNone(user)
            self.assertEqual(err, "invalid_email")


if __name__ == "__main__":
    unittest.main()

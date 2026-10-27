"""Tests for Flask CLI commands, specifically secure owner account bootstrap."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from app import create_app
from app.config import Config
from app.database.connection import init_db
from app.models.user import User, UserRole
from app.repositories.user_repository import UserRepository
from app.services.auth_service import AuthService


class TestCreateOwnerCLI(unittest.TestCase):
    """Test suite for 'flask create-owner' command."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_cli.sqlite3"

        class TestCliConfig(Config):
            DATABASE_PATH = str(self.db_path)
            TESTING = True
            SECRET_KEY = "test-cli-secret-key"
            CSRF_ENABLED = False

        self.app = create_app(TestCliConfig)
        self.runner = self.app.test_cli_runner()

        with self.app.app_context():
            init_db()
            self.user_repo = UserRepository()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_create_owner_success(self) -> None:
        result = self.runner.invoke(
            args=["create-owner", "--email", "boss@example.com"],
            input="OwnerSecret123!\nOwnerSecret123!\n",
        )
        self.assertEqual(result.exit_code, 0)
        self.assertIn("Store owner successfully created: boss@example.com", result.output)

        with self.app.app_context():
            owner = self.user_repo.get_by_email("boss@example.com")
            self.assertIsNotNone(owner)
            self.assertEqual(owner.role, UserRole.OWNER)
            self.assertTrue(owner.is_verified)
            self.assertTrue(owner.has_credentials)

            # Stored as secure Werkzeug scrypt hash
            self.assertTrue(owner.password_hash.startswith("scrypt:"))
            # Password can be authenticated
            auth_service = AuthService()
            authenticated_user = auth_service.authenticate("boss@example.com", "OwnerSecret123!")
            self.assertIsNotNone(authenticated_user)
            self.assertEqual(authenticated_user.id, owner.id)

    def test_create_owner_prompts_for_email_if_omitted(self) -> None:
        result = self.runner.invoke(
            args=["create-owner"],
            input="prompted_owner@example.com\nOwnerSecret123!\nOwnerSecret123!\n",
        )
        self.assertEqual(result.exit_code, 0)
        self.assertIn("Store owner successfully created: prompted_owner@example.com", result.output)

        with self.app.app_context():
            owner = self.user_repo.get_by_email("prompted_owner@example.com")
            self.assertIsNotNone(owner)
            self.assertEqual(owner.role, UserRole.OWNER)

    def test_create_owner_password_confirmation_mismatch(self) -> None:
        result = self.runner.invoke(
            args=["create-owner", "--email", "mismatch@example.com"],
            input="Password123!\nDifferentPassword456!\n",
        )
        # Click's confirmation_prompt triggers an error when confirmation fails
        self.assertNotEqual(result.exit_code, 0)

        with self.app.app_context():
            user = self.user_repo.get_by_email("mismatch@example.com")
            self.assertIsNone(user)

    def test_create_owner_weak_password_rejected(self) -> None:
        result = self.runner.invoke(
            args=["create-owner", "--email", "weak@example.com"],
            input="short\nshort\n",
        )
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("at least 8 characters", result.output.lower())

        with self.app.app_context():
            user = self.user_repo.get_by_email("weak@example.com")
            self.assertIsNone(user)

    def test_create_owner_duplicate_email_rejected(self) -> None:
        # First creation succeeds
        res1 = self.runner.invoke(
            args=["create-owner", "--email", "first_owner@example.com"],
            input="OwnerSecret123!\nOwnerSecret123!\n",
        )
        self.assertEqual(res1.exit_code, 0)

        # Second creation with identical email is rejected
        res2 = self.runner.invoke(
            args=["create-owner", "--email", "first_owner@example.com"],
            input="OwnerSecret123!\nOwnerSecret123!\n",
        )
        self.assertNotEqual(res2.exit_code, 0)
        self.assertIn("already exists", res2.output.lower())

    def test_create_owner_existing_customer_cannot_be_silently_promoted(self) -> None:
        # Create an existing customer account
        with self.app.app_context():
            customer = self.user_repo.create(
                User(email="regular_customer@example.com", role=UserRole.CUSTOMER, is_verified=False)
            )
            self.assertEqual(customer.role, UserRole.CUSTOMER)

        # Attempt to run create-owner with customer's email
        result = self.runner.invoke(
            args=["create-owner", "--email", "regular_customer@example.com"],
            input="OwnerSecret123!\nOwnerSecret123!\n",
        )
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("already exists", result.output.lower())
        self.assertIn("cannot be automatically promoted", result.output.lower())

        # Invariant: Customer role remains strictly CUSTOMER
        with self.app.app_context():
            customer_after = self.user_repo.get_by_id(customer.id)
            self.assertEqual(customer_after.role, UserRole.CUSTOMER)
            self.assertFalse(customer_after.is_verified)

    def test_create_owner_output_never_prints_password_or_hash(self) -> None:
        secret_password = "VerySecretOwnerPass123!"
        result = self.runner.invoke(
            args=["create-owner", "--email", "secure_logs@example.com"],
            input=f"{secret_password}\n{secret_password}\n",
        )
        self.assertEqual(result.exit_code, 0)

        # Assert plaintext password and scrypt hash are never exposed in CLI output
        self.assertNotIn(secret_password, result.output)
        self.assertNotIn("scrypt:", result.output)

    def test_create_owner_does_not_accept_password_cli_argument(self) -> None:
        # Command line must reject --password argument to prevent shell history leakage
        result = self.runner.invoke(
            args=["create-owner", "--email", "hacker@example.com", "--password", "Secret123!"],
        )
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("no such option", result.output.lower())
        self.assertIn("--password", result.output.lower())

    def test_create_owner_invalid_email_rejected(self) -> None:
        result = self.runner.invoke(
            args=["create-owner", "--email", "invalid_email_no_domain"],
            input="OwnerSecret123!\nOwnerSecret123!\n",
        )
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("valid user email", result.output.lower())

    def test_create_owner_database_rollback_on_failure(self) -> None:
        with patch.object(UserRepository, "create", side_effect=RuntimeError("Simulated DB Crash")):
            result = self.runner.invoke(
                args=["create-owner", "--email", "failed_db@example.com"],
                input="OwnerSecret123!\nOwnerSecret123!\n",
            )
            self.assertNotEqual(result.exit_code, 0)
            self.assertIn("database failure", result.output.lower())

        with self.app.app_context():
            self.assertIsNone(self.user_repo.get_by_email("failed_db@example.com"))

    def test_public_registration_cannot_create_owner_account(self) -> None:
        # Even with CLI create-owner available, public HTTP /register strictly ignores role="owner"
        client = self.app.test_client()
        resp = client.post(
            "/register",
            data={
                "email": "sneaky_owner@example.com",
                "password": "Password123!",
                "confirm_password": "Password123!",
                "role": "owner",
                "is_verified": "1",
            },
            follow_redirects=False,
        )
        self.assertEqual(resp.status_code, 302)

        with self.app.app_context():
            user = self.user_repo.get_by_email("sneaky_owner@example.com")
            self.assertIsNotNone(user)
            self.assertEqual(user.role, UserRole.CUSTOMER)
            self.assertFalse(user.is_verified)


if __name__ == "__main__":
    unittest.main()

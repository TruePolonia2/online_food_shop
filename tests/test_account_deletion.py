"""Comprehensive test suite for customer self-service account deletion (Phase 10.11.4)."""
from datetime import datetime, timedelta, timezone
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
from app.models.account_link_intent import AccountLinkIntent
from app.models.category import Category
from app.models.customer import Customer
from app.models.email_verification_token import EmailVerificationToken
from app.models.external_identity import ExternalIdentity
from app.models.order import Order, OrderStatus
from app.models.order_item import OrderItem
from app.models.password_reset_token import PasswordResetToken
from app.models.product import Product
from app.models.user import User, UserRole
from app.oauth import oauth
from app.repositories.account_link_intent_repository import AccountLinkIntentRepository
from app.repositories.category_repository import CategoryRepository
from app.repositories.customer_repository import CustomerRepository
from app.repositories.email_verification_token_repository import EmailVerificationTokenRepository
from app.repositories.external_identity_repository import ExternalIdentityRepository
from app.repositories.order_repository import OrderRepository
from app.repositories.password_reset_token_repository import PasswordResetTokenRepository
from app.repositories.product_repository import ProductRepository
from app.repositories.user_repository import UserRepository
from app.routes.account import SESSION_REAUTH_DELETE_KEY, delete_account_rate_limiter
from app.services.auth_service import AuthService


class BaseAccountDeletionTestCase(unittest.TestCase):
    """Base test case providing isolated temporary database and configured app."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_account_deletion.sqlite3"

        class TestDeletionConfig(Config):
            DATABASE_PATH = str(self.db_path)
            TESTING = True
            SECRET_KEY = "test-account-deletion-secret-key"
            CSRF_ENABLED = False
            GOOGLE_CLIENT_ID = "mock-google-client-id"
            GOOGLE_CLIENT_SECRET = "mock-google-client-secret"

        self.config_cls = TestDeletionConfig
        self.app = create_app(TestDeletionConfig)
        self.client = self.app.test_client()

        with self.app.app_context():
            init_db()
            self.user_repo = UserRepository()
            self.customer_repo = CustomerRepository()
            self.order_repo = OrderRepository()
            self.identity_repo = ExternalIdentityRepository()
            self.category_repo = CategoryRepository()
            self.product_repo = ProductRepository()
            self.token_repo = EmailVerificationTokenRepository()
            self.pwd_reset_repo = PasswordResetTokenRepository()
            self.link_intent_repo = AccountLinkIntentRepository()

        delete_account_rate_limiter.clear()

    def tearDown(self) -> None:
        delete_account_rate_limiter.clear()
        self.temp_dir.cleanup()


class TestAuthServiceAccountDeletion(BaseAccountDeletionTestCase):
    """Unit tests for AuthService.delete_customer_account."""

    def test_delete_customer_account_invalid_id(self) -> None:
        with self.app.app_context():
            auth_service = AuthService()
            # bool is an instance of int in Python, should be rejected
            success, err = auth_service.delete_customer_account(True)  # type: ignore[arg-type]
            self.assertFalse(success)
            self.assertEqual(err, "invalid_user_id")

            success, err = auth_service.delete_customer_account(0)
            self.assertFalse(success)
            self.assertEqual(err, "invalid_user_id")

            success, err = auth_service.delete_customer_account(-5)
            self.assertFalse(success)
            self.assertEqual(err, "invalid_user_id")

    def test_delete_customer_account_nonexistent_user(self) -> None:
        with self.app.app_context():
            auth_service = AuthService()
            success, err = auth_service.delete_customer_account(9999)
            self.assertFalse(success)
            self.assertEqual(err, "user_not_found")

    def test_delete_customer_account_owner_forbidden(self) -> None:
        with self.app.app_context():
            owner = self.user_repo.create(
                User(
                    email="store_owner@example.com",
                    password_hash=AuthService.hash_password("OwnerPassword123!"),
                    role=UserRole.OWNER,
                    is_verified=True,
                )
            )
            auth_service = AuthService()
            success, err = auth_service.delete_customer_account(owner.id)
            self.assertFalse(success)
            self.assertEqual(err, "owner_forbidden")

            # Verify owner still exists
            self.assertIsNotNone(self.user_repo.get_by_id(owner.id))

    def test_delete_customer_without_orders_deletes_cleanly(self) -> None:
        with self.app.app_context():
            user = self.user_repo.create(
                User(
                    email="customer_no_orders@example.com",
                    password_hash=AuthService.hash_password("Password123!"),
                    role=UserRole.CUSTOMER,
                    is_verified=True,
                )
            )
            # Create a customer profile without orders
            customer = self.customer_repo.create(
                Customer(
                    name="Jane Doe",
                    email="customer_no_orders@example.com",
                    phone="1234567890",
                    address="123 Elm St",
                )
            )
            self.user_repo.conn.execute(
                "UPDATE customers SET user_id = ? WHERE id = ?", (user.id, customer.id)
            )

            auth_service = AuthService()
            success, err = auth_service.delete_customer_account(user.id)
            self.assertTrue(success)
            self.assertIsNone(err)

            # User should be deleted
            self.assertIsNone(self.user_repo.get_by_id(user.id))
            # Customer row should be deleted since there were no orders
            self.assertIsNone(self.customer_repo.get_by_id(customer.id))

            # Foreign key check must pass
            fk_cursor = self.user_repo.conn.execute("PRAGMA foreign_key_check;")
            self.assertEqual(len(fk_cursor.fetchall()), 0)

            # Email can be reused immediately for a new account
            new_user, _ = auth_service.register_customer(
                "customer_no_orders@example.com", "NewPassword123!", "NewPassword123!"
            )
            self.assertIsNotNone(new_user)
            self.assertEqual(new_user.email, "customer_no_orders@example.com")

    def test_delete_customer_with_orders_anonymizes_data_and_preserves_financials(self) -> None:
        with self.app.app_context():
            user = self.user_repo.create(
                User(
                    email="buyer@example.com",
                    password_hash=AuthService.hash_password("Password123!"),
                    role=UserRole.CUSTOMER,
                    is_verified=True,
                )
            )
            customer = self.customer_repo.create(
                Customer(
                    name="Alice Buyer",
                    email="buyer@example.com",
                    phone="555-0100",
                    address="456 Oak Avenue",
                )
            )
            self.user_repo.conn.execute(
                "UPDATE customers SET user_id = ? WHERE id = ?", (user.id, customer.id)
            )

            # Create product and order
            cat = self.category_repo.create(Category(name="Bakery"))
            prod = self.product_repo.create(
                Product(name="Sourdough Bread", price=450, stock_quantity=10, category_id=cat.id)
            )

            order = self.order_repo.create(
                Order(
                    customer_id=customer.id,
                    user_id=user.id,
                    status=OrderStatus.PAID,
                    customer_name="Alice Buyer",
                    customer_email="buyer@example.com",
                    customer_phone="555-0100",
                    customer_address="456 Oak Avenue",
                    items=[
                        OrderItem(
                            order_id=0,
                            product_id=prod.id,
                            product_name="Sourdough Bread",
                            unit_price=450,
                            quantity=2,
                        )
                    ],
                )
            )

            auth_service = AuthService()
            success, err = auth_service.delete_customer_account(user.id)
            self.assertTrue(success)
            self.assertIsNone(err)

            # User account is deleted
            self.assertIsNone(self.user_repo.get_by_id(user.id))

            # Foreign keys must remain completely intact
            fk_cursor = self.user_repo.conn.execute("PRAGMA foreign_key_check;")
            violations = fk_cursor.fetchall()
            self.assertEqual(len(violations), 0, f"Foreign key check failed: {violations}")

            # Order is retained for accounting
            saved_order = self.order_repo.get_by_id(order.id)
            self.assertIsNotNone(saved_order)
            self.assertIsNone(saved_order.user_id)
            self.assertEqual(saved_order.customer_name, "Deleted Customer")
            self.assertEqual(saved_order.customer_email, "deleted@deleted.invalid")
            self.assertIsNone(saved_order.customer_phone)
            self.assertEqual(saved_order.customer_address, "Redacted")
            self.assertEqual(saved_order.status, OrderStatus.PAID)
            self.assertEqual(len(saved_order.items), 1)
            self.assertEqual(saved_order.total, 900)

            # Customer row is anonymized due to ON DELETE RESTRICT on orders
            saved_cust = self.customer_repo.get_by_id(customer.id)
            self.assertIsNotNone(saved_cust)
            self.assertEqual(saved_cust.name, "Deleted Customer")
            self.assertTrue(saved_cust.email.startswith("deleted_"))
            self.assertTrue(saved_cust.email.endswith("@deleted.invalid"))
            self.assertIsNone(saved_cust.phone)
            self.assertIsNone(saved_cust.address)

            # Original email is freed in both users and customers
            new_user, _ = auth_service.register_customer(
                "buyer@example.com", "BrandNew123!", "BrandNew123!"
            )
            self.assertIsNotNone(new_user)
            self.assertEqual(new_user.email, "buyer@example.com")

            # New order can be placed with freed email
            new_cust = self.customer_repo.create(
                Customer(name="Alice New", email="buyer@example.com")
            )
            self.assertIsNotNone(new_cust.id)

    def test_delete_customer_cascades_tokens_and_identities(self) -> None:
        with self.app.app_context():
            user = self.user_repo.create(
                User(
                    email="hybrid_user@example.com",
                    password_hash=AuthService.hash_password("Password123!"),
                    role=UserRole.CUSTOMER,
                    is_verified=True,
                )
            )

            # Add external identity
            self.identity_repo.create(
                ExternalIdentity(
                    user_id=user.id,
                    provider="google",
                    provider_user_id="google-sub-9999",
                    email="hybrid_user@example.com",
                )
            )

            # Add email verification token
            self.token_repo.create(
                EmailVerificationToken(
                    user_id=user.id,
                    token_hash="fake_hash_verification",
                    expires_at=datetime.now(timezone.utc),
                )
            )

            # Add password reset token
            self.pwd_reset_repo.create(
                PasswordResetToken(
                    user_id=user.id,
                    token_hash="fake_hash_reset",
                    expires_at=datetime.now(timezone.utc),
                )
            )

            # Add account link intent
            self.link_intent_repo.create(
                AccountLinkIntent(
                    intent_id="fake_intent_id",
                    user_id=user.id,
                    provider="google",
                    provider_user_id="google-sub-9999",
                    email="hybrid_user@example.com",
                    expires_at=datetime.now(timezone.utc),
                )
            )

            auth_service = AuthService()
            success, err = auth_service.delete_customer_account(user.id)
            self.assertTrue(success)

            # Verify cascading deletions
            self.assertIsNone(self.user_repo.get_by_id(user.id))
            self.assertIsNone(
                self.identity_repo.get_by_user_and_provider(user.id, "google")
            )
            self.assertIsNone(
                self.token_repo.get_by_token_hash("fake_hash_verification")
            )
            self.assertIsNone(
                self.pwd_reset_repo.get_by_token_hash("fake_hash_reset")
            )
            self.assertIsNone(
                self.link_intent_repo.get_by_intent_id("fake_intent_id")
            )


class TestAccountDeletionRoutes(BaseAccountDeletionTestCase):
    """Integration tests for /account/delete routes and authorization."""

    def test_delete_page_requires_authentication(self) -> None:
        resp = self.client.get("/account/delete")
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/login", resp.headers["Location"])

    def test_delete_post_requires_authentication(self) -> None:
        resp = self.client.post("/account/delete", data={"password": "Password123!"})
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/login", resp.headers["Location"])

    def test_delete_page_owner_forbidden(self) -> None:
        with self.app.app_context():
            owner = self.user_repo.create(
                User(
                    email="owner@example.com",
                    password_hash=AuthService.hash_password("OwnerPassword123!"),
                    role=UserRole.OWNER,
                    is_verified=True,
                )
            )

        with self.client.session_transaction() as sess:
            sess["user_id"] = owner.id

        resp = self.client.get("/account/delete")
        self.assertEqual(resp.status_code, 403)

    def test_delete_post_owner_forbidden(self) -> None:
        with self.app.app_context():
            owner = self.user_repo.create(
                User(
                    email="owner@example.com",
                    password_hash=AuthService.hash_password("OwnerPassword123!"),
                    role=UserRole.OWNER,
                    is_verified=True,
                )
            )

        with self.client.session_transaction() as sess:
            sess["user_id"] = owner.id

        resp = self.client.post("/account/delete", data={"password": "OwnerPassword123!"})
        self.assertEqual(resp.status_code, 403)

    def test_delete_page_rendered_for_password_customer(self) -> None:
        with self.app.app_context():
            user = self.user_repo.create(
                User(
                    email="customer@example.com",
                    password_hash=AuthService.hash_password("Password123!"),
                    role=UserRole.CUSTOMER,
                    is_verified=True,
                )
            )

        with self.client.session_transaction() as sess:
            sess["user_id"] = user.id

        resp = self.client.get("/account/delete")
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"Delete Your Account", resp.data)
        self.assertIn(b"Current Store Password", resp.data)
        self.assertIn(b"btn-confirm-delete", resp.data)

    def test_delete_get_never_mutates_state(self) -> None:
        with self.app.app_context():
            user = self.user_repo.create(
                User(
                    email="safe_get@example.com",
                    password_hash=AuthService.hash_password("Password123!"),
                    role=UserRole.CUSTOMER,
                    is_verified=True,
                )
            )

        with self.client.session_transaction() as sess:
            sess["user_id"] = user.id

        resp = self.client.get("/account/delete")
        self.assertEqual(resp.status_code, 200)

        # Confirm user is still present
        with self.app.app_context():
            self.assertIsNotNone(self.user_repo.get_by_id(user.id))

    def test_delete_account_post_password_success(self) -> None:
        with self.app.app_context():
            user = self.user_repo.create(
                User(
                    email="delete_me@example.com",
                    password_hash=AuthService.hash_password("CorrectPassword123!"),
                    role=UserRole.CUSTOMER,
                    is_verified=True,
                )
            )

        with self.client.session_transaction() as sess:
            sess["user_id"] = user.id

        resp = self.client.post(
            "/account/delete",
            data={"password": "CorrectPassword123!"},
            follow_redirects=True,
        )
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"Your account and personal data have been permanently deleted.", resp.data)

        # Verify user is deleted from database
        with self.app.app_context():
            self.assertIsNone(self.user_repo.get_by_id(user.id))

        # Verify session is completely cleared
        with self.client.session_transaction() as sess:
            self.assertNotIn("user_id", sess)

    def test_delete_account_post_password_incorrect(self) -> None:
        with self.app.app_context():
            user = self.user_repo.create(
                User(
                    email="keep_me@example.com",
                    password_hash=AuthService.hash_password("CorrectPassword123!"),
                    role=UserRole.CUSTOMER,
                    is_verified=True,
                )
            )

        with self.client.session_transaction() as sess:
            sess["user_id"] = user.id

        resp = self.client.post(
            "/account/delete",
            data={"password": "WrongPassword123!"},
        )
        self.assertEqual(resp.status_code, 401)
        self.assertIn(b"Incorrect password", resp.data)

        # User must still exist
        with self.app.app_context():
            self.assertIsNotNone(self.user_repo.get_by_id(user.id))

        # Session remains active
        with self.client.session_transaction() as sess:
            self.assertEqual(sess.get("user_id"), user.id)

    def test_delete_account_post_rate_limiting(self) -> None:
        with self.app.app_context():
            user = self.user_repo.create(
                User(
                    email="ratelimited@example.com",
                    password_hash=AuthService.hash_password("Password123!"),
                    role=UserRole.CUSTOMER,
                    is_verified=True,
                )
            )

        with self.client.session_transaction() as sess:
            sess["user_id"] = user.id

        # 5 failed attempts
        for _ in range(5):
            resp = self.client.post(
                "/account/delete",
                data={"password": "WrongPassword!"},
            )
            self.assertEqual(resp.status_code, 401)

        # 6th attempt should return 429
        resp = self.client.post(
            "/account/delete",
            data={"password": "WrongPassword!"},
        )
        self.assertEqual(resp.status_code, 429)
        self.assertIn(b"Too many failed attempts", resp.data)

    def test_delete_account_post_without_confirmation_returns_400(self) -> None:
        with self.app.app_context():
            user = self.user_repo.create(
                User(
                    email="no_confirm@example.com",
                    password_hash=AuthService.hash_password("Password123!"),
                    role=UserRole.CUSTOMER,
                    is_verified=True,
                )
            )

        with self.client.session_transaction() as sess:
            sess["user_id"] = user.id

        resp = self.client.post(
            "/account/delete",
            data={},
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn(b"Identity confirmation is required", resp.data)

    def test_delete_hybrid_account_with_password(self) -> None:
        """Verify hybrid account (password + Google) can be deleted using password."""
        with self.app.app_context():
            user = self.user_repo.create(
                User(
                    email="hybrid1@example.com",
                    password_hash=AuthService.hash_password("HybridPassword123!"),
                    role=UserRole.CUSTOMER,
                    is_verified=True,
                )
            )
            self.identity_repo.create(
                ExternalIdentity(
                    user_id=user.id,
                    provider="google",
                    provider_user_id="google-sub-hybrid1",
                    email="hybrid1@example.com",
                )
            )

        with self.client.session_transaction() as sess:
            sess["user_id"] = user.id

        resp = self.client.post(
            "/account/delete",
            data={"password": "HybridPassword123!"},
            follow_redirects=True,
        )
        self.assertEqual(resp.status_code, 200)
        with self.app.app_context():
            self.assertIsNone(self.user_repo.get_by_id(user.id))
            self.assertIsNone(self.identity_repo.get_by_user_and_provider(user.id, "google"))

    def test_delete_hybrid_account_with_google_reauth(self) -> None:
        """Verify hybrid account (password + Google) can also be deleted using Google reauth."""
        with self.app.app_context():
            user = self.user_repo.create(
                User(
                    email="hybrid2@example.com",
                    password_hash=AuthService.hash_password("HybridPassword123!"),
                    role=UserRole.CUSTOMER,
                    is_verified=True,
                )
            )
            self.identity_repo.create(
                ExternalIdentity(
                    user_id=user.id,
                    provider="google",
                    provider_user_id="google-sub-hybrid2",
                    email="hybrid2@example.com",
                )
            )
            auth_service = AuthService()
            intent = auth_service.create_account_deletion_intent(
                user_id=user.id,
                sub="google-sub-hybrid2",
                email="hybrid2@example.com",
            )

        with self.client.session_transaction() as sess:
            sess["user_id"] = user.id
            sess[SESSION_REAUTH_DELETE_KEY] = {
                "intent_id": intent.intent_id,
                "user_id": user.id,
                "expires_at": intent.expires_at.timestamp(),
            }

        resp = self.client.post(
            "/account/delete",
            data={"reauth_token": intent.intent_id},
            follow_redirects=True,
        )
        self.assertEqual(resp.status_code, 200)
        with self.app.app_context():
            self.assertIsNone(self.user_repo.get_by_id(user.id))
            self.assertIsNone(self.identity_repo.get_by_user_and_provider(user.id, "google"))

    def test_delete_account_post_csrf_protection_enforced(self) -> None:
        """Verify that CSRF is strictly enforced when CSRF is enabled."""
        class CsrfConfig(self.config_cls):
            CSRF_ENABLED = True

        csrf_app = create_app(CsrfConfig)
        client = csrf_app.test_client()

        with csrf_app.app_context():
            init_db()
            repo = UserRepository()
            user = repo.create(
                User(
                    email="csrf_test@example.com",
                    password_hash=AuthService.hash_password("Password123!"),
                    role=UserRole.CUSTOMER,
                    is_verified=True,
                )
            )

        with client.session_transaction() as sess:
            sess["user_id"] = user.id

        # POST without CSRF token should be rejected with 400
        resp = client.post(
            "/account/delete",
            data={"password": "Password123!"},
        )
        self.assertEqual(resp.status_code, 400)

    def test_delete_account_post_google_reauth_success(self) -> None:
        with self.app.app_context():
            user = self.user_repo.create(
                User(
                    email="google_only@example.com",
                    password_hash=None,
                    role=UserRole.CUSTOMER,
                    is_verified=True,
                )
            )
            self.identity_repo.create(
                ExternalIdentity(
                    user_id=user.id,
                    provider="google",
                    provider_user_id="google-sub-777",
                    email="google_only@example.com",
                )
            )
            auth_service = AuthService()
            intent = auth_service.create_account_deletion_intent(
                user_id=user.id,
                sub="google-sub-777",
                email="google_only@example.com",
            )

        with self.client.session_transaction() as sess:
            sess["user_id"] = user.id
            sess[SESSION_REAUTH_DELETE_KEY] = {
                "intent_id": intent.intent_id,
                "user_id": user.id,
                "expires_at": intent.expires_at.timestamp(),
            }

        resp = self.client.post(
            "/account/delete",
            data={"reauth_token": intent.intent_id},
            follow_redirects=True,
        )
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"Your account and personal data have been permanently deleted.", resp.data)

        # User deleted
        with self.app.app_context():
            self.assertIsNone(self.user_repo.get_by_id(user.id))

    def test_delete_account_post_google_reauth_expired_returns_400(self) -> None:
        with self.app.app_context():
            user = self.user_repo.create(
                User(
                    email="google_expired@example.com",
                    password_hash=None,
                    role=UserRole.CUSTOMER,
                    is_verified=True,
                )
            )
            self.identity_repo.create(
                ExternalIdentity(
                    user_id=user.id,
                    provider="google",
                    provider_user_id="google-sub-expired",
                    email="google_expired@example.com",
                )
            )
            auth_service = AuthService()
            # Create already-expired server-side intent
            intent = AccountLinkIntent(
                user_id=user.id,
                intent_id="expired_token_123",
                provider="google",
                provider_user_id="google-sub-expired",
                email="google_expired@example.com",
                action="delete_account",
                expires_at=datetime.now(timezone.utc) - timedelta(seconds=10),
            )
            auth_service.account_link_intent_repo.create(intent)

        with self.client.session_transaction() as sess:
            sess["user_id"] = user.id
            sess[SESSION_REAUTH_DELETE_KEY] = {
                "intent_id": "expired_token_123",
                "user_id": user.id,
                "expires_at": time.time() - 10,  # Expired
            }

        resp = self.client.post(
            "/account/delete",
            data={"reauth_token": "expired_token_123"},
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn(b"Google reauthentication has expired or is invalid", resp.data)

        # User still exists
        with self.app.app_context():
            self.assertIsNotNone(self.user_repo.get_by_id(user.id))

    def test_delete_account_post_reauth_token_single_use(self) -> None:
        with self.app.app_context():
            user = self.user_repo.create(
                User(
                    email="single_use@example.com",
                    password_hash=None,
                    role=UserRole.CUSTOMER,
                    is_verified=True,
                )
            )
            self.identity_repo.create(
                ExternalIdentity(
                    user_id=user.id,
                    provider="google",
                    provider_user_id="google-sub-single",
                    email="single_use@example.com",
                )
            )
            auth_service = AuthService()
            intent = auth_service.create_account_deletion_intent(
                user_id=user.id,
                sub="google-sub-single",
                email="single_use@example.com",
            )

        with self.client.session_transaction() as sess:
            sess["user_id"] = user.id
            sess[SESSION_REAUTH_DELETE_KEY] = {
                "intent_id": intent.intent_id,
                "user_id": user.id,
                "expires_at": intent.expires_at.timestamp(),
            }

        # First use succeeds and deletes account
        resp1 = self.client.post("/account/delete", data={"reauth_token": intent.intent_id})
        self.assertEqual(resp1.status_code, 302)

        # Second attempt with same token fails (user is logged out or token is consumed)
        resp2 = self.client.post("/account/delete", data={"reauth_token": intent.intent_id})
        self.assertEqual(resp2.status_code, 302)  # redirected to login

    def test_cookie_restoration_cannot_replay_consumed_intent(self) -> None:
        """Verify that restoring an old signed session cookie cannot replay an already consumed intent."""
        with self.app.app_context():
            user = self.user_repo.create(
                User(
                    email="replay_test@example.com",
                    password_hash=None,
                    role=UserRole.CUSTOMER,
                    is_verified=True,
                )
            )
            self.identity_repo.create(
                ExternalIdentity(
                    user_id=user.id,
                    provider="google",
                    provider_user_id="google-sub-replay",
                    email="replay_test@example.com",
                )
            )
            auth_service = AuthService()
            intent = auth_service.create_account_deletion_intent(
                user_id=user.id,
                sub="google-sub-replay",
                email="replay_test@example.com",
            )

        # Simulate user having an active session with the intent
        with self.client.session_transaction() as sess:
            sess["user_id"] = user.id
            sess[SESSION_REAUTH_DELETE_KEY] = {
                "intent_id": intent.intent_id,
                "user_id": user.id,
                "expires_at": intent.expires_at.timestamp(),
            }

        # Consume the intent directly on the server (as another tab or prior call would)
        with self.app.app_context():
            auth_service = AuthService()
            auth_service.account_link_intent_repo.mark_as_consumed(intent.intent_id)

        # Attempt to submit the deletion using the restored cookie token
        resp = self.client.post("/account/delete", data={"reauth_token": intent.intent_id})
        self.assertEqual(resp.status_code, 400)
        self.assertIn(b"Google reauthentication has expired or is invalid", resp.data)

        # User is NOT deleted
        with self.app.app_context():
            self.assertIsNotNone(self.user_repo.get_by_id(user.id))

    def test_cookie_restoration_cannot_replay_cancelled_intent(self) -> None:
        """Verify that restoring a cookie with a cancelled intent fails."""
        with self.app.app_context():
            user = self.user_repo.create(
                User(
                    email="cancel_replay@example.com",
                    password_hash=None,
                    role=UserRole.CUSTOMER,
                    is_verified=True,
                )
            )
            self.identity_repo.create(
                ExternalIdentity(
                    user_id=user.id,
                    provider="google",
                    provider_user_id="google-sub-cancel",
                    email="cancel_replay@example.com",
                )
            )
            auth_service = AuthService()
            intent = auth_service.create_account_deletion_intent(
                user_id=user.id,
                sub="google-sub-cancel",
                email="cancel_replay@example.com",
            )

        with self.client.session_transaction() as sess:
            sess["user_id"] = user.id
            sess[SESSION_REAUTH_DELETE_KEY] = {
                "intent_id": intent.intent_id,
                "user_id": user.id,
                "expires_at": intent.expires_at.timestamp(),
            }

        # Cancel the intent via the cancel endpoint
        cancel_resp = self.client.post("/account/delete/cancel")
        self.assertEqual(cancel_resp.status_code, 302)

        # Re-inject the old intent into session to simulate signed cookie rollback
        with self.client.session_transaction() as sess:
            sess["user_id"] = user.id
            sess[SESSION_REAUTH_DELETE_KEY] = {
                "intent_id": intent.intent_id,
                "user_id": user.id,
                "expires_at": intent.expires_at.timestamp(),
            }

        # POST to delete should fail with 400 because server-side intent is cancelled
        resp = self.client.post("/account/delete", data={"reauth_token": intent.intent_id})
        self.assertEqual(resp.status_code, 400)
        self.assertIn(b"Google reauthentication has expired or is invalid", resp.data)

    def test_deletion_and_linking_intent_isolation(self) -> None:
        """Verify linking intents cannot be used for deletion and vice-versa."""
        with self.app.app_context():
            user = self.user_repo.create(
                User(
                    email="isolation@example.com",
                    password_hash=AuthService.hash_password("Password123!"),
                    role=UserRole.CUSTOMER,
                    is_verified=True,
                )
            )
            self.identity_repo.create(
                ExternalIdentity(
                    user_id=user.id,
                    provider="google",
                    provider_user_id="google-sub-iso",
                    email="isolation@example.com",
                )
            )
            auth_service = AuthService()

            # 1. Create a linking intent (action="link")
            link_intent = auth_service.create_account_link_intent(
                user_id=user.id,
                sub="google-sub-iso",
                email="isolation@example.com",
            )
            self.assertEqual(link_intent.action, "link")

            # Try to consume the linking intent for account deletion -> MUST FAIL
            del_ok, del_err = auth_service.consume_account_deletion_intent(
                link_intent.intent_id,
                user_id=user.id,
                sub="google-sub-iso",
                email="isolation@example.com",
            )
            self.assertFalse(del_ok)
            self.assertEqual(del_err, "intent_action_mismatch")

            # 2. Create a deletion intent (action="delete_account")
            del_intent = auth_service.create_account_deletion_intent(
                user_id=user.id,
                sub="google-sub-iso",
                email="isolation@example.com",
            )
            self.assertEqual(del_intent.action, "delete_account")

            # Try to use deletion intent in link_google_identity -> MUST FAIL
            link_user, link_err = auth_service.link_google_identity(
                user_id=user.id,
                sub="google-sub-iso",
                email="isolation@example.com",
                password="Password123!",
                intent_id=del_intent.intent_id,
            )
            self.assertIsNone(link_user)
            self.assertEqual(link_err, "intent_mismatch")

    def test_mismatched_google_identity_intent_rejected(self) -> None:
        """Verify deletion intent bound to one sub/email cannot be consumed by another."""
        with self.app.app_context():
            user = self.user_repo.create(
                User(
                    email="target@example.com",
                    password_hash=None,
                    role=UserRole.CUSTOMER,
                    is_verified=True,
                )
            )
            auth_service = AuthService()
            intent = auth_service.create_account_deletion_intent(
                user_id=user.id,
                sub="legit-sub",
                email="target@example.com",
            )

            # Mismatched sub
            ok, err = auth_service.consume_account_deletion_intent(
                intent.intent_id,
                user_id=user.id,
                sub="attacker-sub",
                email="target@example.com",
            )
            self.assertFalse(ok)
            self.assertEqual(err, "intent_mismatch")

            # Mismatched user_id
            ok, err = auth_service.consume_account_deletion_intent(
                intent.intent_id,
                user_id=user.id + 1,
                sub="legit-sub",
                email="target@example.com",
            )
            self.assertFalse(ok)
            self.assertEqual(err, "intent_mismatch")

    def test_concurrent_deletion_intent_consumption(self) -> None:
        """Verify concurrent consumption attempts only allow exactly one success."""
        from concurrent.futures import ThreadPoolExecutor

        with self.app.app_context():
            user = self.user_repo.create(
                User(
                    email="concurrent@example.com",
                    password_hash=None,
                    role=UserRole.CUSTOMER,
                    is_verified=True,
                )
            )
            auth_service = AuthService()
            intent = auth_service.create_account_deletion_intent(
                user_id=user.id,
                sub="sub-concurrent",
                email="concurrent@example.com",
            )
            intent_id = intent.intent_id

        results = []

        def consume_attempt():
            with self.app.app_context():
                service = AuthService()
                ok, _ = service.consume_account_deletion_intent(
                    intent_id,
                    user_id=user.id,
                    sub="sub-concurrent",
                    email="concurrent@example.com",
                )
                return ok

        with ThreadPoolExecutor(max_workers=5) as executor:
            futures = [executor.submit(consume_attempt) for _ in range(5)]
            results = [f.result() for f in futures]

        self.assertEqual(results.count(True), 1)
        self.assertEqual(results.count(False), 4)

    def test_cancel_endpoint_rejects_get(self) -> None:
        """Verify GET /account/delete/cancel is rejected (method not allowed)."""
        with self.app.app_context():
            user = self.user_repo.create(
                User(
                    email="get_cancel@example.com",
                    password_hash=AuthService.hash_password("Password123!"),
                    role=UserRole.CUSTOMER,
                    is_verified=True,
                )
            )

        with self.client.session_transaction() as sess:
            sess["user_id"] = user.id

        resp = self.client.get("/account/delete/cancel")
        self.assertEqual(resp.status_code, 405)

    def test_orders_page_includes_delete_account_link(self) -> None:
        with self.app.app_context():
            user = self.user_repo.create(
                User(
                    email="shopper@example.com",
                    password_hash=AuthService.hash_password("Password123!"),
                    role=UserRole.CUSTOMER,
                    is_verified=True,
                )
            )

        with self.client.session_transaction() as sess:
            sess["user_id"] = user.id

        resp = self.client.get("/account/orders")
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b'id="delete-account-link"', resp.data)
        self.assertIn(b"/account/delete", resp.data)


class TestGoogleReauthCallback(BaseAccountDeletionTestCase):
    """Tests for the Google OIDC reauthentication callback flow."""

    def test_delete_account_google_starts_oauth_flow(self) -> None:
        with self.app.app_context():
            user = self.user_repo.create(
                User(
                    email="google_user@example.com",
                    password_hash=None,
                    role=UserRole.CUSTOMER,
                    is_verified=True,
                )
            )
            self.identity_repo.create(
                ExternalIdentity(
                    user_id=user.id,
                    provider="google",
                    provider_user_id="google-sub-abc",
                    email="google_user@example.com",
                )
            )

        with self.client.session_transaction() as sess:
            sess["user_id"] = user.id

        resp = self.client.get("/account/delete/google")
        self.assertEqual(resp.status_code, 302)
        self.assertIn("accounts.google.com", resp.headers["Location"])

    def test_google_callback_reauth_success(self) -> None:
        with self.app.app_context():
            user = self.user_repo.create(
                User(
                    email="reauth_test@example.com",
                    password_hash=None,
                    role=UserRole.CUSTOMER,
                    is_verified=True,
                )
            )
            self.identity_repo.create(
                ExternalIdentity(
                    user_id=user.id,
                    provider="google",
                    provider_user_id="sub-12345",
                    email="reauth_test@example.com",
                )
            )

        # Seed session state as Authlib does
        state_key = "_state_google_mock_state_123"
        with self.client.session_transaction() as sess:
            sess["user_id"] = user.id
            sess[state_key] = {
                "data": {
                    "action": "delete_account",
                    "expected_user_id": user.id,
                }
            }

        with patch.object(oauth.google, "authorize_access_token") as mock_token:
            mock_token.return_value = {
                "access_token": "mock-token",
                "userinfo": {
                    "sub": "sub-12345",
                    "email": "reauth_test@example.com",
                    "email_verified": True,
                },
            }
            resp = self.client.get("/auth/google/callback?state=mock_state_123")
            self.assertEqual(resp.status_code, 302)
            self.assertEqual(resp.headers.get("Location"), "/account/delete")

        # Session should now contain valid reauth token and DB should contain server-side intent
        with self.client.session_transaction() as sess:
            reauth = sess.get(SESSION_REAUTH_DELETE_KEY)
            self.assertIsNotNone(reauth)
            self.assertEqual(reauth.get("user_id"), user.id)
            self.assertTrue(reauth.get("expires_at") > time.time())
            intent_id = reauth.get("intent_id")

        with self.app.app_context():
            auth_service = AuthService()
            intent = auth_service.get_account_link_intent(intent_id)
            self.assertIsNotNone(intent)
            self.assertEqual(intent.action, "delete_account")
            self.assertEqual(intent.user_id, user.id)
            self.assertEqual(intent.provider_user_id, "sub-12345")
            self.assertEqual(intent.status, "pending")

    def test_google_callback_reauth_cancelled(self) -> None:
        with self.app.app_context():
            user = self.user_repo.create(
                User(
                    email="cancel_reauth@example.com",
                    password_hash=None,
                    role=UserRole.CUSTOMER,
                    is_verified=True,
                )
            )

        state_key = "_state_google_mock_cancel_state"
        with self.client.session_transaction() as sess:
            sess["user_id"] = user.id
            sess[state_key] = {
                "data": {
                    "action": "delete_account",
                    "expected_user_id": user.id,
                }
            }

        resp = self.client.get(
            "/auth/google/callback?error=access_denied&state=mock_cancel_state"
        )
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp.headers.get("Location"), "/account/delete")

        with self.client.session_transaction() as sess:
            self.assertNotIn(SESSION_REAUTH_DELETE_KEY, sess)

    def test_google_callback_reauth_sub_mismatch_rejected(self) -> None:
        with self.app.app_context():
            user = self.user_repo.create(
                User(
                    email="mismatch@example.com",
                    password_hash=None,
                    role=UserRole.CUSTOMER,
                    is_verified=True,
                )
            )
            self.identity_repo.create(
                ExternalIdentity(
                    user_id=user.id,
                    provider="google",
                    provider_user_id="original-sub",
                    email="mismatch@example.com",
                )
            )

        state_key = "_state_google_mock_mismatch"
        with self.client.session_transaction() as sess:
            sess["user_id"] = user.id
            sess[state_key] = {
                "data": {
                    "action": "delete_account",
                    "expected_user_id": user.id,
                }
            }

        with patch.object(oauth.google, "authorize_access_token") as mock_token:
            mock_token.return_value = {
                "access_token": "mock-token",
                "userinfo": {
                    "sub": "attacker-different-sub",
                    "email": "mismatch@example.com",
                    "email_verified": True,
                },
            }
            resp = self.client.get("/auth/google/callback?state=mock_mismatch")
            self.assertEqual(resp.status_code, 302)
            self.assertEqual(resp.headers.get("Location"), "/account/delete")

        with self.client.session_transaction() as sess:
            self.assertNotIn(SESSION_REAUTH_DELETE_KEY, sess)

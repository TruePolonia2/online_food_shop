"""Comprehensive tests for Stripe Checkout Integration (Phase 10.12 - TEST MODE ONLY)."""
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from flask import session
import stripe

from app import create_app
from app.config import Config, is_stripe_configured
from app.database.connection import init_db
from app.models.category import Category
from app.models.customer import Customer
from app.models.order import Order, OrderStatus
from app.models.order_item import OrderItem
from app.models.payment import Payment, PaymentMethod, PaymentStatus
from app.models.product import Product
from app.models.user import User, UserRole
from app.repositories.category_repository import CategoryRepository
from app.repositories.customer_repository import CustomerRepository
from app.repositories.order_repository import OrderRepository
from app.repositories.payment_repository import PaymentRepository
from app.repositories.product_repository import ProductRepository
from app.repositories.user_repository import UserRepository
from app.services.auth_service import AuthService
from app.services.order_service import OrderService
from app.services.stripe_service import (
    MAX_EUR_CENTS,
    MIN_EUR_CENTS,
    StripeAmbiguousOutcomeError,
    StripeDefinitiveError,
    StripePaymentError,
    StripeService,
)


class BaseStripeTestCase(unittest.TestCase):
    """Base test case setting up isolated database and application context."""

    def setUp(self) -> None:
        self.original_env = dict(os.environ)
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_stripe.sqlite3"

        # Safe test mode credentials by default
        os.environ["STRIPE_SECRET_KEY"] = "sk_test_mock_secret_key_12345"
        os.environ["STRIPE_PUBLISHABLE_KEY"] = "pk_test_mock_publishable_key_12345"
        os.environ["STRIPE_CURRENCY"] = "eur"
        os.environ["APP_BASE_URL"] = "http://127.0.0.1:5000"

        class TestStripeConfig(Config):
            DATABASE_PATH = str(self.db_path)
            TESTING = True
            SECRET_KEY = "test-stripe-secret-key-32chars"
            CSRF_ENABLED = False

        self.app = create_app(TestStripeConfig)
        self.client = self.app.test_client()

        with self.app.app_context():
            init_db()
            self.user_repo = UserRepository()
            self.customer_repo = CustomerRepository()
            self.product_repo = ProductRepository()
            self.category_repo = CategoryRepository()
            self.order_repo = OrderRepository()
            self.payment_repo = PaymentRepository()
            self.auth_service = AuthService()

            cat = self.category_repo.create(Category(name="Produce"))
            self.apple = self.product_repo.create(
                Product(name="Organic Apple", price=150, stock_quantity=50, category_id=cat.id)
            )
            self.banana = self.product_repo.create(
                Product(name="Ripe Banana", price=200, stock_quantity=50, category_id=cat.id)
            )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()
        os.environ.clear()
        os.environ.update(self.original_env)


class TestStripeConfiguration(unittest.TestCase):
    """Tests covering Stripe configuration descriptors, key validation, and test mode gating."""

    def setUp(self) -> None:
        self.original_env = dict(os.environ)

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self.original_env)

    def test_missing_stripe_secret_key_is_unconfigured(self) -> None:
        os.environ.pop("STRIPE_SECRET_KEY", None)
        self.assertFalse(is_stripe_configured())
        self.assertIsNone(Config.STRIPE_SECRET_KEY)

    def test_valid_stripe_test_key_configured(self) -> None:
        os.environ["STRIPE_SECRET_KEY"] = "sk_test_sample_valid_key"
        self.assertTrue(is_stripe_configured())
        self.assertEqual(Config.STRIPE_SECRET_KEY, "sk_test_sample_valid_key")

    def test_rejection_of_live_secret_key_sk_live(self) -> None:
        os.environ["STRIPE_SECRET_KEY"] = "sk_live_prohibited_production_key"
        with self.assertRaises(ValueError) as ctx:
            _ = Config.STRIPE_SECRET_KEY
        self.assertIn("Live Stripe credentials are prohibited", str(ctx.exception))
        # Ensure raw live key is not echoed in exception
        self.assertNotIn("sk_live_prohibited_production_key", str(ctx.exception))

    def test_rejection_of_invalid_secret_key_prefix(self) -> None:
        os.environ["STRIPE_SECRET_KEY"] = "invalid_prefix_key"
        with self.assertRaises(ValueError) as ctx:
            _ = Config.STRIPE_SECRET_KEY
        self.assertIn("Only test mode keys starting with 'sk_test_'", str(ctx.exception))

    def test_rejection_of_live_publishable_key_pk_live(self) -> None:
        os.environ["STRIPE_PUBLISHABLE_KEY"] = "pk_live_prohibited_key"
        with self.assertRaises(ValueError) as ctx:
            _ = Config.STRIPE_PUBLISHABLE_KEY
        self.assertIn("Live Stripe credentials are prohibited", str(ctx.exception))
        self.assertNotIn("pk_live_prohibited_key", str(ctx.exception))

    def test_valid_stripe_publishable_key_pk_test(self) -> None:
        os.environ["STRIPE_PUBLISHABLE_KEY"] = "pk_test_sample_pub_key"
        self.assertEqual(Config.STRIPE_PUBLISHABLE_KEY, "pk_test_sample_pub_key")

    def test_currency_defaults_to_eur_and_normalizes(self) -> None:
        os.environ.pop("STRIPE_CURRENCY", None)
        self.assertEqual(Config.STRIPE_CURRENCY, "eur")

        os.environ["STRIPE_CURRENCY"] = "  EUR  "
        self.assertEqual(Config.STRIPE_CURRENCY, "eur")


class TestStripePricingAndLimits(BaseStripeTestCase):
    """Tests ensuring integer-cent accuracy, server-side price calculation, and amount limit validation."""

    def test_correct_eur_integer_cent_pricing_and_line_items_match(self) -> None:
        with self.app.app_context():
            customer = self.customer_repo.create(Customer(name="John Doe", email="john@example.com"))
            order = self.order_repo.create(
                Order(
                    customer_id=customer.id,
                    items=[
                        OrderItem(product_id=self.apple.id, product_name=self.apple.name, unit_price=150, quantity=3),
                        OrderItem(product_id=self.banana.id, product_name=self.banana.name, unit_price=200, quantity=2),
                    ],
                )
            )
            # Total should be (150 * 3) + (200 * 2) = 450 + 400 = 850 cents
            self.assertEqual(order.total, 850)

            service = StripeService()
            service.validate_order_for_checkout(order)

    def test_order_total_below_50_cents_rejected(self) -> None:
        with self.app.app_context():
            customer = self.customer_repo.create(Customer(name="John Doe", email="john@example.com"))
            order = self.order_repo.create(
                Order(
                    customer_id=customer.id,
                    items=[
                        OrderItem(product_id=self.apple.id, product_name=self.apple.name, unit_price=40, quantity=1),
                    ],
                )
            )
            self.assertEqual(order.total, 40)
            service = StripeService()
            with self.assertRaises(ValueError) as ctx:
                service.validate_order_for_checkout(order)
            self.assertIn("at least 0.50 €", str(ctx.exception))

    def test_order_total_above_maximum_rejected(self) -> None:
        with self.app.app_context():
            customer = self.customer_repo.create(Customer(name="John Doe", email="john@example.com"))
            order = self.order_repo.create(
                Order(
                    customer_id=customer.id,
                    items=[
                        OrderItem(product_id=self.apple.id, product_name=self.apple.name, unit_price=100_000_000, quantity=1),
                    ],
                )
            )
            service = StripeService()
            with self.assertRaises(ValueError) as ctx:
                service.validate_order_for_checkout(order)
            self.assertIn("exceeds maximum supported", str(ctx.exception))

    def test_empty_order_items_rejected(self) -> None:
        with self.app.app_context():
            customer = self.customer_repo.create(Customer(name="John Doe", email="john@example.com"))
            order = self.order_repo.create(Order(customer_id=customer.id, items=[]))
            service = StripeService()
            with self.assertRaises(ValueError) as ctx:
                service.validate_order_for_checkout(order)
            self.assertIn("no items", str(ctx.exception))


class TestStripeSessionCreationAndSecurity(BaseStripeTestCase):
    """Tests verifying hosted Checkout Session creation parameters, canonical URLs, and safe redirects."""

    @patch("stripe.checkout.Session.create")
    def test_checkout_session_creation_success_and_metadata(self, mock_stripe_create) -> None:
        mock_stripe_create.return_value = MagicMock(
            id="cs_test_session_abc123",
            url="https://checkout.stripe.com/c/pay/cs_test_session_abc123",
        )

        with self.app.app_context():
            customer = self.customer_repo.create(Customer(name="Alice", email="alice@example.com"))
            order = self.order_repo.create(
                Order(
                    customer_id=customer.id,
                    customer_email="alice@example.com",
                    items=[
                        OrderItem(product_id=self.apple.id, product_name=self.apple.name, unit_price=150, quantity=2),
                    ],
                )
            )
            payment = self.payment_repo.create(
                Payment(
                    order_id=order.id,
                    amount=order.total,
                    currency="eur",
                    idempotency_key="idemp_key_123",
                )
            )

            service = StripeService(payment_repo=self.payment_repo, order_repo=self.order_repo)
            session_obj = service.create_checkout_session(order=order, payment=payment)

            self.assertEqual(session_obj.id, "cs_test_session_abc123")
            mock_stripe_create.assert_called_once()
            _, kwargs = mock_stripe_create.call_args

            self.assertEqual(kwargs["mode"], "payment")
            self.assertEqual(kwargs["customer_email"], "alice@example.com")
            self.assertEqual(kwargs["client_reference_id"], str(order.id))
            self.assertEqual(kwargs["idempotency_key"], "idemp_key_123")
            self.assertEqual(kwargs["metadata"]["order_id"], str(order.id))
            self.assertEqual(kwargs["metadata"]["payment_id"], str(payment.id))

            # Verify unsupported legacy parameter 'payment_method_types' is omitted (dynamic payment methods)
            self.assertNotIn("payment_method_types", kwargs)

            # Verify line items
            line_items = kwargs["line_items"]
            self.assertEqual(len(line_items), 1)
            self.assertEqual(line_items[0]["price_data"]["currency"], "eur")
            self.assertEqual(line_items[0]["price_data"]["unit_amount"], 150)
            self.assertEqual(line_items[0]["quantity"], 2)

            # Verify canonical URLs (using APP_BASE_URL, ignoring untrusted Host)
            self.assertTrue(kwargs["success_url"].startswith("http://127.0.0.1:5000/order/stripe/success"))
            self.assertTrue(kwargs["cancel_url"].startswith("http://127.0.0.1:5000/order/stripe/cancel"))

            # Verify payment and order records updated with stripe_session_id
            updated_payment = self.payment_repo.get_by_id(payment.id)
            self.assertEqual(updated_payment.stripe_session_id, "cs_test_session_abc123")
            updated_order = self.order_repo.get_by_id(order.id)
            self.assertEqual(updated_order.stripe_session_id, "cs_test_session_abc123")

    @patch("stripe.checkout.Session.create")
    def test_checkout_session_creation_omits_unsupported_payment_method_types(self, mock_stripe_create) -> None:
        """Regression test ensuring Checkout Session creation omits payment_method_types.

        Modern Stripe API versions prohibit payment_method_types when creating
        Checkout Sessions, managing payment methods dynamically from Dashboard settings.
        """
        mock_stripe_create.return_value = MagicMock(
            id="cs_test_session_reg123",
            url="https://checkout.stripe.com/c/pay/cs_test_session_reg123",
        )

        with self.app.app_context():
            customer = self.customer_repo.create(Customer(name="Bob", email="bob@example.com"))
            order = self.order_repo.create(
                Order(
                    customer_id=customer.id,
                    customer_email="bob@example.com",
                    items=[
                        OrderItem(product_id=self.apple.id, product_name=self.apple.name, unit_price=150, quantity=1),
                    ],
                )
            )
            payment = self.payment_repo.create(
                Payment(
                    order_id=order.id,
                    amount=order.total,
                    currency="eur",
                    idempotency_key="idemp_key_regression_1",
                )
            )

            service = StripeService(payment_repo=self.payment_repo, order_repo=self.order_repo)
            service.create_checkout_session(order=order, payment=payment)

            mock_stripe_create.assert_called_once()
            _, kwargs = mock_stripe_create.call_args
            self.assertNotIn("payment_method_types", kwargs)
            self.assertEqual(kwargs["mode"], "payment")

    def test_safe_redirect_url_validation(self) -> None:
        service = StripeService()
        # Valid Stripe URLs
        service.validate_redirect_url("https://checkout.stripe.com/pay/cs_test_123")
        service.validate_redirect_url("https://sub.stripe.com/pay/cs_test_123")

        # Insecure or external URLs
        with self.assertRaises(StripePaymentError):
            service.validate_redirect_url("http://checkout.stripe.com/pay/cs_test_123")
        with self.assertRaises(StripePaymentError):
            service.validate_redirect_url("https://evil-attacker.com/pay")
        with self.assertRaises(StripePaymentError):
            service.validate_redirect_url("javascript:alert(1)")
        with self.assertRaises(StripePaymentError):
            service.validate_redirect_url("")


class TestStripeFailureAndRecovery(BaseStripeTestCase):
    """Tests verifying failure recovery: inventory restoration, cart preservation, and payment status."""

    @patch("stripe.checkout.Session.create")
    def test_stripe_api_error_restores_inventory_and_preserves_cart(self, mock_stripe_create) -> None:
        # Simulate definitive Stripe API rejection
        mock_stripe_create.side_effect = stripe.InvalidRequestError("Invalid payment configuration.", "param")

        # Put items in cart
        with self.client.session_transaction() as sess:
            sess["cart"] = {str(self.apple.id): 4}

        initial_stock = self.apple.stock_quantity

        # Submit checkout with Stripe
        resp = self.client.post(
            "/checkout",
            data={
                "name": "Bob Smith",
                "email": "bob@example.com",
                "payment_method": "stripe",
            },
            follow_redirects=True,
        )

        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"Unable to initiate Stripe payment session", resp.data)

        # Inventory must be restored
        with self.app.app_context():
            refreshed_apple = self.product_repo.get_by_id(self.apple.id)
            self.assertEqual(refreshed_apple.stock_quantity, initial_stock)

            # Order status must be CANCELLED and payment marked FAILED
            orders = self.order_repo.get_all()
            self.assertEqual(len(orders), 1)
            self.assertEqual(orders[0].status, OrderStatus.CANCELLED)

            payments = self.payment_repo.get_by_order_id(orders[0].id)
            self.assertEqual(len(payments), 1)
            self.assertEqual(payments[0].status, PaymentStatus.FAILED)

        # Cart in session must still be intact so customer can retry
        with self.client.session_transaction() as sess:
            self.assertEqual(sess.get("cart"), {str(self.apple.id): 4})


class TestStripeCheckoutFlow(BaseStripeTestCase):
    """Integration tests for checkout routes, payment method selection, and guest vs authenticated checkout."""

    @patch("stripe.checkout.Session.create")
    def test_guest_checkout_with_stripe_redirects_to_stripe_url(self, mock_stripe_create) -> None:
        mock_stripe_create.return_value = MagicMock(
            id="cs_test_guest_123",
            url="https://checkout.stripe.com/pay/cs_test_guest_123",
        )

        with self.client.session_transaction() as sess:
            sess["cart"] = {str(self.apple.id): 2}

        resp = self.client.post(
            "/checkout",
            data={
                "name": "Guest Customer",
                "email": "guest@example.com",
                "payment_method": "stripe",
            },
            follow_redirects=False,
        )

        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp.location, "https://checkout.stripe.com/pay/cs_test_guest_123")

        with self.app.app_context():
            orders = self.order_repo.get_all()
            self.assertEqual(len(orders), 1)
            self.assertIsNone(orders[0].user_id)
            self.assertEqual(orders[0].payment_method, "stripe")
            self.assertEqual(orders[0].payment_status, PaymentStatus.PENDING)
            self.assertEqual(orders[0].status, OrderStatus.PENDING)

    @patch("stripe.checkout.Session.create")
    def test_authenticated_checkout_with_stripe_associates_user_id(self, mock_stripe_create) -> None:
        mock_stripe_create.return_value = MagicMock(
            id="cs_test_auth_123",
            url="https://checkout.stripe.com/pay/cs_test_auth_123",
        )

        with self.app.app_context():
            user, _ = self.auth_service.register_customer("authcust@example.com", "Password123!")
            user_id = user.id

        with self.client.session_transaction() as sess:
            sess["user_id"] = user_id
            sess["cart"] = {str(self.apple.id): 2}

        resp = self.client.post(
            "/checkout",
            data={
                "name": "Auth Customer",
                "email": "authcust@example.com",
                "payment_method": "stripe",
            },
            follow_redirects=False,
        )

        self.assertEqual(resp.status_code, 302)
        with self.app.app_context():
            order = self.order_repo.get_by_id(1)
            self.assertEqual(order.user_id, user_id)
            self.assertEqual(order.payment_method, "stripe")
            self.assertEqual(order.payment_status, PaymentStatus.PENDING)

    def test_standard_checkout_continues_to_operate_normally(self) -> None:
        with self.client.session_transaction() as sess:
            sess["cart"] = {str(self.apple.id): 2}

        resp = self.client.post(
            "/checkout",
            data={
                "name": "Standard Customer",
                "email": "standard@example.com",
                "payment_method": "standard",
            },
            follow_redirects=False,
        )

        self.assertEqual(resp.status_code, 302)
        self.assertTrue(resp.location.startswith("/orders/"))

        with self.app.app_context():
            order = self.order_repo.get_by_id(1)
            self.assertEqual(order.payment_method, "standard")
            self.assertEqual(order.payment_status, PaymentStatus.UNPAID)

    def test_invalid_payment_method_rejected(self) -> None:
        with self.client.session_transaction() as sess:
            sess["cart"] = {str(self.apple.id): 1}

        resp = self.client.post(
            "/checkout",
            data={
                "name": "Test Customer",
                "email": "test@example.com",
                "payment_method": "bitcoin",
            },
            follow_redirects=True,
        )

        self.assertIn(b"Invalid payment method selected", resp.data)

    def test_stripe_checkout_when_unconfigured_flashes_error(self) -> None:
        self.app.config["STRIPE_SECRET_KEY"] = None
        os.environ.pop("STRIPE_SECRET_KEY", None)
        with self.client.session_transaction() as sess:
            sess["cart"] = {str(self.apple.id): 1}

        resp = self.client.post(
            "/checkout",
            data={
                "name": "Test Customer",
                "email": "test@example.com",
                "payment_method": "stripe",
            },
            follow_redirects=True,
        )

        self.assertIn(b"Stripe card payment is not currently available", resp.data)


class TestStripeSuccessAndCancelRoutes(BaseStripeTestCase):
    """Tests verifying success and cancellation routes, authorization checks, and payment status invariants."""

    def test_stripe_success_page_rendered_and_never_marks_order_paid(self) -> None:
        with self.app.app_context():
            customer = self.customer_repo.create(Customer(name="Alice", email="alice@example.com"))
            order = self.order_repo.create(
                Order(
                    customer_id=customer.id,
                    customer_name="Alice",
                    customer_email="alice@example.com",
                    payment_method="stripe",
                    payment_status=PaymentStatus.PENDING,
                    stripe_session_id="cs_test_success_123",
                    items=[OrderItem(product_id=self.apple.id, product_name=self.apple.name, unit_price=150, quantity=1)],
                )
            )
            self.payment_repo.create(
                Payment(
                    order_id=order.id,
                    amount=order.total,
                    currency="eur",
                    status=PaymentStatus.PENDING,
                    stripe_session_id="cs_test_success_123",
                    idempotency_key="idemp_test_success_123",
                )
            )
            order_id = order.id

        # Authorized session
        with self.client.session_transaction() as sess:
            sess["last_order_id"] = order_id

        resp = self.client.get("/order/stripe/success?session_id=cs_test_success_123")
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"Payment Submitted", resp.data)
        self.assertIn(b"Pending Confirmation", resp.data)

        # INVARIANT: A browser redirect must NEVER mark the order or payment as paid!
        with self.app.app_context():
            refreshed = self.order_repo.get_by_id(order_id)
            self.assertEqual(refreshed.payment_status, PaymentStatus.PENDING)
            self.assertEqual(refreshed.status, OrderStatus.PENDING)

    def test_stripe_success_unauthorized_visitor_returns_404(self) -> None:
        with self.app.app_context():
            customer = self.customer_repo.create(Customer(name="Alice", email="alice@example.com"))
            order = self.order_repo.create(
                Order(
                    customer_id=customer.id,
                    payment_method="stripe",
                    payment_status=PaymentStatus.PENDING,
                    stripe_session_id="cs_test_secret_123",
                    items=[OrderItem(product_id=self.apple.id, product_name=self.apple.name, unit_price=150, quantity=1)],
                )
            )
            self.payment_repo.create(
                Payment(
                    order_id=order.id,
                    amount=order.total,
                    currency="eur",
                    status=PaymentStatus.PENDING,
                    stripe_session_id="cs_test_secret_123",
                    idempotency_key="idemp_secret_123",
                )
            )

        # Visitor has no last_order_id in session
        resp = self.client.get("/order/stripe/success?session_id=cs_test_secret_123")
        self.assertEqual(resp.status_code, 404)

    def test_stripe_success_repeated_visits_safe_and_idempotent(self) -> None:
        with self.app.app_context():
            customer = self.customer_repo.create(Customer(name="Alice", email="alice@example.com"))
            order = self.order_repo.create(
                Order(
                    customer_id=customer.id,
                    customer_name="Alice",
                    customer_email="alice@example.com",
                    payment_method="stripe",
                    payment_status=PaymentStatus.PENDING,
                    stripe_session_id="cs_test_success_repeat",
                    items=[OrderItem(product_id=self.apple.id, product_name=self.apple.name, unit_price=150, quantity=1)],
                )
            )
            self.payment_repo.create(
                Payment(
                    order_id=order.id,
                    amount=order.total,
                    currency="eur",
                    status=PaymentStatus.PENDING,
                    stripe_session_id="cs_test_success_repeat",
                    idempotency_key="idemp_test_repeat_123",
                )
            )
            order_id = order.id

        with self.client.session_transaction() as sess:
            sess["last_order_id"] = order_id

        # Repeated visits must return 200 without changing status
        for _ in range(3):
            resp = self.client.get("/order/stripe/success?session_id=cs_test_success_repeat")
            self.assertEqual(resp.status_code, 200)

        with self.app.app_context():
            refreshed = self.order_repo.get_by_id(order_id)
            self.assertEqual(refreshed.payment_status, PaymentStatus.PENDING)

    def test_stripe_success_forged_or_missing_session_id_returns_404(self) -> None:
        resp1 = self.client.get("/order/stripe/success")
        self.assertEqual(resp1.status_code, 404)

        resp2 = self.client.get("/order/stripe/success?session_id=cs_test_nonexistent_forged")
        self.assertEqual(resp2.status_code, 404)

    def test_stripe_cancel_page_rendered_is_strictly_read_only_and_preserves_pending_status(self) -> None:
        with self.app.app_context():
            customer = self.customer_repo.create(Customer(name="Bob", email="bob@example.com"))
            order = self.order_repo.create(
                Order(
                    customer_id=customer.id,
                    customer_name="Bob",
                    payment_method="stripe",
                    payment_status=PaymentStatus.PENDING,
                    items=[OrderItem(product_id=self.apple.id, product_name=self.apple.name, unit_price=150, quantity=2)],
                )
            )
            payment = self.payment_repo.create(
                Payment(
                    order_id=order.id,
                    amount=order.total,
                    currency="eur",
                    status=PaymentStatus.PENDING,
                    idempotency_key="idemp_cancel_123",
                )
            )
            order_id = order.id
            payment_id = payment.id

            # Stock was decremented to 48 (50 - 2)
            prod = self.product_repo.get_by_id(self.apple.id)
            prod.stock_quantity = 48
            self.product_repo.update(prod)

        with self.client.session_transaction() as sess:
            sess["last_order_id"] = order_id

        resp = self.client.get(f"/order/stripe/cancel?order_id={order_id}")
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"Payment Not Completed", resp.data)
        self.assertIn(b"Payment Pending", resp.data)

        # CRITICAL INVARIANT: GET cancel must NEVER mark order/payment as cancelled or release inventory!
        with self.app.app_context():
            refreshed_order = self.order_repo.get_by_id(order_id)
            self.assertEqual(refreshed_order.payment_status, PaymentStatus.PENDING)
            self.assertEqual(refreshed_order.status, OrderStatus.PENDING)

            refreshed_payment = self.payment_repo.get_by_id(payment_id)
            self.assertEqual(refreshed_payment.status, PaymentStatus.PENDING)

            # Stock must remain reserved (48)
            apple_prod = self.product_repo.get_by_id(self.apple.id)
            self.assertEqual(apple_prod.stock_quantity, 48)

    def test_stripe_cancel_repeated_visits_have_zero_side_effects(self) -> None:
        with self.app.app_context():
            customer = self.customer_repo.create(Customer(name="Bob", email="bob@example.com"))
            order = self.order_repo.create(
                Order(
                    customer_id=customer.id,
                    payment_method="stripe",
                    payment_status=PaymentStatus.PENDING,
                    items=[OrderItem(product_id=self.apple.id, product_name=self.apple.name, unit_price=150, quantity=1)],
                )
            )
            payment = self.payment_repo.create(
                Payment(
                    order_id=order.id,
                    amount=order.total,
                    currency="eur",
                    status=PaymentStatus.PENDING,
                    idempotency_key="idemp_repeat_cancel",
                )
            )
            order_id = order.id
            payment_id = payment.id

        with self.client.session_transaction() as sess:
            sess["last_order_id"] = order_id

        # Visit 5 times consecutively
        for _ in range(5):
            resp = self.client.get(f"/order/stripe/cancel?order_id={order_id}")
            self.assertEqual(resp.status_code, 200)

        with self.app.app_context():
            refreshed_order = self.order_repo.get_by_id(order_id)
            self.assertEqual(refreshed_order.payment_status, PaymentStatus.PENDING)
            refreshed_payment = self.payment_repo.get_by_id(payment_id)
            self.assertEqual(refreshed_payment.status, PaymentStatus.PENDING)

    def test_stripe_cancel_forged_or_invalid_order_id_returns_404(self) -> None:
        # Missing order_id
        self.assertEqual(self.client.get("/order/stripe/cancel").status_code, 404)
        # Non-numeric
        self.assertEqual(self.client.get("/order/stripe/cancel?order_id=xyz").status_code, 404)
        # Non-existent
        self.assertEqual(self.client.get("/order/stripe/cancel?order_id=999999").status_code, 404)

    def test_stripe_cancel_unauthorized_visitor_returns_404(self) -> None:
        with self.app.app_context():
            customer = self.customer_repo.create(Customer(name="Bob", email="bob@example.com"))
            order = self.order_repo.create(Order(customer_id=customer.id, items=[]))
            order_id = order.id

        resp = self.client.get(f"/order/stripe/cancel?order_id={order_id}")
        self.assertEqual(resp.status_code, 404)

    def test_out_of_order_success_and_cancel_visits_safe(self) -> None:
        with self.app.app_context():
            customer = self.customer_repo.create(Customer(name="Bob", email="bob@example.com"))
            order = self.order_repo.create(
                Order(
                    customer_id=customer.id,
                    payment_method="stripe",
                    payment_status=PaymentStatus.PENDING,
                    stripe_session_id="cs_test_out_of_order",
                    items=[OrderItem(product_id=self.apple.id, product_name=self.apple.name, unit_price=150, quantity=1)],
                )
            )
            self.payment_repo.create(
                Payment(
                    order_id=order.id,
                    amount=order.total,
                    currency="eur",
                    status=PaymentStatus.PENDING,
                    stripe_session_id="cs_test_out_of_order",
                    idempotency_key="idemp_out_of_order",
                )
            )
            order_id = order.id

        with self.client.session_transaction() as sess:
            sess["last_order_id"] = order_id

        # Visit cancel first, then success
        resp_cancel = self.client.get(f"/order/stripe/cancel?order_id={order_id}")
        self.assertEqual(resp_cancel.status_code, 200)

        resp_success = self.client.get("/order/stripe/success?session_id=cs_test_out_of_order")
        self.assertEqual(resp_success.status_code, 200)

        # Neither visit mutates order to PAID or CANCELLED
        with self.app.app_context():
            refreshed = self.order_repo.get_by_id(order_id)
            self.assertEqual(refreshed.payment_status, PaymentStatus.PENDING)


class TestStripeIdempotencyAndConcurrency(BaseStripeTestCase):
    """Tests covering cart fingerprinting, stable checkout tokens, and duplicate prevention."""

    def test_checkout_form_generates_and_preserves_checkout_token(self) -> None:
        with self.client.session_transaction() as sess:
            sess["cart"] = {str(self.apple.id): 2}

        resp1 = self.client.get("/checkout")
        self.assertEqual(resp1.status_code, 200)
        self.assertIn(b'name="checkout_token"', resp1.data)

        with self.client.session_transaction() as sess:
            token1 = sess.get("checkout_token")
            fingerprint1 = sess.get("cart_fingerprint")
            self.assertIsNotNone(token1)
            self.assertIsNotNone(fingerprint1)

        # Consecutive GET with same cart preserves exact token
        resp2 = self.client.get("/checkout")
        self.assertEqual(resp2.status_code, 200)
        with self.client.session_transaction() as sess:
            self.assertEqual(sess.get("checkout_token"), token1)
            self.assertEqual(sess.get("cart_fingerprint"), fingerprint1)

    def test_cart_modification_invalidates_old_token_and_generates_new(self) -> None:
        with self.client.session_transaction() as sess:
            sess["cart"] = {str(self.apple.id): 1}

        self.client.get("/checkout")
        with self.client.session_transaction() as sess:
            token1 = sess.get("checkout_token")

        # Modify cart
        with self.client.session_transaction() as sess:
            sess["cart"] = {str(self.apple.id): 2, str(self.banana.id): 1}

        self.client.get("/checkout")
        with self.client.session_transaction() as sess:
            token2 = sess.get("checkout_token")
            self.assertNotEqual(token1, token2)

    @patch("stripe.checkout.Session.retrieve")
    @patch("stripe.checkout.Session.create")
    def test_rapid_duplicate_stripe_submission_reuses_session_without_duplicate_orders(
        self, mock_create, mock_retrieve
    ) -> None:
        mock_create.return_value = MagicMock(
            id="cs_test_idemp_100",
            url="https://checkout.stripe.com/pay/cs_test_idemp_100",
        )
        mock_retrieve.return_value = MagicMock(
            id="cs_test_idemp_100",
            url="https://checkout.stripe.com/pay/cs_test_idemp_100",
        )

        with self.client.session_transaction() as sess:
            sess["cart"] = {str(self.apple.id): 2}

        self.client.get("/checkout")
        with self.client.session_transaction() as sess:
            checkout_token = sess["checkout_token"]

        # First POST submission
        resp1 = self.client.post(
            "/checkout",
            data={
                "name": "Maria Silva",
                "email": "maria@example.com",
                "payment_method": "stripe",
                "checkout_token": checkout_token,
            },
            follow_redirects=False,
        )
        self.assertEqual(resp1.status_code, 302)
        self.assertEqual(resp1.location, "https://checkout.stripe.com/pay/cs_test_idemp_100")

        # Second rapid submission with identical checkout_token (e.g. double click or retry)
        resp2 = self.client.post(
            "/checkout",
            data={
                "name": "Maria Silva",
                "email": "maria@example.com",
                "payment_method": "stripe",
                "checkout_token": checkout_token,
            },
            follow_redirects=False,
        )
        self.assertEqual(resp2.status_code, 302)
        self.assertEqual(resp2.location, "https://checkout.stripe.com/pay/cs_test_idemp_100")

        # VERIFICATION: Exactly ONE order was created in the database!
        with self.app.app_context():
            orders = self.order_repo.get_all()
            self.assertEqual(len(orders), 1)
            self.assertEqual(orders[0].checkout_token, checkout_token)

            # Inventory decremented only ONCE (50 - 2 = 48)
            apple_prod = self.product_repo.get_by_id(self.apple.id)
            self.assertEqual(apple_prod.stock_quantity, 48)

            # Exactly ONE payment attempt created
            payments = self.payment_repo.get_by_order_id(orders[0].id)
            self.assertEqual(len(payments), 1)
            self.assertEqual(payments[0].idempotency_key, f"checkout_{checkout_token}")

    def test_standard_checkout_duplicate_submission_creates_single_order(self) -> None:
        with self.client.session_transaction() as sess:
            sess["cart"] = {str(self.apple.id): 1}

        self.client.get("/checkout")
        with self.client.session_transaction() as sess:
            checkout_token = sess["checkout_token"]

        # First standard checkout POST
        resp1 = self.client.post(
            "/checkout",
            data={
                "name": "Standard User",
                "email": "std@example.com",
                "payment_method": "standard",
                "checkout_token": checkout_token,
            },
            follow_redirects=False,
        )
        self.assertEqual(resp1.status_code, 302)

        # Rapid second POST with identical token
        resp2 = self.client.post(
            "/checkout",
            data={
                "name": "Standard User",
                "email": "std@example.com",
                "payment_method": "standard",
                "checkout_token": checkout_token,
            },
            follow_redirects=False,
        )
        self.assertEqual(resp2.status_code, 302)

        with self.app.app_context():
            orders = self.order_repo.get_all()
            self.assertEqual(len(orders), 1)
            apple_prod = self.product_repo.get_by_id(self.apple.id)
            self.assertEqual(apple_prod.stock_quantity, 49)


class TestStripeFailureAndTimeoutRecovery(BaseStripeTestCase):
    """Tests covering Stripe timeout resilience, definitive errors, and inventory integrity."""

    @patch("stripe.checkout.Session.create")
    def test_stripe_api_connection_timeout_preserves_order_and_reserved_stock(
        self, mock_stripe_create
    ) -> None:
        mock_stripe_create.side_effect = stripe.APIConnectionError("Connection timeout to Stripe API")

        with self.client.session_transaction() as sess:
            sess["cart"] = {str(self.apple.id): 2}

        self.client.get("/checkout")
        with self.client.session_transaction() as sess:
            checkout_token = sess["checkout_token"]

        resp = self.client.post(
            "/checkout",
            data={
                "name": "Timeout User",
                "email": "timeout@example.com",
                "payment_method": "stripe",
                "checkout_token": checkout_token,
            },
            follow_redirects=True,
        )

        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"A network timeout occurred while connecting to the payment provider", resp.data)

        # CRITICAL INVARIANT: The order must NOT be cancelled! Stock must NOT be released!
        with self.app.app_context():
            orders = self.order_repo.get_all()
            self.assertEqual(len(orders), 1)
            self.assertEqual(orders[0].payment_status, PaymentStatus.PENDING)
            self.assertEqual(orders[0].status, OrderStatus.PENDING)

            payments = self.payment_repo.get_by_order_id(orders[0].id)
            self.assertEqual(len(payments), 1)
            self.assertEqual(payments[0].status, PaymentStatus.PENDING)

            # Stock remains reserved at 48 (50 - 2)
            apple_prod = self.product_repo.get_by_id(self.apple.id)
            self.assertEqual(apple_prod.stock_quantity, 48)

        # User's cart is preserved in session to allow retry
        with self.client.session_transaction() as sess:
            self.assertIn(str(self.apple.id), sess.get("cart", {}))

    @patch("stripe.checkout.Session.create")
    def test_retry_after_timeout_reuses_same_order_and_idempotency_key(
        self, mock_stripe_create
    ) -> None:
        # First call times out
        mock_stripe_create.side_effect = stripe.APIConnectionError("Connection timeout")

        with self.client.session_transaction() as sess:
            sess["cart"] = {str(self.apple.id): 2}

        self.client.get("/checkout")
        with self.client.session_transaction() as sess:
            checkout_token = sess["checkout_token"]

        self.client.post(
            "/checkout",
            data={
                "name": "Retry User",
                "email": "retry@example.com",
                "payment_method": "stripe",
                "checkout_token": checkout_token,
            },
            follow_redirects=False,
        )

        # Second call succeeds
        mock_stripe_create.side_effect = None
        mock_stripe_create.return_value = MagicMock(
            id="cs_test_retry_recovered",
            url="https://checkout.stripe.com/pay/cs_test_retry_recovered",
        )

        resp = self.client.post(
            "/checkout",
            data={
                "name": "Retry User",
                "email": "retry@example.com",
                "payment_method": "stripe",
                "checkout_token": checkout_token,
            },
            follow_redirects=False,
        )

        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp.location, "https://checkout.stripe.com/pay/cs_test_retry_recovered")

        with self.app.app_context():
            orders = self.order_repo.get_all()
            self.assertEqual(len(orders), 1)  # No duplicate order!
            self.assertEqual(orders[0].stripe_session_id, "cs_test_retry_recovered")

            # Stock decremented only once (48)
            apple_prod = self.product_repo.get_by_id(self.apple.id)
            self.assertEqual(apple_prod.stock_quantity, 48)

    @patch("stripe.checkout.Session.create")
    def test_stripe_definitive_error_cancels_order_and_restores_inventory(
        self, mock_stripe_create
    ) -> None:
        mock_stripe_create.side_effect = stripe.InvalidRequestError("Invalid currency parameter", "currency")

        with self.client.session_transaction() as sess:
            sess["cart"] = {str(self.apple.id): 2}

        resp = self.client.post(
            "/checkout",
            data={
                "name": "Definitive Error User",
                "email": "deferr@example.com",
                "payment_method": "stripe",
            },
            follow_redirects=True,
        )

        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"Unable to initiate Stripe payment session", resp.data)

        # On definitive error: order is CANCELLED, payment is FAILED, inventory is RESTORED
        with self.app.app_context():
            orders = self.order_repo.get_all()
            self.assertEqual(len(orders), 1)
            self.assertEqual(orders[0].status, OrderStatus.CANCELLED)

            payments = self.payment_repo.get_by_order_id(orders[0].id)
            self.assertEqual(payments[0].status, PaymentStatus.FAILED)

            # Stock restored back to 50
            apple_prod = self.product_repo.get_by_id(self.apple.id)
            self.assertEqual(apple_prod.stock_quantity, 50)

    @patch("stripe.checkout.Session.create")
    def test_database_persistence_failure_after_stripe_creation_does_not_cancel_order(
        self, mock_stripe_create
    ) -> None:
        mock_stripe_create.return_value = MagicMock(
            id="cs_test_db_fail_123",
            url="https://checkout.stripe.com/pay/cs_test_db_fail_123",
        )

        with self.client.session_transaction() as sess:
            sess["cart"] = {str(self.apple.id): 2}

        # Mock update_stripe_session_id on PaymentRepository to simulate DB write failure after Stripe creates session
        with patch.object(
            PaymentRepository,
            "update_stripe_session_id",
            side_effect=sqlite3.OperationalError("Simulated database write error"),
        ):
            resp = self.client.post(
                "/checkout",
                data={
                    "name": "DB Fail User",
                    "email": "dbfail@example.com",
                    "payment_method": "stripe",
                },
                follow_redirects=True,
            )

        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"A network timeout occurred while connecting to the payment provider", resp.data)

        # INVARIANT: Order and payment must remain PENDING; stock must NOT be released!
        with self.app.app_context():
            orders = self.order_repo.get_all()
            self.assertEqual(len(orders), 1)
            self.assertEqual(orders[0].status, OrderStatus.PENDING)
            self.assertEqual(orders[0].payment_status, PaymentStatus.PENDING)

            payments = self.payment_repo.get_by_order_id(orders[0].id)
            self.assertEqual(len(payments), 1)
            self.assertEqual(payments[0].status, PaymentStatus.PENDING)

            # Stock remains reserved (48)
            apple_prod = self.product_repo.get_by_id(self.apple.id)
            self.assertEqual(apple_prod.stock_quantity, 48)


class TestStripeDatabaseIntegrity(BaseStripeTestCase):
    """Tests verifying database migrations, constraints, and foreign key integrity."""

    def test_payments_idempotency_key_unique_constraint(self) -> None:
        with self.app.app_context():
            customer = self.customer_repo.create(Customer(name="John", email="john@example.com"))
            order = self.order_repo.create(Order(customer_id=customer.id, items=[]))

            self.payment_repo.create(
                Payment(
                    order_id=order.id,
                    amount=100,
                    currency="eur",
                    idempotency_key="unique_key_abc",
                )
            )

            # Duplicate idempotency key must raise integrity error
            with self.assertRaises(Exception):
                self.payment_repo.create(
                    Payment(
                        order_id=order.id,
                        amount=100,
                        currency="eur",
                        idempotency_key="unique_key_abc",
                    )
                )

    def test_orders_checkout_token_unique_constraint(self) -> None:
        with self.app.app_context():
            customer = self.customer_repo.create(Customer(name="John", email="john2@example.com"))
            self.order_repo.create(
                Order(
                    customer_id=customer.id,
                    items=[],
                    checkout_token="unique_tok_xyz",
                )
            )

            # Inserting another order with identical non-null checkout_token must raise IntegrityError
            with self.assertRaises(sqlite3.IntegrityError):
                self.order_repo.create(
                    Order(
                        customer_id=customer.id,
                        items=[],
                        checkout_token="unique_tok_xyz",
                    )
                )

    def test_foreign_key_check_passes(self) -> None:
        with self.app.app_context():
            cursor = self.order_repo.conn.execute("PRAGMA foreign_key_check;")
            violations = cursor.fetchall()
            self.assertEqual(len(violations), 0, f"Foreign key violations found: {violations}")


class TestStripeCSRFAndSecurity(BaseStripeTestCase):
    """Tests covering CSRF protection and price tampering prevention."""

    def test_checkout_post_csrf_protection_enforced(self) -> None:
        # Create an app instance with CSRF protection enabled
        class CSRFConfig(Config):
            DATABASE_PATH = str(self.db_path)
            TESTING = True
            SECRET_KEY = "test-stripe-secret-key-32chars"
            CSRF_ENABLED = True

        app = create_app(CSRFConfig)
        client = app.test_client()

        with client.session_transaction() as sess:
            sess["cart"] = {str(self.apple.id): 1}

        # POST without CSRF token must fail with HTTP 400 Bad Request
        resp = client.post(
            "/checkout",
            data={
                "name": "Attacker",
                "email": "attacker@example.com",
                "payment_method": "stripe",
            },
        )
        self.assertEqual(resp.status_code, 400)

    @patch("stripe.checkout.Session.create")
    def test_client_price_tampering_ignored_and_computed_on_server(self, mock_stripe_create) -> None:
        mock_stripe_create.return_value = MagicMock(
            id="cs_test_tamper_123",
            url="https://checkout.stripe.com/pay/cs_test_tamper_123",
        )

        with self.client.session_transaction() as sess:
            sess["cart"] = {str(self.apple.id): 2}

        # Client attempts to forge a price of 1 cent and total of 2 cents in form data
        resp = self.client.post(
            "/checkout",
            data={
                "name": "Price Tamperer",
                "email": "tamperer@example.com",
                "payment_method": "stripe",
                "price": "1",
                "total": "2",
                "amount": "2",
            },
        )
        self.assertEqual(resp.status_code, 302)

        # Ensure server used catalog price (150 * 2 = 300 cents), completely ignoring forged inputs
        with self.app.app_context():
            order = self.order_repo.get_by_id(1)
            self.assertEqual(order.total, 300)
            self.assertEqual(order.items[0].unit_price, 150)
            payment = self.payment_repo.get_by_order_id(order.id)[0]
            self.assertEqual(payment.amount, 300)


class TestStripeUIRendering(BaseStripeTestCase):
    """Tests verifying checkout template rendering with and without Stripe enabled."""

    def test_checkout_form_renders_stripe_option_when_configured(self) -> None:
        with self.client.session_transaction() as sess:
            sess["cart"] = {str(self.apple.id): 1}

        resp = self.client.get("/checkout")
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"Credit / Debit Card (Stripe)", resp.data)
        self.assertIn(b"Test Mode Only", resp.data)
        self.assertIn(b"Standard Checkout (Pay on Delivery)", resp.data)

    def test_checkout_form_omits_stripe_option_when_unconfigured(self) -> None:
        self.app.config["STRIPE_SECRET_KEY"] = None
        os.environ.pop("STRIPE_SECRET_KEY", None)

        with self.client.session_transaction() as sess:
            sess["cart"] = {str(self.apple.id): 1}

        resp = self.client.get("/checkout")
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn(b"Test Mode Only", resp.data)
        self.assertIn(b"Standard Checkout (Pay on Delivery)", resp.data)


class TestStripeIdempotencyRecoveryAndParameterConflict(BaseStripeTestCase):
    """Regression tests covering idempotency conflict recovery, parameter migration, and session reuse."""

    @patch("stripe.checkout.Session.create")
    def test_idempotency_conflict_recovers_with_new_key_and_updated_params(
        self, mock_stripe_create
    ) -> None:
        """1. Old idempotency key with changed parameters triggers IdempotencyError and recovers with fresh key."""
        idempotency_err = stripe.error.IdempotencyError(
            "Keys for idempotent requests can only be used with the same parameters they were first used with.",
            http_status=400,
        )
        mock_stripe_create.side_effect = [
            idempotency_err,
            MagicMock(
                id="cs_test_recovered_key_101",
                url="https://checkout.stripe.com/pay/cs_test_recovered_key_101",
            ),
        ]

        with self.client.session_transaction() as sess:
            sess["cart"] = {str(self.apple.id): 2}

        self.client.get("/checkout")
        with self.client.session_transaction() as sess:
            checkout_token = sess["checkout_token"]

        resp = self.client.post(
            "/checkout",
            data={
                "name": "Alice Wonderland",
                "email": "alice@example.com",
                "payment_method": "stripe",
                "checkout_token": checkout_token,
            },
            follow_redirects=False,
        )

        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp.location, "https://checkout.stripe.com/pay/cs_test_recovered_key_101")
        self.assertEqual(mock_stripe_create.call_count, 2)

        # Inspect the two calls: second call used an updated, non-colliding idempotency key
        call1_kwargs = mock_stripe_create.call_args_list[0][1]
        call2_kwargs = mock_stripe_create.call_args_list[1][1]
        self.assertNotEqual(call1_kwargs["idempotency_key"], call2_kwargs["idempotency_key"])

        # Exactly ONE order exists, exactly ONE payment attempt, stock decremented only once
        with self.app.app_context():
            orders = self.order_repo.get_all()
            self.assertEqual(len(orders), 1)
            self.assertEqual(orders[0].stripe_session_id, "cs_test_recovered_key_101")
            payments = self.payment_repo.get_by_order_id(orders[0].id)
            self.assertEqual(len(payments), 1)
            self.assertEqual(payments[0].stripe_session_id, "cs_test_recovered_key_101")
            self.assertEqual(payments[0].idempotency_key, call2_kwargs["idempotency_key"])

            apple_prod = self.product_repo.get_by_id(self.apple.id)
            self.assertEqual(apple_prod.stock_quantity, 48)

    @patch("stripe.checkout.Session.create")
    def test_repeated_identical_checkout_submissions_reuses_session_and_key(
        self, mock_stripe_create
    ) -> None:
        """2. Repeated identical checkout submissions reuse the existing session without calling create twice."""
        mock_stripe_create.return_value = MagicMock(
            id="cs_test_repeat_ident_102",
            url="https://checkout.stripe.com/pay/cs_test_repeat_ident_102",
            status="open",
            payment_status="unpaid",
        )

        with self.client.session_transaction() as sess:
            sess["cart"] = {str(self.apple.id): 2}

        self.client.get("/checkout")
        with self.client.session_transaction() as sess:
            token = sess["checkout_token"]

        # First submit
        r1 = self.client.post(
            "/checkout",
            data={"name": "Alice", "email": "alice@example.com", "payment_method": "stripe", "checkout_token": token},
            follow_redirects=False,
        )
        self.assertEqual(r1.status_code, 302)
        self.assertEqual(mock_stripe_create.call_count, 1)

        # Second submit with identical inputs
        with patch.object(
            StripeService,
            "retrieve_checkout_session",
            return_value=MagicMock(
                id="cs_test_repeat_ident_102",
                url="https://checkout.stripe.com/pay/cs_test_repeat_ident_102",
                status="open",
                payment_status="unpaid",
            ),
        ):
            r2 = self.client.post(
                "/checkout",
                data={"name": "Alice", "email": "alice@example.com", "payment_method": "stripe", "checkout_token": token},
                follow_redirects=False,
            )
            self.assertEqual(r2.status_code, 302)
            self.assertEqual(r2.location, "https://checkout.stripe.com/pay/cs_test_repeat_ident_102")
            # create was NOT called again
            self.assertEqual(mock_stripe_create.call_count, 1)

    @patch("stripe.checkout.Session.create")
    def test_previous_request_definitely_failed_allows_clean_new_attempt(
        self, mock_stripe_create
    ) -> None:
        """3. When a previous request definitely failed, subsequent attempt starts fresh without reusing cancelled state."""
        mock_stripe_create.side_effect = stripe.InvalidRequestError("Invalid param", "param")

        with self.client.session_transaction() as sess:
            sess["cart"] = {str(self.apple.id): 2}

        self.client.get("/checkout")
        with self.client.session_transaction() as sess:
            first_token = sess["checkout_token"]

        # Attempt 1: Fails definitively
        r1 = self.client.post(
            "/checkout",
            data={"name": "Alice", "email": "alice@example.com", "payment_method": "stripe", "checkout_token": first_token},
            follow_redirects=True,
        )
        self.assertEqual(r1.status_code, 200)

        # First order was cancelled and inventory restored
        with self.app.app_context():
            orders = self.order_repo.get_all()
            self.assertEqual(len(orders), 1)
            self.assertEqual(orders[0].status, OrderStatus.CANCELLED)
            self.assertEqual(self.product_repo.get_by_id(self.apple.id).stock_quantity, 50)

        # Attempt 2: Fixed parameters now succeed
        mock_stripe_create.side_effect = None
        mock_stripe_create.return_value = MagicMock(
            id="cs_test_new_clean_103",
            url="https://checkout.stripe.com/pay/cs_test_new_clean_103",
        )

        r2 = self.client.post(
            "/checkout",
            data={"name": "Alice", "email": "alice@example.com", "payment_method": "stripe", "checkout_token": first_token},
            follow_redirects=False,
        )
        self.assertEqual(r2.status_code, 302)
        self.assertEqual(r2.location, "https://checkout.stripe.com/pay/cs_test_new_clean_103")

        with self.app.app_context():
            orders = self.order_repo.get_all()
            self.assertEqual(len(orders), 2)
            order1 = self.order_repo.get_by_id(1)
            order2 = self.order_repo.get_by_id(2)
            self.assertEqual(order1.status, OrderStatus.CANCELLED)
            self.assertEqual(order2.status, OrderStatus.PENDING)
            self.assertEqual(order2.stripe_session_id, "cs_test_new_clean_103")
            # Stock is reserved for order 2 (50 - 2 = 48), NOT double-decremented
            self.assertEqual(self.product_repo.get_by_id(self.apple.id).stock_quantity, 48)

    @patch("stripe.checkout.Session.create")
    def test_previous_request_outcome_ambiguous_preserves_pending_order_and_recovers(
        self, mock_stripe_create
    ) -> None:
        """4. Ambiguous timeout preserves pending order reservation and recovers safely on retry."""
        mock_stripe_create.side_effect = stripe.APIConnectionError("Timeout connecting to Stripe")

        with self.client.session_transaction() as sess:
            sess["cart"] = {str(self.apple.id): 2}

        self.client.get("/checkout")
        with self.client.session_transaction() as sess:
            token = sess["checkout_token"]

        # Attempt 1: Network timeout
        r1 = self.client.post(
            "/checkout",
            data={"name": "Alice", "email": "alice@example.com", "payment_method": "stripe", "checkout_token": token},
            follow_redirects=True,
        )
        self.assertEqual(r1.status_code, 200)

        with self.app.app_context():
            orders = self.order_repo.get_all()
            self.assertEqual(len(orders), 1)
            self.assertEqual(orders[0].status, OrderStatus.PENDING)
            self.assertEqual(self.product_repo.get_by_id(self.apple.id).stock_quantity, 48)

        # Attempt 2: Reconnection succeeds
        mock_stripe_create.side_effect = None
        mock_stripe_create.return_value = MagicMock(
            id="cs_test_reconnect_104",
            url="https://checkout.stripe.com/pay/cs_test_reconnect_104",
        )

        r2 = self.client.post(
            "/checkout",
            data={"name": "Alice", "email": "alice@example.com", "payment_method": "stripe", "checkout_token": token},
            follow_redirects=False,
        )
        self.assertEqual(r2.status_code, 302)

        with self.app.app_context():
            orders = self.order_repo.get_all()
            self.assertEqual(len(orders), 1)  # EXACT same order reused!
            self.assertEqual(orders[0].stripe_session_id, "cs_test_reconnect_104")
            self.assertEqual(self.product_repo.get_by_id(self.apple.id).stock_quantity, 48)

    @patch("stripe.checkout.Session.create")
    def test_existing_stripe_session_already_created_is_reconciled_and_reused(
        self, mock_stripe_create
    ) -> None:
        """5. Reconciles existing session found on Stripe API when local DB record is missing session ID."""
        with self.client.session_transaction() as sess:
            sess["cart"] = {str(self.apple.id): 2}

        self.client.get("/checkout")
        with self.client.session_transaction() as sess:
            token = sess["checkout_token"]

        mock_existing = MagicMock(
            id="cs_test_existing_cloud_105",
            url="https://checkout.stripe.com/pay/cs_test_existing_cloud_105",
            status="open",
            payment_status="unpaid",
            client_reference_id="1",
        )

        with patch.object(StripeService, "find_session_for_order", return_value=mock_existing):
            r = self.client.post(
                "/checkout",
                data={"name": "Alice", "email": "alice@example.com", "payment_method": "stripe", "checkout_token": token},
                follow_redirects=False,
            )
            self.assertEqual(r.status_code, 302)
            self.assertEqual(r.location, "https://checkout.stripe.com/pay/cs_test_existing_cloud_105")
            mock_stripe_create.assert_not_called()

    @patch("stripe.checkout.Session.create")
    def test_cart_or_customer_details_changed_between_attempts_cancels_old_and_starts_fresh(
        self, mock_stripe_create
    ) -> None:
        """6. Changing customer details between attempts safely cancels old pending order and creates clean new one."""
        # Attempt 1 encounters network timeout; order remains pending with preserved cart and token
        mock_stripe_create.side_effect = stripe.APIConnectionError("Timeout")

        with self.client.session_transaction() as sess:
            sess["cart"] = {str(self.apple.id): 2}

        self.client.get("/checkout")
        with self.client.session_transaction() as sess:
            token = sess["checkout_token"]

        # Attempt 1 for Alice
        r1 = self.client.post(
            "/checkout",
            data={"name": "Alice", "email": "alice@example.com", "payment_method": "stripe", "checkout_token": token},
            follow_redirects=True,
        )
        self.assertEqual(r1.status_code, 200)

        # Attempt 2: User changes name to Bob and email to bob@example.com and resubmits
        mock_stripe_create.side_effect = None
        mock_stripe_create.return_value = MagicMock(
            id="cs_test_customer_mod_107",
            url="https://checkout.stripe.com/pay/cs_test_customer_mod_107",
        )
        r2 = self.client.post(
            "/checkout",
            data={"name": "Bob", "email": "bob@example.com", "payment_method": "stripe", "checkout_token": token},
            follow_redirects=False,
        )
        self.assertEqual(r2.status_code, 302)
        self.assertEqual(r2.location, "https://checkout.stripe.com/pay/cs_test_customer_mod_107")

        with self.app.app_context():
            orders = self.order_repo.get_all()
            self.assertEqual(len(orders), 2)
            order1 = self.order_repo.get_by_id(1)
            order2 = self.order_repo.get_by_id(2)
            self.assertEqual(order1.status, OrderStatus.CANCELLED)
            self.assertEqual(order2.status, OrderStatus.PENDING)
            self.assertEqual(order2.customer_name, "Bob")
            # Stock is exactly 48 (50 - 2), not decremented twice (46)
            self.assertEqual(self.product_repo.get_by_id(self.apple.id).stock_quantity, 48)

    @patch("stripe.checkout.Session.create")
    def test_no_duplicate_orders_or_stock_reservations_on_rapid_retries(
        self, mock_stripe_create
    ) -> None:
        """7. Rapid retries produce no duplicate orders or duplicate inventory decrements."""
        mock_stripe_create.return_value = MagicMock(
            id="cs_test_rapid_108",
            url="https://checkout.stripe.com/pay/cs_test_rapid_108",
            status="open",
            payment_status="unpaid",
        )

        with self.client.session_transaction() as sess:
            sess["cart"] = {str(self.apple.id): 3}

        self.client.get("/checkout")
        with self.client.session_transaction() as sess:
            token = sess["checkout_token"]

        for _ in range(3):
            resp = self.client.post(
                "/checkout",
                data={"name": "Rapid User", "email": "rapid@example.com", "payment_method": "stripe", "checkout_token": token},
                follow_redirects=False,
            )
            self.assertEqual(resp.status_code, 302)

        with self.app.app_context():
            orders = self.order_repo.get_all()
            self.assertEqual(len(orders), 1)
            # Stock decremented only once for 3 items (50 - 3 = 47)
            self.assertEqual(self.product_repo.get_by_id(self.apple.id).stock_quantity, 47)

    @patch("stripe.checkout.Session.create")
    def test_concurrent_retries_handle_integrity_error_gracefully(
        self, mock_stripe_create
    ) -> None:
        """8. Concurrent requests colliding on unique checkout_token safely resolve the created order."""
        mock_stripe_create.return_value = MagicMock(
            id="cs_test_concurrent_109",
            url="https://checkout.stripe.com/pay/cs_test_concurrent_109",
        )

        with self.client.session_transaction() as sess:
            sess["cart"] = {str(self.apple.id): 1}

        self.client.get("/checkout")
        with self.client.session_transaction() as sess:
            token = sess["checkout_token"]

        # First request creates order
        resp1 = self.client.post(
            "/checkout",
            data={"name": "Thread1", "email": "t1@example.com", "payment_method": "stripe", "checkout_token": token},
            follow_redirects=False,
        )
        self.assertEqual(resp1.status_code, 302)

        # Second request encounters IntegrityError on checkout(), safely resolves existing order
        resp2 = self.client.post(
            "/checkout",
            data={"name": "Thread1", "email": "t1@example.com", "payment_method": "stripe", "checkout_token": token},
            follow_redirects=False,
        )
        self.assertEqual(resp2.status_code, 302)

        with self.app.app_context():
            orders = self.order_repo.get_all()
            self.assertEqual(len(orders), 1)
            self.assertEqual(self.product_repo.get_by_id(self.apple.id).stock_quantity, 49)


"""Comprehensive tests for Stripe Webhook Processing (Phase 10.13 - TEST MODE ONLY)."""
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
import uuid

import stripe

from app import create_app
from app.config import Config
from app.database.connection import init_db
from app.models.category import Category
from app.models.customer import Customer
from app.models.order import Order, OrderStatus
from app.models.order_item import OrderItem
from app.models.payment import Payment, PaymentMethod, PaymentStatus
from app.models.product import Product
from app.models.user import User, UserRole
from app.models.webhook_event import StripeWebhookEvent, WebhookEventStatus
from app.repositories.category_repository import CategoryRepository
from app.repositories.customer_repository import CustomerRepository
from app.repositories.order_repository import OrderRepository
from app.repositories.payment_repository import PaymentRepository
from app.repositories.product_repository import ProductRepository
from app.repositories.user_repository import UserRepository
from app.repositories.webhook_event_repository import WebhookEventRepository
from app.services.auth_service import AuthService
from app.services.order_service import OrderService
from app.services.stripe_service import StripeService


def generate_stripe_signature_header(payload: bytes, secret: str, timestamp: int | None = None) -> str:
    """Helper to generate a valid Stripe-Signature header matching Stripe's HMAC-SHA256 scheme."""
    t = timestamp if timestamp is not None else int(time.time())
    payload_str = payload.decode("utf-8") if isinstance(payload, bytes) else str(payload)
    sig = stripe.WebhookSignature._compute_signature(f"{t}.{payload_str}", secret)
    return f"t={t},v1={sig}"


class BaseWebhookTestCase(unittest.TestCase):
    """Base test case setting up isolated database, app context, and seed data."""

    def setUp(self) -> None:
        self.original_env = dict(os.environ)
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_stripe_webhook.sqlite3"

        self.webhook_secret = "whsec_test_mock_webhook_secret_12345"
        os.environ["STRIPE_SECRET_KEY"] = "sk_test_mock_secret_key_12345"
        os.environ["STRIPE_PUBLISHABLE_KEY"] = "pk_test_mock_publishable_key_12345"
        os.environ["STRIPE_WEBHOOK_SECRET"] = self.webhook_secret
        os.environ["STRIPE_CURRENCY"] = "eur"
        os.environ["APP_BASE_URL"] = "http://127.0.0.1:5000"

        class TestWebhookConfig(Config):
            DATABASE_PATH = str(self.db_path)
            TESTING = True
            SECRET_KEY = "test-stripe-webhook-secret-32chars"
            CSRF_ENABLED = False

        self.app = create_app(TestWebhookConfig)
        self.client = self.app.test_client()

        with self.app.app_context():
            init_db()
            self.user_repo = UserRepository()
            self.customer_repo = CustomerRepository()
            self.product_repo = ProductRepository()
            self.category_repo = CategoryRepository()
            self.order_repo = OrderRepository()
            self.payment_repo = PaymentRepository()
            self.webhook_event_repo = WebhookEventRepository()
            self.order_service = OrderService()
            self.stripe_service = StripeService()

            cat = self.category_repo.create(Category(name="Fresh Produce"))
            self.apple = self.product_repo.create(
                Product(name="Organic Gala Apple", price=150, stock_quantity=50, category_id=cat.id)
            )
            self.banana = self.product_repo.create(
                Product(name="Fairtrade Banana", price=200, stock_quantity=50, category_id=cat.id)
            )

            self.customer = self.customer_repo.create(
                Customer(
                    name="Elena Rostova",
                    email="elena@example.com",
                    phone="555-0199",
                    address="100 Market St",
                )
            )

            pwd_hash = AuthService.hash_password("OwnerSecurePass123!")
            self.owner_user = self.user_repo.create(
                User(
                    email="store-owner@example.com",
                    password_hash=pwd_hash,
                    role=UserRole.OWNER,
                    is_verified=True,
                )
            )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()
        os.environ.clear()
        os.environ.update(self.original_env)

    def get_stock(self, product_id: int) -> int:
        """Helper to safely query current stock within an app context."""
        with self.app.app_context():
            p = self.product_repo.get_by_id(product_id)
            return p.stock_quantity if p else 0

    def create_test_order(
        self,
        qty: int = 2,
        initial_order_status: OrderStatus = OrderStatus.PENDING,
        initial_payment_status: PaymentStatus = PaymentStatus.PENDING,
        session_id: str | None = "cs_test_session_1001",
    ) -> tuple[Order, Payment]:
        """Helper to create an order with reserved inventory and payment attempt."""
        with self.app.app_context():
            # Reserve stock as checkout would
            self.apple.stock_quantity -= qty
            self.product_repo.update(self.apple)

            order = self.order_repo.create(
                Order(
                    customer_id=self.customer.id,
                    status=initial_order_status,
                    customer_name=self.customer.name,
                    customer_email=self.customer.email,
                    customer_phone=self.customer.phone,
                    customer_address=self.customer.address,
                    payment_method=PaymentMethod.STRIPE.value,
                    payment_status=initial_payment_status,
                    stripe_session_id=session_id,
                    items=[
                        OrderItem(
                            product_id=self.apple.id,
                            product_name=self.apple.name,
                            unit_price=self.apple.price,
                            quantity=qty,
                        )
                    ],
                )
            )

            payment = self.payment_repo.create(
                Payment(
                    order_id=order.id,
                    amount=order.total,
                    currency="eur",
                    payment_method=PaymentMethod.STRIPE.value,
                    status=initial_payment_status,
                    stripe_session_id=session_id,
                    idempotency_key=f"idem_{uuid.uuid4().hex}",
                )
            )

            return order, payment

    def post_webhook(self, payload_dict: dict, secret: str | None = None, timestamp: int | None = None) -> tuple[int, dict]:
        """Send a signed webhook request and return (status_code, json_response)."""
        sec = secret if secret is not None else self.webhook_secret
        raw_bytes = json.dumps(payload_dict).encode("utf-8")
        sig_header = generate_stripe_signature_header(raw_bytes, sec, timestamp=timestamp)

        res = self.client.post(
            "/webhooks/stripe",
            data=raw_bytes,
            headers={
                "Content-Type": "application/json",
                "Stripe-Signature": sig_header,
            },
        )
        try:
            data = res.get_json() or {}
        except Exception:
            data = {}
        return res.status_code, data


class TestWebhookSignatureAndSecurity(BaseWebhookTestCase):
    """Test cryptographic signature verification and boundary enforcement."""

    def test_missing_signature_header_returns_400(self) -> None:
        raw_bytes = json.dumps({"id": "evt_test_1"}).encode("utf-8")
        res = self.client.post("/webhooks/stripe", data=raw_bytes, content_type="application/json")
        self.assertEqual(res.status_code, 400)
        self.assertIn("Missing Stripe-Signature", res.get_json()["error"])

    def test_empty_signature_header_returns_400(self) -> None:
        raw_bytes = json.dumps({"id": "evt_test_1"}).encode("utf-8")
        res = self.client.post(
            "/webhooks/stripe",
            data=raw_bytes,
            headers={"Content-Type": "application/json", "Stripe-Signature": "   "},
        )
        self.assertEqual(res.status_code, 400)

    def test_invalid_signature_format_returns_400(self) -> None:
        raw_bytes = json.dumps({"id": "evt_test_1"}).encode("utf-8")
        res = self.client.post(
            "/webhooks/stripe",
            data=raw_bytes,
            headers={"Content-Type": "application/json", "Stripe-Signature": "invalid_sig_without_t_or_v1"},
        )
        self.assertEqual(res.status_code, 400)

    def test_tampered_payload_rejected_with_400(self) -> None:
        legit_payload = json.dumps({"id": "evt_test_legit", "livemode": False}).encode("utf-8")
        sig_header = generate_stripe_signature_header(legit_payload, self.webhook_secret)

        tampered_payload = json.dumps({"id": "evt_test_tampered", "livemode": False}).encode("utf-8")
        res = self.client.post(
            "/webhooks/stripe",
            data=tampered_payload,
            headers={"Content-Type": "application/json", "Stripe-Signature": sig_header},
        )
        self.assertEqual(res.status_code, 400)
        self.assertIn("signature", res.get_json()["error"].lower())

    def test_expired_timestamp_rejected_with_400(self) -> None:
        # 305 seconds in the past exceeds tolerance (300 seconds)
        expired_ts = int(time.time()) - 305
        payload = {"id": "evt_test_expired", "type": "checkout.session.completed", "livemode": False}
        status, data = self.post_webhook(payload, timestamp=expired_ts)
        self.assertEqual(status, 400)
        self.assertIn("expired", data["error"].lower())

    def test_unconfigured_webhook_secret_returns_503(self) -> None:
        orig = self.app.config.get("STRIPE_WEBHOOK_SECRET")
        self.app.config["STRIPE_WEBHOOK_SECRET"] = None
        try:
            raw_bytes = json.dumps({"id": "evt_test_1"}).encode("utf-8")
            res = self.client.post(
                "/webhooks/stripe",
                data=raw_bytes,
                headers={"Content-Type": "application/json", "Stripe-Signature": "t=123,v1=abc"},
            )
            self.assertEqual(res.status_code, 503)
            self.assertIn("not configured", res.get_json()["error"].lower())
        finally:
            self.app.config["STRIPE_WEBHOOK_SECRET"] = orig

    def test_livemode_event_rejected_with_400(self) -> None:
        payload = {
            "id": "evt_test_livemode",
            "type": "checkout.session.completed",
            "livemode": True,  # Must be rejected!
            "data": {"object": {"id": "cs_test_1"}},
        }
        status, data = self.post_webhook(payload)
        self.assertEqual(status, 400)
        self.assertIn("livemode", data["error"].lower())

    def test_invalid_json_payload_returns_400(self) -> None:
        raw_bytes = b"not-a-valid-json-string{"
        sig = generate_stripe_signature_header(raw_bytes, self.webhook_secret)
        res = self.client.post(
            "/webhooks/stripe",
            data=raw_bytes,
            headers={"Content-Type": "application/json", "Stripe-Signature": sig},
        )
        self.assertEqual(res.status_code, 400)

    def test_csrf_exemption_for_webhook_endpoint(self) -> None:
        """Verify POST /webhooks/stripe is exempt from CSRF when CSRF_ENABLED=True."""
        class TestCsrfConfig(Config):
            DATABASE_PATH = str(self.db_path)
            TESTING = True
            SECRET_KEY = "test-stripe-webhook-secret-32chars"
            CSRF_ENABLED = True  # Enable CSRF globally

        csrf_app = create_app(TestCsrfConfig)
        csrf_client = csrf_app.test_client()

        # 1. Normal state-modifying route (e.g. /checkout) MUST fail without CSRF token
        checkout_res = csrf_client.post("/checkout", data={"customer_name": "Test"})
        self.assertEqual(checkout_res.status_code, 400)

        # 2. Webhook route MUST succeed without CSRF token
        payload = {
            "id": "evt_test_csrf_exempt",
            "type": "unknown.unhandled.event",
            "livemode": False,
            "data": {"object": {}},
        }
        raw_bytes = json.dumps(payload).encode("utf-8")
        sig = generate_stripe_signature_header(raw_bytes, self.webhook_secret)
        webhook_res = csrf_client.post(
            "/webhooks/stripe",
            data=raw_bytes,
            headers={"Content-Type": "application/json", "Stripe-Signature": sig},
        )
        self.assertEqual(webhook_res.status_code, 200)

    def test_rejection_of_non_stripe_payment_method(self) -> None:
        """A Stripe webhook attempting to confirm an order with payment_method='standard' must be rejected."""
        with self.app.app_context():
            order = self.order_repo.create(
                Order(
                    customer_id=self.customer.id,
                    status=OrderStatus.PENDING,
                    customer_name=self.customer.name,
                    payment_method="standard",
                    items=[OrderItem(product_id=self.apple.id, product_name=self.apple.name, unit_price=150, quantity=1)],
                )
            )
            payment = self.payment_repo.create(
                Payment(
                    order_id=order.id,
                    amount=order.total,
                    currency="eur",
                    payment_method="standard",
                    status=PaymentStatus.PENDING,
                    stripe_session_id="cs_test_standard_order",
                    idempotency_key=f"idem_{uuid.uuid4().hex}",
                )
            )

        payload = {
            "id": "evt_test_standard_method",
            "type": "checkout.session.completed",
            "livemode": False,
            "data": {
                "object": {
                    "id": "cs_test_standard_order",
                    "mode": "payment",
                    "payment_status": "paid",
                    "amount_total": order.total,
                    "currency": "eur",
                    "payment_intent": "pi_test_standard",
                    "client_reference_id": str(order.id),
                    "metadata": {"order_id": str(order.id)},
                }
            },
        }
        status, data = self.post_webhook(payload)
        self.assertEqual(status, 400)
        self.assertIn("payment method", data["error"].lower())


class TestWebhookCheckoutSessionCompleted(BaseWebhookTestCase):
    """Test checkout.session.completed event handling."""

    def test_session_completed_paid_updates_payment_and_order(self) -> None:
        order, payment = self.create_test_order(qty=2, session_id="cs_test_completed_1")
        initial_stock = self.get_stock(self.apple.id)

        payload = {
            "id": "evt_test_completed_paid",
            "type": "checkout.session.completed",
            "livemode": False,
            "data": {
                "object": {
                    "id": "cs_test_completed_1",
                    "mode": "payment",
                    "payment_status": "paid",
                    "amount_total": order.total,
                    "currency": "eur",
                    "payment_intent": "pi_test_intent_999",
                    "client_reference_id": str(order.id),
                    "metadata": {"order_id": str(order.id)},
                }
            },
        }

        status, data = self.post_webhook(payload)
        self.assertEqual(status, 200)
        self.assertEqual(data["status"], "success")

        # Verify state in database
        with self.app.app_context():
            updated_payment = self.payment_repo.get_by_id(payment.id)
            updated_order = self.order_repo.get_by_id(order.id)
            webhook_record = self.webhook_event_repo.get_by_event_id("evt_test_completed_paid")

            self.assertEqual(updated_payment.status, PaymentStatus.PAID)
            self.assertEqual(updated_payment.stripe_payment_intent_id, "pi_test_intent_999")
            self.assertEqual(updated_order.status, OrderStatus.CONFIRMED)
            self.assertEqual(updated_order.payment_status, PaymentStatus.PAID)

            self.assertIsNotNone(webhook_record)
            self.assertEqual(webhook_record.status, WebhookEventStatus.PROCESSED)
            self.assertEqual(webhook_record.order_id, order.id)
            self.assertEqual(webhook_record.payment_id, payment.id)

            # Reserved stock was NOT restored (order is confirmed/paid)
            final_stock = self.product_repo.get_by_id(self.apple.id).stock_quantity
            self.assertEqual(final_stock, initial_stock)

    def test_session_completed_unpaid_preserves_pending_status(self) -> None:
        order, payment = self.create_test_order(qty=1, session_id="cs_test_completed_unpaid")

        payload = {
            "id": "evt_test_completed_unpaid",
            "type": "checkout.session.completed",
            "livemode": False,
            "data": {
                "object": {
                    "id": "cs_test_completed_unpaid",
                    "mode": "payment",
                    "payment_status": "unpaid",  # Delayed / asynchronous payment method
                    "amount_total": order.total,
                    "currency": "eur",
                    "client_reference_id": str(order.id),
                    "metadata": {"order_id": str(order.id)},
                }
            },
        }

        status, data = self.post_webhook(payload)
        self.assertEqual(status, 200)
        self.assertEqual(data["status"], "success")

        with self.app.app_context():
            updated_payment = self.payment_repo.get_by_id(payment.id)
            updated_order = self.order_repo.get_by_id(order.id)

            # Must remain PENDING until authoritative confirmation arrives
            self.assertEqual(updated_payment.status, PaymentStatus.PENDING)
            self.assertEqual(updated_order.status, OrderStatus.PENDING)
            self.assertEqual(updated_order.payment_status, PaymentStatus.PENDING)


class TestWebhookAsyncPayments(BaseWebhookTestCase):
    """Test asynchronous payment success and failure events."""

    def test_async_payment_succeeded_transitions_to_paid(self) -> None:
        order, payment = self.create_test_order(qty=1, session_id="cs_test_async_ok")

        payload = {
            "id": "evt_test_async_succeeded",
            "type": "checkout.session.async_payment_succeeded",
            "livemode": False,
            "data": {
                "object": {
                    "id": "cs_test_async_ok",
                    "mode": "payment",
                    "payment_status": "paid",
                    "amount_total": order.total,
                    "currency": "eur",
                    "payment_intent": "pi_test_async_confirmed",
                    "client_reference_id": str(order.id),
                    "metadata": {"order_id": str(order.id)},
                }
            },
        }

        status, data = self.post_webhook(payload)
        self.assertEqual(status, 200)

        with self.app.app_context():
            updated_payment = self.payment_repo.get_by_id(payment.id)
            updated_order = self.order_repo.get_by_id(order.id)

            self.assertEqual(updated_payment.status, PaymentStatus.PAID)
            self.assertEqual(updated_payment.stripe_payment_intent_id, "pi_test_async_confirmed")
            self.assertEqual(updated_order.status, OrderStatus.CONFIRMED)
            self.assertEqual(updated_order.payment_status, PaymentStatus.PAID)

    def test_async_payment_failed_cancels_order_and_restores_stock(self) -> None:
        order, payment = self.create_test_order(qty=3, session_id="cs_test_async_fail")
        # Initial stock was 50, qty 3 reserved -> currently 47
        stock_before = self.get_stock(self.apple.id)
        self.assertEqual(stock_before, 47)

        payload = {
            "id": "evt_test_async_failed",
            "type": "checkout.session.async_payment_failed",
            "livemode": False,
            "data": {
                "object": {
                    "id": "cs_test_async_fail",
                    "mode": "payment",
                    "payment_status": "unpaid",
                    "amount_total": order.total,
                    "currency": "eur",
                    "client_reference_id": str(order.id),
                    "metadata": {"order_id": str(order.id)},
                }
            },
        }

        status, data = self.post_webhook(payload)
        self.assertEqual(status, 200)

        with self.app.app_context():
            updated_payment = self.payment_repo.get_by_id(payment.id)
            updated_order = self.order_repo.get_by_id(order.id)
            stock_after = self.product_repo.get_by_id(self.apple.id).stock_quantity

            self.assertEqual(updated_payment.status, PaymentStatus.FAILED)
            self.assertEqual(updated_order.status, OrderStatus.CANCELLED)
            self.assertEqual(updated_order.payment_status, PaymentStatus.FAILED)

            # Stock must be restored from 47 back to 50
            self.assertEqual(stock_after, 50)


class TestWebhookCheckoutSessionExpired(BaseWebhookTestCase):
    """Test checkout.session.expired event handling."""

    def test_session_expired_cancels_order_and_restores_inventory(self) -> None:
        order, payment = self.create_test_order(qty=4, session_id="cs_test_expired_1")
        stock_before = self.get_stock(self.apple.id)
        self.assertEqual(stock_before, 46)

        payload = {
            "id": "evt_test_expired_event_1",
            "type": "checkout.session.expired",
            "livemode": False,
            "data": {
                "object": {
                    "id": "cs_test_expired_1",
                    "mode": "payment",
                    "payment_status": "unpaid",
                    "amount_total": order.total,
                    "currency": "eur",
                    "client_reference_id": str(order.id),
                    "metadata": {"order_id": str(order.id)},
                }
            },
        }

        status, data = self.post_webhook(payload)
        self.assertEqual(status, 200)

        with self.app.app_context():
            updated_payment = self.payment_repo.get_by_id(payment.id)
            updated_order = self.order_repo.get_by_id(order.id)
            stock_after = self.product_repo.get_by_id(self.apple.id).stock_quantity

            self.assertEqual(updated_payment.status, PaymentStatus.CANCELLED)
            self.assertEqual(updated_order.status, OrderStatus.CANCELLED)
            self.assertEqual(updated_order.payment_status, PaymentStatus.CANCELLED)
            self.assertEqual(stock_after, 50)

    def test_transaction_rollback_on_stock_restoration_failure_and_subsequent_retry(self) -> None:
        """When an exception occurs during stock restoration, transaction rolls back cleanly and leaves order retryable."""
        order, payment = self.create_test_order(qty=2, session_id="cs_test_rollback_1")
        initial_stock = self.get_stock(self.apple.id)

        payload = {
            "id": "evt_test_rollback_crash",
            "type": "checkout.session.expired",
            "livemode": False,
            "data": {
                "object": {
                    "id": "cs_test_rollback_1",
                    "mode": "payment",
                    "payment_status": "unpaid",
                    "amount_total": order.total,
                    "currency": "eur",
                    "client_reference_id": str(order.id),
                    "metadata": {"order_id": str(order.id)},
                }
            },
        }

        # Simulate exception during cancel_order
        with patch.object(OrderService, "cancel_order", side_effect=RuntimeError("Simulated database lock during cancellation")):
            status, data = self.post_webhook(payload)
            self.assertEqual(status, 500)

        # Confirm rollback: order and payment are STILL pending, stock was NOT modified
        with self.app.app_context():
            o = self.order_repo.get_by_id(order.id)
            p = self.payment_repo.get_by_id(payment.id)
            self.assertEqual(o.status, OrderStatus.PENDING)
            self.assertEqual(p.status, PaymentStatus.PENDING)
            self.assertEqual(self.get_stock(self.apple.id), initial_stock)

            # Webhook event was marked FAILED, not PROCESSED
            rec = self.webhook_event_repo.get_by_event_id("evt_test_rollback_crash")
            self.assertIsNotNone(rec)
            self.assertEqual(rec.status, WebhookEventStatus.FAILED)

        # Retry the exact same event without error
        status2, data2 = self.post_webhook(payload)
        self.assertEqual(status2, 200)

        # Stock is now restored and order is cancelled
        with self.app.app_context():
            o2 = self.order_repo.get_by_id(order.id)
            p2 = self.payment_repo.get_by_id(payment.id)
            self.assertEqual(o2.status, OrderStatus.CANCELLED)
            self.assertEqual(p2.status, PaymentStatus.CANCELLED)
            self.assertEqual(self.get_stock(self.apple.id), 50)


class TestWebhookIdempotencyAndAtMostOnce(BaseWebhookTestCase):
    """Test deduplication, idempotency, and at-most-once inventory release."""

    def test_duplicate_event_returns_200_already_processed(self) -> None:
        order, payment = self.create_test_order(qty=1, session_id="cs_test_idem_1")

        payload = {
            "id": "evt_test_idempotent_single",
            "type": "checkout.session.completed",
            "livemode": False,
            "data": {
                "object": {
                    "id": "cs_test_idem_1",
                    "mode": "payment",
                    "payment_status": "paid",
                    "amount_total": order.total,
                    "currency": "eur",
                    "payment_intent": "pi_test_idem_1",
                    "client_reference_id": str(order.id),
                    "metadata": {"order_id": str(order.id)},
                }
            },
        }

        # First delivery
        status1, data1 = self.post_webhook(payload)
        self.assertEqual(status1, 200)
        self.assertEqual(data1["status"], "success")

        # Second delivery with exact same event ID
        status2, data2 = self.post_webhook(payload)
        self.assertEqual(status2, 200)
        self.assertEqual(data2["status"], "already_processed")

    def test_duplicate_expired_event_does_not_double_restore_stock(self) -> None:
        order, payment = self.create_test_order(qty=5, session_id="cs_test_double_expired")
        # 50 - 5 = 45
        self.assertEqual(self.get_stock(self.apple.id), 45)

        payload = {
            "id": "evt_test_double_expire",
            "type": "checkout.session.expired",
            "livemode": False,
            "data": {
                "object": {
                    "id": "cs_test_double_expired",
                    "mode": "payment",
                    "payment_status": "unpaid",
                    "amount_total": order.total,
                    "currency": "eur",
                    "client_reference_id": str(order.id),
                    "metadata": {"order_id": str(order.id)},
                }
            },
        }

        # First delivery: restored to 50
        status1, _ = self.post_webhook(payload)
        self.assertEqual(status1, 200)
        self.assertEqual(self.get_stock(self.apple.id), 50)

        # Duplicate delivery: stock must NOT become 55!
        status2, data2 = self.post_webhook(payload)
        self.assertEqual(status2, 200)
        self.assertEqual(data2["status"], "already_processed")
        self.assertEqual(self.get_stock(self.apple.id), 50)

    def test_concurrent_in_flight_event_returns_503_retryable(self) -> None:
        """If event is currently in 'processing' status in db and not stale, concurrent delivery returns 503 retryable."""
        with self.app.app_context():
            self.webhook_event_repo.record_processing(
                event_id="evt_test_in_flight",
                event_type="checkout.session.completed",
                livemode=False,
            )

        payload = {
            "id": "evt_test_in_flight",
            "type": "checkout.session.completed",
            "livemode": False,
            "data": {"object": {"id": "cs_test_dummy"}},
        }
        status, data = self.post_webhook(payload)
        self.assertEqual(status, 503)
        self.assertEqual(data["status"], "processing")

    def test_stale_processing_record_recovered_and_processed(self) -> None:
        """If a processing record is older than the stale lease threshold (120s), it is recovered and processed."""
        order, payment = self.create_test_order(qty=1, session_id="cs_test_stale_recov")

        from datetime import datetime, timezone, timedelta
        stale_time = (datetime.now(timezone.utc) - timedelta(seconds=150)).strftime("%Y-%m-%dT%H:%M:%SZ")

        with self.app.app_context():
            with self.webhook_event_repo.conn:
                self.webhook_event_repo.conn.execute(
                    """
                    INSERT INTO stripe_webhook_events (
                        event_id, event_type, livemode, status, created_at
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    ("evt_test_crashed_1", "checkout.session.completed", 0, "processing", stale_time),
                )

        payload = {
            "id": "evt_test_crashed_1",
            "type": "checkout.session.completed",
            "livemode": False,
            "data": {
                "object": {
                    "id": "cs_test_stale_recov",
                    "mode": "payment",
                    "payment_status": "paid",
                    "amount_total": order.total,
                    "currency": "eur",
                    "payment_intent": "pi_test_stale_intent",
                    "client_reference_id": str(order.id),
                    "metadata": {"order_id": str(order.id)},
                }
            },
        }

        status, data = self.post_webhook(payload)
        self.assertEqual(status, 200)
        self.assertEqual(data["status"], "success")

        with self.app.app_context():
            rec = self.webhook_event_repo.get_by_event_id("evt_test_crashed_1")
            self.assertEqual(rec.status, WebhookEventStatus.PROCESSED)
            p = self.payment_repo.get_by_id(payment.id)
            self.assertEqual(p.status, PaymentStatus.PAID)

    def test_concurrent_different_event_ids_restore_inventory_at_most_once(self) -> None:
        """Two different webhook events for the same order (e.g. expired then async_failed) release inventory exactly once."""
        order, payment = self.create_test_order(qty=3, session_id="cs_test_diff_events")
        # 50 - 3 = 47
        self.assertEqual(self.get_stock(self.apple.id), 47)

        # Event 1: checkout.session.expired
        payload1 = {
            "id": "evt_diff_1_expired",
            "type": "checkout.session.expired",
            "livemode": False,
            "data": {
                "object": {
                    "id": "cs_test_diff_events",
                    "mode": "payment",
                    "payment_status": "unpaid",
                    "amount_total": order.total,
                    "currency": "eur",
                    "client_reference_id": str(order.id),
                    "metadata": {"order_id": str(order.id)},
                }
            },
        }
        status1, _ = self.post_webhook(payload1)
        self.assertEqual(status1, 200)
        self.assertEqual(self.get_stock(self.apple.id), 50)

        # Event 2: checkout.session.async_payment_failed for the same order but DIFFERENT event ID
        payload2 = {
            "id": "evt_diff_2_failed",
            "type": "checkout.session.async_payment_failed",
            "livemode": False,
            "data": {
                "object": {
                    "id": "cs_test_diff_events",
                    "mode": "payment",
                    "payment_status": "unpaid",
                    "amount_total": order.total,
                    "currency": "eur",
                    "client_reference_id": str(order.id),
                    "metadata": {"order_id": str(order.id)},
                }
            },
        }
        status2, _ = self.post_webhook(payload2)
        self.assertEqual(status2, 200)

        # Stock must remain exactly 50, NOT 53!
        self.assertEqual(self.get_stock(self.apple.id), 50)

    def test_retry_of_previously_failed_webhook_event(self) -> None:
        """A webhook event that previously failed can be safely retried and processed to completion."""
        order, payment = self.create_test_order(qty=1, session_id="cs_test_retry_failed")

        # Mark event as previously FAILED in DB
        with self.app.app_context():
            with self.webhook_event_repo.conn:
                self.webhook_event_repo.conn.execute(
                    """
                    INSERT INTO stripe_webhook_events (
                        event_id, event_type, livemode, status, error_message, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    ("evt_test_failed_retry", "checkout.session.completed", 0, "failed", "Simulated transient error", "2026-10-29T10:00:00Z"),
                )

        payload = {
            "id": "evt_test_failed_retry",
            "type": "checkout.session.completed",
            "livemode": False,
            "data": {
                "object": {
                    "id": "cs_test_retry_failed",
                    "mode": "payment",
                    "payment_status": "paid",
                    "amount_total": order.total,
                    "currency": "eur",
                    "payment_intent": "pi_test_retried_intent",
                    "client_reference_id": str(order.id),
                    "metadata": {"order_id": str(order.id)},
                }
            },
        }

        status, data = self.post_webhook(payload)
        self.assertEqual(status, 200)
        self.assertEqual(data["status"], "success")

        with self.app.app_context():
            rec = self.webhook_event_repo.get_by_event_id("evt_test_failed_retry")
            self.assertEqual(rec.status, WebhookEventStatus.PROCESSED)
            p = self.payment_repo.get_by_id(payment.id)
            self.assertEqual(p.status, PaymentStatus.PAID)


class TestWebhookFinancialImmutability(BaseWebhookTestCase):
    """Verify that PAID status is immutable and never regresses."""

    def test_paid_order_never_regresses_on_subsequent_expired_event(self) -> None:
        order, payment = self.create_test_order(
            qty=2,
            initial_order_status=OrderStatus.CONFIRMED,
            initial_payment_status=PaymentStatus.PAID,
            session_id="cs_test_paid_immutable",
        )
        stock_before = self.get_stock(self.apple.id)

        # Out-of-order or malicious expired event for this session
        payload = {
            "id": "evt_test_expired_on_paid",
            "type": "checkout.session.expired",
            "livemode": False,
            "data": {
                "object": {
                    "id": "cs_test_paid_immutable",
                    "mode": "payment",
                    "payment_status": "unpaid",
                    "amount_total": order.total,
                    "currency": "eur",
                    "client_reference_id": str(order.id),
                    "metadata": {"order_id": str(order.id)},
                }
            },
        }

        status, data = self.post_webhook(payload)
        self.assertEqual(status, 200)

        with self.app.app_context():
            updated_payment = self.payment_repo.get_by_id(payment.id)
            updated_order = self.order_repo.get_by_id(order.id)
            stock_after = self.product_repo.get_by_id(self.apple.id).stock_quantity

            # MUST remain PAID and CONFIRMED
            self.assertEqual(updated_payment.status, PaymentStatus.PAID)
            self.assertEqual(updated_order.status, OrderStatus.CONFIRMED)
            self.assertEqual(updated_order.payment_status, PaymentStatus.PAID)

            # Stock MUST NOT be released
            self.assertEqual(stock_after, stock_before)

    def test_paid_order_never_regresses_on_subsequent_async_failed_event(self) -> None:
        order, payment = self.create_test_order(
            qty=2,
            initial_order_status=OrderStatus.CONFIRMED,
            initial_payment_status=PaymentStatus.PAID,
            session_id="cs_test_paid_immutable_2",
        )
        stock_before = self.get_stock(self.apple.id)

        payload = {
            "id": "evt_test_failed_on_paid",
            "type": "checkout.session.async_payment_failed",
            "livemode": False,
            "data": {
                "object": {
                    "id": "cs_test_paid_immutable_2",
                    "mode": "payment",
                    "payment_status": "unpaid",
                    "amount_total": order.total,
                    "currency": "eur",
                    "client_reference_id": str(order.id),
                    "metadata": {"order_id": str(order.id)},
                }
            },
        }

        status, data = self.post_webhook(payload)
        self.assertEqual(status, 200)

        with self.app.app_context():
            updated_payment = self.payment_repo.get_by_id(payment.id)
            updated_order = self.order_repo.get_by_id(order.id)
            stock_after = self.product_repo.get_by_id(self.apple.id).stock_quantity

            self.assertEqual(updated_payment.status, PaymentStatus.PAID)
            self.assertEqual(updated_order.status, OrderStatus.CONFIRMED)
            self.assertEqual(stock_after, stock_before)

    def test_late_expiration_on_paid_order_with_different_event_id(self) -> None:
        """A late expiration event with a DIFFERENT event ID on an already PAID order is safely ignored."""
        order, payment = self.create_test_order(
            qty=2,
            initial_order_status=OrderStatus.CONFIRMED,
            initial_payment_status=PaymentStatus.PAID,
            session_id="cs_test_paid_late_expire",
        )
        stock_before = self.get_stock(self.apple.id)

        payload = {
            "id": "evt_test_different_late_expired_id",
            "type": "checkout.session.expired",
            "livemode": False,
            "data": {
                "object": {
                    "id": "cs_test_paid_late_expire",
                    "mode": "payment",
                    "payment_status": "unpaid",
                    "amount_total": order.total,
                    "currency": "eur",
                    "client_reference_id": str(order.id),
                    "metadata": {"order_id": str(order.id)},
                }
            },
        }

        status, data = self.post_webhook(payload)
        self.assertEqual(status, 200)

        with self.app.app_context():
            o = self.order_repo.get_by_id(order.id)
            p = self.payment_repo.get_by_id(payment.id)
            self.assertEqual(o.status, OrderStatus.CONFIRMED)
            self.assertEqual(p.status, PaymentStatus.PAID)
            self.assertEqual(self.get_stock(self.apple.id), stock_before)


class TestWebhookUnmatchedSessionReconciliation(BaseWebhookTestCase):
    """Test safe recovery of sessions where initial session_id was unlinked."""

    def test_unmatched_session_reconciled_when_order_and_amount_match(self) -> None:
        # Create order and payment with stripe_session_id=None
        order, payment = self.create_test_order(qty=1, session_id=None)
        self.assertIsNone(payment.stripe_session_id)

        payload = {
            "id": "evt_test_reconcile_ok",
            "type": "checkout.session.completed",
            "livemode": False,
            "data": {
                "object": {
                    "id": "cs_test_reconciled_999",
                    "mode": "payment",
                    "payment_status": "paid",
                    "amount_total": order.total,
                    "currency": "eur",
                    "payment_intent": "pi_test_reconciled",
                    "client_reference_id": str(order.id),
                    "metadata": {"order_id": str(order.id)},
                }
            },
        }

        status, data = self.post_webhook(payload)
        self.assertEqual(status, 200)
        self.assertEqual(data["status"], "success")

        with self.app.app_context():
            updated_payment = self.payment_repo.get_by_id(payment.id)
            updated_order = self.order_repo.get_by_id(order.id)

            self.assertEqual(updated_payment.stripe_session_id, "cs_test_reconciled_999")
            self.assertEqual(updated_payment.status, PaymentStatus.PAID)
            self.assertEqual(updated_order.payment_status, PaymentStatus.PAID)

    def test_reconciliation_rejected_on_amount_mismatch(self) -> None:
        order, payment = self.create_test_order(qty=1, session_id=None)

        payload = {
            "id": "evt_test_reconcile_bad_amt",
            "type": "checkout.session.completed",
            "livemode": False,
            "data": {
                "object": {
                    "id": "cs_test_bad_amt",
                    "mode": "payment",
                    "payment_status": "paid",
                    "amount_total": order.total + 500,  # Different amount!
                    "currency": "eur",
                    "payment_intent": "pi_test_bad",
                    "client_reference_id": str(order.id),
                    "metadata": {"order_id": str(order.id)},
                }
            },
        }

        status, data = self.post_webhook(payload)
        # Should return 503 retryable with reconciliation_needed
        self.assertEqual(status, 503)
        self.assertEqual(data["status"], "reconciliation_needed")

        with self.app.app_context():
            updated_payment = self.payment_repo.get_by_id(payment.id)
            self.assertEqual(updated_payment.status, PaymentStatus.PENDING)
            self.assertIsNone(updated_payment.stripe_session_id)

    def test_reconciliation_rejected_on_currency_mismatch(self) -> None:
        order, payment = self.create_test_order(qty=1, session_id="cs_test_currency_bad")

        payload = {
            "id": "evt_test_reconcile_bad_curr",
            "type": "checkout.session.completed",
            "livemode": False,
            "data": {
                "object": {
                    "id": "cs_test_currency_bad",
                    "mode": "payment",
                    "payment_status": "paid",
                    "amount_total": order.total,
                    "currency": "usd",  # Rejected: only eur permitted
                    "payment_intent": "pi_test_curr",
                    "client_reference_id": str(order.id),
                    "metadata": {"order_id": str(order.id)},
                }
            },
        }

        status, data = self.post_webhook(payload)
        self.assertEqual(status, 400)
        self.assertIn("currency", data["error"].lower())

    def test_reconciliation_rejected_on_mode_mismatch(self) -> None:
        order, payment = self.create_test_order(qty=1, session_id="cs_test_mode_bad")

        payload = {
            "id": "evt_test_mode_bad",
            "type": "checkout.session.completed",
            "livemode": False,
            "data": {
                "object": {
                    "id": "cs_test_mode_bad",
                    "mode": "subscription",  # Rejected: only 'payment' supported
                    "payment_status": "paid",
                    "amount_total": order.total,
                    "currency": "eur",
                    "client_reference_id": str(order.id),
                    "metadata": {"order_id": str(order.id)},
                }
            },
        }

        status, data = self.post_webhook(payload)
        self.assertEqual(status, 400)
        self.assertIn("mode", data["error"].lower())


    def test_reconciliation_rejected_when_client_ref_and_metadata_conflict(self) -> None:
        order, payment = self.create_test_order(qty=1, session_id=None)

        payload = {
            "id": "evt_test_reconcile_conflict",
            "type": "checkout.session.completed",
            "livemode": False,
            "data": {
                "object": {
                    "id": "cs_test_conflict",
                    "mode": "payment",
                    "payment_status": "paid",
                    "amount_total": order.total,
                    "currency": "eur",
                    "payment_intent": "pi_test_conflict",
                    "client_reference_id": str(order.id),
                    "metadata": {"order_id": "999999"},  # Conflicting ID!
                }
            },
        }

        status, data = self.post_webhook(payload)
        self.assertEqual(status, 503)
        self.assertEqual(data["status"], "reconciliation_needed")


class TestWebhookUnknownEvents(BaseWebhookTestCase):
    """Test handling of unhandled or informational Stripe webhook events."""

    def test_unknown_event_type_acknowledged_with_200_ignored(self) -> None:
        payload = {
            "id": "evt_test_unknown_type",
            "type": "customer.created",
            "livemode": False,
            "data": {"object": {"id": "cus_12345"}},
        }

        status, data = self.post_webhook(payload)
        self.assertEqual(status, 200)
        self.assertEqual(data["status"], "ignored")

        with self.app.app_context():
            rec = self.webhook_event_repo.get_by_event_id("evt_test_unknown_type")
            self.assertIsNotNone(rec)
            self.assertEqual(rec.status, WebhookEventStatus.IGNORED)


class TestAdminOrderDisplayWithStripePayment(BaseWebhookTestCase):
    """Test admin order detail and listing presentation of payment details."""

    def login_as_owner(self) -> None:
        with self.client.session_transaction() as sess:
            sess["user_id"] = self.owner_user.id

    def test_admin_order_detail_renders_payment_details_card(self) -> None:
        order, payment = self.create_test_order(
            qty=1,
            initial_order_status=OrderStatus.CONFIRMED,
            initial_payment_status=PaymentStatus.PAID,
            session_id="cs_test_admin_display_123",
        )
        with self.app.app_context():
            self.payment_repo.update_status(
                payment.id,
                PaymentStatus.PAID,
                stripe_payment_intent_id="pi_test_admin_intent_456",
            )

        self.login_as_owner()
        res = self.client.get(f"/admin/orders/{order.id}")
        self.assertEqual(res.status_code, 200)

        # Payment Details Card
        self.assertIn(b"Payment Details", res.data)
        self.assertIn(b"Stripe (Card)", res.data)
        self.assertIn(b"Paid", res.data)
        self.assertIn(b"cs_test_admin_display_123", res.data)
        self.assertIn(b"pi_test_admin_intent_456", res.data)

    def test_admin_order_index_renders_payment_status_badge(self) -> None:
        order, payment = self.create_test_order(
            qty=1,
            initial_order_status=OrderStatus.CONFIRMED,
            initial_payment_status=PaymentStatus.PAID,
            session_id="cs_test_index_badge",
        )

        self.login_as_owner()
        res = self.client.get("/admin/orders")
        self.assertEqual(res.status_code, 200)

        self.assertIn(b"Payment: Paid", res.data)

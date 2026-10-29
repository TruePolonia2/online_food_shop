"""Stripe Checkout service for managing hosted payment sessions (TEST MODE ONLY)."""
import logging
from typing import Optional
import urllib.parse
import uuid

from flask import current_app
import stripe

from app.models.order import Order
from app.models.payment import Payment, PaymentStatus
from app.repositories.order_repository import OrderRepository
from app.repositories.payment_repository import PaymentRepository

logger = logging.getLogger(__name__)

# Minimum and maximum charge limits for EUR in minor units (cents)
MIN_EUR_CENTS = 50  # 0.50 EUR
MAX_EUR_CENTS = 99_999_999  # 999,999.99 EUR


class StripePaymentError(Exception):
    """Base exception for Stripe payment gateway errors."""

    pass


class StripeDefinitiveError(StripePaymentError):
    """Raised when Stripe API definitively rejects a request (e.g. invalid request, card error, authentication).

    The session definitely was NOT created. It is safe to mark payment failed, cancel the order,
    and restore inventory.
    """

    pass


class StripeAmbiguousOutcomeError(StripePaymentError):
    """Raised when communication with Stripe fails due to a network timeout or connection error.

    The outcome on Stripe's server is unknown: the session may or may not have been created.
    The order and payment attempt must remain PENDING. Stock must NOT be released!
    This enables safe retry with the identical idempotency key.
    """

    pass


_AMBIGUOUS_EXCEPTIONS: list[type[BaseException]] = [TimeoutError]
if hasattr(stripe, "APIConnectionError"):
    _AMBIGUOUS_EXCEPTIONS.append(stripe.APIConnectionError)
try:
    import requests.exceptions

    _AMBIGUOUS_EXCEPTIONS.extend(
        [requests.exceptions.Timeout, requests.exceptions.ConnectionError]
    )
except ImportError:
    pass
AMBIGUOUS_EXCEPTIONS = tuple(_AMBIGUOUS_EXCEPTIONS)


class StripeService:
    """Service handling Stripe hosted Checkout Session creation and lifecycle."""

    def __init__(
        self,
        payment_repo: Optional[PaymentRepository] = None,
        order_repo: Optional[OrderRepository] = None,
    ) -> None:
        """Initialize StripeService with injected repositories."""
        self.payment_repo = payment_repo or PaymentRepository()
        self.order_repo = order_repo or OrderRepository()

    def get_secret_key(self) -> str:
        """Retrieve and validate the Stripe test secret key from application config.

        Raises:
            RuntimeError: If the Stripe secret key is not configured.
            ValueError: If a live secret key (sk_live_*) is detected or the key format is invalid.
        """
        key = current_app.config.get("STRIPE_SECRET_KEY")
        if not key or not str(key).strip():
            raise RuntimeError("Stripe secret key is not configured.")

        cleaned = str(key).strip()
        if cleaned.startswith("sk_live_"):
            raise ValueError(
                "Live Stripe credentials are prohibited. Only Stripe test mode keys (sk_test_*) are supported."
            )
        if not cleaned.startswith("sk_test_"):
            raise ValueError(
                "Invalid Stripe secret key format. Only test mode keys starting with 'sk_test_' are supported."
            )
        return cleaned

    def get_currency(self) -> str:
        """Retrieve and validate the configured Stripe currency."""
        currency = current_app.config.get("STRIPE_CURRENCY", "eur")
        cleaned = currency.strip().lower() if currency else "eur"
        if cleaned != "eur":
            raise ValueError(f"Unsupported Stripe currency '{currency}'. Only 'eur' is supported.")
        return cleaned

    def validate_order_for_checkout(self, order: Order) -> None:
        """Validate that an order satisfies pricing, currency, and Stripe constraints.

        Args:
            order: The Order domain entity.

        Raises:
            ValueError: If order items are empty, pricing is out of bounds, or calculations mismatch.
        """
        if not order.items:
            raise ValueError("Cannot initiate payment for an order with no items.")

        total = order.total
        if total < MIN_EUR_CENTS:
            raise ValueError(
                f"Order total must be at least {MIN_EUR_CENTS / 100:.2f} € to process card payment."
            )
        if total > MAX_EUR_CENTS:
            raise ValueError("Order total exceeds maximum supported card payment amount.")

        # Verify integrity of snapshot line item pricing
        computed_total = 0
        for item in order.items:
            if item.unit_price < 0:
                raise ValueError(f"Invalid unit price for product '{item.product_name}'.")
            if item.quantity <= 0:
                raise ValueError(f"Invalid quantity for product '{item.product_name}'.")
            computed_total += item.unit_price * item.quantity

        if computed_total != total:
            raise ValueError("Line items total does not match internal order total.")

    def create_checkout_session(
        self,
        order: Order,
        payment: Payment,
    ) -> stripe.checkout.Session:
        """Create a hosted Stripe Checkout Session with mode='payment'.

        Uses canonical APP_BASE_URL for redirect URLs, passes server-calculated
        line items, and attaches metadata for tracking.

        Args:
            order: Persisted Order entity.
            payment: Persisted Payment entity.

        Returns:
            The created Stripe Checkout Session object.

        Raises:
            StripePaymentError: If session creation fails or the redirect URL is unsafe.
        """
        secret_key = self.get_secret_key()
        currency = self.get_currency()
        self.validate_order_for_checkout(order)

        # Build canonical URLs using APP_BASE_URL (never trusting Host header)
        base_url = current_app.config.get("APP_BASE_URL", "http://127.0.0.1:5000").strip().rstrip("/")
        success_url = f"{base_url}/order/stripe/success?session_id={{CHECKOUT_SESSION_ID}}"
        cancel_url = f"{base_url}/order/stripe/cancel?order_id={order.id}"

        # Construct server-calculated line items
        line_items = []
        for item in order.items:
            line_items.append(
                {
                    "price_data": {
                        "currency": currency,
                        "unit_amount": item.unit_price,
                        "product_data": {
                            "name": item.product_name,
                        },
                    },
                    "quantity": item.quantity,
                }
            )

        # Generate idempotency key for network resilience
        idempotency_key = payment.idempotency_key or f"checkout_{order.id}_{payment.id}_{uuid.uuid4().hex[:12]}"

        metadata = {
            "order_id": str(order.id),
            "user_id": str(order.user_id or ""),
            "payment_id": str(payment.id),
        }

        try:
            stripe.api_key = secret_key
            session = stripe.checkout.Session.create(
                payment_method_types=["card"],
                mode="payment",
                line_items=line_items,
                customer_email=order.customer_email,
                client_reference_id=str(order.id),
                metadata=metadata,
                success_url=success_url,
                cancel_url=cancel_url,
                idempotency_key=idempotency_key,
            )
        except AMBIGUOUS_EXCEPTIONS as exc:
            logger.error(
                "Stripe Checkout Session connection timeout/network error for order %s: %s",
                order.id,
                exc,
            )
            raise StripeAmbiguousOutcomeError(
                "A network timeout occurred while connecting to the payment provider. "
                "Your order reservation remains pending. Please retry to complete your payment."
            ) from exc
        except stripe.StripeError as exc:
            logger.error("Stripe Checkout Session creation failed for order %s: %s", order.id, exc)
            user_msg = getattr(exc, "user_message", None) or str(exc)
            raise StripeDefinitiveError(f"Stripe error: {user_msg}") from exc
        except Exception as exc:
            logger.error("Unexpected error creating Stripe session for order %s: %s", order.id, exc)
            raise StripeDefinitiveError(f"Payment gateway error: {exc}") from exc

        # Validate that redirect URL is a safe HTTPS Stripe Checkout URL
        self.validate_redirect_url(session.url)

        # Update payment attempt and order with the created Stripe session ID
        try:
            if session.id:
                if payment.id is not None:
                    self.payment_repo.update_stripe_session_id(payment.id, session.id)
                if order.id is not None:
                    self.order_repo.update_payment_status(
                        order.id, PaymentStatus.PENDING, stripe_session_id=session.id
                    )
        except Exception as db_exc:
            logger.error(
                "CRITICAL: Failed to persist stripe_session_id %s for order %s: %s. "
                "Phase 10.13 webhooks will reconcile via client_reference_id.",
                session.id,
                order.id,
                db_exc,
            )
            raise StripeAmbiguousOutcomeError(
                "Payment session was initiated but database persistence encountered a temporary issue. "
                "Your order reservation remains pending."
            ) from db_exc

        return session

    def retrieve_checkout_session(self, session_id: str) -> stripe.checkout.Session:
        """Retrieve an existing Stripe Checkout Session by ID.

        Args:
            session_id: Stripe Checkout Session ID (cs_test_*).

        Returns:
            The Stripe Checkout Session object.

        Raises:
            StripePaymentError: If retrieval fails.
        """
        secret_key = self.get_secret_key()
        stripe.api_key = secret_key
        try:
            return stripe.checkout.Session.retrieve(session_id.strip())
        except AMBIGUOUS_EXCEPTIONS as exc:
            raise StripeAmbiguousOutcomeError(
                f"Timeout retrieving Stripe session: {exc}"
            ) from exc
        except stripe.StripeError as exc:
            raise StripeDefinitiveError(f"Stripe error retrieving session: {exc}") from exc
        except Exception as exc:
            raise StripePaymentError(f"Error retrieving Stripe session: {exc}") from exc

    @staticmethod
    def validate_redirect_url(url: Optional[str]) -> None:
        """Ensure the redirect URL is a valid HTTPS URL pointing to Stripe's domain.

        Args:
            url: The checkout session URL returned by Stripe.

        Raises:
            StripePaymentError: If the URL is missing, not HTTPS, or not on stripe.com.
        """
        if not url or not isinstance(url, str):
            raise StripePaymentError("Stripe returned an empty or invalid checkout URL.")

        parsed = urllib.parse.urlparse(url.strip())
        if parsed.scheme != "https":
            raise StripePaymentError(f"Unsafe redirect scheme '{parsed.scheme}'. HTTPS is required.")

        netloc = parsed.netloc.lower()
        if netloc != "checkout.stripe.com" and not netloc.endswith(".stripe.com"):
            raise StripePaymentError(f"Unsafe redirect domain '{netloc}'. Must be Stripe domain.")

"""Stripe Checkout service for managing hosted payment sessions (TEST MODE ONLY)."""
import logging
from typing import Any, Optional
import urllib.parse
import uuid

from flask import current_app
import stripe

from app.database.connection import transaction
from app.models.order import Order, OrderStatus
from app.models.payment import Payment, PaymentStatus
from app.models.webhook_event import StripeWebhookEvent, WebhookEventStatus
from app.repositories.order_repository import OrderRepository
from app.repositories.payment_repository import PaymentRepository
from app.repositories.webhook_event_repository import WebhookEventRepository

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
        webhook_event_repo: Optional[WebhookEventRepository] = None,
        order_service: Optional[Any] = None,
    ) -> None:
        """Initialize StripeService with injected repositories sharing transaction connection."""
        self.payment_repo = payment_repo or PaymentRepository()
        self.order_repo = order_repo or OrderRepository(self.payment_repo._conn)
        self.webhook_event_repo = webhook_event_repo or WebhookEventRepository(self.payment_repo._conn)
        self._order_service = order_service

    @property
    def order_service(self):
        """Lazily initialize OrderService to avoid circular dependency."""
        if self._order_service is None:
            from app.services.order_service import OrderService

            self._order_service = OrderService(
                order_repo=self.order_repo,
                conn=self.order_repo._conn,
            )
        return self._order_service

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

    def get_webhook_secret(self) -> str:
        """Retrieve and validate the Stripe test webhook signing secret.

        Raises:
            RuntimeError: If the webhook secret is not configured.
            ValueError: If a live secret key is detected.
        """
        secret = current_app.config.get("STRIPE_WEBHOOK_SECRET")
        if not secret or not str(secret).strip():
            raise RuntimeError("Stripe webhook secret is not configured.")

        cleaned = str(secret).strip()
        if cleaned.startswith("whsec_live_"):
            raise ValueError(
                "Live Stripe credentials are prohibited. Only Stripe test mode webhook secrets are supported."
            )
        return cleaned

    def verify_webhook_event(self, payload: bytes, sig_header: str) -> stripe.Event:
        """Cryptographically verify the Stripe webhook signature and construct the event.

        Args:
            payload: Exact raw request payload bytes.
            sig_header: The Stripe-Signature HTTP request header value.

        Returns:
            The verified stripe.Event instance.

        Raises:
            ValueError: If payload is invalid or live-mode is detected.
            stripe.error.SignatureVerificationError: If signature check fails or timestamp expired.
        """
        secret = self.get_webhook_secret()
        event = stripe.Webhook.construct_event(
            payload=payload,
            sig_header=sig_header,
            secret=secret,
            tolerance=300,
        )

        is_live = False
        if isinstance(event, dict):
            is_live = bool(event.get("livemode"))
        else:
            is_live = bool(getattr(event, "livemode", False))

        if is_live:
            raise ValueError("Livemode events are prohibited. Only Stripe test mode is supported.")

        return event

    def process_webhook_event(self, event: stripe.Event | dict) -> tuple[bool, str]:
        """Dispatch and process a verified Stripe webhook event idempotently.

        Args:
            event: Verified stripe.Event or dictionary representation.

        Returns:
            Tuple of (success: bool, message: str).
        """
        event_id = event["id"] if isinstance(event, dict) else event.id
        event_type = event["type"] if isinstance(event, dict) else event.type

        data_payload = event["data"]["object"] if isinstance(event, dict) else event.data.object
        if hasattr(data_payload, "to_dict"):
            session = data_payload.to_dict()
        elif isinstance(data_payload, dict):
            session = data_payload
        else:
            session = dict(data_payload)

        if event_type == "checkout.session.completed":
            return self._handle_checkout_session_completed(session, event_id)
        elif event_type == "checkout.session.async_payment_succeeded":
            return self._handle_async_payment_succeeded(session, event_id)
        elif event_type == "checkout.session.async_payment_failed":
            return self._handle_async_payment_failed(session, event_id)
        elif event_type == "checkout.session.expired":
            return self._handle_checkout_session_expired(session, event_id)
        else:
            logger.info("Ignoring unhandled Stripe event type '%s' for event %s", event_type, event_id)
            self.webhook_event_repo.mark_ignored(event_id, reason=f"Unhandled event type '{event_type}'")
            return True, f"Event '{event_type}' acknowledged and ignored"

    def _validate_and_resolve_session(
        self, session: dict
    ) -> tuple[Optional[Payment], Optional[Order]]:
        """Validate core session constraints and resolve associated payment and order entities.

        Raises:
            ValueError: If session structure, mode, currency, or identifiers are invalid or inconsistent.
        """
        session_id = session.get("id")
        if not session_id or not str(session_id).strip().startswith("cs_"):
            raise ValueError(f"Invalid or missing Stripe Checkout Session ID: '{session_id}'")

        mode = session.get("mode")
        if mode != "payment":
            raise ValueError(f"Invalid checkout session mode '{mode}'. Only 'payment' is supported.")

        currency = session.get("currency", "").strip().lower()
        if currency != "eur":
            raise ValueError(f"Invalid currency '{currency}'. Only 'eur' is supported.")

        amount_total = session.get("amount_total")
        if amount_total is None or not isinstance(amount_total, int) or amount_total <= 0:
            raise ValueError(f"Invalid amount_total '{amount_total}'. Must be positive integer cents.")

        # Payment intent identifier validation when present
        payment_intent_id = session.get("payment_intent")
        if payment_intent_id is not None:
            if not isinstance(payment_intent_id, str) or not payment_intent_id.strip().startswith("pi_"):
                raise ValueError(f"Invalid payment_intent identifier: '{payment_intent_id}'")

        # Lookup payment attempt by session ID
        payment = self.payment_repo.get_by_stripe_session_id(str(session_id).strip())
        order = None

        if payment is not None:
            order = self.order_repo.get_by_id(payment.order_id)
            if order is None:
                raise ValueError(
                    f"Order #{payment.order_id} referenced by payment #{payment.id} does not exist."
                )
        else:
            # Ambiguous checkout attempt recovery (Section 8)
            payment, order = self._reconcile_unmatched_session(session)
            if payment is None or order is None:
                return None, None

        # Verify amounts and currencies match exactly
        if payment.amount != amount_total:
            raise ValueError(
                f"Amount mismatch: payment #{payment.id} recorded {payment.amount} cents, but session specifies {amount_total} cents."
            )
        if order.total != amount_total:
            raise ValueError(
                f"Amount mismatch: order #{order.id} total {order.total} cents, but session specifies {amount_total} cents."
            )
        if payment.currency.lower() != currency:
            raise ValueError(
                f"Currency mismatch: payment currency '{payment.currency}' != session currency '{currency}'."
            )

        # Verify client_reference_id consistency if present
        client_ref = session.get("client_reference_id")
        if client_ref is not None and str(client_ref).strip() != str(order.id):
            raise ValueError(
                f"client_reference_id mismatch: expected '{order.id}', got '{client_ref}'."
            )

        # Verify metadata order_id consistency if present
        metadata = session.get("metadata") or {}
        meta_order_id = metadata.get("order_id")
        if meta_order_id is not None and str(meta_order_id).strip() != str(order.id):
            raise ValueError(
                f"metadata.order_id mismatch: expected '{order.id}', got '{meta_order_id}'."
            )

        # Verify payment method is stripe
        if payment.payment_method.lower() != "stripe":
            raise ValueError(
                f"Invalid payment method: payment #{payment.id} has method '{payment.payment_method}', expected 'stripe'."
            )
        if order.payment_method.lower() != "stripe":
            raise ValueError(
                f"Invalid order payment method: order #{order.id} has method '{order.payment_method}', expected 'stripe'."
            )

        return payment, order

    def _reconcile_unmatched_session(
        self, session: dict
    ) -> tuple[Optional[Payment], Optional[Order]]:
        """Attempt safe recovery of an unmatched Stripe Checkout Session.

        Requires agreement between client_reference_id and metadata.order_id,
        exact order total match, exact currency match, and exactly one
        unambiguous candidate pending payment attempt.
        """
        client_ref = session.get("client_reference_id")
        metadata = session.get("metadata") or {}
        meta_order_id = metadata.get("order_id")

        if not client_ref or not meta_order_id:
            logger.warning("Reconciliation failed: missing client_reference_id or metadata.order_id.")
            return None, None

        clean_client_ref = str(client_ref).strip()
        clean_meta_id = str(meta_order_id).strip()
        if clean_client_ref != clean_meta_id:
            logger.warning(
                "Reconciliation failed: client_reference_id '%s' != metadata.order_id '%s'.",
                clean_client_ref,
                clean_meta_id,
            )
            return None, None

        try:
            order_id = int(clean_client_ref)
        except (ValueError, TypeError):
            return None, None

        order = self.order_repo.get_by_id(order_id)
        if order is None:
            logger.warning("Reconciliation failed: order #%s not found.", order_id)
            return None, None

        amount_total = session.get("amount_total")
        currency = session.get("currency", "").strip().lower()

        if order.total != amount_total:
            logger.warning(
                "Reconciliation failed: order #%s total %s != session amount %s.",
                order_id,
                order.total,
                amount_total,
            )
            return None, None

        if order.payment_method.lower() != "stripe":
            logger.warning("Reconciliation failed: order #%s has payment method '%s', expected 'stripe'.", order_id, order.payment_method)
            return None, None

        payments = self.payment_repo.get_by_order_id(order_id)
        candidates = [
            p
            for p in payments
            if p.amount == amount_total
            and p.currency.lower() == currency
            and p.status == PaymentStatus.PENDING
            and p.payment_method.lower() == "stripe"
            and (p.stripe_session_id is None or p.stripe_session_id == session.get("id"))
        ]

        if len(candidates) != 1:
            logger.warning(
                "Reconciliation failed: found %s matching pending payments for order #%s (must be exactly 1).",
                len(candidates),
                order_id,
            )
            return None, None

        payment = candidates[0]
        session_id = session.get("id")

        with transaction(self.payment_repo.conn):
            self.payment_repo.update_stripe_session_id(payment.id, session_id)
            self.order_repo.update_payment_status(
                order.id, order.payment_status, stripe_session_id=session_id
            )

        payment.stripe_session_id = session_id
        order.stripe_session_id = session_id
        logger.info(
            "Successfully reconciled Stripe session %s with payment #%s for order #%s.",
            session_id,
            payment.id,
            order.id,
        )
        return payment, order

    def _handle_checkout_session_completed(
        self, session: dict, event_id: str
    ) -> tuple[bool, str]:
        """Process 'checkout.session.completed' webhook event."""
        payment, order = self._validate_and_resolve_session(session)
        if payment is None or order is None:
            return False, f"Could not match session {session.get('id')} to an order."

        payment_status = session.get("payment_status")
        payment_intent_id = session.get("payment_intent")

        if payment_status == "paid":
            with transaction(self.payment_repo.conn):
                fresh_order = self.order_repo.get_by_id(order.id)
                fresh_payment = self.payment_repo.get_by_id(payment.id)
                if fresh_order is None or fresh_payment is None:
                    return False, "Order or payment record missing."

                # Idempotency check: if payment is already PAID, leave state unchanged
                if fresh_payment.status == PaymentStatus.PAID:
                    self.webhook_event_repo.mark_processed(
                        event_id, order_id=fresh_order.id, payment_id=fresh_payment.id
                    )
                    return True, "Payment already marked PAID."

                if fresh_payment.status in (PaymentStatus.FAILED, PaymentStatus.CANCELLED):
                    return False, f"Payment #{fresh_payment.id} already {fresh_payment.status.value}; reconciliation required."

                self.payment_repo.update_status(
                    fresh_payment.id,
                    PaymentStatus.PAID,
                    stripe_payment_intent_id=payment_intent_id,
                )
                self.order_repo.update_payment_status(fresh_order.id, PaymentStatus.PAID)
                if fresh_order.status == OrderStatus.PENDING:
                    self.order_repo.update_status(fresh_order.id, OrderStatus.CONFIRMED)
                self.webhook_event_repo.mark_processed(
                    event_id, order_id=fresh_order.id, payment_id=fresh_payment.id
                )

            logger.info("Order #%s and payment #%s marked PAID via webhook.", fresh_order.id, fresh_payment.id)
            return True, "Payment marked PAID and order confirmed."
        else:
            # Asynchronous or unpaid session: preserve pending status
            with transaction(self.payment_repo.conn):
                self.webhook_event_repo.mark_processed(
                    event_id, order_id=order.id, payment_id=payment.id
                )
            logger.info(
                "Session %s completed with payment_status='%s'. Preserved PENDING.",
                session.get("id"),
                payment_status,
            )
            return True, f"Session completed with payment_status='{payment_status}'; preserved PENDING."

    def _handle_async_payment_succeeded(
        self, session: dict, event_id: str
    ) -> tuple[bool, str]:
        """Process 'checkout.session.async_payment_succeeded' webhook event."""
        payment, order = self._validate_and_resolve_session(session)
        if payment is None or order is None:
            return False, f"Could not match session {session.get('id')} to an order."

        payment_intent_id = session.get("payment_intent")

        with transaction(self.payment_repo.conn):
            fresh_order = self.order_repo.get_by_id(order.id)
            fresh_payment = self.payment_repo.get_by_id(payment.id)
            if fresh_order is None or fresh_payment is None:
                return False, "Order or payment record missing."

            if fresh_payment.status == PaymentStatus.PAID:
                self.webhook_event_repo.mark_processed(
                    event_id, order_id=fresh_order.id, payment_id=fresh_payment.id
                )
                return True, "Payment already marked PAID."

            if fresh_payment.status in (PaymentStatus.FAILED, PaymentStatus.CANCELLED):
                return False, f"Payment #{fresh_payment.id} already {fresh_payment.status.value}; reconciliation required."

            self.payment_repo.update_status(
                fresh_payment.id,
                PaymentStatus.PAID,
                stripe_payment_intent_id=payment_intent_id,
            )
            self.order_repo.update_payment_status(fresh_order.id, PaymentStatus.PAID)
            if fresh_order.status == OrderStatus.PENDING:
                self.order_repo.update_status(fresh_order.id, OrderStatus.CONFIRMED)
            self.webhook_event_repo.mark_processed(
                event_id, order_id=fresh_order.id, payment_id=fresh_payment.id
            )

        logger.info("Order #%s marked PAID via async_payment_succeeded.", fresh_order.id)
        return True, "Async payment succeeded; marked PAID."

    def _handle_async_payment_failed(
        self, session: dict, event_id: str
    ) -> tuple[bool, str]:
        """Process 'checkout.session.async_payment_failed' webhook event."""
        payment, order = self._validate_and_resolve_session(session)
        if payment is None or order is None:
            return False, f"Could not match session {session.get('id')} to an order."

        with transaction(self.payment_repo.conn):
            fresh_order = self.order_repo.get_by_id(order.id)
            fresh_payment = self.payment_repo.get_by_id(payment.id)
            if fresh_order is None or fresh_payment is None:
                return False, "Order or payment record missing."

            # Invariant: PAID must never regress to FAILED or CANCELLED
            if fresh_payment.status == PaymentStatus.PAID or fresh_order.payment_status == PaymentStatus.PAID:
                logger.warning(
                    "Received async_payment_failed for already PAID order #%s. Protected.",
                    fresh_order.id,
                )
                self.webhook_event_repo.mark_processed(
                    event_id, order_id=fresh_order.id, payment_id=fresh_payment.id
                )
                return True, "Paid order cannot regress to failed."

            self.payment_repo.update_status(fresh_payment.id, PaymentStatus.FAILED)
            if fresh_order.status != OrderStatus.CANCELLED:
                self.order_service.cancel_order(fresh_order.id)
            self.order_repo.update_payment_status(fresh_order.id, PaymentStatus.FAILED)
            self.webhook_event_repo.mark_processed(
                event_id, order_id=fresh_order.id, payment_id=fresh_payment.id
            )

        logger.info("Order #%s cancelled and inventory restored due to async payment failure.", fresh_order.id)
        return True, "Async payment failed; order cancelled and inventory released."

    def _handle_checkout_session_expired(
        self, session: dict, event_id: str
    ) -> tuple[bool, str]:
        """Process 'checkout.session.expired' webhook event."""
        payment, order = self._validate_and_resolve_session(session)
        if payment is None or order is None:
            return False, f"Could not match session {session.get('id')} to an order."

        with transaction(self.payment_repo.conn):
            fresh_order = self.order_repo.get_by_id(order.id)
            fresh_payment = self.payment_repo.get_by_id(payment.id)
            if fresh_order is None or fresh_payment is None:
                return False, "Order or payment record missing."

            # Invariant: PAID must never regress to CANCELLED
            if fresh_payment.status == PaymentStatus.PAID or fresh_order.payment_status == PaymentStatus.PAID:
                logger.warning(
                    "Received checkout.session.expired for already PAID order #%s. Protected.",
                    fresh_order.id,
                )
                self.webhook_event_repo.mark_processed(
                    event_id, order_id=fresh_order.id, payment_id=fresh_payment.id
                )
                return True, "Paid order cannot be expired."

            self.payment_repo.update_status(fresh_payment.id, PaymentStatus.CANCELLED)
            if fresh_order.status != OrderStatus.CANCELLED:
                self.order_service.cancel_order(fresh_order.id)
            self.order_repo.update_payment_status(fresh_order.id, PaymentStatus.CANCELLED)
            self.webhook_event_repo.mark_processed(
                event_id, order_id=fresh_order.id, payment_id=fresh_payment.id
            )

        logger.info("Order #%s cancelled and inventory released due to session expiration.", fresh_order.id)
        return True, "Session expired; order cancelled and inventory released."
        return True, "Session expired; order cancelled and inventory released."

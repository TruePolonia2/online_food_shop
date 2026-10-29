"""Stripe webhook routes for handling asynchronous payment events (TEST MODE ONLY)."""
import logging
from flask import Blueprint, current_app, jsonify, request
import stripe

from app.csrf import csrf_exempt
from app.models.webhook_event import WebhookEventStatus
from app.repositories.webhook_event_repository import WebhookEventRepository
from app.services.stripe_service import StripeService

logger = logging.getLogger(__name__)

webhook_bp = Blueprint("webhook", __name__)


@webhook_bp.route("/webhooks/stripe", methods=["POST"])
@csrf_exempt
def stripe_webhook():
    """Handle incoming Stripe webhook events with cryptographic signature verification.

    Exempt exclusively from CSRF verification because external payment provider webhooks
    authenticate using cryptographic HMAC-SHA256 signatures in the Stripe-Signature header.
    Never relies on session cookies or browser credentials.
    """
    stripe_service = StripeService()

    # 1. Verify that Stripe webhook signing secret is configured
    try:
        stripe_service.get_webhook_secret()
    except (RuntimeError, ValueError) as exc:
        logger.error("Stripe webhook received but webhook secret is unconfigured or invalid: %s", exc)
        return jsonify({"error": "Webhook secret is not configured"}), 503

    # 2. Extract raw request payload and signature header
    payload = request.get_data()
    sig_header = request.headers.get("Stripe-Signature", "").strip()
    if not sig_header:
        logger.warning("Stripe webhook rejected: Missing Stripe-Signature header.")
        return jsonify({"error": "Missing Stripe-Signature header"}), 400

    # 3. Cryptographically verify webhook signature
    try:
        event = stripe_service.verify_webhook_event(payload=payload, sig_header=sig_header)
    except ValueError as exc:
        logger.warning("Stripe webhook rejected: %s", exc)
        return jsonify({"error": str(exc)}), 400
    except stripe.error.SignatureVerificationError as exc:
        logger.warning("Stripe webhook signature verification failed: %s", exc)
        return jsonify({"error": "Invalid or expired webhook signature"}), 400
    except Exception as exc:
        logger.error("Unexpected error during Stripe webhook verification: %s", exc)
        return jsonify({"error": "Webhook verification failed"}), 400

    # 4. Strict test-mode check
    is_live = bool(event.get("livemode")) if isinstance(event, dict) else bool(getattr(event, "livemode", False))
    if is_live:
        logger.error("Live-mode Stripe event rejected: %s", event.get("id"))
        return jsonify({"error": "Livemode events are prohibited"}), 400

    event_id = event["id"] if isinstance(event, dict) else event.id
    event_type = event["type"] if isinstance(event, dict) else event.type

    # 5. Idempotency management & lease acquisition via database
    webhook_event_repo = WebhookEventRepository()
    try:
        acquired, event_record = webhook_event_repo.record_processing(
            event_id=event_id,
            event_type=event_type,
            livemode=False,
            payload_summary=f"type={event_type}",
        )
    except Exception as exc:
        logger.warning("Database error during webhook idempotency check: %s", exc)
        return jsonify({"error": "Service temporarily unavailable"}), 503

    if not acquired:
        if event_record.status == WebhookEventStatus.PROCESSED:
            logger.info("Event %s already processed. Returning HTTP 200.", event_id)
            return jsonify({"status": "already_processed", "event_id": event_id}), 200
        elif event_record.status == WebhookEventStatus.IGNORED:
            return jsonify({"status": "ignored", "event_id": event_id}), 200
        elif event_record.status == WebhookEventStatus.PROCESSING:
            # Active in-flight execution by another worker
            logger.info("Event %s is currently being processed concurrently.", event_id)
            response = jsonify({
                "status": "processing",
                "event_id": event_id,
                "message": "Event is currently processing",
            })
            response.status_code = 503
            response.headers["Retry-After"] = "5"
            return response

    # 6. Process the event and update payment/order state atomically
    try:
        success, message = stripe_service.process_webhook_event(event)
        if success:
            saved_event = webhook_event_repo.get_by_event_id(event_id)
            status_val = "ignored" if (saved_event and saved_event.status == WebhookEventStatus.IGNORED) else "success"
            return jsonify({"status": status_val, "event_id": event_id, "message": message}), 200
        else:
            # Processing could not safely complete (e.g. order not found yet or reconciliation needed)
            logger.warning("Event %s processing incomplete: %s", event_id, message)
            webhook_event_repo.mark_failed(event_id, message)
            response = jsonify({
                "status": "reconciliation_needed",
                "event_id": event_id,
                "message": message,
            })
            response.status_code = 503
            response.headers["Retry-After"] = "15"
            return response
    except ValueError as exc:
        # Data integrity or mismatch error
        logger.warning("Data verification error processing event %s: %s", event_id, exc)
        webhook_event_repo.mark_failed(event_id, str(exc))
        return jsonify({"status": "verification_failed", "event_id": event_id, "error": str(exc)}), 400
    except Exception as exc:
        # Transient database or server error: return 500 so Stripe retries
        logger.exception("Server error processing Stripe webhook event %s: %s", event_id, exc)
        webhook_event_repo.mark_failed(event_id, str(exc))
        return jsonify({"error": "Internal error processing webhook"}), 500

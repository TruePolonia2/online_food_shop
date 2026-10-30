"""Order and checkout routes."""
import hashlib
import logging
import sqlite3
import uuid

from flask import Blueprint, abort, flash, redirect, render_template, request, session, url_for

from app.config import is_stripe_configured
from app.database.connection import transaction
from app.models.cart import Cart
from app.models.order import OrderStatus
from app.models.payment import Payment, PaymentMethod, PaymentStatus
from app.repositories.order_repository import OrderRepository
from app.repositories.payment_repository import PaymentRepository
from app.routes.auth import get_current_user
from app.routes.cart_session import clear_cart_session, load_cart_from_session
from app.services.cart_service import CartService
from app.services.customer_service import CustomerService
from app.services.order_service import OrderService
from app.services.product_service import ProductService
from app.services.stripe_service import (
    StripeAmbiguousOutcomeError,
    StripeDefinitiveError,
    StripePaymentError,
    StripeService,
)

logger = logging.getLogger(__name__)

order_bp = Blueprint("order", __name__)


def _compute_cart_fingerprint(cart: Cart) -> str:
    """Compute a deterministic SHA-256 fingerprint of cart items, quantities, and pricing."""
    items_repr = [
        f"{item.product.id}:{item.quantity}:{item.product.price}"
        for item in sorted(cart.items, key=lambda x: x.product.id or 0)
    ]
    raw = f"{','.join(items_repr)}|total={cart.total}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


@order_bp.route("/checkout", methods=["GET"])
def checkout_form():
    """Display checkout form with customer inputs and order summary."""
    cart_service = CartService()
    product_service = ProductService()

    cart = load_cart_from_session(cart_service, product_service)
    if cart.is_empty:
        flash("Your cart is empty.", "warning")
        return redirect(url_for("cart.view_cart"))

    validation_errors = cart_service.validate_cart(cart)
    if validation_errors:
        for err in validation_errors:
            flash(err, "error")
        return redirect(url_for("cart.view_cart"))

    # Establish or reuse stable checkout token bound to cart fingerprint
    current_fingerprint = _compute_cart_fingerprint(cart)
    saved_token = session.get("checkout_token")
    saved_fingerprint = session.get("cart_fingerprint")

    # If saved token references an order that is already completed or cancelled, rotate token
    if saved_token:
        order_service = OrderService()
        existing_order = order_service.get_by_checkout_token(saved_token)
        if existing_order and existing_order.status in (OrderStatus.CANCELLED, OrderStatus.CONFIRMED):
            saved_token = None

    if not saved_token or saved_fingerprint != current_fingerprint:
        checkout_token = uuid.uuid4().hex
        session["checkout_token"] = checkout_token
        session["cart_fingerprint"] = current_fingerprint
        session.modified = True
    else:
        checkout_token = saved_token

    return render_template("order/checkout.html", cart=cart, checkout_token=checkout_token)


@order_bp.route("/checkout", methods=["POST"])
def process_checkout():
    """Validate customer information and execute atomic order checkout."""
    cart_service = CartService()
    product_service = ProductService()
    customer_service = CustomerService()
    order_service = OrderService()

    name = request.form.get("name", "").strip()
    email = request.form.get("email", "").strip().lower()
    phone = request.form.get("phone", "").strip() or None
    address = request.form.get("address", "").strip() or None

    if not name:
        flash("Customer name is required.", "error")
        return redirect(url_for("order.checkout_form"))

    if not email or "@" not in email:
        flash("A valid email address is required.", "error")
        return redirect(url_for("order.checkout_form"))

    submitted_token = request.form.get("checkout_token", "").strip()
    cart = load_cart_from_session(cart_service, product_service)
    if cart.is_empty:
        # Rapid duplicate submission handling: if cart was already cleared by a concurrent
        # request for this checkout token, redirect cleanly to the existing session/order.
        if submitted_token:
            existing_order = order_service.get_by_checkout_token(submitted_token)
            if existing_order is not None:
                if existing_order.payment_method == "stripe" and existing_order.stripe_session_id:
                    payment_repo = PaymentRepository()
                    stripe_service = StripeService(payment_repo=payment_repo)
                    try:
                        existing_session = stripe_service.retrieve_checkout_session(
                            existing_order.stripe_session_id
                        )
                        if existing_session and getattr(existing_session, "url", None):
                            session["last_order_id"] = existing_order.id
                            session["stripe_session_id"] = existing_session.id
                            session.modified = True
                            return redirect(existing_session.url)
                    except Exception:
                        pass
                elif existing_order.payment_method == "standard":
                    session["last_order_id"] = existing_order.id
                    session.modified = True
                    return redirect(url_for("order.order_confirmation", order_id=existing_order.id))

        flash("Cannot checkout with an empty cart.", "warning")
        return redirect(url_for("cart.view_cart"))

    validation_errors = cart_service.validate_cart(cart)
    if validation_errors:
        for err in validation_errors:
            flash(err, "error")
        return redirect(url_for("cart.view_cart"))

    # Resolve customer: reuse existing record without mutation, or register new customer
    customer = customer_service.get_by_email(email)
    if customer is None:
        try:
            customer = customer_service.register_customer(
                name=name, email=email, phone=phone, address=address
            )
        except ValueError as err:
            # Handle concurrent conflict safely: if another request registered the email, reuse it
            customer = customer_service.get_by_email(email)
            if customer is None:
                flash(str(err), "error")
                return redirect(url_for("order.checkout_form"))

    if customer.id is None:
        flash("Customer account could not be resolved.", "error")
        return redirect(url_for("order.checkout_form"))

    # Resolve authenticated user identity strictly from database via get_current_user()
    current_user = get_current_user()
    authenticated_user_id = current_user.id if current_user is not None else None

    # Resolve and validate checkout token bound to current cart
    submitted_token = request.form.get("checkout_token", "").strip()
    current_fingerprint = _compute_cart_fingerprint(cart)
    saved_token = session.get("checkout_token")
    saved_fingerprint = session.get("cart_fingerprint")

    is_valid_token = (
        bool(submitted_token)
        and submitted_token == saved_token
        and saved_fingerprint == current_fingerprint
    )

    if is_valid_token:
        checkout_token = submitted_token
    else:
        checkout_token = uuid.uuid4().hex
        session["checkout_token"] = checkout_token
        session["cart_fingerprint"] = current_fingerprint
        session.modified = True

    # Resolve payment method (standard vs stripe)
    payment_method = request.form.get("payment_method", "standard").strip().lower()
    if payment_method not in ("standard", "stripe"):
        flash("Invalid payment method selected.", "error")
        return redirect(url_for("order.checkout_form"))

    if payment_method == "stripe":
        if not is_stripe_configured():
            flash(
                "Stripe card payment is not currently available. Please select standard checkout.",
                "error",
            )
            return redirect(url_for("order.checkout_form"))
        if cart.total < 50:
            flash(
                "Order total must be at least 0.50 € to process card payments.",
                "error",
            )
            return redirect(url_for("order.checkout_form"))

        payment_repo = PaymentRepository()
        order_repo = OrderRepository()
        stripe_service = StripeService(payment_repo=payment_repo, order_repo=order_repo)
        idempotency_key = f"checkout_{checkout_token}"

        # Check if an existing order was already initiated for this checkout attempt
        order = order_service.get_by_checkout_token(checkout_token) if is_valid_token else None

        # A cancelled or confirmed order must NEVER be reused for a new checkout attempt
        if order is not None and order.status in (OrderStatus.CANCELLED, OrderStatus.CONFIRMED):
            checkout_token = uuid.uuid4().hex
            session["checkout_token"] = checkout_token
            session["cart_fingerprint"] = current_fingerprint
            session.modified = True
            idempotency_key = f"checkout_{checkout_token}"
            order = None

        # Verify order total, items, and customer details match before reusing pending order
        if order is not None:
            customer_changed = (
                order.customer_name != name
                or order.customer_email != email
                or order.customer_phone != phone
                or order.customer_address != address
            )
            cart_items_match = (
                len(order.items) == len(cart.items)
                and all(
                    any(
                        oi.product_id == ci.product.id
                        and oi.quantity == ci.quantity
                        and oi.unit_price == ci.product.price
                        for ci in cart.items
                    )
                    for oi in order.items
                )
            )
            if order.total != cart.total or customer_changed or not cart_items_match:
                # Cart contents, totals, or customer details changed between attempts!
                # Before replacing, check whether Stripe already created a Checkout Session
                existing_session = stripe_service.find_session_for_order(order.id)
                if existing_session and (
                    getattr(existing_session, "payment_status", None) == "paid"
                    or getattr(existing_session, "status", None) == "complete"
                ):
                    # Order was already paid! Reconcile and redirect to success
                    clear_cart_session()
                    session["last_order_id"] = order.id
                    session["stripe_session_id"] = existing_session.id
                    session.modified = True
                    flash("Your order has already been paid and confirmed.", "info")
                    return redirect(url_for("order.stripe_success", session_id=existing_session.id))
                elif existing_session and getattr(existing_session, "status", None) == "open":
                    # Expire old session at Stripe so customer cannot pay on outdated cart
                    try:
                        stripe_service.expire_checkout_session(existing_session.id)
                    except Exception:
                        pass
                    order_service.cancel_order(order.id)
                else:
                    # No session at Stripe: safely cancel old pending order to release its reserved inventory
                    order_service.cancel_order(order.id)

                checkout_token = uuid.uuid4().hex
                session["checkout_token"] = checkout_token
                session["cart_fingerprint"] = current_fingerprint
                session.modified = True
                idempotency_key = f"checkout_{checkout_token}"
                order = None

        cart_backup = dict(session.get("cart", {}))

        if order is None:
            try:
                order = order_service.checkout(
                    customer_id=customer.id,
                    cart=cart,
                    customer_name=name,
                    customer_email=email,
                    customer_phone=phone,
                    customer_address=address,
                    user_id=authenticated_user_id,
                    payment_method="stripe",
                    payment_status=PaymentStatus.PENDING,
                    checkout_token=checkout_token,
                )
            except sqlite3.IntegrityError:
                # Concurrent race condition: another request created the order with this checkout_token
                order = order_service.get_by_checkout_token(checkout_token)
                if order is None:
                    flash("Checkout could not be completed. Please try again.", "error")
                    return redirect(url_for("order.checkout_form"))
            except ValueError as err:
                flash(f"Checkout could not be completed: {err}", "error")
                return redirect(url_for("cart.view_cart"))

        # Look up or create idempotent Payment attempt
        payment = payment_repo.get_by_idempotency_key(idempotency_key)
        if payment is None and order is not None and order.id is not None:
            existing_payments = payment_repo.get_by_order_id(order.id)
            if existing_payments:
                payment = existing_payments[0]

        if payment is None:
            try:
                payment = payment_repo.create(
                    Payment(
                        order_id=order.id,
                        amount=order.total,
                        currency="eur",
                        payment_method="stripe",
                        status=PaymentStatus.PENDING,
                        idempotency_key=idempotency_key,
                    )
                )
            except sqlite3.IntegrityError:
                payment = payment_repo.get_by_idempotency_key(idempotency_key)

        # Before calling create, check if a valid Stripe session is already associated or exists at Stripe
        existing_session = None
        if payment and payment.stripe_session_id:
            try:
                existing_session = stripe_service.retrieve_checkout_session(payment.stripe_session_id)
            except Exception:
                existing_session = None

        if not existing_session and order is not None and order.id is not None:
            existing_session = stripe_service.find_session_for_order(order.id)

        if existing_session and getattr(existing_session, "status", None) in ("open", "complete"):
            if (
                getattr(existing_session, "payment_status", None) == "paid"
                or getattr(existing_session, "status", None) == "complete"
            ):
                with transaction(payment_repo.conn):
                    if payment and payment.id is not None:
                        payment_repo.update_status(payment.id, PaymentStatus.PAID)
                    order_repo.update_payment_status(
                        order.id, PaymentStatus.PAID, stripe_session_id=existing_session.id
                    )
                    order_repo.update_status(order.id, OrderStatus.CONFIRMED)
                clear_cart_session()
                session["last_order_id"] = order.id
                session["stripe_session_id"] = existing_session.id
                session.modified = True
                return redirect(url_for("order.stripe_success", session_id=existing_session.id))
            elif getattr(existing_session, "url", None):
                clear_cart_session()
                session["last_order_id"] = order.id
                session["stripe_session_id"] = existing_session.id
                session.modified = True
                return redirect(existing_session.url)

        # Create Stripe Checkout Session (idempotent at Stripe API via idempotency_key)
        try:
            checkout_session = stripe_service.create_checkout_session(order=order, payment=payment)
        except StripeDefinitiveError as exc:
            # Definitive failure: mark payment failed, cancel order to release inventory, restore cart
            if payment and payment.id is not None:
                payment_repo.update_status(payment.id, PaymentStatus.FAILED)
            order_service.cancel_order(order.id)
            session["cart"] = cart_backup
            # Rotate checkout_token so subsequent retry does not reuse the cancelled order or failed idempotency key
            session["checkout_token"] = uuid.uuid4().hex
            session.modified = True
            flash(f"Unable to initiate Stripe payment session: {exc}", "error")
            return redirect(url_for("order.checkout_form"))
        except (StripeAmbiguousOutcomeError, Exception) as exc:
            # Network timeout or ambiguous outcome: DO NOT cancel order or release inventory!
            # Preserve pending status and cart backup so user can retry safely.
            session["cart"] = cart_backup
            session["last_order_id"] = order.id
            session.modified = True
            logger.warning(
                "Stripe session creation timeout or ambiguous error for order %s: %s",
                order.id,
                exc,
            )
            flash(
                "A network timeout occurred while connecting to the payment provider. "
                "Your order reservation has been saved. Please retry to complete your payment.",
                "error",
            )
            return redirect(url_for("order.checkout_form"))

        clear_cart_session()
        session["last_order_id"] = order.id
        session["stripe_session_id"] = checkout_session.id
        session.modified = True
        return redirect(checkout_session.url)

    # Standard (Pay on Delivery) checkout flow
    existing_std_order = order_service.get_by_checkout_token(checkout_token) if is_valid_token else None
    if existing_std_order is not None and existing_std_order.status in (OrderStatus.CANCELLED, OrderStatus.CONFIRMED):
        checkout_token = uuid.uuid4().hex
        session["checkout_token"] = checkout_token
        session["cart_fingerprint"] = current_fingerprint
        session.modified = True

    try:
        order = order_service.checkout(
            customer_id=customer.id,
            cart=cart,
            customer_name=name,
            customer_email=email,
            customer_phone=phone,
            customer_address=address,
            user_id=authenticated_user_id,
            payment_method="standard",
            payment_status=PaymentStatus.UNPAID,
            checkout_token=checkout_token,
        )
    except sqlite3.IntegrityError:
        # Rapid double-submission: reuse order created by identical checkout token
        order = order_service.get_by_checkout_token(checkout_token)
        if order is None:
            flash("Checkout could not be completed. Please try again.", "error")
            return redirect(url_for("cart.view_cart"))
    except ValueError as err:
        flash(f"Checkout could not be completed: {err}", "error")
        # On checkout failure, cart contents are preserved in session
        return redirect(url_for("cart.view_cart"))

    # Clear cart and authorize confirmation view only after successful transaction
    clear_cart_session()
    session["last_order_id"] = order.id
    session.modified = True
    flash("Your order has been placed successfully!", "success")

    return redirect(url_for("order.order_confirmation", order_id=order.id))


@order_bp.route("/order/stripe/success")
def stripe_success():
    """Display confirmation page following Stripe Hosted Checkout redirect."""
    session_id = request.args.get("session_id", "").strip()
    if not session_id:
        abort(404)

    payment_repo = PaymentRepository()
    payment = payment_repo.get_by_stripe_session_id(session_id)
    if payment is None:
        abort(404)

    order_service = OrderService()
    order = order_service.get_by_id(payment.order_id)
    if order is None:
        abort(404)

    # Authorization check
    current_user = get_current_user()
    if order.user_id is not None:
        if current_user is None or current_user.id != order.user_id:
            abort(404)
    elif session.get("last_order_id") != order.id:
        abort(404)

    # Note: A browser redirect NEVER marks an order as paid.
    # Payment status remains pending until server-side webhook verification in Phase 10.13.
    return render_template(
        "order/stripe_success.html",
        order=order,
        payment=payment,
    )


@order_bp.route("/order/stripe/cancel")
def stripe_cancel():
    """Display cancellation page when user returns from Stripe without completing payment.

    INVARIANT: GET requests must NEVER mutate payment or order state.
    Payments and orders remain in PENDING status until an authoritative server-side
    outcome is determined via webhook events (Phase 10.13).
    """
    order_id_raw = request.args.get("order_id", "").strip()
    if not order_id_raw:
        abort(404)

    try:
        order_id = int(order_id_raw)
    except (ValueError, TypeError):
        abort(404)

    order_service = OrderService()
    order = order_service.get_by_id(order_id)
    if order is None:
        abort(404)

    # Authorization check
    current_user = get_current_user()
    if order.user_id is not None:
        if current_user is None or current_user.id != order.user_id:
            abort(404)
    elif session.get("last_order_id") != order.id:
        abort(404)

    # Safe GET semantics: No state mutations, no status updates, no stock releases.
    return render_template(
        "order/stripe_cancel.html",
        order=order,
    )


@order_bp.route("/orders/<int:order_id>")
def order_confirmation(order_id: int):
    """Display order confirmation details for the current checkout session."""
    # Enforce session access control: only the session that completed checkout can view
    if session.get("last_order_id") != order_id:
        abort(404)

    order_service = OrderService()
    order = order_service.get_by_id(order_id)
    if order is None:
        abort(404)

    # Additional authorization: prevent cross-identity confirmation access
    current_user = get_current_user()
    if order.user_id is not None:
        # If the order is owned by an authenticated user, the viewer must be that exact user
        if current_user is None or current_user.id != order.user_id:
            abort(404)
    elif current_user is not None:
        # If the order was a guest checkout, an authenticated user must not view it
        abort(404)

    return render_template(
        "order/confirmation.html",
        order=order,
    )

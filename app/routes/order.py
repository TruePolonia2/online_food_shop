"""Order and checkout routes."""
from flask import Blueprint, abort, flash, redirect, render_template, request, session, url_for

from app.routes.auth import get_current_user
from app.routes.cart_session import clear_cart_session, load_cart_from_session
from app.services.cart_service import CartService
from app.services.customer_service import CustomerService
from app.services.order_service import OrderService
from app.services.product_service import ProductService

order_bp = Blueprint("order", __name__)


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

    return render_template("order/checkout.html", cart=cart)


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

    cart = load_cart_from_session(cart_service, product_service)
    if cart.is_empty:
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

    # Execute atomic checkout with immutable delivery snapshot
    try:
        order = order_service.checkout(
            customer_id=customer.id,
            cart=cart,
            customer_name=name,
            customer_email=email,
            customer_phone=phone,
            customer_address=address,
            user_id=authenticated_user_id,
        )
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

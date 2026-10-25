"""Cart routes for managing shopping cart operations."""
from flask import Blueprint, flash, redirect, render_template, request, url_for

from app.routes.cart_session import (
    clear_cart_session,
    load_cart_from_session,
    save_cart_to_session,
)
from app.services.cart_service import CartService
from app.services.product_service import ProductService

cart_bp = Blueprint("cart", __name__)


@cart_bp.route("/")
def view_cart():
    """Display the current shopping cart contents and validation status."""
    cart_service = CartService()
    product_service = ProductService()

    cart = load_cart_from_session(cart_service, product_service)
    validation_errors = cart_service.validate_cart(cart)

    return render_template(
        "cart/view.html",
        cart=cart,
        validation_errors=validation_errors,
    )


@cart_bp.route("/add", methods=["POST"])
def add_item():
    """Add a product and quantity to the cart."""
    cart_service = CartService()
    product_service = ProductService()

    raw_pid = request.form.get("product_id")
    raw_qty = request.form.get("quantity", "1")

    try:
        product_id = int(raw_pid)  # type: ignore[arg-type]
    except (ValueError, TypeError):
        flash("Invalid product ID.", "error")
        return redirect(url_for("cart.view_cart"))

    try:
        quantity = int(raw_qty)
        if quantity <= 0:
            raise ValueError
    except (ValueError, TypeError):
        flash("Quantity must be a positive integer.", "error")
        return redirect(url_for("cart.view_cart"))

    cart = load_cart_from_session(cart_service, product_service)

    try:
        cart_service.add_item(cart, product_id=product_id, quantity=quantity)
        save_cart_to_session(cart)
        flash("Item added to cart.", "success")
    except ValueError as err:
        flash(str(err), "error")

    return redirect(url_for("cart.view_cart"))


@cart_bp.route("/update", methods=["POST"])
def update_item():
    """Update quantity for an item in the cart."""
    cart_service = CartService()
    product_service = ProductService()

    raw_pid = request.form.get("product_id")
    raw_qty = request.form.get("quantity")

    try:
        product_id = int(raw_pid)  # type: ignore[arg-type]
    except (ValueError, TypeError):
        flash("Invalid product ID.", "error")
        return redirect(url_for("cart.view_cart"))

    try:
        quantity = int(raw_qty)  # type: ignore[arg-type]
        if quantity < 0:
            raise ValueError
    except (ValueError, TypeError):
        flash("Quantity must be zero or a positive integer.", "error")
        return redirect(url_for("cart.view_cart"))

    cart = load_cart_from_session(cart_service, product_service)

    try:
        cart_service.update_item_quantity(cart, product_id=product_id, quantity=quantity)
        save_cart_to_session(cart)
        flash("Cart updated successfully.", "success")
    except ValueError as err:
        flash(str(err), "error")

    return redirect(url_for("cart.view_cart"))


@cart_bp.route("/remove", methods=["POST"])
def remove_item():
    """Remove a product completely from the cart."""
    cart_service = CartService()
    product_service = ProductService()

    raw_pid = request.form.get("product_id")

    try:
        product_id = int(raw_pid)  # type: ignore[arg-type]
    except (ValueError, TypeError):
        flash("Invalid product ID.", "error")
        return redirect(url_for("cart.view_cart"))

    cart = load_cart_from_session(cart_service, product_service)
    cart_service.remove_item(cart, product_id=product_id)
    save_cart_to_session(cart)
    flash("Item removed from cart.", "success")

    return redirect(url_for("cart.view_cart"))


@cart_bp.route("/clear", methods=["POST"])
def clear():
    """Clear all items from the shopping cart."""
    clear_cart_session()
    flash("Cart cleared.", "success")
    return redirect(url_for("cart.view_cart"))

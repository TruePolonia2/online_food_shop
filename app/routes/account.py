"""Customer account and private order history routes."""
from flask import Blueprint, abort, render_template

from app.authorization import login_required
from app.routes.auth import get_current_user
from app.services.order_service import OrderService

account_bp = Blueprint("account", __name__, url_prefix="/account")


@account_bp.route("/orders", methods=["GET"])
@login_required
def order_history():
    """Display read-only private order history for the authenticated user.

    Only retrieves orders where orders.user_id matches the active authenticated
    user ID. Historical guest orders and orders belonging to other users are excluded.
    """
    current_user = get_current_user()
    if current_user is None or current_user.id is None:
        abort(401)

    order_service = OrderService()
    orders = order_service.get_user_orders(current_user.id)
    return render_template("account/orders.html", orders=orders)


@account_bp.route("/orders/<int:order_id>", methods=["GET"])
@login_required
def order_detail(order_id: int):
    """Display read-only details for a specific order owned by the authenticated user.

    Uses an ownership-scoped database query to guarantee that only the owner can
    retrieve the order. Nonexistent orders, guest orders, or orders belonging to
    other accounts return HTTP 404 to avoid disclosing order existence.
    """
    current_user = get_current_user()
    if current_user is None or current_user.id is None:
        abort(401)

    order_service = OrderService()
    order = order_service.get_user_order(order_id, current_user.id)
    if order is None:
        abort(404)

    return render_template("account/order_detail.html", order=order)

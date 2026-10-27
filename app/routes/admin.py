"""Admin blueprint and routes for store owner administration."""
from flask import Blueprint, redirect, render_template, url_for

from app.authorization import owner_required
from app.services.admin_service import AdminService

admin_bp = Blueprint("admin", __name__, url_prefix="/admin")


@admin_bp.route("", methods=["GET"])
@admin_bp.route("/", methods=["GET"])
@owner_required
def dashboard():
    """Display the store administration dashboard with aggregate statistics.

    Protected strictly by owner_required decorator: only authenticated, verified
    store owners can access this view.
    """
    admin_service = AdminService()
    stats = admin_service.get_dashboard_stats()
    return render_template("admin/dashboard.html", stats=stats)


@admin_bp.route("/dashboard", methods=["GET"])
@owner_required
def dashboard_alias():
    """Redirect canonical dashboard URL to avoid duplicate implementations.

    Protected strictly by owner_required decorator to ensure unverified or customer
    accounts are rejected before any redirect logic executes.
    """
    return redirect(url_for("admin.dashboard"))

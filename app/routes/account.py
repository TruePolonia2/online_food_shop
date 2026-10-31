"""Customer account and private order history routes."""
import logging
import secrets
import time
from flask import Blueprint, abort, flash, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash

from app.authorization import login_required
from app.csrf import generate_csrf_token
from app.models.user import UserRole
from app.oauth import is_google_auth_configured, oauth
from app.repositories.customer_repository import CustomerRepository
from app.routes.auth import LoginRateLimiter, get_canonical_oauth_redirect_uri, get_current_user
from app.services.auth_service import AuthService
from app.services.order_service import OrderService

logger = logging.getLogger(__name__)

account_bp = Blueprint("account", __name__, url_prefix="/account")

# Bounded in-memory sliding-window rate limiter for account deletion attempts
delete_account_rate_limiter = LoginRateLimiter(max_attempts=5, window_seconds=300, max_keys=10000)
SESSION_REAUTH_DELETE_KEY = "_reauth_delete_token"


@account_bp.route("", methods=["GET"])
@account_bp.route("/", methods=["GET"])
@account_bp.route("/settings", methods=["GET"])
@login_required
def account_settings():
    """Display authenticated user account details and settings overview."""
    current_user = get_current_user()
    if current_user is None or current_user.id is None:
        abort(401)

    customer_repo = CustomerRepository()
    customer = customer_repo.get_by_email(current_user.email)

    auth_service = AuthService()
    google_identity = auth_service.external_identity_repo.get_by_user_and_provider(
        current_user.id, "google"
    )
    is_google_linked = google_identity is not None

    order_service = OrderService()
    orders = order_service.get_user_orders(current_user.id)
    recent_orders = orders[:3]
    total_orders_count = len(orders)

    is_owner = current_user.role == UserRole.OWNER or (
        isinstance(current_user.role, str) and current_user.role == "owner"
    )

    display_name = None
    if customer and customer.name:
        display_name = customer.name.strip()
    elif current_user.email:
        local_part = current_user.email.split("@")[0].replace(".", " ").replace("_", " ").title()
        display_name = local_part

    return render_template(
        "account/settings.html",
        user=current_user,
        customer=customer,
        display_name=display_name,
        is_google_linked=is_google_linked,
        google_identity=google_identity,
        is_owner=is_owner,
        recent_orders=recent_orders,
        total_orders_count=total_orders_count,
    )


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


@account_bp.route("/delete", methods=["GET"])
@login_required
def delete_account_page():
    """Display account deletion warning and identity confirmation options."""
    current_user = get_current_user()
    if current_user is None or current_user.id is None:
        abort(401)

    if current_user.role == UserRole.OWNER or (
        isinstance(current_user.role, str) and current_user.role == "owner"
    ):
        abort(403)

    auth_service = AuthService()
    has_password = current_user.password_hash is not None
    google_identity = auth_service.external_identity_repo.get_by_user_and_provider(
        current_user.id, "google"
    )
    has_google = google_identity is not None and is_google_auth_configured()

    # Check for active Google reauthentication in server-side intent
    reauth_data = session.get(SESSION_REAUTH_DELETE_KEY)
    google_reauthenticated = False
    reauth_token = None

    if reauth_data and isinstance(reauth_data, dict):
        intent_id = reauth_data.get("intent_id") or reauth_data.get("token")
        if intent_id:
            intent = auth_service.get_account_link_intent(intent_id)
            if (
                intent is not None
                and intent.action == "delete_account"
                and intent.status == "pending"
                and not intent.is_expired
                and intent.user_id == current_user.id
            ):
                google_reauthenticated = True
                reauth_token = intent.intent_id
            else:
                session.pop(SESSION_REAUTH_DELETE_KEY, None)

    return render_template(
        "account/delete_account.html",
        user=current_user,
        has_password=has_password,
        has_google=has_google,
        google_reauthenticated=google_reauthenticated,
        reauth_token=reauth_token,
    )


@account_bp.route("/delete", methods=["POST"])
@login_required
def delete_account():
    """Permanently delete customer account after explicit identity verification."""
    current_user = get_current_user()
    if current_user is None or current_user.id is None:
        abort(401)

    if current_user.role == UserRole.OWNER or (
        isinstance(current_user.role, str) and current_user.role == "owner"
    ):
        abort(403)

    client_ip = request.remote_addr or "127.0.0.1"
    auth_service = AuthService()
    has_password = current_user.password_hash is not None
    google_identity = auth_service.external_identity_repo.get_by_user_and_provider(
        current_user.id, "google"
    )
    has_google = google_identity is not None and is_google_auth_configured()

    # Enforce rate limiting on failed deletion attempts
    if delete_account_rate_limiter.is_rate_limited(client_ip, current_user.email):
        flash("Too many failed attempts. Please wait 5 minutes before trying again.", "error")
        return (
            render_template(
                "account/delete_account.html",
                user=current_user,
                has_password=has_password,
                has_google=has_google,
                google_reauthenticated=False,
                reauth_token=None,
                rate_limit_error="Too many failed attempts. Please wait 5 minutes before trying again.",
            ),
            429,
        )

    password = request.form.get("password")
    reauth_token = request.form.get("reauth_token")
    confirmed = False

    # 1. Password confirmation path
    if password is not None and password != "":
        if not has_password:
            flash("Password confirmation is not available for this account.", "error")
            return (
                render_template(
                    "account/delete_account.html",
                    user=current_user,
                    has_password=has_password,
                    has_google=has_google,
                    google_reauthenticated=False,
                    reauth_token=None,
                    error="Password confirmation is not available for this account.",
                ),
                400,
            )

        if not check_password_hash(current_user.password_hash, password):
            delete_account_rate_limiter.record_failure(client_ip, current_user.email)
            flash(
                "Incorrect password. Please enter your valid current password to confirm account deletion.",
                "error",
            )
            return (
                render_template(
                    "account/delete_account.html",
                    user=current_user,
                    has_password=has_password,
                    has_google=has_google,
                    google_reauthenticated=False,
                    reauth_token=None,
                    error="Incorrect password. Please enter your valid current password to confirm account deletion.",
                ),
                401,
            )
        confirmed = True

    # 2. Google reauthentication token confirmation path (server-side intent validated)
    elif reauth_token is not None and reauth_token != "":
        if not has_google or google_identity is None:
            flash("Google identity confirmation is not available for this account.", "error")
            return (
                render_template(
                    "account/delete_account.html",
                    user=current_user,
                    has_password=has_password,
                    has_google=has_google,
                    google_reauthenticated=False,
                    reauth_token=None,
                    error="Google identity confirmation is not available for this account.",
                ),
                400,
            )

        success, intent_err = auth_service.consume_account_deletion_intent(
            intent_id=reauth_token.strip(),
            user_id=current_user.id,
            sub=google_identity.provider_user_id,
            email=current_user.email,
        )
        if not success:
            session.pop(SESSION_REAUTH_DELETE_KEY, None)
            flash(
                "Google reauthentication has expired or is invalid. Please verify with Google again.",
                "error",
            )
            return (
                render_template(
                    "account/delete_account.html",
                    user=current_user,
                    has_password=has_password,
                    has_google=has_google,
                    google_reauthenticated=False,
                    reauth_token=None,
                    error="Google reauthentication has expired or is invalid. Please verify with Google again.",
                ),
                400,
            )

        session.pop(SESSION_REAUTH_DELETE_KEY, None)
        confirmed = True

    # 3. Neither confirmation method provided
    else:
        flash("Identity confirmation is required to delete your account.", "error")
        return (
            render_template(
                "account/delete_account.html",
                user=current_user,
                has_password=has_password,
                has_google=has_google,
                google_reauthenticated=False,
                reauth_token=None,
                error="Identity confirmation is required to delete your account.",
            ),
            400,
        )

    if not confirmed:
        abort(400)

    # Confirmation succeeded: reset rate limiter and execute atomic deletion
    delete_account_rate_limiter.reset(client_ip, current_user.email)
    user_id_to_delete = current_user.id

    success, error_code = auth_service.delete_customer_account(user_id_to_delete)
    if not success:
        logger.error(
            "Failed to delete customer account %s (error=%s)", user_id_to_delete, error_code
        )
        flash("An error occurred while deleting your account. Please try again.", "error")
        return redirect(url_for("account.order_history"))

    # Wipe session completely, rotate CSRF token, and redirect
    session.clear()
    generate_csrf_token()
    session.modified = True
    flash("Your account and personal data have been permanently deleted.", "info")
    return redirect(url_for("catalog.index"))


@account_bp.route("/delete/google", methods=["GET"])
@login_required
def delete_account_google():
    """Initiate fresh Google OIDC reauthentication for account deletion confirmation."""
    current_user = get_current_user()
    if current_user is None or current_user.id is None:
        abort(401)

    if current_user.role == UserRole.OWNER or (
        isinstance(current_user.role, str) and current_user.role == "owner"
    ):
        abort(403)

    if not is_google_auth_configured():
        flash("Google sign-in is not currently configured.", "error")
        return redirect(url_for("account.delete_account_page"))

    auth_service = AuthService()
    google_identity = auth_service.external_identity_repo.get_by_user_and_provider(
        current_user.id, "google"
    )
    if google_identity is None:
        flash("No linked Google account was found for your profile.", "error")
        return redirect(url_for("account.delete_account_page"))

    redirect_uri = get_canonical_oauth_redirect_uri()
    rv = oauth.google.create_authorization_url(redirect_uri, prompt="select_account")
    oauth.google.save_authorize_data(
        redirect_uri=redirect_uri,
        action="delete_account",
        expected_user_id=current_user.id,
        **rv,
    )
    return redirect(rv["url"])


@account_bp.route("/delete/cancel", methods=["POST"])
@login_required
def cancel_delete_account():
    """Cancel pending account deletion intent and return to order history."""
    reauth_data = session.pop(SESSION_REAUTH_DELETE_KEY, None)
    if reauth_data and isinstance(reauth_data, dict):
        intent_id = reauth_data.get("intent_id") or reauth_data.get("token")
        if intent_id:
            auth_service = AuthService()
            auth_service.cancel_account_link_intent(intent_id)
    session.modified = True
    flash("Account deletion was cancelled.", "info")
    return redirect(url_for("account.order_history"))

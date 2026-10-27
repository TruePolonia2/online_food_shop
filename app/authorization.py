"""Role-based access control and authorization primitives."""
import functools
from typing import Callable, Sequence, Union

from flask import abort, jsonify, redirect, request, url_for

from app.models.user import User, UserRole
from app.routes.auth import get_current_user, is_safe_redirect_url

__all__ = ["login_required", "owner_required", "role_required"]


def _is_api_request() -> bool:
    """Determine whether the current request expects an API-like response.

    Returns:
        True if the request explicitly asks for JSON, has a JSON content type,
        or targets an API path; False otherwise.
    """
    if request.is_json:
        return True
    if request.path.startswith("/api/"):
        return True
    accept = request.accept_mimetypes
    return accept.best == "application/json" and accept["text/html"] < accept["application/json"]


def _get_safe_next_url() -> str | None:
    """Derive a safe local relative redirect destination for unauthenticated browser requests."""
    next_url = request.full_path.rstrip("?") if request.query_string else request.path
    if not is_safe_redirect_url(next_url):
        return None
    return next_url


def login_required(view_func: Callable) -> Callable:
    """Decorator ensuring that a request is authenticated with an active database user.

    Resolves the user entity strictly from the database via get_current_user().
    If unauthenticated, redirects browser requests to /login with a validated local
    'next' destination parameter, or returns HTTP 401 for API requests.

    Args:
        view_func: The route view function to wrap.

    Returns:
        The protected view function.
    """
    @functools.wraps(view_func)
    def wrapper(*args, **kwargs):
        user = get_current_user()
        if user is None:
            if _is_api_request():
                return jsonify({"error": "Authentication required."}), 401
            next_url = _get_safe_next_url()
            return redirect(url_for("auth.login", next=next_url) if next_url else url_for("auth.login"))
        return view_func(*args, **kwargs)

    return wrapper


def role_required(
    *roles: Union[UserRole, str],
    require_verified: bool = False,
) -> Callable:
    """Parameterized decorator enforcing database-backed role authorization.

    Resolves user identity strictly from the database via get_current_user().
    Never trusts client-provided roles, session['role'], or request headers.

    Args:
        *roles: One or more allowed UserRole enum values or case-insensitive role strings.
        require_verified: If True, requires user.is_verified to be True in addition
            to matching one of the required roles.

    Returns:
        A decorator that protects view functions against unauthorized role access.
    """
    normalized_roles: set[UserRole] = set()
    for r in roles:
        if isinstance(r, UserRole):
            normalized_roles.add(r)
        elif isinstance(r, str):
            try:
                normalized_roles.add(UserRole(r.strip().lower()))
            except ValueError:
                raise ValueError(f"Unknown UserRole '{r}'.")
        else:
            raise TypeError(f"Role must be a UserRole or string, got {type(r).__name__}")

    def decorator(view_func: Callable) -> Callable:
        @functools.wraps(view_func)
        def wrapper(*args, **kwargs):
            user = get_current_user()
            if user is None:
                if _is_api_request():
                    return jsonify({"error": "Authentication required."}), 401
                next_url = _get_safe_next_url()
                return redirect(url_for("auth.login", next=next_url) if next_url else url_for("auth.login"))

            # Database-backed role authorization check
            if user.role not in normalized_roles:
                if _is_api_request():
                    return jsonify({"error": "Access forbidden: insufficient role permissions."}), 403
                abort(403)

            # Email verification policy check
            if require_verified and not user.is_verified:
                if _is_api_request():
                    return jsonify({"error": "Access forbidden: verified email required."}), 403
                abort(403)

            return view_func(*args, **kwargs)

        return wrapper

    return decorator


def owner_required(view_func: Callable) -> Callable:
    """Decorator restricting route access strictly to verified store owners.

    Requires:
    1. An authenticated session backed by an active User database record.
    2. User database role strictly equal to UserRole.OWNER.
    3. User email verification status strictly equal to True.

    Unauthenticated requests are redirected to /login with a safe 'next' URL (or 401 for API).
    Authenticated requests with a non-owner role or unverified email receive HTTP 403 Forbidden.

    Args:
        view_func: The route view function to wrap.

    Returns:
        The protected view function.
    """
    return role_required(UserRole.OWNER, require_verified=True)(view_func)

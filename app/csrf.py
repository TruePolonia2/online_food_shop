"""Lightweight CSRF protection using Python standard library."""
import hmac
import secrets
from flask import Flask, abort, request, session

SESSION_CSRF_KEY = "_csrf_token"
STATE_CHANGING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def generate_csrf_token() -> str:
    """Generate or retrieve a cryptographically secure CSRF token for the session.

    Returns:
        A 64-character hexadecimal token string stored in the user's session.
    """
    token = session.get(SESSION_CSRF_KEY)
    if not token or not isinstance(token, str):
        token = secrets.token_hex(32)
        session[SESSION_CSRF_KEY] = token
        session.modified = True
    return token


def validate_csrf_token(token: str | None) -> bool:
    """Validate a supplied CSRF token against the session token in constant time.

    Args:
        token: The CSRF token received in the request.

    Returns:
        True if the token is valid and matches the session token, False otherwise.
    """
    stored_token = session.get(SESSION_CSRF_KEY)
    if not stored_token or not isinstance(stored_token, str):
        return False
    if not token or not isinstance(token, str):
        return False
    return hmac.compare_digest(stored_token, token)


def init_csrf(app: Flask) -> None:
    """Initialize CSRF protection on the Flask application instance.

    Protection is enabled by default. It can only be disabled by setting
    the application configuration option CSRF_ENABLED = False.
    """
    app.jinja_env.globals["csrf_token"] = generate_csrf_token

    @app.before_request
    def check_csrf() -> None:
        # CSRF is active by default; disabled only if CSRF_ENABLED is explicitly False
        if app.config.get("CSRF_ENABLED") is False:
            return

        if request.method in STATE_CHANGING_METHODS:
            token = request.form.get("csrf_token")
            if not token:
                token = request.headers.get("X-CSRFToken") or request.headers.get("X-CSRF-Token")

            if not validate_csrf_token(token):
                abort(400, description="Invalid or missing CSRF token.")

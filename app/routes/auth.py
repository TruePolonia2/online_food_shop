"""Customer authentication and session management routes."""
import logging
from threading import Lock
import time
from typing import Optional
from urllib.parse import unquote, urljoin, urlparse

from flask import (
    Blueprint,
    Response,
    flash,
    g,
    has_request_context,
    make_response,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

from app.csrf import generate_csrf_token
from app.models.user import User
from app.repositories.user_repository import UserRepository
from app.routes.cart_session import SESSION_CART_KEY, extract_sanitized_cart_data
from app.services.auth_service import AuthService

logger = logging.getLogger(__name__)

auth_bp = Blueprint("auth", __name__)


class LoginRateLimiter:
    """Thread-safe, bounded sliding-window rate limiter for login attempts.

    Maintains in-memory failed attempt history per (ip, email) with bounded
    capacity, automatic expired timestamp pruning, and deletion of empty keys.
    Protects actively rate-limited entries from eviction under capacity pressure.

    Note: This in-memory limiter is process-local and suitable for single-instance
    or development environments. Production deployments with multiple WSGI workers
    should back rate limiting with a shared store (e.g., Redis) or reverse proxy.
    """

    def __init__(
        self,
        max_attempts: int = 5,
        window_seconds: int = 300,
        max_keys: int = 10000,
    ) -> None:
        self.max_attempts = max_attempts
        self.window_seconds = window_seconds
        self.max_keys = max_keys
        self._lock = Lock()
        self._attempts: dict[str, list[float]] = {}

    def _key(self, ip: str, email: str) -> str:
        return f"{ip.strip()}:{email.strip().lower()}"

    def _prune_expired_locked(self, now: float) -> None:
        """Remove expired timestamps and delete empty keys. Must be called with self._lock held."""
        cutoff = now - self.window_seconds
        keys_to_delete = []
        for k, timestamps in self._attempts.items():
            valid = [t for t in timestamps if t > cutoff]
            if not valid:
                keys_to_delete.append(k)
            else:
                self._attempts[k] = valid
        for k in keys_to_delete:
            del self._attempts[k]

    def _enforce_capacity_locked(self, now: float) -> None:
        """Enforce max_keys bound by pruning expired entries and evicting oldest non-locked keys."""
        self._prune_expired_locked(now)
        if len(self._attempts) < self.max_keys:
            return

        # Evict only non-locked keys (< max_attempts) to prevent an attacker from
        # evicting an active lockout on a targeted account via flooding.
        candidates = [
            (k, max(ts) if ts else 0.0)
            for k, ts in self._attempts.items()
            if len(ts) < self.max_attempts
        ]
        candidates.sort(key=lambda item: item[1])  # oldest last-attempt first

        excess = (len(self._attempts) - self.max_keys) + 1
        for k, _ in candidates[:excess]:
            del self._attempts[k]

    def is_rate_limited(self, ip: str, email: str) -> bool:
        """Check if attempts from this (ip, email) exceed max_attempts within the window."""
        key = self._key(ip, email)
        now = time.time()
        with self._lock:
            self._prune_expired_locked(now)
            timestamps = self._attempts.get(key)
            if timestamps is not None:
                return len(timestamps) >= self.max_attempts

            # Key is not tracked. Check if limiter is at capacity with all stored keys locked out.
            if len(self._attempts) >= self.max_keys:
                has_evictable = any(
                    len(ts) < self.max_attempts for ts in self._attempts.values()
                )
                if not has_evictable:
                    # Limiter is fully saturated with active blocks: fail closed by returning True (HTTP 429)
                    logger.warning(
                        "LoginRateLimiter reached maximum capacity (%d) with active blocks. Throttling new request.",
                        self.max_keys,
                    )
                    return True

            return False

    def record_failure(self, ip: str, email: str) -> None:
        """Record a failed login attempt with bounded capacity enforcement."""
        key = self._key(ip, email)
        now = time.time()
        with self._lock:
            if key in self._attempts:
                self._attempts[key].append(now)
                return

            # Key is not yet tracked: enforce capacity before inserting a new key
            self._enforce_capacity_locked(now)
            if len(self._attempts) < self.max_keys:
                self._attempts[key] = [now]
            else:
                logger.warning(
                    "LoginRateLimiter reached maximum capacity (%d) with active blocks.",
                    self.max_keys,
                )

    def reset(self, ip: str, email: str) -> None:
        """Reset failed attempts upon successful authentication."""
        key = self._key(ip, email)
        with self._lock:
            self._attempts.pop(key, None)

    def clear(self) -> None:
        """Clear all rate limiter state (primarily for test isolation)."""
        with self._lock:
            self._attempts.clear()


login_rate_limiter = LoginRateLimiter(max_attempts=5, window_seconds=300, max_keys=10000)


def is_safe_redirect_url(target: Optional[str]) -> bool:
    """Validate that target URL is a safe local relative path.

    Rejects external URLs, protocol-relative URLs (e.g. //evil.com),
    backslashes anywhere in the target, and control characters to prevent
    open-redirect vulnerabilities.

    Args:
        target: Destination URL string to validate.

    Returns:
        True if target is safe to redirect to, False otherwise.
    """
    if not target or not isinstance(target, str):
        return False

    target = target.strip()
    if not target:
        return False

    # Disallow backslashes anywhere in target (raw or percent-decoded)
    decoded = unquote(target)
    if "\\" in target or "\\" in decoded:
        return False

    # Disallow protocol-relative URLs (e.g., '//evil.com')
    if target.startswith("//") or decoded.startswith("//"):
        return False

    # Disallow CRLF, tabs, or null bytes
    if any(c in target or c in decoded for c in "\r\n\t\x00"):
        return False

    ref_url = urlparse(request.host_url)
    test_url = urlparse(urljoin(request.host_url, target))

    return (
        test_url.scheme in ("http", "https")
        and ref_url.netloc == test_url.netloc
        and target.startswith("/")
        and not target.startswith("/\\")
    )


def get_current_user_id() -> Optional[int]:
    """Retrieve the authenticated user ID from the session if present.

    Returns:
        Positive integer user ID if logged in, None otherwise.
    """
    if not has_request_context():
        return None
    uid = session.get("user_id")
    if isinstance(uid, bool) or not isinstance(uid, int) or uid <= 0:
        return None
    return uid


def get_current_user() -> Optional[User]:
    """Retrieve the authenticated User domain model for the current request.

    Caches the lookup on Flask's 'g' object to avoid redundant database queries
    during a single request. Purges stale session IDs if the user no longer exists.

    Returns:
        The active User entity if authenticated, None otherwise.
    """
    if not has_request_context():
        return None

    if hasattr(g, "current_user"):
        return g.current_user

    uid = get_current_user_id()
    if not uid:
        g.current_user = None
        return None

    user_repo = UserRepository()
    user = user_repo.get_by_id(uid)
    if user is None:
        session.pop("user_id", None)
        g.current_user = None
        return None

    g.current_user = user
    return user


@auth_bp.route("/register", methods=["GET"])
def register_form():
    """Display the customer registration form."""
    if get_current_user() is not None:
        return redirect(url_for("catalog.index"))
    return render_template("auth/register.html")


@auth_bp.route("/register", methods=["POST"])
def register():
    """Process customer registration and trigger email verification."""
    if get_current_user() is not None:
        return redirect(url_for("catalog.index"))

    email = request.form.get("email", "").strip()
    password = request.form.get("password", "")
    confirm_password = request.form.get("confirm_password", "")

    auth_service = AuthService()

    try:
        auth_service.register_customer(
            email=email,
            password=password,
            confirm_password=confirm_password,
        )
    except ValueError as err:
        flash(str(err), "error")
        # Do NOT repopulate passwords upon validation failure
        return render_template("auth/register.html", email=email), 400

    flash(
        "Account created successfully. Email verification is required. "
        "In this development environment, email delivery is simulated and no message is sent to your inbox.",
        "success",
    )
    return redirect(url_for("auth.login"))


@auth_bp.route("/login", methods=["GET"])
def login_form():
    """Display the user sign-in form."""
    if get_current_user() is not None:
        return redirect(url_for("catalog.index"))
    next_url = request.args.get("next", "")
    return render_template("auth/login.html", next_url=next_url)


@auth_bp.route("/login", methods=["POST"])
def login():
    """Authenticate user credentials and establish a secure session."""
    if get_current_user() is not None:
        return redirect(url_for("catalog.index"))

    email = request.form.get("email", "").strip()
    password = request.form.get("password", "")
    next_url = request.form.get("next", "")
    client_ip = request.remote_addr or "127.0.0.1"

    # Check bounded login rate limiter
    if login_rate_limiter.is_rate_limited(client_ip, email):
        flash("Too many failed login attempts. Please wait 5 minutes before trying again.", "error")
        return render_template("auth/login.html", email=email, next_url=next_url), 429

    auth_service = AuthService()
    user = auth_service.authenticate(email=email, password=password)

    if user is None:
        login_rate_limiter.record_failure(client_ip, email)
        # Generic error message to prevent account enumeration
        flash("Invalid email or password.", "error")
        return render_template("auth/login.html", email=email, next_url=next_url), 401

    # Successful authentication: reset rate limiter failures
    login_rate_limiter.reset(client_ip, email)

    # Session fixation defense: preserve validated guest cart, clear previous session data
    raw_cart = session.get(SESSION_CART_KEY)
    sanitized_cart = extract_sanitized_cart_data(raw_cart)
    session.clear()

    # Re-issue CSRF token and set authenticated user ID
    generate_csrf_token()
    session["user_id"] = user.id

    # Restore valid guest cart mapping
    if sanitized_cart:
        session[SESSION_CART_KEY] = sanitized_cart

    session.modified = True

    # Inform unverified accounts
    if not user.is_verified:
        flash(
            "Your email address is not yet verified. Please verify your email or request a new verification link.",
            "warning",
        )

    flash(f"Welcome back, {user.email}!", "success")

    # Safe redirect
    if is_safe_redirect_url(next_url):
        return redirect(next_url)
    return redirect(url_for("catalog.index"))


@auth_bp.route("/logout", methods=["POST"])
def logout():
    """Terminate the authenticated session via CSRF-protected POST."""
    raw_cart = session.get(SESSION_CART_KEY)
    sanitized_cart = extract_sanitized_cart_data(raw_cart)

    # Clear entire session to wipe all auth, order, and customer state (including last_order_id)
    session.clear()

    # Rotate CSRF token for the new guest session
    generate_csrf_token()

    # Restore validated guest cart
    if sanitized_cart:
        session[SESSION_CART_KEY] = sanitized_cart

    session.modified = True
    flash("You have been logged out.", "info")
    return redirect(url_for("catalog.index"))


@auth_bp.route("/logout", methods=["GET"])
def logout_get():
    """Reject state-changing logout via GET requests."""
    return redirect(url_for("catalog.index"))


@auth_bp.route("/verify-email/<token>", methods=["GET"])
def verify_email_confirm(token: str):
    """Present a safe confirmation page without consuming the verification token on GET.

    Applies a strict no-referrer policy to avoid leaking the raw token to third-party assets,
    and sets Cache-Control to prevent intermediate caches from storing the sensitive token page.
    """
    cleaned_token = token.strip()
    if not cleaned_token:
        flash("Invalid verification link.", "error")
        return redirect(url_for("auth.resend_verification_form"))

    response = make_response(render_template("auth/verify_email.html", token=cleaned_token))
    # Prevent referrer header leakage to external resources
    response.headers["Referrer-Policy"] = "no-referrer"
    # Prevent caching of sensitive token confirmation page
    response.headers["Cache-Control"] = "no-store, private"
    return response


@auth_bp.route("/verify-email", methods=["POST"])
def verify_email():
    """Consume an email verification token and activate the account."""
    raw_token = request.form.get("token", "").strip()
    if not raw_token:
        flash("Missing verification token.", "error")
        return redirect(url_for("auth.resend_verification_form"))

    auth_service = AuthService()
    verified_user = auth_service.verify_email(raw_token)

    if verified_user is None:
        flash("The verification link is invalid, expired, or has already been used.", "error")
        return redirect(url_for("auth.resend_verification_form"))

    flash("Your email address has been successfully verified! You may now sign in.", "success")
    return redirect(url_for("auth.login"))


@auth_bp.route("/verify-email", methods=["GET"])
@auth_bp.route("/verify-email/resend", methods=["GET"])
def resend_verification_form():
    """Display the form to request a new email verification link."""
    return render_template("auth/resend_verification.html")


@auth_bp.route("/verify-email/resend", methods=["POST"])
def resend_verification():
    """Dispatch a new verification email with anti-enumeration protection."""
    email = request.form.get("email", "").strip()

    if email:
        auth_service = AuthService()
        try:
            auth_service.request_verification_resend(email)
        except ValueError:
            # Internal cooldown error is absorbed to prevent timing-based enumeration
            pass

    # Generic neutral response: always display the same message regardless of whether
    # the email exists, is already verified, or cooldown was triggered.
    flash(
        "If an eligible unverified account exists for that email, a verification link has been generated. "
        "In this development environment, email delivery is simulated and no message is sent to your inbox.",
        "info",
    )
    return redirect(url_for("auth.login"))

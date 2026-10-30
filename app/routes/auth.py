"""Customer authentication and session management routes."""
from datetime import datetime, timezone
import logging
import secrets
from threading import Lock
import time
from typing import Optional
from urllib.parse import unquote, urljoin, urlparse

from flask import (
    Blueprint,
    Response,
    abort,
    current_app,
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
from app.models.user import User, UserRole
from app.oauth import is_google_auth_configured, oauth
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
account_link_rate_limiter = LoginRateLimiter(max_attempts=5, window_seconds=300, max_keys=10000)
SESSION_PENDING_LINK_KEY = "_pending_google_link"


class PasswordResetRateLimiter:
    """Thread-safe, multi-dimensional bounded sliding-window rate limiter for password reset requests.

    Enforces three independent bounds within the configured time window:
    1. Combined (IP, email) limit: Prevents repeated spamming of a single account from one origin.
    2. Per-email limit: Prevents distributed requests (multiple IPs) from flooding the same email.
    3. Aggregate per-IP limit: Prevents a single origin from scanning or submitting resets across
       many different email addresses.

    Maintains bounded capacity with automatic pruning of expired timestamps and eviction
    for entries under memory pressure, preserving active lockouts.
    """

    def __init__(
        self,
        max_per_ip_email: int = 5,
        max_per_email: int = 5,
        max_per_ip: int = 15,
        window_seconds: int = 300,
        max_keys: int = 10000,
    ) -> None:
        self.max_per_ip_email = max_per_ip_email
        self.max_per_email = max_per_email
        self.max_per_ip = max_per_ip
        self.window_seconds = window_seconds
        self.max_keys = max_keys
        self._lock = Lock()
        self._attempts: dict[str, list[float]] = {}

    def _keys_for(self, ip: str, email: str) -> dict[str, int]:
        """Return dict of {storage_key: max_allowed_attempts} for a given (ip, email)."""
        ip_clean = ip.strip()
        email_clean = email.strip().lower()
        keys = {}
        if ip_clean and email_clean:
            keys[f"combo:{ip_clean}:{email_clean}"] = self.max_per_ip_email
        if email_clean:
            keys[f"email:{email_clean}"] = self.max_per_email
        if ip_clean:
            keys[f"ip:{ip_clean}"] = self.max_per_ip
        return keys

    def _prune_expired_locked(self, now: float) -> None:
        """Remove expired timestamps and empty keys. Must be called with self._lock held."""
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
        """Enforce max_keys bound by pruning expired entries and evicting oldest unlocked keys."""
        self._prune_expired_locked(now)
        if len(self._attempts) < self.max_keys:
            return

        candidates = [
            (k, max(ts) if ts else 0.0)
            for k, ts in self._attempts.items()
        ]
        candidates.sort(key=lambda item: item[1])  # oldest last-attempt first
        excess = (len(self._attempts) - self.max_keys) + 1
        for k, _ in candidates[:excess]:
            del self._attempts[k]

    def is_rate_limited(self, ip: str, email: str) -> bool:
        """Check if any rate limit (combo, per-email, or per-IP) is exceeded."""
        now = time.time()
        key_limits = self._keys_for(ip, email)
        with self._lock:
            self._prune_expired_locked(now)
            for key, limit in key_limits.items():
                timestamps = self._attempts.get(key)
                if timestamps is not None and len(timestamps) >= limit:
                    return True
            return False

    def record_attempt(self, ip: str, email: str) -> None:
        """Record a password reset attempt across all relevant dimensions."""
        now = time.time()
        key_limits = self._keys_for(ip, email)
        with self._lock:
            self._enforce_capacity_locked(now)
            for key in key_limits:
                if key in self._attempts:
                    self._attempts[key].append(now)
                elif len(self._attempts) < self.max_keys:
                    self._attempts[key] = [now]

    # Backward-compatible alias
    record_failure = record_attempt

    def reset(self, ip: str, email: str) -> None:
        """Reset rate limiter attempts for this specific account after successful reset."""
        ip_clean = ip.strip()
        email_clean = email.strip().lower()
        with self._lock:
            if ip_clean and email_clean:
                self._attempts.pop(f"combo:{ip_clean}:{email_clean}", None)
            if email_clean:
                self._attempts.pop(f"email:{email_clean}", None)

    def clear(self) -> None:
        """Clear all rate limiter state (for tests)."""
        with self._lock:
            self._attempts.clear()


forgot_password_rate_limiter = PasswordResetRateLimiter(
    max_per_ip_email=5,
    max_per_email=5,
    max_per_ip=15,
    window_seconds=300,
    max_keys=10000,
)


def build_absolute_reset_url(raw_token: str) -> str:
    """Build a trusted absolute password reset URL.

    Derives reset URLs strictly from application configuration
    (e.g., APP_BASE_URL or SERVER_NAME) to prevent password-reset poisoning
    via untrusted HTTP Host headers. Falls back safely to localhost if not configured.
    """
    path = f"/auth/reset-password/{raw_token}"
    app_base_url = current_app.config.get("APP_BASE_URL")
    if app_base_url and isinstance(app_base_url, str) and app_base_url.strip():
        return urljoin(app_base_url.strip().rstrip("/") + "/", path.lstrip("/"))

    server_name = current_app.config.get("SERVER_NAME")
    if server_name and isinstance(server_name, str) and server_name.strip():
        scheme = current_app.config.get("PREFERRED_URL_SCHEME", "http")
        return f"{scheme}://{server_name.strip()}{path}"

    return f"http://127.0.0.1:5000{path}"


def get_canonical_oauth_redirect_uri() -> str:
    """Build a trusted canonical absolute OAuth callback URL.

    Derives the callback URL strictly from application configuration (APP_BASE_URL
    or SERVER_NAME) rather than dynamic or untrusted HTTP Host headers, ensuring
    deterministic matching against Google Cloud Console authorized redirect URIs
    and mitigating redirect_uri_mismatch errors. Falls back safely to localhost
    if neither is configured.
    """
    path = "/auth/google/callback"
    app_base_url = current_app.config.get("APP_BASE_URL")
    if app_base_url and isinstance(app_base_url, str) and app_base_url.strip():
        return urljoin(app_base_url.strip().rstrip("/") + "/", path.lstrip("/"))

    server_name = current_app.config.get("SERVER_NAME")
    if server_name and isinstance(server_name, str) and server_name.strip():
        scheme = current_app.config.get("PREFERRED_URL_SCHEME", "http")
        return f"{scheme}://{server_name.strip()}{path}"

    return url_for("auth.google_callback", _external=True)


def build_absolute_verification_url(raw_token: str) -> str:
    """Build a trusted absolute email verification URL.

    Derives verification URLs strictly from application configuration
    (e.g., APP_BASE_URL or SERVER_NAME) to prevent Host-header injection.
    Falls back safely to localhost if not configured.
    """
    path = f"/verify-email/{raw_token}"
    app_base_url = current_app.config.get("APP_BASE_URL")
    if app_base_url and isinstance(app_base_url, str) and app_base_url.strip():
        return urljoin(app_base_url.strip().rstrip("/") + "/", path.lstrip("/"))

    server_name = current_app.config.get("SERVER_NAME")
    if server_name and isinstance(server_name, str) and server_name.strip():
        scheme = current_app.config.get("PREFERRED_URL_SCHEME", "http")
        return f"{scheme}://{server_name.strip()}{path}"

    return f"http://127.0.0.1:5000{path}"


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
            verification_url_builder=build_absolute_verification_url,
        )
    except ValueError as err:
        flash(str(err), "error")
        # Do NOT repopulate passwords upon validation failure
        return render_template("auth/register.html", email=email), 400

    backend = current_app.config.get("EMAIL_BACKEND", "memory")
    if backend == "smtp":
        if auth_service.last_delivery_success:
            flash(
                "Account created successfully. A verification link has been sent to your email address.",
                "success",
            )
        else:
            flash(
                "Account created, but we could not deliver your verification email at this time. "
                "Please use 'Resend verification link' once your email connection is restored.",
                "warning",
            )
    else:
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
    user_locale = session.get("locale")

    # Clear entire session to wipe all auth, order, and customer state (including last_order_id)
    session.clear()

    # Rotate CSRF token for the new guest session
    generate_csrf_token()

    # Restore validated guest cart
    if sanitized_cart:
        session[SESSION_CART_KEY] = sanitized_cart

    # Restore language preference
    if user_locale:
        session["locale"] = user_locale

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
            auth_service.request_verification_resend(
                email, verification_url_builder=build_absolute_verification_url
            )
        except ValueError:
            # Internal cooldown error is absorbed to prevent timing-based enumeration
            pass

    backend = current_app.config.get("EMAIL_BACKEND", "memory")
    if backend == "smtp":
        flash(
            "If an eligible unverified account exists for that email, a verification link has been sent.",
            "info",
        )
    else:
        # Generic neutral response: always display the same message regardless of whether
        # the email exists, is already verified, or cooldown was triggered.
        flash(
            "If an eligible unverified account exists for that email, a verification link has been generated. "
            "In this development environment, email delivery is simulated and no message is sent to your inbox.",
            "info",
        )
    return redirect(url_for("auth.login"))


@auth_bp.route("/auth/google", methods=["GET"])
def google_login():
    """Initiate Google OAuth 2.0 authorization code flow with OpenID Connect and PKCE."""
    if get_current_user() is not None:
        return redirect(url_for("catalog.index"))

    if not is_google_auth_configured():
        flash("Google sign-in is not currently configured.", "error")
        return redirect(url_for("auth.login"))

    # Safely capture post-auth redirect target
    next_url = request.args.get("next", "")
    redirect_uri = get_canonical_oauth_redirect_uri()

    # Generate authorization URL, state, nonce, and PKCE challenge with explicit account selection
    rv = oauth.google.create_authorization_url(redirect_uri, prompt="select_account")

    # Bind return destination to this specific authorization state
    if next_url and is_safe_redirect_url(next_url):
        oauth.google.save_authorize_data(redirect_uri=redirect_uri, next_url=next_url, **rv)
    else:
        oauth.google.save_authorize_data(redirect_uri=redirect_uri, **rv)

    return redirect(rv["url"])


@auth_bp.route("/auth/google/callback", methods=["GET"])
def google_callback():
    """Handle Google OpenID Connect callback and authenticate or register customer."""
    if not is_google_auth_configured():
        flash("Google sign-in is not currently configured.", "error")
        return redirect(url_for("auth.login"))

    # Extract state-bound return destination and action before token exchange consumes the state
    state_param = request.args.get("state")
    state_data = (
        oauth.google.framework.get_state_data(session, state_param)
        if state_param
        else None
    )

    # Handle provider cancellation or errors
    error = request.args.get("error")
    if error:
        if state_data and state_data.get("action") == "delete_account":
            if state_param:
                oauth.google.framework.clear_state_data(session, state_param)
            flash("Google reauthentication was cancelled.", "info")
            return redirect(url_for("account.delete_account_page"))

        if error in ("access_denied", "cancelled"):
            flash("Google sign-in was cancelled.", "info")
        else:
            logger.warning("Google OAuth error in callback: %s", error)
            flash("Google sign-in failed. Please try again or sign in with your password.", "error")
        return redirect(url_for("auth.login"))

    # Dedicated account deletion reauthentication verification
    if state_data and state_data.get("action") == "delete_account":
        current_user = get_current_user()
        expected_user_id = state_data.get("expected_user_id")
        if current_user is None or current_user.id != expected_user_id:
            if state_param:
                oauth.google.framework.clear_state_data(session, state_param)
            flash("Your session has expired or is invalid. Please sign in again.", "warning")
            return redirect(url_for("auth.login"))

        if current_user.role == UserRole.OWNER or (
            isinstance(current_user.role, str) and current_user.role == "owner"
        ):
            if state_param:
                oauth.google.framework.clear_state_data(session, state_param)
            abort(403)

        try:
            token = oauth.google.authorize_access_token()
        except Exception as exc:
            logger.warning("Google OAuth token exchange for deletion reauth failed: %s", exc)
            flash("Google reauthentication failed. Please try again.", "error")
            return redirect(url_for("account.delete_account_page"))

        userinfo = token.get("userinfo") if token else None
        if not userinfo or not isinstance(userinfo, dict):
            logger.warning("Missing or invalid userinfo in Google reauth token response")
            flash("Google reauthentication failed to obtain verified account identity.", "error")
            return redirect(url_for("account.delete_account_page"))

        sub = userinfo.get("sub")
        email = userinfo.get("email")
        email_verified = userinfo.get("email_verified")

        if not sub or not isinstance(sub, str) or not sub.strip():
            logger.warning("Invalid or missing sub claim from Google in deletion reauth")
            flash("Invalid identity information received from Google.", "error")
            return redirect(url_for("account.delete_account_page"))

        if not email or not isinstance(email, str) or not email.strip() or "@" not in email:
            logger.warning("Invalid or missing email claim from Google in deletion reauth")
            flash("No valid email address was provided by Google.", "error")
            return redirect(url_for("account.delete_account_page"))

        is_verified = email_verified is True or str(email_verified).lower() == "true"
        if not is_verified:
            logger.warning("Unverified Google email claim in deletion reauth for '%s'", email)
            flash(
                "Your Google email address is not verified. Please verify your email with Google first.",
                "error",
            )
            return redirect(url_for("account.delete_account_page"))

        auth_service = AuthService()
        linked_identity = auth_service.external_identity_repo.get_by_user_and_provider(
            current_user.id, "google"
        )
        if linked_identity is None or linked_identity.provider_user_id != sub.strip():
            logger.warning("Google reauthentication sub mismatch for user %s", current_user.id)
            flash(
                "The selected Google account does not match the Google account linked to this profile.",
                "error",
            )
            return redirect(url_for("account.delete_account_page"))

        if email.strip().lower() != current_user.email.strip().lower():
            logger.warning("Google reauthentication email mismatch for user %s", current_user.id)
            flash(
                "The Google account email does not match your store account email.",
                "error",
            )
            return redirect(url_for("account.delete_account_page"))

        # Create server-side single-use deletion intent bound to user, provider sub, and deletion action
        deletion_intent = auth_service.create_account_deletion_intent(
            user_id=current_user.id,
            sub=sub.strip(),
            email=current_user.email,
            validity_minutes=5,
        )
        session["_reauth_delete_token"] = {
            "intent_id": deletion_intent.intent_id,
            "user_id": current_user.id,
            "expires_at": deletion_intent.expires_at.timestamp(),
        }
        session.modified = True
        flash(
            "Google identity verified. Please review the warning and submit final confirmation below to permanently delete your account.",
            "info",
        )
        return redirect(url_for("account.delete_account_page"))

    next_url = state_data.get("next_url") if state_data else None
    if not is_safe_redirect_url(next_url):
        next_url = None

    # If user is already authenticated (e.g. concurrent tab completed login earlier)
    if get_current_user() is not None:
        if state_param:
            oauth.google.framework.clear_state_data(session, state_param)
        if next_url:
            return redirect(next_url)
        return redirect(url_for("catalog.index"))

    # Exchange authorization code for tokens and parse verified identity claims (with PKCE verifier)
    try:
        token = oauth.google.authorize_access_token()
    except Exception as exc:
        logger.warning("Google OAuth token exchange failed: %s", exc)
        flash("Google sign-in failed. Please try again or sign in with your password.", "error")
        return redirect(url_for("auth.login"))

    userinfo = token.get("userinfo") if token else None
    if not userinfo or not isinstance(userinfo, dict):
        logger.warning("Missing or invalid userinfo in Google token response")
        flash("Google sign-in failed to obtain verified account identity.", "error")
        return redirect(url_for("auth.login"))

    sub = userinfo.get("sub")
    email = userinfo.get("email")
    email_verified = userinfo.get("email_verified")
    name = userinfo.get("name")

    if not sub or not isinstance(sub, str) or not sub.strip():
        logger.warning("Invalid or missing sub claim from Google")
        flash("Invalid identity information received from Google.", "error")
        return redirect(url_for("auth.login"))

    if not email or not isinstance(email, str) or not email.strip() or "@" not in email:
        logger.warning("Invalid or missing email claim from Google")
        flash("No valid email address was provided by Google.", "error")
        return redirect(url_for("auth.login"))

    # Strictly require verified email claim
    is_verified = email_verified is True or str(email_verified).lower() == "true"
    if not is_verified:
        logger.warning("Unverified Google email claim for '%s'", email)
        flash(
            "Your Google email address is not verified. Please verify your email with Google first.",
            "error",
        )
        return redirect(url_for("auth.login"))

    # Process identity association via AuthService
    auth_service = AuthService()
    user, error_code = auth_service.authenticate_or_register_google_user(
        sub=sub, email=email, name=name
    )

    if error_code == "collision":
        local_user = auth_service.user_repo.get_by_email(email.strip().lower())
        if local_user is not None:
            # 1. Owner protection: Never link owner accounts
            if local_user.role == UserRole.OWNER:
                flash(
                    "An account with this email address already exists. Store owner accounts cannot be linked to external providers. Please sign in with your password.",
                    "error",
                )
                return redirect(url_for("auth.login"))

            # 2. Unverified protection: Never bypass email verification
            if not local_user.is_verified:
                flash(
                    "An account with this email address exists but has not been verified yet. "
                    "Please check your email to verify your account before linking Google sign-in.",
                    "warning",
                )
                return redirect(url_for("auth.login"))

            # 3. Check if local customer already has a different Google account linked
            existing_link = auth_service.external_identity_repo.get_by_user_and_provider(
                local_user.id, "google"
            )
            if existing_link is not None:
                flash(
                    "An account with this email already has a different Google account linked. Please sign in with your password.",
                    "error",
                )
                return redirect(url_for("auth.login"))

            # 4. Check if this Google sub is already linked to another account
            sub_link = auth_service.external_identity_repo.get_by_provider_sub(
                "google", sub.strip()
            )
            if sub_link is not None:
                flash(
                    "This Google identity is already linked to a different account.",
                    "error",
                )
                return redirect(url_for("auth.login"))

            # 5. Eligible customer with local password: start secure one-time linking flow
            if local_user.password_hash is not None:
                intent = auth_service.create_account_link_intent(
                    user_id=local_user.id,
                    sub=sub.strip(),
                    email=local_user.email,
                    next_url=next_url,
                )
                session[SESSION_PENDING_LINK_KEY] = {
                    "intent_id": intent.intent_id,
                    "email": local_user.email,
                    "expires_at": intent.expires_at.timestamp(),
                    "next_url": next_url,
                }
                session.modified = True
                return redirect(url_for("auth.google_link_page"))

        flash(
            "An account with this email address already exists. Please log in using your password.",
            "warning",
        )
        return redirect(url_for("auth.login"))

    if user is None:
        logger.warning("Google user authentication failed (code=%s)", error_code)
        flash("Google sign-in could not be completed. Please try again.", "error")
        return redirect(url_for("auth.login"))

    # Session fixation defense: preserve validated cart and concurrent OAuth states
    raw_cart = session.get(SESSION_CART_KEY)
    sanitized_cart = extract_sanitized_cart_data(raw_cart)
    pending_states = {k: v for k, v in session.items() if k.startswith("_state_google_")}

    # Clear entire session: wipes previous auth identity, old CSRF token, and stale keys
    session.clear()

    # Restore any other pending OAuth states for concurrent browser tabs
    session.update(pending_states)

    # Re-issue CSRF token and set authenticated user ID
    generate_csrf_token()
    session["user_id"] = user.id

    # Restore valid guest cart
    if sanitized_cart:
        session[SESSION_CART_KEY] = sanitized_cart

    session.modified = True

    flash(f"Welcome back, {user.email}!", "success")

    # Safe redirect
    if next_url and is_safe_redirect_url(next_url):
        return redirect(next_url)
    return redirect(url_for("catalog.index"))


@auth_bp.route("/auth/google/link", methods=["GET"])
def google_link_page():
    """Present the password confirmation page to link an existing local account to Google."""
    if get_current_user() is not None:
        return redirect(url_for("catalog.index"))

    pending = session.get(SESSION_PENDING_LINK_KEY)
    if not pending or not isinstance(pending, dict) or not pending.get("intent_id"):
        flash("No pending account linking request was found. Please sign in again.", "info")
        return redirect(url_for("auth.login"))

    intent_id = str(pending["intent_id"]).strip()
    auth_service = AuthService()
    intent = auth_service.get_account_link_intent(intent_id)

    if intent is None:
        session.pop(SESSION_PENDING_LINK_KEY, None)
        session.modified = True
        flash("No pending account linking request was found. Please sign in again.", "info")
        return redirect(url_for("auth.login"))

    if intent.status == "cancelled":
        session.pop(SESSION_PENDING_LINK_KEY, None)
        session.modified = True
        flash("The account linking request was cancelled.", "info")
        return redirect(url_for("auth.login"))

    if intent.status == "consumed":
        session.pop(SESSION_PENDING_LINK_KEY, None)
        session.modified = True
        flash("This account linking request has already been used.", "warning")
        return redirect(url_for("auth.login"))

    now_ts = datetime.now(timezone.utc).timestamp()
    if intent.is_expired or now_ts > pending.get("expires_at", float("inf")):
        session.pop(SESSION_PENDING_LINK_KEY, None)
        session.modified = True
        flash("The account linking request has expired. Please try signing in again.", "warning")
        return redirect(url_for("auth.login"))

    return render_template(
        "auth/link_account.html",
        email=intent.email,
        next_url=intent.next_url or "",
    )


@auth_bp.route("/auth/google/link", methods=["POST"])
def google_link_confirm():
    """Confirm local account password and atomically link the verified Google identity."""
    if get_current_user() is not None:
        return redirect(url_for("catalog.index"))

    pending = session.get(SESSION_PENDING_LINK_KEY)
    if not pending or not isinstance(pending, dict) or not pending.get("intent_id"):
        flash("The account linking session is invalid or has expired. Please sign in again.", "error")
        return redirect(url_for("auth.login"))

    intent_id = str(pending["intent_id"]).strip()
    auth_service = AuthService()
    intent = auth_service.get_account_link_intent(intent_id)

    if intent is None:
        session.pop(SESSION_PENDING_LINK_KEY, None)
        session.modified = True
        flash("The account linking session is invalid or has expired. Please sign in again.", "error")
        return redirect(url_for("auth.login"))

    if intent.status == "cancelled":
        session.pop(SESSION_PENDING_LINK_KEY, None)
        session.modified = True
        flash("The account linking request was cancelled. Please try signing in again.", "info")
        return redirect(url_for("auth.login"))

    if intent.status == "consumed":
        session.pop(SESSION_PENDING_LINK_KEY, None)
        session.modified = True
        flash("This account linking request has already been used. Please try signing in again.", "warning")
        return redirect(url_for("auth.login"))

    now_ts = datetime.now(timezone.utc).timestamp()
    if intent.is_expired or now_ts > pending.get("expires_at", float("inf")):
        session.pop(SESSION_PENDING_LINK_KEY, None)
        session.modified = True
        flash("The account linking request has expired. Please try signing in again.", "warning")
        return redirect(url_for("auth.login"))

    client_ip = request.remote_addr or "127.0.0.1"
    email = intent.email

    # Rate limiting protection against credential stuffing
    if account_link_rate_limiter.is_rate_limited(client_ip, email) or login_rate_limiter.is_rate_limited(client_ip, email):
        return render_template(
            "auth/link_account.html",
            email=email,
            rate_limit_error="Too many failed attempts. Please wait 5 minutes before trying again.",
        ), 429

    password = request.form.get("password", "")
    if not password or not password.strip():
        return render_template(
            "auth/link_account.html",
            email=email,
            error="Please enter your store password to confirm linking.",
        ), 400

    user, err = auth_service.link_google_identity(
        user_id=intent.user_id,
        sub=intent.provider_user_id,
        email=intent.email,
        password=password,
        intent_id=intent.intent_id,
    )

    if err == "invalid_password":
        account_link_rate_limiter.record_failure(client_ip, email)
        login_rate_limiter.record_failure(client_ip, email)
        return render_template(
            "auth/link_account.html",
            email=email,
            error="Incorrect password. Please check your password and try again.",
        ), 401

    if user is None or err is not None:
        # Non-recoverable error (e.g. user already linked, owner forbidden, intent already consumed)
        session.pop(SESSION_PENDING_LINK_KEY, None)
        session.modified = True
        logger.warning("Account linking rejected for %s: %s", email, err)
        if err in ("intent_cancelled", "intent_already_consumed", "intent_expired"):
            flash("The account linking request is no longer valid. Please try signing in again.", "warning")
        else:
            flash("Account linking could not be completed. Please log in using your password.", "error")
        return redirect(url_for("auth.login"))

    # Successful confirmation & atomic linking:
    account_link_rate_limiter.reset(client_ip, email)
    login_rate_limiter.reset(client_ip, email)
    next_url = intent.next_url

    # Session fixation defense: preserve validated cart and concurrent OAuth states
    raw_cart = session.get(SESSION_CART_KEY)
    sanitized_cart = extract_sanitized_cart_data(raw_cart)
    pending_states = {k: v for k, v in session.items() if k.startswith("_state_google_")}

    # Clear entire session: wipes pending link, old CSRF token, and stale keys
    session.clear()

    # Restore any other pending OAuth states for concurrent browser tabs
    session.update(pending_states)

    # Re-issue CSRF token and set authenticated user ID
    generate_csrf_token()
    session["user_id"] = user.id

    if sanitized_cart:
        session[SESSION_CART_KEY] = sanitized_cart

    session.modified = True

    flash(f"Your Google account has been successfully linked! Welcome back, {user.email}!", "success")

    if next_url and is_safe_redirect_url(next_url):
        return redirect(next_url)
    return redirect(url_for("catalog.index"))


@auth_bp.route("/auth/google/link/cancel", methods=["POST"])
def cancel_google_link():
    """Cancel pending account linking via CSRF-protected POST."""
    pending = session.get(SESSION_PENDING_LINK_KEY)
    if pending and isinstance(pending, dict) and pending.get("intent_id"):
        auth_service = AuthService()
        auth_service.cancel_account_link_intent(str(pending["intent_id"]).strip())

    session.pop(SESSION_PENDING_LINK_KEY, None)
    session.modified = True
    flash("Google account linking was cancelled.", "info")
    return redirect(url_for("auth.login"))


@auth_bp.route("/auth/google/link/cancel", methods=["GET"])
def cancel_google_link_get():
    """Reject state-changing cancel via GET; redirect safely without mutating session or intent state."""
    return redirect(url_for("auth.login"))


@auth_bp.route("/forgot-password", methods=["GET"])
@auth_bp.route("/auth/forgot-password", methods=["GET"])
def forgot_password_form():
    """Display the password reset request form."""
    if get_current_user() is not None:
        return redirect(url_for("catalog.index"))
    return render_template("auth/forgot_password.html")


@auth_bp.route("/forgot-password", methods=["POST"])
@auth_bp.route("/auth/forgot-password", methods=["POST"])
def forgot_password():
    """Handle password reset request with anti-enumeration protection and rate limiting."""
    if get_current_user() is not None:
        return redirect(url_for("catalog.index"))

    email = request.form.get("email", "").strip()
    client_ip = request.remote_addr or "127.0.0.1"

    if forgot_password_rate_limiter.is_rate_limited(client_ip, email):
        flash("Too many password reset requests. Please wait 5 minutes before trying again.", "error")
        return render_template("auth/forgot_password.html", email=email), 429

    forgot_password_rate_limiter.record_attempt(client_ip, email)

    if email:
        auth_service = AuthService()
        auth_service.request_password_reset(email, reset_url_builder=build_absolute_reset_url)

    backend = current_app.config.get("EMAIL_BACKEND", "memory")
    if backend == "smtp":
        flash(
            "If an eligible account exists for that email, a password reset link has been sent.",
            "info",
        )
    else:
        flash(
            "If an eligible account exists for that email, a password reset link has been generated. "
            "In this development environment, email delivery is simulated and no message is sent to your inbox.",
            "info",
        )
    return redirect(url_for("auth.login"))


@auth_bp.route("/reset-password/<token>", methods=["GET"])
@auth_bp.route("/auth/reset-password/<token>", methods=["GET"])
def reset_password_form(token: str):
    """Display the password reset form after validating the token.

    Sets Referrer-Policy and Cache-Control headers to prevent token leakage
    and caching of sensitive token-bearing pages.
    """
    cleaned_token = token.strip() if token else ""
    if not cleaned_token:
        flash("Invalid or missing password reset token.", "error")
        return redirect(url_for("auth.forgot_password_form"))

    auth_service = AuthService()
    reset_token = auth_service.get_valid_password_reset_token(cleaned_token)
    if reset_token is None:
        flash("The password reset link is invalid, expired, or has already been used.", "error")
        return redirect(url_for("auth.forgot_password_form"))

    response = make_response(render_template("auth/reset_password.html", token=cleaned_token))
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Cache-Control"] = "no-store, private"
    return response


@auth_bp.route("/reset-password/<token>", methods=["POST"])
@auth_bp.route("/auth/reset-password/<token>", methods=["POST"])
def reset_password(token: str):
    """Atomically consume a password reset token and update account password."""
    cleaned_token = token.strip() if token else request.form.get("token", "").strip()
    if not cleaned_token:
        flash("Invalid or missing password reset token.", "error")
        return redirect(url_for("auth.forgot_password_form"))

    auth_service = AuthService()
    reset_token = auth_service.get_valid_password_reset_token(cleaned_token)
    if reset_token is None:
        flash("The password reset link is invalid, expired, or has already been used.", "error")
        return redirect(url_for("auth.forgot_password_form"))

    password = request.form.get("password", "")
    confirm_password = request.form.get("confirm_password", "")

    try:
        auth_service.validate_password(password, confirm_password)
    except ValueError as e:
        flash(str(e), "error")
        response = make_response(
            render_template("auth/reset_password.html", token=cleaned_token), 400
        )
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store, private"
        return response

    updated_user = auth_service.reset_password(
        raw_token=cleaned_token,
        new_password=password,
        confirm_password=confirm_password,
    )
    if updated_user is None:
        flash("The password reset link is invalid, expired, or has already been used.", "error")
        return redirect(url_for("auth.forgot_password_form"))

    # Reset failed login/forgot-password rate limiters for this user
    client_ip = request.remote_addr or "127.0.0.1"
    login_rate_limiter.reset(client_ip, updated_user.email)
    forgot_password_rate_limiter.reset(client_ip, updated_user.email)

    # Do NOT auto-login user upon reset; wipe any existing local session to ensure clean re-auth
    raw_cart = session.get(SESSION_CART_KEY)
    sanitized_cart = extract_sanitized_cart_data(raw_cart)
    session.clear()
    generate_csrf_token()
    if sanitized_cart:
        session[SESSION_CART_KEY] = sanitized_cart
    session.modified = True

    flash(
        "Your password has been reset successfully. Please log in with your new password.",
        "success",
    )
    return redirect(url_for("auth.login"))

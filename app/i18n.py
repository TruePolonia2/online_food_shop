"""Internationalization and European Portuguese localization support (Phase 11.0.2)."""
from typing import Optional
from urllib.parse import unquote, urlparse

from flask import (
    Blueprint,
    Flask,
    current_app,
    has_request_context,
    redirect,
    request,
    session,
    url_for,
)
from flask_babel import Babel, get_locale, lazy_gettext as _

SUPPORTED_LOCALES = ("en", "pt_PT")
LOCALE_COOKIE_NAME = "locale"
LOCALE_COOKIE_MAX_AGE = 365 * 24 * 60 * 60  # 1 year

# Static markers for gettext extraction of status labels used dynamically by badge components
_STATUS_TRANSLATIONS = (
    _("Pending"),
    _("Confirmed"),
    _("Processing"),
    _("Shipped"),
    _("Delivered"),
    _("Cancelled"),
    _("Paid"),
    _("Unpaid"),
    _("Failed"),
    _("Refunded"),
)

locale_bp = Blueprint("locale", __name__)


def is_safe_redirect_url(target: Optional[str]) -> bool:
    """Validate that target URL is a safe local relative path.

    Rejects external URLs, protocol-relative URLs (e.g. //evil.com),
    backslashes, and control characters to prevent open-redirect vulnerabilities.
    """
    if not target or not isinstance(target, str):
        return False

    target = target.strip()
    if not target:
        return False

    decoded = unquote(target)
    if "\\" in target or "\\" in decoded:
        return False

    if target.startswith("//") or decoded.startswith("//"):
        return False

    parsed = urlparse(target)
    return parsed.scheme == "" and parsed.netloc == ""


def normalize_locale(locale_str: Optional[str]) -> str:
    """Normalize a locale identifier to supported application locales.

    Normalizes 'pt', 'pt-PT', 'pt_PT' (case-insensitive) to 'pt_PT'.
    All other values fall back to 'en'. Brazilian Portuguese ('pt-BR') is explicitly not mapped.
    """
    if not locale_str or not isinstance(locale_str, str):
        return "en"

    cleaned = locale_str.strip().lower().replace("-", "_")
    if cleaned in ("pt_pt", "pt"):
        return "pt_PT"
    return "en"


def get_locale_from_request() -> str:
    """Resolve the active locale for the current request context.

    Precedence order:
    1. Explicit session preference ('locale' in session).
    2. Explicit cookie preference ('locale' cookie).
    3. Accept-Language request header best match.
    4. Configured BABEL_DEFAULT_LOCALE ('en').
    """
    if not has_request_context():
        return "en"

    # 1. Session preference
    sess_locale = session.get("locale")
    if sess_locale:
        normalized = normalize_locale(sess_locale)
        if normalized in SUPPORTED_LOCALES:
            return normalized

    # 2. Cookie preference
    cookie_locale = request.cookies.get(LOCALE_COOKIE_NAME)
    if cookie_locale:
        normalized = normalize_locale(cookie_locale)
        if normalized in SUPPORTED_LOCALES:
            return normalized

    # 3. Accept-Language header matching
    best = request.accept_languages.best_match(["pt_PT", "pt", "en"])
    if best in ("pt_PT", "pt"):
        return "pt_PT"

    # 4. Fallback default
    if current_app:
        return current_app.config.get("BABEL_DEFAULT_LOCALE", "en")
    return "en"


def init_babel(app: Flask) -> Babel:
    """Initialize Flask-Babel extension on the Flask application."""
    babel = Babel(app, locale_selector=get_locale_from_request)

    @app.context_processor
    def inject_locale_context() -> dict:
        loc = str(get_locale()) if has_request_context() else "en"
        return {
            "current_locale": loc,
            "supported_locales": SUPPORTED_LOCALES,
        }

    return babel


@locale_bp.route("/set-locale", methods=["POST"])
def set_locale():
    """Update the user's active language preference via CSRF-protected POST."""
    raw_locale = request.form.get("locale", "")
    target_locale = normalize_locale(raw_locale)

    session["locale"] = target_locale
    session.modified = True

    next_url = request.form.get("next") or request.args.get("next")
    if not next_url or not is_safe_redirect_url(next_url):
        next_url = url_for("catalog.index")

    response = redirect(next_url)
    response.set_cookie(
        LOCALE_COOKIE_NAME,
        target_locale,
        max_age=LOCALE_COOKIE_MAX_AGE,
        samesite="Lax",
        secure=current_app.config.get("SESSION_COOKIE_SECURE", False),
        httponly=False,
    )
    return response

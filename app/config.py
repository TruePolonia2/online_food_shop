"""Configuration settings for the online food shop application."""
import os
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent


def _parse_bool_env(val: str | None, default: bool = False) -> bool:
    """Safely and predictably parse a boolean value from an environment string.

    Recognized truthy values (case-insensitive, whitespace-trimmed):
        '1', 'true', 'yes', 'on'
    Recognized falsy values:
        '0', 'false', 'no', 'off', ''
    Any unrecognized or missing value falls back to the provided default.
    """
    if val is None:
        return default
    val_clean = val.strip().lower()
    if val_clean in ("1", "true", "yes", "on"):
        return True
    if val_clean in ("0", "false", "no", "off", ""):
        return False
    return default


def _parse_int_env(val: str | None, default: int) -> int:
    """Safely parse an integer from an environment string, falling back to default."""
    if val is None or not val.strip():
        return default
    try:
        return int(val.strip())
    except ValueError:
        return default


def _parse_float_env(val: str | None, default: float) -> float:
    """Safely parse a float from an environment string, falling back to default."""
    if val is None or not val.strip():
        return default
    try:
        return float(val.strip())
    except ValueError:
        return default


def load_project_env(dotenv_path: Path | str | None = None, override: bool = False) -> bool:
    """Load environment variables from a project-local .env file.

    Preserves existing environment variables (override=False by default) to allow
    explicit process-level overrides. Returns True if a .env file was found and loaded.
    Respects FLASK_SKIP_DOTENV to allow deterministic control in automated test environments.
    """
    if dotenv_path is None and _parse_bool_env(os.environ.get("FLASK_SKIP_DOTENV"), default=False):
        return False
    target = Path(dotenv_path) if dotenv_path else BASE_DIR / ".env"
    if target.is_file():
        return load_dotenv(target, override=override)
    return False


# Automatically load environment variables from project-local .env if present
load_project_env()

# Known development-only secret key placeholder.
# This MUST NEVER be used in production environments.
DEV_SECRET_KEY = "dev-secret-key-change-in-production"


def _resolve_environment_name() -> str | None:
    """Resolve environment name according to strict documented precedence:
        1. APP_CONFIG
        2. FLASK_CONFIG
        3. FLASK_ENV
        4. ENV
    Returns None if none of these variables are set (or if empty/whitespace).
    """
    for var in ("APP_CONFIG", "FLASK_CONFIG", "FLASK_ENV", "ENV"):
        val = os.environ.get(var)
        if val is not None and val.strip():
            return val.strip().lower()
    return None


def is_production_env() -> bool:
    """Determine whether the currently configured environment is production."""
    env_name = _resolve_environment_name()
    return env_name in ("prod", "production")


class _ProductionSecretKeyDescriptor:
    """Descriptor that validates SECRET_KEY when loaded into Flask application config.

    Raises RuntimeError at application startup if SECRET_KEY is missing, empty,
    or set to the known development placeholder. Never logs or prints the secret.
    """

    def __get__(self, instance, owner):
        secret = os.environ.get("SECRET_KEY")
        if not secret or not secret.strip() or secret == DEV_SECRET_KEY:
            raise RuntimeError(
                "SECRET_KEY must be set in production to a secure, non-default value."
            )
        return secret


class _BaseSecretKeyDescriptor:
    """Descriptor for Config.SECRET_KEY.

    If in an automated test environment (TESTING=True), allows fallback to test secret.
    If running under production environment (APP_CONFIG, FLASK_CONFIG, FLASK_ENV, or ENV set to 'production'),
    enforces strict validation prohibiting missing or default dev keys.
    In development, provides DEV_SECRET_KEY as a convenient local default.
    """

    def __get__(self, instance, owner):
        if owner is not None and getattr(owner, "TESTING", False):
            return os.environ.get("SECRET_KEY", "testing-secret-key")

        secret = os.environ.get("SECRET_KEY")
        is_prod = is_production_env()

        if is_prod:
            if not secret or not secret.strip() or secret == DEV_SECRET_KEY:
                raise RuntimeError(
                    "SECRET_KEY must be set in production to a secure, non-default value."
                )
            return secret

        if secret and secret.strip():
            return secret

        return DEV_SECRET_KEY


class _DevelopmentSecretKeyDescriptor:
    """Descriptor for DevelopmentConfig.SECRET_KEY."""

    def __get__(self, instance, owner):
        return os.environ.get("SECRET_KEY", DEV_SECRET_KEY)


class _BaseDebugDescriptor:
    """Descriptor for Config.DEBUG (defaults to False)."""

    def __get__(self, instance, owner):
        return _parse_bool_env(os.environ.get("FLASK_DEBUG"), default=False)


class _DevelopmentDebugDescriptor:
    """Descriptor for DevelopmentConfig.DEBUG (defaults to True)."""

    def __get__(self, instance, owner):
        return _parse_bool_env(os.environ.get("FLASK_DEBUG"), default=True)


class _BaseSessionCookieSecureDescriptor:
    """Descriptor for Config.SESSION_COOKIE_SECURE.

    Defaults to True in recognized production environments and False elsewhere.
    Allows intentional override via SESSION_COOKIE_SECURE environment variable.
    """

    def __get__(self, instance, owner):
        default_secure = is_production_env()
        return _parse_bool_env(os.environ.get("SESSION_COOKIE_SECURE"), default=default_secure)


class _ProductionSessionCookieSecureDescriptor:
    """Descriptor for ProductionConfig.SESSION_COOKIE_SECURE (defaults to True)."""

    def __get__(self, instance, owner):
        return _parse_bool_env(os.environ.get("SESSION_COOKIE_SECURE"), default=True)


class _ProductionEmailPreviewDescriptor:
    """Descriptor that strictly prevents email console preview in production.

    Always returns False regardless of environment variable settings.
    """

    def __get__(self, instance, owner):
        return False


class _DevelopmentSessionCookieSecureDescriptor:
    """Descriptor for DevelopmentConfig.SESSION_COOKIE_SECURE (defaults to False)."""

    def __get__(self, instance, owner):
        return _parse_bool_env(os.environ.get("SESSION_COOKIE_SECURE"), default=False)


class _DatabasePathDescriptor:
    """Descriptor for Config.DATABASE_PATH."""

    def __get__(self, instance, owner):
        return os.environ.get(
            "DATABASE_PATH",
            str(BASE_DIR / "instance" / "online_food_shop.sqlite3"),
        )


class _GoogleClientIdDescriptor:
    """Descriptor for Config.GOOGLE_CLIENT_ID."""

    def __get__(self, instance, owner):
        return os.environ.get("GOOGLE_CLIENT_ID")


class _GoogleClientSecretDescriptor:
    """Descriptor for Config.GOOGLE_CLIENT_SECRET."""

    def __get__(self, instance, owner):
        return os.environ.get("GOOGLE_CLIENT_SECRET")


class _StripeSecretKeyDescriptor:
    """Descriptor for Config.STRIPE_SECRET_KEY.

    Enforces that only Stripe TEST MODE credentials (sk_test_*) are accepted.
    Rejects any live credentials (sk_live_*) immediately.
    """

    def __get__(self, instance, owner):
        key = os.environ.get("STRIPE_SECRET_KEY")
        if not key or not key.strip():
            return None
        cleaned = key.strip()
        if cleaned.startswith("sk_live_"):
            raise ValueError(
                "Live Stripe credentials are prohibited. Only Stripe test mode keys (sk_test_*) are supported."
            )
        if not cleaned.startswith("sk_test_"):
            raise ValueError(
                "Invalid Stripe secret key format. Only test mode keys starting with 'sk_test_' are supported."
            )
        return cleaned


class _StripePublishableKeyDescriptor:
    """Descriptor for Config.STRIPE_PUBLISHABLE_KEY.

    Validates test mode key format (pk_test_*). Rejects live keys (pk_live_*).
    """

    def __get__(self, instance, owner):
        key = os.environ.get("STRIPE_PUBLISHABLE_KEY")
        if not key or not key.strip():
            return None
        cleaned = key.strip()
        if cleaned.startswith("pk_live_"):
            raise ValueError(
                "Live Stripe credentials are prohibited. Only Stripe test mode keys (pk_test_*) are supported."
            )
        if not cleaned.startswith("pk_test_"):
            raise ValueError(
                "Invalid Stripe publishable key format. Only test mode keys starting with 'pk_test_' are supported."
            )
        return cleaned


class _StripeWebhookSecretDescriptor:
    """Descriptor for Config.STRIPE_WEBHOOK_SECRET.

    Stores the webhook signing secret for Stripe event verification.
    Rejects live webhook secrets to enforce test mode exclusively.
    """

    def __get__(self, instance, owner):
        key = os.environ.get("STRIPE_WEBHOOK_SECRET")
        if not key or not key.strip():
            return None
        cleaned = key.strip()
        if cleaned.startswith("whsec_live_"):
            raise ValueError(
                "Live Stripe credentials are prohibited. Only Stripe test mode webhook secrets are supported."
            )
        return cleaned


class _StripeCurrencyDescriptor:
    """Descriptor for Config.STRIPE_CURRENCY (defaults to 'eur')."""

    def __get__(self, instance, owner):
        curr = os.environ.get("STRIPE_CURRENCY", "eur")
        return curr.strip().lower() if curr else "eur"


class Config:
    """Base application configuration.

    Production deployment guidelines:
    - SECRET_KEY: Set via the SECRET_KEY environment variable to a cryptographically
      secure, persistent random string (e.g. `python -c "import secrets; print(secrets.token_hex(32))"`).
    - FLASK_DEBUG: Keep unset or set to '0' / 'false'.
    - SESSION_COOKIE_SECURE: Defaults to True in production (HTTPS) and False in development (HTTP).
    - SESSION_COOKIE_HTTPONLY: Enabled by default (True) to protect cookies from XSS.
    - SESSION_COOKIE_SAMESITE: Set to 'Lax' by default to protect against CSRF attacks.
    """

    ENV_NAME = "base"
    DATABASE_PATH = _DatabasePathDescriptor()
    TESTING = False
    CSRF_ENABLED = True

    # Google OAuth 2.0 / OpenID Connect credentials
    GOOGLE_CLIENT_ID = _GoogleClientIdDescriptor()
    GOOGLE_CLIENT_SECRET = _GoogleClientSecretDescriptor()

    # Stripe Payment Configuration (Phase 10.12 - Test Mode Only)
    STRIPE_SECRET_KEY = _StripeSecretKeyDescriptor()
    STRIPE_PUBLISHABLE_KEY = _StripePublishableKeyDescriptor()
    STRIPE_WEBHOOK_SECRET = _StripeWebhookSecretDescriptor()
    STRIPE_CURRENCY = _StripeCurrencyDescriptor()

    # Email Delivery Configuration (Phase 10.11)
    EMAIL_BACKEND = os.environ.get("EMAIL_BACKEND", "memory").strip().lower()
    SMTP_HOST = os.environ.get("SMTP_HOST", "").strip()
    SMTP_PORT = _parse_int_env(os.environ.get("SMTP_PORT"), 587)
    SMTP_SECURITY = os.environ.get("SMTP_SECURITY", "starttls").strip().lower()
    SMTP_USERNAME = os.environ.get("SMTP_USERNAME", "").strip()
    SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
    SMTP_FROM_ADDRESS = os.environ.get("SMTP_FROM_ADDRESS", "").strip()
    SMTP_TIMEOUT = _parse_float_env(os.environ.get("SMTP_TIMEOUT"), 10.0)
    APP_BASE_URL = os.environ.get("APP_BASE_URL", "http://127.0.0.1:5000").strip()
    EMAIL_DEV_CONSOLE_PREVIEW = _parse_bool_env(
        os.environ.get("EMAIL_DEV_CONSOLE_PREVIEW"), default=False
    )

    # Hardened session cookie settings
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"

    DEBUG = _BaseDebugDescriptor()
    SECRET_KEY = _BaseSecretKeyDescriptor()
    SESSION_COOKIE_SECURE = _BaseSessionCookieSecureDescriptor()


class ProductionConfig(Config):
    """Production configuration enforcing strict security requirements.

    Requirements:
    - SECRET_KEY must be explicitly provided in the environment and must not
      match the development placeholder.
    - DEBUG is strictly disabled by default.
    - SESSION_COOKIE_SECURE defaults to True for HTTPS, configurable via env var.
    - EMAIL_DEV_CONSOLE_PREVIEW is strictly disabled (False) regardless of env vars.
    """

    ENV_NAME = "production"
    DEBUG = _BaseDebugDescriptor()
    SECRET_KEY = _ProductionSecretKeyDescriptor()
    SESSION_COOKIE_SECURE = _ProductionSessionCookieSecureDescriptor()
    EMAIL_DEV_CONSOLE_PREVIEW = _ProductionEmailPreviewDescriptor()


class DevelopmentConfig(Config):
    """Development configuration with convenient local defaults.

    - DEBUG defaults to True for interactive debugging.
    - SECRET_KEY falls back to DEV_SECRET_KEY for zero-config local runs.
    - SESSION_COOKIE_SECURE defaults to False for local HTTP servers.
    - EMAIL_DEV_CONSOLE_PREVIEW defaults to False (explicit opt-in required).
    """

    ENV_NAME = "development"
    DEBUG = _DevelopmentDebugDescriptor()
    SECRET_KEY = _DevelopmentSecretKeyDescriptor()
    SESSION_COOKIE_SECURE = _DevelopmentSessionCookieSecureDescriptor()
    EMAIL_DEV_CONSOLE_PREVIEW = _parse_bool_env(
        os.environ.get("EMAIL_DEV_CONSOLE_PREVIEW"), default=False
    )


class TestingConfig(Config):
    """Testing configuration for automated tests.

    - TESTING is True.
    - DEBUG is False.
    - SECRET_KEY is set to a deterministic test secret.
    - SESSION_COOKIE_SECURE is False for HTTP test clients.
    - EMAIL_BACKEND defaults to memory for isolated offline testing.
    - EMAIL_DEV_CONSOLE_PREVIEW is False to avoid noisy test output.
    """

    ENV_NAME = "testing"
    TESTING = True
    DEBUG = False
    SECRET_KEY = os.environ.get("SECRET_KEY", "testing-secret-key")
    CSRF_ENABLED = True
    SESSION_COOKIE_SECURE = False
    EMAIL_BACKEND = "memory"
    EMAIL_DEV_CONSOLE_PREVIEW = False


CONFIG_MAP: dict[str, type[Config]] = {
    "development": DevelopmentConfig,
    "dev": DevelopmentConfig,
    "production": ProductionConfig,
    "prod": ProductionConfig,
    "testing": TestingConfig,
    "test": TestingConfig,
}


def get_config_by_name(name: str | None = None) -> type[Config]:
    """Retrieve configuration class by environment name or configuration alias.

    Precedence when name is None:
        APP_CONFIG -> FLASK_CONFIG -> FLASK_ENV -> ENV -> 'development'

    Raises ValueError if the name or environment variable is unrecognized.
    """
    if name is None:
        name = _resolve_environment_name()
        if name is None:
            return DevelopmentConfig

    name_clean = name.strip().lower()
    if name_clean in CONFIG_MAP:
        return CONFIG_MAP[name_clean]

    valid_options = ", ".join(f"'{k}'" for k in sorted(CONFIG_MAP.keys()))
    raise ValueError(
        f"Unknown configuration name '{name}'. Valid options are: {valid_options}."
    )


def is_stripe_configured(config: Any = None) -> bool:
    """Check whether Stripe integration is configured with a valid test mode key.

    Args:
        config: Application configuration dictionary or Config object.
            If omitted or None, uses current_app.config or os.environ.

    Returns:
        True if STRIPE_SECRET_KEY is configured with a test key starting with 'sk_test_'.
    """
    if config is not None:
        if hasattr(config, "get"):
            key = config.get("STRIPE_SECRET_KEY")
        else:
            key = getattr(config, "STRIPE_SECRET_KEY", None)
    else:
        try:
            from flask import current_app, has_app_context

            if has_app_context() and current_app:
                key = current_app.config.get("STRIPE_SECRET_KEY")
            else:
                key = os.environ.get("STRIPE_SECRET_KEY")
        except ImportError:
            key = os.environ.get("STRIPE_SECRET_KEY")

    if not key or not isinstance(key, str) or not key.strip():
        return False
    return key.strip().startswith("sk_test_")


def is_stripe_webhook_configured(config: Any = None) -> bool:
    """Check whether Stripe webhook integration is configured with a secret.

    Args:
        config: Application configuration dictionary or Config object.

    Returns:
        True if STRIPE_WEBHOOK_SECRET is non-empty.
    """
    if config is not None:
        if hasattr(config, "get"):
            secret = config.get("STRIPE_WEBHOOK_SECRET")
        else:
            secret = getattr(config, "STRIPE_WEBHOOK_SECRET", None)
    else:
        try:
            from flask import current_app, has_app_context

            if has_app_context() and current_app:
                secret = current_app.config.get("STRIPE_WEBHOOK_SECRET")
            else:
                secret = os.environ.get("STRIPE_WEBHOOK_SECRET")
        except ImportError:
            secret = os.environ.get("STRIPE_WEBHOOK_SECRET")

    if not secret or not isinstance(secret, str) or not secret.strip():
        return False
    return True

"""Configuration settings for the online food shop application."""
import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent


def load_project_env(dotenv_path: Path | str | None = None, override: bool = False) -> bool:
    """Load environment variables from a project-local .env file.

    Preserves existing environment variables (override=False by default) to allow
    explicit process-level overrides. Returns True if a .env file was found and loaded.
    """
    target = Path(dotenv_path) if dotenv_path else BASE_DIR / ".env"
    if target.is_file():
        return load_dotenv(target, override=override)
    return False


# Automatically load environment variables from project-local .env if present
load_project_env()

# Known development-only secret key placeholder.
# This MUST NEVER be used in production environments.
DEV_SECRET_KEY = "dev-secret-key-change-in-production"


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

    DATABASE_PATH = _DatabasePathDescriptor()
    TESTING = False
    CSRF_ENABLED = True

    # Google OAuth 2.0 / OpenID Connect credentials
    GOOGLE_CLIENT_ID = _GoogleClientIdDescriptor()
    GOOGLE_CLIENT_SECRET = _GoogleClientSecretDescriptor()

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
    """

    DEBUG = _BaseDebugDescriptor()
    SECRET_KEY = _ProductionSecretKeyDescriptor()
    SESSION_COOKIE_SECURE = _ProductionSessionCookieSecureDescriptor()


class DevelopmentConfig(Config):
    """Development configuration with convenient local defaults.

    - DEBUG defaults to True for interactive debugging.
    - SECRET_KEY falls back to DEV_SECRET_KEY for zero-config local runs.
    - SESSION_COOKIE_SECURE defaults to False for local HTTP servers.
    """

    DEBUG = _DevelopmentDebugDescriptor()
    SECRET_KEY = _DevelopmentSecretKeyDescriptor()
    SESSION_COOKIE_SECURE = _DevelopmentSessionCookieSecureDescriptor()


class TestingConfig(Config):
    """Testing configuration for automated tests.

    - TESTING is True.
    - DEBUG is False.
    - SECRET_KEY is set to a deterministic test secret.
    - SESSION_COOKIE_SECURE is False for HTTP test clients.
    """

    TESTING = True
    DEBUG = False
    SECRET_KEY = os.environ.get("SECRET_KEY", "testing-secret-key")
    CSRF_ENABLED = True
    SESSION_COOKIE_SECURE = False


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

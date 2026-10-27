"""Google OAuth 2.0 and OpenID Connect integration using Authlib."""
from typing import Any, Optional
from authlib.integrations.flask_client import OAuth
from flask import Flask, current_app

oauth = OAuth()


def is_google_auth_configured(config: Optional[Any] = None) -> bool:
    """Check whether Google OAuth credentials are configured.

    Args:
        config: Application configuration dictionary or Config object.
            If omitted or None, uses current_app.config.

    Returns:
        True if both GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET are non-empty strings.
    """
    if config is not None:
        if hasattr(config, "get"):
            cfg = config
        else:
            cfg = getattr(config, "__dict__", {})
    else:
        if not current_app:
            return False
        cfg = current_app.config

    client_id = cfg.get("GOOGLE_CLIENT_ID")
    client_secret = cfg.get("GOOGLE_CLIENT_SECRET")

    if not client_id or not isinstance(client_id, str) or not client_id.strip():
        return False
    if not client_secret or not isinstance(client_secret, str) or not client_secret.strip():
        return False
    return True


def init_oauth(app: Flask) -> None:
    """Register Google OAuth client and bind Authlib to the Flask application.

    Prepopulates the standard Google OpenID Connect endpoints and sets _loaded_at
    so that metadata fetching does not trigger live network requests during
    application startup or authorization URL generation.
    """
    oauth.init_app(app)
    oauth._clients.clear()
    oauth.register(
        name="google",
        client_id=app.config.get("GOOGLE_CLIENT_ID"),
        client_secret=app.config.get("GOOGLE_CLIENT_SECRET"),
        server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
        authorization_endpoint="https://accounts.google.com/o/oauth2/v2/auth",
        token_endpoint="https://oauth2.googleapis.com/token",
        userinfo_endpoint="https://openidconnect.googleapis.com/v1/userinfo",
        jwks_uri="https://www.googleapis.com/oauth2/v3/certs",
        issuer="https://accounts.google.com",
        _loaded_at=1.0,
        overwrite=True,
        client_kwargs={
            "scope": "openid email profile",
            "prompt": "select_account",
            "code_challenge_method": "S256",
        },
    )

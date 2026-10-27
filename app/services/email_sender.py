"""Email delivery abstractions, SMTP provider, and in-memory test sender."""
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.message import EmailMessage
import logging
import os
import smtplib
import ssl
from typing import Optional, Protocol, Union, runtime_checkable

from flask import Flask, current_app, has_app_context

from app.config import is_production_env

logger = logging.getLogger(__name__)


@dataclass
class SentEmail:
    """Record of an email sent by an EmailSender implementation."""

    to_email: str
    raw_token: str
    sent_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    subject: str = "Verify your email address"
    email_type: str = "verification"
    reset_url: Optional[str] = None
    verification_url: Optional[str] = None


@runtime_checkable
class EmailSender(Protocol):
    """Protocol defining the interface for sending system emails."""

    def send_verification_email(
        self, to_email: str, raw_token: str, verification_url: Optional[str] = None
    ) -> bool:
        """Send an email verification message containing the raw token.

        Args:
            to_email: The destination email address.
            raw_token: The plaintext verification token.
            verification_url: Optional trusted absolute URL for email verification.

        Returns:
            True if delivery succeeded, False otherwise.
        """
        ...

    def send_password_reset_email(
        self, to_email: str, raw_token: str, reset_url: Optional[str] = None
    ) -> bool:
        """Send a password reset message containing the raw token and reset URL.

        Args:
            to_email: The destination email address.
            raw_token: The plaintext reset token.
            reset_url: Optional trusted absolute URL for password reset.

        Returns:
            True if delivery succeeded, False otherwise.
        """
        ...


class InMemoryEmailSender:
    """In-memory implementation of EmailSender for tests and local development.

    Records sent emails in an internal list without network or SMTP I/O.
    Optionally prints a clean console preview during local development.
    """

    def __init__(
        self, simulate_failure: bool = False, dev_preview: bool = False
    ) -> None:
        """Initialize InMemoryEmailSender.

        Args:
            simulate_failure: If True, calls to send methods fail.
            dev_preview: If True, prints a formatted console preview of each email.
        """
        self.sent_messages: list[SentEmail] = []
        self.simulate_failure: bool = simulate_failure
        self.dev_preview: bool = dev_preview

    def _print_dev_preview(self, email: SentEmail) -> None:
        url = email.reset_url or email.verification_url or f"(token: {email.raw_token})"
        print("\n" + "=" * 70)
        print("[SIMULATED EMAIL - DEV PREVIEW]")
        print(f"To:      {email.to_email}")
        print(f"Subject: {email.subject}")
        print(f"Type:    {email.email_type}")
        print(f"Link:    {url}")
        print("=" * 70 + "\n", flush=True)

    def send_verification_email(
        self, to_email: str, raw_token: str, verification_url: Optional[str] = None
    ) -> bool:
        """Record the sent verification email in-memory, or simulate failure.

        Args:
            to_email: The destination email address.
            raw_token: The plaintext verification token.
            verification_url: Optional trusted verification URL.

        Returns:
            True if delivery succeeded, False if simulated failure.
        """
        if self.simulate_failure:
            return False

        message = SentEmail(
            to_email=to_email,
            raw_token=raw_token,
            subject="Verify your email address",
            email_type="verification",
            verification_url=verification_url,
        )
        self.sent_messages.append(message)
        if self.dev_preview:
            self._print_dev_preview(message)
        return True

    def send_password_reset_email(
        self, to_email: str, raw_token: str, reset_url: Optional[str] = None
    ) -> bool:
        """Record the sent password reset email in-memory, or simulate failure.

        Args:
            to_email: The destination email address.
            raw_token: The plaintext reset token.
            reset_url: Optional trusted absolute URL for password reset.

        Returns:
            True if delivery succeeded, False if simulated failure.
        """
        if self.simulate_failure:
            return False

        message = SentEmail(
            to_email=to_email,
            raw_token=raw_token,
            subject="Reset your password",
            email_type="password_reset",
            reset_url=reset_url,
        )
        self.sent_messages.append(message)
        if self.dev_preview:
            self._print_dev_preview(message)
        return True

    def clear(self) -> None:
        """Clear all recorded sent messages."""
        self.sent_messages.clear()

    @property
    def last_email(self) -> Optional[SentEmail]:
        """Return the most recently sent email, or None if none sent."""
        return self.sent_messages[-1] if self.sent_messages else None


class SMTPEmailSender:
    """Production SMTP email sender supporting STARTTLS and implicit TLS.

    Security & operational guarantees:
    - Supports STARTTLS (port 587) and implicit TLS/SMTPS (port 465).
    - Verifies TLS certificates using standard SSLContext by default.
    - Strictly refuses to transmit credentials over unencrypted connections ('security=none').
    - Configurable connection and socket read timeouts.
    - Strictly UTF-8 formatted plain text messages.
    - Never logs passwords, SMTP credentials, or token secrets.
    """

    def __init__(
        self,
        host: str,
        port: int = 587,
        security: str = "starttls",
        username: Optional[str] = None,
        password: Optional[str] = None,
        from_address: str = "",
        timeout: float = 10.0,
        ssl_context: Optional[ssl.SSLContext] = None,
        app_base_url: Optional[str] = None,
    ) -> None:
        if not host or not isinstance(host, str) or not host.strip():
            raise ValueError("SMTP host must be a non-empty string.")
        self.host = host.strip()

        if isinstance(port, bool) or not isinstance(port, int) or port < 1 or port > 65535:
            raise ValueError(f"SMTP port must be an integer between 1 and 65535, got '{port}'.")
        self.port = port

        normalized_security = security.strip().lower() if isinstance(security, str) else "starttls"
        if normalized_security == "tls":
            normalized_security = "ssl"
        if normalized_security not in ("starttls", "ssl", "none"):
            raise ValueError(
                f"Unsupported SMTP security '{security}'. Supported options: 'starttls', 'ssl', 'none'."
            )
        self.security = normalized_security

        self.username = username.strip() if username and isinstance(username, str) else None
        self.password = password if password and isinstance(password, str) else None

        if (self.username or self.password) and self.security == "none":
            raise ValueError(
                "Refusing to send SMTP credentials over an unencrypted connection ('security=none'). "
                "Use 'starttls' or 'ssl'."
            )

        if not from_address or not isinstance(from_address, str) or "@" not in from_address:
            raise ValueError("SMTP from_address must be a valid email address.")
        self.from_address = from_address.strip()

        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
            raise ValueError(f"SMTP timeout must be a positive number, got '{timeout}'.")
        self.timeout = float(timeout)

        self.ssl_context = ssl_context or ssl.create_default_context()
        self.app_base_url = (
            app_base_url.strip().rstrip("/")
            if app_base_url and isinstance(app_base_url, str)
            else "http://127.0.0.1:5000"
        )

    def _build_verification_url(self, raw_token: str) -> str:
        return f"{self.app_base_url}/verify-email/{raw_token}"

    def _build_reset_url(self, raw_token: str) -> str:
        return f"{self.app_base_url}/auth/reset-password/{raw_token}"

    def _create_message(
        self, to_email: str, subject: str, text_content: str, email_type: str
    ) -> EmailMessage:
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = self.from_address
        msg["To"] = to_email.strip()
        msg["X-Email-Type"] = email_type
        msg.set_content(text_content, charset="utf-8")
        return msg

    def _send_via_smtp(self, msg: EmailMessage, to_email: str) -> bool:
        server = None
        try:
            if self.security == "ssl":
                server = smtplib.SMTP_SSL(
                    self.host,
                    self.port,
                    timeout=self.timeout,
                    context=self.ssl_context,
                )
                server.ehlo()
            else:
                server = smtplib.SMTP(self.host, self.port, timeout=self.timeout)
                server.ehlo()
                if self.security == "starttls":
                    server.starttls(context=self.ssl_context)
                    server.ehlo()

            if self.username and self.password:
                server.login(self.username, self.password)

            server.send_message(msg)
            return True
        except (smtplib.SMTPException, ssl.SSLError, OSError, TimeoutError) as exc:
            # Privacy guarantee: Never log credentials, tokens, or passwords
            logger.error(
                "SMTP delivery failed for recipient '%s' (host=%s, port=%d): %s",
                to_email,
                self.host,
                self.port,
                type(exc).__name__,
            )
            return False
        finally:
            if server is not None:
                try:
                    server.quit()
                except Exception:
                    pass

    def send_verification_email(
        self, to_email: str, raw_token: str, verification_url: Optional[str] = None
    ) -> bool:
        """Send an account verification email via SMTP."""
        url = verification_url or self._build_verification_url(raw_token)
        subject = "Verify your email address - Online Food Shop"
        body = (
            f"Hello,\n\n"
            f"Thank you for registering an account at Online Food Shop!\n\n"
            f"Please click the link below to verify your email address and activate your account:\n\n"
            f"{url}\n\n"
            f"This link is valid for 24 hours.\n\n"
            f"If you did not create an account with us, please ignore this email.\n\n"
            f"Best regards,\n"
            f"Online Food Shop Team\n"
        )
        msg = self._create_message(to_email, subject, body, "verification")
        return self._send_via_smtp(msg, to_email)

    def send_password_reset_email(
        self, to_email: str, raw_token: str, reset_url: Optional[str] = None
    ) -> bool:
        """Send a password reset email via SMTP."""
        url = reset_url or self._build_reset_url(raw_token)
        subject = "Reset your password - Online Food Shop"
        body = (
            f"Hello,\n\n"
            f"We received a request to reset the password for your Online Food Shop account.\n\n"
            f"Please click the link below to set a new password:\n\n"
            f"{url}\n\n"
            f"For your security:\n"
            f"- This link will expire in 30 minutes.\n"
            f"- This link can only be used once.\n\n"
            f"If you did not request a password reset, please ignore this email. Your password will remain unchanged.\n\n"
            f"Best regards,\n"
            f"Online Food Shop Team\n"
        )
        msg = self._create_message(to_email, subject, body, "password_reset")
        return self._send_via_smtp(msg, to_email)


def is_safe_dev_preview_allowed(config: Union[dict, any]) -> bool:
    """Determine whether development console preview is strictly permitted.

    Requires all 4 security conditions:
    1. EMAIL_BACKEND is 'memory'.
    2. EMAIL_DEV_CONSOLE_PREVIEW is explicitly True.
    3. Flask DEBUG is True.
    4. The application is explicitly in a development environment, not production or testing.
    """
    def _get(key, default=None):
        if hasattr(config, "get"):
            return config.get(key, default)
        return getattr(config, key, default)

    # Condition 1: Backend must be memory
    backend = str(_get("EMAIL_BACKEND", "memory")).strip().lower()
    if backend != "memory":
        return False

    # Condition 2: Explicitly enabled
    preview = _get("EMAIL_DEV_CONSOLE_PREVIEW", False)
    if preview is not True:
        return False

    # Condition 3: Flask DEBUG must be True
    debug_mode = _get("DEBUG", False)
    if debug_mode is not True:
        return False

    # Condition 4: Not testing and not production; explicitly development
    if bool(_get("TESTING", False)):
        return False

    if is_production_env():
        return False

    env_name = str(_get("ENV_NAME", "")).strip().lower()
    if env_name in ("prod", "production"):
        return False

    if env_name not in ("dev", "development"):
        return False

    return True


def get_email_sender(config: Optional[Union[dict, any]] = None) -> EmailSender:
    """Factory creating the configured EmailSender instance.

    Validates configuration strictly if EMAIL_BACKEND is 'smtp'.
    Never exposes passwords or SMTP secrets in error messages.
    Never silently falls back from 'smtp' to 'memory' upon configuration or delivery failure.
    """
    if config is None:
        if has_app_context():
            config = current_app.config
        else:
            config = os.environ

    backend_val = (
        config.get("EMAIL_BACKEND", "memory")
        if hasattr(config, "get")
        else getattr(config, "EMAIL_BACKEND", "memory")
    )
    backend = str(backend_val).strip().lower() if backend_val is not None else "memory"

    if backend == "memory":
        dev_preview = is_safe_dev_preview_allowed(config)
        return InMemoryEmailSender(dev_preview=dev_preview)

    if backend == "smtp":
        def _get(key, default=""):
            if hasattr(config, "get"):
                return config.get(key, default)
            return getattr(config, key, default)

        host = str(_get("SMTP_HOST", "")).strip()
        port_val = _get("SMTP_PORT", 587)
        try:
            port = int(port_val)
        except (ValueError, TypeError):
            raise ValueError(f"SMTP port must be an integer between 1 and 65535, got '{port_val}'.")

        security = str(_get("SMTP_SECURITY", "starttls")).strip()
        username = str(_get("SMTP_USERNAME", "")).strip() or None
        password = str(_get("SMTP_PASSWORD", "")) or None
        from_address = str(_get("SMTP_FROM_ADDRESS", "")).strip()
        timeout_val = _get("SMTP_TIMEOUT", 10.0)
        try:
            timeout = float(timeout_val)
        except (ValueError, TypeError):
            timeout = 10.0
        app_base_url = str(_get("APP_BASE_URL", "http://127.0.0.1:5000")).strip()

        return SMTPEmailSender(
            host=host,
            port=port,
            security=security,
            username=username,
            password=password,
            from_address=from_address,
            timeout=timeout,
            app_base_url=app_base_url,
        )

    raise ValueError(
        f"Unsupported EMAIL_BACKEND '{backend}'. Supported options are: 'memory', 'smtp'."
    )


def init_email(app: Flask) -> None:
    """Initialize the email sender extension on the Flask application."""
    app.extensions["email_sender"] = get_email_sender(app.config)

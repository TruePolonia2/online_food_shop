"""Tests for SMTP email sender, configuration, security, and integration."""
from email.message import EmailMessage
import logging
import ssl
from unittest.mock import MagicMock, patch

import pytest

from app import create_app
from app.config import Config, DevelopmentConfig, ProductionConfig, TestingConfig
from app.database.connection import get_db, init_db, transaction
from app.models.user import User, UserRole
from app.repositories.user_repository import UserRepository
from app.services.auth_service import AuthService
from app.services.email_sender import (
    EmailSender,
    InMemoryEmailSender,
    SMTPEmailSender,
    get_email_sender,
    init_email,
)


class TestSMTPConfigurationValidation:
    """Tests configuration parsing, parameter validation, and security constraints for SMTPEmailSender."""

    def test_valid_starttls_configuration(self):
        sender = SMTPEmailSender(
            host="smtp.example.com",
            port=587,
            security="starttls",
            username="user@example.com",
            password="secret-password",
            from_address="noreply@example.com",
            timeout=15.0,
            app_base_url="https://shop.example.com",
        )
        assert sender.host == "smtp.example.com"
        assert sender.port == 587
        assert sender.security == "starttls"
        assert sender.username == "user@example.com"
        assert sender.password == "secret-password"
        assert sender.from_address == "noreply@example.com"
        assert sender.timeout == 15.0
        assert sender.app_base_url == "https://shop.example.com"

    def test_valid_implicit_ssl_configuration(self):
        sender = SMTPEmailSender(
            host="smtp.gmail.com",
            port=465,
            security="ssl",
            from_address="noreply@example.com",
        )
        assert sender.port == 465
        assert sender.security == "ssl"

    def test_security_tls_alias_normalized_to_ssl(self):
        sender = SMTPEmailSender(
            host="smtp.example.com",
            port=465,
            security="tls",
            from_address="noreply@example.com",
        )
        assert sender.security == "ssl"

    def test_empty_host_raises_value_error(self):
        with pytest.raises(ValueError, match="SMTP host must be a non-empty string"):
            SMTPEmailSender(host="", from_address="test@example.com")

        with pytest.raises(ValueError, match="SMTP host must be a non-empty string"):
            SMTPEmailSender(host="   ", from_address="test@example.com")

    def test_invalid_port_raises_value_error(self):
        with pytest.raises(ValueError, match="SMTP port must be an integer between 1 and 65535"):
            SMTPEmailSender(host="smtp.example.com", port=0, from_address="test@example.com")

        with pytest.raises(ValueError, match="SMTP port must be an integer between 1 and 65535"):
            SMTPEmailSender(host="smtp.example.com", port=70000, from_address="test@example.com")

        with pytest.raises(ValueError, match="SMTP port must be an integer between 1 and 65535"):
            SMTPEmailSender(host="smtp.example.com", port=True, from_address="test@example.com")

    def test_invalid_security_mode_raises_value_error(self):
        with pytest.raises(ValueError, match="Unsupported SMTP security 'invalid'"):
            SMTPEmailSender(
                host="smtp.example.com",
                security="invalid",
                from_address="test@example.com",
            )

    def test_refuse_credentials_over_unencrypted_connection(self):
        """Security requirement: Never send credentials over unencrypted security=none."""
        with pytest.raises(ValueError, match="Refusing to send SMTP credentials over an unencrypted connection"):
            SMTPEmailSender(
                host="smtp.example.com",
                port=25,
                security="none",
                username="plain_user",
                password="plain_password",
                from_address="noreply@example.com",
            )

    def test_invalid_from_address_raises_value_error(self):
        with pytest.raises(ValueError, match="SMTP from_address must be a valid email address"):
            SMTPEmailSender(host="smtp.example.com", from_address="")

        with pytest.raises(ValueError, match="SMTP from_address must be a valid email address"):
            SMTPEmailSender(host="smtp.example.com", from_address="invalid-email-no-at")

    def test_invalid_timeout_raises_value_error(self):
        with pytest.raises(ValueError, match="SMTP timeout must be a positive number"):
            SMTPEmailSender(host="smtp.example.com", from_address="test@example.com", timeout=-5)

        with pytest.raises(ValueError, match="SMTP timeout must be a positive number"):
            SMTPEmailSender(host="smtp.example.com", from_address="test@example.com", timeout=True)


class TestSMTPEmailSenderDelivery:
    """Tests SMTP connection handling, STARTTLS, SSL, authentication, formatting, and offline mocking."""

    @patch("smtplib.SMTP")
    def test_send_verification_starttls_success(self, mock_smtp_cls):
        mock_server = MagicMock()
        mock_smtp_cls.return_value = mock_server

        sender = SMTPEmailSender(
            host="smtp.example.com",
            port=587,
            security="starttls",
            username="sender@example.com",
            password="secretpassword",
            from_address="noreply@example.com",
            timeout=10.0,
            app_base_url="http://127.0.0.1:5000",
        )

        sent = sender.send_verification_email("user@example.com", "raw-token-12345")
        assert sent is True

        mock_smtp_cls.assert_called_once_with("smtp.example.com", 587, timeout=10.0)
        assert mock_server.ehlo.call_count == 2
        mock_server.starttls.assert_called_once_with(context=sender.ssl_context)
        mock_server.login.assert_called_once_with("sender@example.com", "secretpassword")
        mock_server.send_message.assert_called_once()
        mock_server.quit.assert_called_once()

        # Check delivered message structure
        sent_msg = mock_server.send_message.call_args[0][0]
        assert isinstance(sent_msg, EmailMessage)
        assert sent_msg["Subject"] == "Verify your email address - Online Food Shop"
        assert sent_msg["From"] == "noreply@example.com"
        assert sent_msg["To"] == "user@example.com"
        assert sent_msg["X-Email-Type"] == "verification"
        body = sent_msg.get_content()
        assert "http://127.0.0.1:5000/verify-email/raw-token-12345" in body
        assert "24 hours" in body

    @patch("smtplib.SMTP_SSL")
    def test_send_password_reset_implicit_ssl_success(self, mock_ssl_cls):
        mock_server = MagicMock()
        mock_ssl_cls.return_value = mock_server

        sender = SMTPEmailSender(
            host="smtp.gmail.com",
            port=465,
            security="ssl",
            username="shop@gmail.com",
            password="app-password-xyz",
            from_address="shop@gmail.com",
            timeout=12.0,
            app_base_url="https://myshop.com",
        )

        sent = sender.send_password_reset_email("customer@example.com", "reset-token-67890")
        assert sent is True

        mock_ssl_cls.assert_called_once_with(
            "smtp.gmail.com", 465, timeout=12.0, context=sender.ssl_context
        )
        mock_server.ehlo.assert_called_once()
        mock_server.login.assert_called_once_with("shop@gmail.com", "app-password-xyz")
        mock_server.send_message.assert_called_once()
        mock_server.quit.assert_called_once()

        sent_msg = mock_server.send_message.call_args[0][0]
        assert sent_msg["Subject"] == "Reset your password - Online Food Shop"
        assert sent_msg["From"] == "shop@gmail.com"
        assert sent_msg["To"] == "customer@example.com"
        assert sent_msg["X-Email-Type"] == "password_reset"
        body = sent_msg.get_content()
        assert "https://myshop.com/auth/reset-password/reset-token-67890" in body
        assert "30 minutes" in body

    @patch("smtplib.SMTP")
    def test_custom_verification_and_reset_urls_take_precedence(self, mock_smtp_cls):
        mock_server = MagicMock()
        mock_smtp_cls.return_value = mock_server

        sender = SMTPEmailSender(
            host="smtp.example.com",
            port=587,
            from_address="noreply@example.com",
        )

        sender.send_verification_email(
            "user@example.com",
            "token-abc",
            verification_url="https://custom.trusted.com/verify-email/token-abc",
        )
        msg1 = mock_server.send_message.call_args[0][0]
        assert "https://custom.trusted.com/verify-email/token-abc" in msg1.get_content()

        sender.send_password_reset_email(
            "user@example.com",
            "token-def",
            reset_url="https://custom.trusted.com/auth/reset-password/token-def",
        )
        msg2 = mock_server.send_message.call_args[0][0]
        assert "https://custom.trusted.com/auth/reset-password/token-def" in msg2.get_content()

    @patch("smtplib.SMTP")
    def test_unauthenticated_smtp_does_not_call_login(self, mock_smtp_cls):
        mock_server = MagicMock()
        mock_smtp_cls.return_value = mock_server

        sender = SMTPEmailSender(
            host="localhost",
            port=25,
            security="none",
            username=None,
            password=None,
            from_address="local@example.com",
        )
        sent = sender.send_verification_email("user@example.com", "token-xyz")
        assert sent is True
        mock_server.login.assert_not_called()

    @patch("smtplib.SMTP")
    def test_smtp_auth_failure_returns_false_and_does_not_leak_credentials(
        self, mock_smtp_cls, caplog
    ):
        mock_server = MagicMock()
        mock_smtp_cls.return_value = mock_server
        import smtplib

        mock_server.login.side_effect = smtplib.SMTPAuthenticationError(535, b"Authentication failed")

        sender = SMTPEmailSender(
            host="smtp.example.com",
            port=587,
            security="starttls",
            username="testuser",
            password="super-secret-password-12345",
            from_address="noreply@example.com",
        )

        with caplog.at_level(logging.ERROR):
            sent = sender.send_verification_email("victim@example.com", "token-secret-999")

        assert sent is False
        # Server quit is still called in finally block
        mock_server.quit.assert_called_once()

        # Guarantee: No credentials or tokens appear in error log
        log_text = caplog.text
        assert "super-secret-password-12345" not in log_text
        assert "token-secret-999" not in log_text
        assert "SMTPAuthenticationError" in log_text
        assert "victim@example.com" in log_text

    @patch("smtplib.SMTP")
    def test_smtp_connection_timeout_returns_false(self, mock_smtp_cls):
        mock_smtp_cls.side_effect = TimeoutError("Connection timed out")

        sender = SMTPEmailSender(
            host="10.255.255.1",
            port=587,
            from_address="noreply@example.com",
            timeout=2.0,
        )
        sent = sender.send_verification_email("user@example.com", "token-timeout")
        assert sent is False

    @patch("smtplib.SMTP")
    def test_smtp_tls_error_returns_false(self, mock_smtp_cls):
        mock_server = MagicMock()
        mock_smtp_cls.return_value = mock_server
        mock_server.starttls.side_effect = ssl.SSLError("Certificate verify failed")

        sender = SMTPEmailSender(
            host="smtp.example.com",
            port=587,
            security="starttls",
            from_address="noreply@example.com",
        )
        sent = sender.send_password_reset_email("user@example.com", "token-tls")
        assert sent is False


class TestEmailBackendFactory:
    """Tests backend selection, configuration loading, and isolation."""

    def test_factory_memory_backend_default(self):
        sender = get_email_sender({"EMAIL_BACKEND": "memory"})
        assert isinstance(sender, InMemoryEmailSender)
        assert sender.dev_preview is False

    def test_factory_memory_backend_with_dev_preview(self):
        sender = get_email_sender({
            "EMAIL_BACKEND": "memory",
            "EMAIL_DEV_CONSOLE_PREVIEW": True,
            "DEBUG": True,
            "ENV_NAME": "development",
        })
        assert isinstance(sender, InMemoryEmailSender)
        assert sender.dev_preview is True

    def test_factory_smtp_backend_instantiates_smtp_sender(self):
        sender = get_email_sender({
            "EMAIL_BACKEND": "smtp",
            "SMTP_HOST": "smtp.test.com",
            "SMTP_PORT": 587,
            "SMTP_SECURITY": "starttls",
            "SMTP_USERNAME": "test_user",
            "SMTP_PASSWORD": "test_password",
            "SMTP_FROM_ADDRESS": "sender@test.com",
            "SMTP_TIMEOUT": 5.0,
            "APP_BASE_URL": "http://localhost:5000",
        })
        assert isinstance(sender, SMTPEmailSender)
        assert sender.host == "smtp.test.com"
        assert sender.port == 587
        assert sender.security == "starttls"
        assert sender.username == "test_user"
        assert sender.from_address == "sender@test.com"
        assert sender.timeout == 5.0
        assert sender.app_base_url == "http://localhost:5000"

    def test_factory_invalid_backend_raises_value_error(self):
        with pytest.raises(ValueError, match="Unsupported EMAIL_BACKEND 'sendgrid'"):
            get_email_sender({"EMAIL_BACKEND": "sendgrid"})

    def test_factory_never_silently_falls_back_from_smtp_to_memory(self):
        """Security requirement: invalid SMTP config must raise, never secretly fallback to memory."""
        with pytest.raises(ValueError):
            get_email_sender({
                "EMAIL_BACKEND": "smtp",
                "SMTP_HOST": "",  # invalid
                "SMTP_FROM_ADDRESS": "noreply@example.com",
            })

    def test_init_email_registers_extension(self):
        app = create_app(TestingConfig)
        assert "email_sender" in app.extensions
        assert isinstance(app.extensions["email_sender"], InMemoryEmailSender)


class TestDevEmailPreviewSecurityGating:
    """Rigorous security gating tests for development console preview (M-1 & L-1)."""

    def test_memory_dev_debug_explicit_preview_enables_console_preview(self):
        config = {
            "EMAIL_BACKEND": "memory",
            "EMAIL_DEV_CONSOLE_PREVIEW": True,
            "DEBUG": True,
            "TESTING": False,
            "ENV_NAME": "development",
        }
        sender = get_email_sender(config)
        assert isinstance(sender, InMemoryEmailSender)
        assert sender.dev_preview is True

    def test_memory_dev_debug_preview_false_disables_console_preview(self):
        config = {
            "EMAIL_BACKEND": "memory",
            "EMAIL_DEV_CONSOLE_PREVIEW": False,
            "DEBUG": True,
            "TESTING": False,
            "ENV_NAME": "development",
        }
        sender = get_email_sender(config)
        assert isinstance(sender, InMemoryEmailSender)
        assert sender.dev_preview is False

    def test_production_config_never_enables_preview(self):
        sender = get_email_sender(ProductionConfig)
        assert isinstance(sender, InMemoryEmailSender)
        assert sender.dev_preview is False

    def test_production_with_debug_true_accidentally_enabled_still_does_not_enable_preview(self):
        class AccidentalDebugProdConfig(ProductionConfig):
            DEBUG = True
            EMAIL_DEV_CONSOLE_PREVIEW = True

        sender = get_email_sender(AccidentalDebugProdConfig)
        assert isinstance(sender, InMemoryEmailSender)
        assert sender.dev_preview is False

    def test_development_with_debug_false_never_enables_preview(self):
        config = {
            "EMAIL_BACKEND": "memory",
            "EMAIL_DEV_CONSOLE_PREVIEW": True,
            "DEBUG": False,
            "TESTING": False,
            "ENV_NAME": "development",
        }
        sender = get_email_sender(config)
        assert isinstance(sender, InMemoryEmailSender)
        assert sender.dev_preview is False

    def test_testing_config_does_not_print_token_links_by_default(self, capsys):
        app = create_app(TestingConfig)
        sender = app.extensions["email_sender"]
        assert isinstance(sender, InMemoryEmailSender)
        assert sender.dev_preview is False
        sender.send_verification_email("test@example.com", "sensitive-token-123")
        captured = capsys.readouterr().out
        assert "sensitive-token-123" not in captured
        assert "[SIMULATED EMAIL - DEV PREVIEW]" not in captured

    def test_smtp_backend_never_enables_memory_console_preview(self):
        config = {
            "EMAIL_BACKEND": "smtp",
            "SMTP_HOST": "smtp.example.com",
            "SMTP_FROM_ADDRESS": "noreply@example.com",
            "EMAIL_DEV_CONSOLE_PREVIEW": True,
            "DEBUG": True,
            "ENV_NAME": "development",
        }
        sender = get_email_sender(config)
        assert isinstance(sender, SMTPEmailSender)
        assert not hasattr(sender, "dev_preview")

    def test_environment_variables_cannot_override_production_safety_rule(self, monkeypatch):
        monkeypatch.setenv("APP_CONFIG", "production")
        monkeypatch.setenv("EMAIL_DEV_CONSOLE_PREVIEW", "true")
        monkeypatch.setenv("FLASK_DEBUG", "true")
        monkeypatch.setenv("SECRET_KEY", "secure-prod-key-12345678901234567890")

        # ProductionConfig descriptor strictly forces False
        assert ProductionConfig.EMAIL_DEV_CONSOLE_PREVIEW is False
        sender = get_email_sender(ProductionConfig)
        assert isinstance(sender, InMemoryEmailSender)
        assert sender.dev_preview is False

    def test_development_config_defaults_preview_to_false(self, monkeypatch):
        monkeypatch.delenv("EMAIL_DEV_CONSOLE_PREVIEW", raising=False)
        assert DevelopmentConfig.EMAIL_DEV_CONSOLE_PREVIEW is False


class TestInMemoryDevPreview:
    """Tests development console preview for in-memory email sender."""

    def test_dev_preview_prints_formatted_message(self, capsys):
        sender = InMemoryEmailSender(dev_preview=True)
        sender.send_verification_email(
            "dev_user@example.com",
            "dev-token-111",
            verification_url="http://127.0.0.1:5000/verify-email/dev-token-111",
        )
        captured = capsys.readouterr().out
        assert "[SIMULATED EMAIL - DEV PREVIEW]" in captured
        assert "To:      dev_user@example.com" in captured
        assert "Subject: Verify your email address" in captured
        assert "Link:    http://127.0.0.1:5000/verify-email/dev-token-111" in captured

    def test_dev_preview_disabled_by_default(self, capsys):
        sender = InMemoryEmailSender(dev_preview=False)
        sender.send_verification_email("silent@example.com", "token-222")
        captured = capsys.readouterr().out
        assert "[SIMULATED EMAIL - DEV PREVIEW]" not in captured


class TestRegistrationVerificationWithSMTP:
    """Integration tests verifying registration and verification with SMTP backend."""

    @pytest.fixture
    def smtp_app(self, tmp_path):
        db_path = str(tmp_path / "test_smtp_app.db")

        class SMTPTestConfig(TestingConfig):
            DATABASE_PATH = db_path
            CSRF_ENABLED = False
            EMAIL_BACKEND = "smtp"
            SMTP_HOST = "smtp.mock.test"
            SMTP_PORT = 587
            SMTP_SECURITY = "starttls"
            SMTP_USERNAME = "mock_user"
            SMTP_PASSWORD = "mock_password"
            SMTP_FROM_ADDRESS = "noreply@foodshop.test"
            APP_BASE_URL = "http://127.0.0.1:5000"

        app = create_app(SMTPTestConfig)
        with app.app_context():
            init_db()
        return app

    @patch.object(SMTPEmailSender, "send_verification_email", return_value=True)
    def test_registration_with_smtp_success_flash_message(self, mock_send, smtp_app):
        client = smtp_app.test_client()
        res = client.post(
            "/register",
            data={
                "email": "smtp_customer@example.com",
                "password": "ValidPassword123!",
                "confirm_password": "ValidPassword123!",
            },
            follow_redirects=True,
        )
        assert res.status_code == 200
        mock_send.assert_called_once()
        args, kwargs = mock_send.call_args
        assert args[0] == "smtp_customer@example.com"
        assert kwargs["verification_url"].startswith("http://127.0.0.1:5000/verify-email/")
        # Flash message for successful SMTP dispatch
        assert b"A verification link has been sent to your email address." in res.data

    @patch.object(SMTPEmailSender, "send_verification_email", return_value=False)
    def test_registration_with_smtp_failure_warns_user_without_breaking_account(
        self, mock_send, smtp_app
    ):
        """When SMTP delivery fails, account is safely created and user receives warning."""
        client = smtp_app.test_client()
        res = client.post(
            "/register",
            data={
                "email": "unreachable_user@example.com",
                "password": "ValidPassword123!",
                "confirm_password": "ValidPassword123!",
            },
            follow_redirects=True,
        )
        assert res.status_code == 200
        # Account is created in database
        with smtp_app.app_context():
            user = UserRepository().get_by_email("unreachable_user@example.com")
            assert user is not None
            assert user.is_verified is False

        # Flash indicates delivery failed and instructs user to resend
        assert b"could not deliver your verification email at this time" in res.data

    @patch.object(SMTPEmailSender, "send_verification_email", return_value=True)
    def test_anti_host_header_injection_in_verification_url(self, mock_send, smtp_app):
        """Host header must NEVER pollute the verification link."""
        client = smtp_app.test_client()
        client.post(
            "/register",
            headers={"Host": "evil-phishing-host.com"},
            data={
                "email": "security_check@example.com",
                "password": "ValidPassword123!",
                "confirm_password": "ValidPassword123!",
            },
        )
        mock_send.assert_called_once()
        _, kwargs = mock_send.call_args
        verification_url = kwargs["verification_url"]
        assert "evil-phishing-host.com" not in verification_url
        assert verification_url.startswith("http://127.0.0.1:5000/verify-email/")

    @patch.object(SMTPEmailSender, "send_verification_email", return_value=True)
    def test_resend_verification_smtp_success_neutral_flash(self, mock_send, smtp_app):
        # Register user first with mock_send active
        with smtp_app.app_context():
            auth_service = AuthService()
            auth_service.register_customer("resend_smtp@example.com", "Password123!")

        client = smtp_app.test_client()
        res = client.post(
            "/verify-email/resend",
            data={
                "email": "resend_smtp@example.com",
            },
            follow_redirects=True,
        )
        assert res.status_code == 200
        assert b"If an eligible unverified account exists for that email, a verification link has been sent." in res.data


class TestPasswordResetWithSMTP:
    """Integration tests verifying password reset flow with SMTP backend."""

    @pytest.fixture
    def smtp_app(self, tmp_path):
        db_path = str(tmp_path / "test_reset_smtp.db")

        class ResetSMTPConfig(TestingConfig):
            DATABASE_PATH = db_path
            CSRF_ENABLED = False
            EMAIL_BACKEND = "smtp"
            SMTP_HOST = "smtp.mock.test"
            SMTP_PORT = 587
            SMTP_SECURITY = "starttls"
            SMTP_FROM_ADDRESS = "noreply@foodshop.test"
            APP_BASE_URL = "http://127.0.0.1:5000"

        app = create_app(ResetSMTPConfig)
        with app.app_context():
            init_db()
        return app

    @patch.object(SMTPEmailSender, "send_verification_email", return_value=True)
    @patch.object(SMTPEmailSender, "send_password_reset_email", return_value=True)
    def test_password_reset_smtp_success(self, mock_reset, mock_verify, smtp_app):
        # Register user with local password
        with smtp_app.app_context():
            auth_service = AuthService()
            auth_service.register_customer("reset_target@example.com", "OriginalPassword123!")

        client = smtp_app.test_client()
        res = client.post(
            "/auth/forgot-password",
            data={
                "email": "reset_target@example.com",
            },
            follow_redirects=True,
        )
        assert res.status_code == 200
        mock_reset.assert_called_once()
        args, kwargs = mock_reset.call_args
        assert args[0] == "reset_target@example.com"
        assert kwargs["reset_url"].startswith("http://127.0.0.1:5000/auth/reset-password/")
        assert b"If an eligible account exists for that email, a password reset link has been sent." in res.data

    @patch.object(SMTPEmailSender, "send_verification_email", return_value=True)
    @patch.object(SMTPEmailSender, "send_password_reset_email", return_value=False)
    def test_password_reset_smtp_failure_preserves_neutral_anti_enumeration_flash(
        self, mock_reset, mock_verify, smtp_app
    ):
        """Even when SMTP fails, the public response must remain completely identical."""
        with smtp_app.app_context():
            auth_service = AuthService()
            auth_service.register_customer("fail_target@example.com", "OriginalPassword123!")

        client = smtp_app.test_client()
        res = client.post(
            "/auth/forgot-password",
            data={
                "email": "fail_target@example.com",
            },
            follow_redirects=True,
        )
        assert res.status_code == 200
        assert b"If an eligible account exists for that email, a password reset link has been sent." in res.data

    @patch.object(SMTPEmailSender, "send_password_reset_email", return_value=True)
    def test_nonexistent_email_does_not_send_and_returns_identical_message(
        self, mock_send, smtp_app
    ):
        client = smtp_app.test_client()
        res = client.post(
            "/auth/forgot-password",
            data={
                "email": "unknown_email_404@example.com",
            },
            follow_redirects=True,
        )
        assert res.status_code == 200
        mock_send.assert_not_called()
        assert b"If an eligible account exists for that email, a password reset link has been sent." in res.data

    @patch.object(SMTPEmailSender, "send_verification_email", return_value=True)
    @patch.object(SMTPEmailSender, "send_password_reset_email", return_value=True)
    def test_anti_host_header_injection_in_reset_url(self, mock_reset, mock_verify, smtp_app):
        with smtp_app.app_context():
            auth_service = AuthService()
            auth_service.register_customer("host_target@example.com", "OriginalPassword123!")

        client = smtp_app.test_client()
        client.post(
            "/auth/forgot-password",
            headers={"Host": "evil-domain.org"},
            data={
                "email": "host_target@example.com",
            },
        )
        mock_reset.assert_called_once()
        _, kwargs = mock_reset.call_args
        reset_url = kwargs["reset_url"]
        assert "evil-domain.org" not in reset_url
        assert reset_url.startswith("http://127.0.0.1:5000/auth/reset-password/")

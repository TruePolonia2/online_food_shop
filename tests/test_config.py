"""Unit and integration tests for security configuration and application factory initialization."""
import os
from pathlib import Path
import tempfile
import unittest

from app import create_app
from app.config import (
    DEV_SECRET_KEY,
    Config,
    DevelopmentConfig,
    ProductionConfig,
    TestingConfig,
    _parse_bool_env,
    _resolve_environment_name,
    get_config_by_name,
    is_production_env,
    load_project_env,
)
from app.oauth import is_google_auth_configured


class BaseConfigTestCase(unittest.TestCase):
    """Base test case that reliably captures and restores the original environment."""

    def setUp(self) -> None:
        self._original_env = dict(os.environ)

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self._original_env)


class TestSecretKeyConfiguration(BaseConfigTestCase):
    """Tests covering SECRET_KEY enforcement, rejection of unsafe values, and isolation."""

    def test_production_config_rejects_missing_secret_key(self) -> None:
        os.environ.pop("SECRET_KEY", None)
        with self.assertRaises(RuntimeError) as ctx:
            create_app(ProductionConfig)
        self.assertIn("SECRET_KEY must be set in production", str(ctx.exception))

    def test_production_config_rejects_empty_or_whitespace_secret_key(self) -> None:
        for empty_val in ["", "   ", "\t\n"]:
            os.environ["SECRET_KEY"] = empty_val
            with self.subTest(empty_val=empty_val):
                with self.assertRaises(RuntimeError) as ctx:
                    create_app(ProductionConfig)
                self.assertIn("SECRET_KEY must be set in production", str(ctx.exception))

    def test_production_config_rejects_development_placeholder(self) -> None:
        os.environ["SECRET_KEY"] = DEV_SECRET_KEY
        with self.assertRaises(RuntimeError) as ctx:
            create_app(ProductionConfig)
        self.assertIn("SECRET_KEY must be set in production", str(ctx.exception))

    def test_production_config_error_does_not_leak_secret(self) -> None:
        dummy_secret = "my-secret-attempt-12345"
        os.environ["SECRET_KEY"] = DEV_SECRET_KEY
        with self.assertRaises(RuntimeError) as ctx:
            create_app(ProductionConfig)
        error_msg = str(ctx.exception)
        self.assertNotIn(DEV_SECRET_KEY, error_msg)
        self.assertNotIn(dummy_secret, error_msg)

    def test_production_config_accepts_valid_secret(self) -> None:
        valid_secret = "cf5b62b0833a693c4e38e6db3b19451121d5aefbb0d7496be1dc2850942e61a2"
        os.environ["SECRET_KEY"] = valid_secret
        app = create_app(ProductionConfig)
        self.assertEqual(app.config["SECRET_KEY"], valid_secret)
        self.assertEqual(ProductionConfig.SECRET_KEY, valid_secret)

    def test_base_config_enforces_secret_in_production_environment(self) -> None:
        for env_var in ["APP_CONFIG", "FLASK_CONFIG", "FLASK_ENV", "ENV"]:
            os.environ.clear()
            os.environ.update(self._original_env)
            os.environ[env_var] = "production"
            os.environ.pop("SECRET_KEY", None)

            with self.subTest(env_var=env_var):
                with self.assertRaises(RuntimeError) as ctx:
                    create_app(Config)
                self.assertIn("SECRET_KEY must be set in production", str(ctx.exception))

                os.environ["SECRET_KEY"] = DEV_SECRET_KEY
                with self.assertRaises(RuntimeError) as ctx:
                    create_app(Config)
                self.assertIn("SECRET_KEY must be set in production", str(ctx.exception))

    def test_development_config_falls_back_to_dev_placeholder(self) -> None:
        os.environ.pop("SECRET_KEY", None)
        app = create_app(DevelopmentConfig)
        self.assertEqual(app.config["SECRET_KEY"], DEV_SECRET_KEY)
        self.assertEqual(DevelopmentConfig.SECRET_KEY, DEV_SECRET_KEY)

    def test_development_config_uses_custom_secret_when_provided(self) -> None:
        custom_secret = "custom-dev-secret-override"
        os.environ["SECRET_KEY"] = custom_secret
        app = create_app(DevelopmentConfig)
        self.assertEqual(app.config["SECRET_KEY"], custom_secret)

    def test_testing_config_provides_deterministic_secret(self) -> None:
        os.environ.pop("SECRET_KEY", None)
        app = create_app(TestingConfig)
        self.assertEqual(app.config["SECRET_KEY"], "testing-secret-key")
        self.assertTrue(app.config["TESTING"])


class TestDebugConfiguration(BaseConfigTestCase):
    """Tests covering DEBUG default settings and safe boolean environment parsing."""

    def test_base_config_debug_disabled_by_default(self) -> None:
        os.environ.pop("FLASK_DEBUG", None)
        self.assertFalse(Config.DEBUG)
        app = create_app(Config)
        self.assertFalse(app.config["DEBUG"])

    def test_production_config_debug_disabled_by_default(self) -> None:
        os.environ["SECRET_KEY"] = "prod-test-key-32-chars-long-minimum"
        os.environ.pop("FLASK_DEBUG", None)
        self.assertFalse(ProductionConfig.DEBUG)
        app = create_app(ProductionConfig)
        self.assertFalse(app.config["DEBUG"])

    def test_testing_config_debug_disabled(self) -> None:
        self.assertFalse(TestingConfig.DEBUG)
        app = create_app(TestingConfig)
        self.assertFalse(app.config["DEBUG"])

    def test_development_config_debug_enabled_by_default(self) -> None:
        os.environ.pop("FLASK_DEBUG", None)
        self.assertTrue(DevelopmentConfig.DEBUG)
        app = create_app(DevelopmentConfig)
        self.assertTrue(app.config["DEBUG"])

    def test_parse_bool_env_truthy_values(self) -> None:
        truthy_inputs = ["1", "true", "True", "TRUE", "yes", "YES", "on", "ON", "  true  "]
        for val in truthy_inputs:
            with self.subTest(val=val):
                self.assertTrue(_parse_bool_env(val, default=False))

    def test_parse_bool_env_falsy_values(self) -> None:
        falsy_inputs = ["0", "false", "False", "FALSE", "no", "NO", "off", "OFF", "", "   "]
        for val in falsy_inputs:
            with self.subTest(val=val):
                self.assertFalse(_parse_bool_env(val, default=True))

    def test_parse_bool_env_arbitrary_string_uses_default(self) -> None:
        arbitrary_inputs = ["not_a_bool", "potato", "2", "none", "null"]
        for val in arbitrary_inputs:
            with self.subTest(val=val):
                self.assertFalse(_parse_bool_env(val, default=False))
                self.assertTrue(_parse_bool_env(val, default=True))

    def test_flask_debug_opt_in_for_base_config(self) -> None:
        os.environ["FLASK_DEBUG"] = "1"
        self.assertTrue(_parse_bool_env(os.environ.get("FLASK_DEBUG"), default=False))

        os.environ["FLASK_DEBUG"] = "false"
        self.assertFalse(_parse_bool_env(os.environ.get("FLASK_DEBUG"), default=False))

    def test_flask_debug_opt_out_for_development_config(self) -> None:
        os.environ["FLASK_DEBUG"] = "0"
        self.assertFalse(_parse_bool_env(os.environ.get("FLASK_DEBUG"), default=True))

        os.environ["FLASK_DEBUG"] = "false"
        self.assertFalse(_parse_bool_env(os.environ.get("FLASK_DEBUG"), default=True))


class TestSessionCookieConfiguration(BaseConfigTestCase):
    """Tests covering session cookie security flags (HttpOnly, SameSite, Secure)."""

    def test_session_cookie_httponly_and_samesite_defaults(self) -> None:
        configs = [Config, ProductionConfig, DevelopmentConfig, TestingConfig]
        for cfg in configs:
            with self.subTest(config=cfg.__name__):
                self.assertTrue(cfg.SESSION_COOKIE_HTTPONLY)
                self.assertEqual(cfg.SESSION_COOKIE_SAMESITE, "Lax")

    def test_session_cookie_secure_defaults(self) -> None:
        os.environ.pop("SESSION_COOKIE_SECURE", None)
        os.environ.pop("APP_CONFIG", None)
        os.environ.pop("FLASK_CONFIG", None)
        os.environ.pop("FLASK_ENV", None)
        os.environ.pop("ENV", None)

        # In non-production environments, Development, Testing, and Config default to False
        self.assertFalse(DevelopmentConfig.SESSION_COOKIE_SECURE)
        self.assertFalse(TestingConfig.SESSION_COOKIE_SECURE)
        self.assertFalse(Config.SESSION_COOKIE_SECURE)

        # ProductionConfig defaults to True to enforce HTTPS security
        self.assertTrue(ProductionConfig.SESSION_COOKIE_SECURE)

    def test_base_config_secure_cookie_defaults_true_in_production(self) -> None:
        os.environ.pop("SESSION_COOKIE_SECURE", None)
        os.environ["APP_CONFIG"] = "production"
        self.assertTrue(is_production_env())
        # Defense-in-depth: Config.SESSION_COOKIE_SECURE defaults to True under production env
        self.assertTrue(Config.SESSION_COOKIE_SECURE)

    def test_session_cookie_secure_configurable_via_env(self) -> None:
        os.environ["SESSION_COOKIE_SECURE"] = "1"
        self.assertTrue(_parse_bool_env(os.environ.get("SESSION_COOKIE_SECURE"), default=False))

        os.environ["SESSION_COOKIE_SECURE"] = "0"
        self.assertFalse(_parse_bool_env(os.environ.get("SESSION_COOKIE_SECURE"), default=True))

    def test_production_cookie_secure_intentional_override(self) -> None:
        os.environ["SECRET_KEY"] = "prod-test-key-32-chars-long-minimum"
        os.environ["SESSION_COOKIE_SECURE"] = "0"

        app = create_app(ProductionConfig)
        self.assertFalse(app.config["SESSION_COOKIE_SECURE"])

    def test_session_cookie_headers_in_response(self) -> None:
        os.environ["SECRET_KEY"] = "secure-production-secret-for-cookies-123"
        os.environ["SESSION_COOKIE_SECURE"] = "1"

        app = create_app(ProductionConfig)
        self.assertTrue(app.config["SESSION_COOKIE_HTTPONLY"])
        self.assertEqual(app.config["SESSION_COOKIE_SAMESITE"], "Lax")
        self.assertTrue(app.config["SESSION_COOKIE_SECURE"])

        client = app.test_client()
        with client.session_transaction() as sess:
            sess["cart"] = {"1": 2}

        # Make a request and verify cookie attributes
        response = client.get("/")
        set_cookie_header = response.headers.get("Set-Cookie", "")
        if set_cookie_header:
            self.assertIn("HttpOnly", set_cookie_header)
            self.assertIn("SameSite=Lax", set_cookie_header)


class TestConfigurationResolutionAndFactory(BaseConfigTestCase):
    """Tests covering precedence, rejection of invalid names, import paths, and factory compatibility."""

    def test_precedence_hierarchy(self) -> None:
        # 1. APP_CONFIG wins over FLASK_CONFIG, FLASK_ENV, ENV
        os.environ["APP_CONFIG"] = "production"
        os.environ["FLASK_CONFIG"] = "testing"
        os.environ["FLASK_ENV"] = "development"
        os.environ["ENV"] = "development"
        self.assertEqual(_resolve_environment_name(), "production")
        self.assertIs(get_config_by_name(), ProductionConfig)

        # 2. FLASK_CONFIG wins over FLASK_ENV, ENV
        os.environ.pop("APP_CONFIG")
        self.assertEqual(_resolve_environment_name(), "testing")
        self.assertIs(get_config_by_name(), TestingConfig)

        # 3. FLASK_ENV wins over ENV
        os.environ.pop("FLASK_CONFIG")
        os.environ["FLASK_ENV"] = "production"
        os.environ["ENV"] = "development"
        self.assertEqual(_resolve_environment_name(), "production")
        self.assertIs(get_config_by_name(), ProductionConfig)

        # 4. ENV resolves when higher precedence variables are unset
        os.environ.pop("FLASK_ENV")
        os.environ["ENV"] = "dev"
        self.assertEqual(_resolve_environment_name(), "dev")
        self.assertIs(get_config_by_name(), DevelopmentConfig)

        # 5. Default when all are unset
        os.environ.pop("ENV")
        self.assertIsNone(_resolve_environment_name())
        self.assertIs(get_config_by_name(), DevelopmentConfig)

    def test_get_config_by_name_aliases(self) -> None:
        self.assertIs(get_config_by_name("production"), ProductionConfig)
        self.assertIs(get_config_by_name("prod"), ProductionConfig)
        self.assertIs(get_config_by_name("development"), DevelopmentConfig)
        self.assertIs(get_config_by_name("dev"), DevelopmentConfig)
        self.assertIs(get_config_by_name("testing"), TestingConfig)
        self.assertIs(get_config_by_name("test"), TestingConfig)

    def test_reject_unknown_name_in_get_config_by_name(self) -> None:
        for unknown in ["staging", "invalid", "produciton", "123"]:
            with self.subTest(unknown=unknown):
                with self.assertRaises(ValueError) as ctx:
                    get_config_by_name(unknown)
                self.assertIn(f"Unknown configuration name '{unknown}'", str(ctx.exception))
                self.assertIn("Valid options are:", str(ctx.exception))

    def test_reject_unknown_environment_variable(self) -> None:
        os.environ["APP_CONFIG"] = "invalid_profile"
        with self.assertRaises(ValueError) as ctx:
            create_app()
        self.assertIn("Unknown configuration name 'invalid_profile'", str(ctx.exception))

        os.environ.pop("APP_CONFIG")
        os.environ["FLASK_ENV"] = "bogus_env"
        with self.assertRaises(ValueError) as ctx:
            create_app()
        self.assertIn("Unknown configuration name 'bogus_env'", str(ctx.exception))

    def test_create_app_zero_arguments_without_env_defaults_to_development(self) -> None:
        for v in ["APP_CONFIG", "FLASK_CONFIG", "FLASK_ENV", "ENV", "SECRET_KEY"]:
            os.environ.pop(v, None)

        app = create_app()
        self.assertTrue(app.config["DEBUG"])
        self.assertEqual(app.config["SECRET_KEY"], DEV_SECRET_KEY)
        self.assertFalse(app.config["SESSION_COOKIE_SECURE"])

    def test_create_app_zero_arguments_with_production_env(self) -> None:
        os.environ["APP_CONFIG"] = "production"
        os.environ["SECRET_KEY"] = "prod-test-key-32-chars-long-minimum"

        app = create_app()
        self.assertFalse(app.config["DEBUG"])
        self.assertEqual(app.config["SECRET_KEY"], "prod-test-key-32-chars-long-minimum")
        self.assertTrue(app.config["SESSION_COOKIE_SECURE"])

    def test_create_app_with_string_alias(self) -> None:
        os.environ["SECRET_KEY"] = "prod-test-key-32-chars-long-minimum"

        app_prod = create_app("production")
        self.assertFalse(app_prod.config["DEBUG"])
        self.assertTrue(app_prod.config["SESSION_COOKIE_SECURE"])

        app_dev = create_app("dev")
        self.assertTrue(app_dev.config["DEBUG"])
        self.assertFalse(app_dev.config["SESSION_COOKIE_SECURE"])

    def test_create_app_with_unknown_string_raises_value_error(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            create_app("unknown_name")
        self.assertIn("Unknown configuration name 'unknown_name'", str(ctx.exception))

    def test_create_app_with_valid_import_path(self) -> None:
        os.environ["SECRET_KEY"] = "prod-test-key-32-chars-long-minimum"
        app = create_app("app.config.ProductionConfig")
        self.assertFalse(app.config["DEBUG"])
        self.assertTrue(app.config["SESSION_COOKIE_SECURE"])

    def test_create_app_with_invalid_import_path_raises_value_error(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            create_app("app.config.NonExistentConfig")
        self.assertIn("Could not import configuration 'app.config.NonExistentConfig'", str(ctx.exception))

    def test_create_app_with_explicit_custom_class(self) -> None:
        class CustomTestConfig(Config):
            CUSTOM_SETTING = "custom_value_123"
            TESTING = True

        app = create_app(CustomTestConfig)
        self.assertEqual(app.config["CUSTOM_SETTING"], "custom_value_123")
        self.assertTrue(app.config["TESTING"])


class TestDotenvConfigurationLoading(BaseConfigTestCase):
    """Tests for .env file discovery, loading, precedence, and Google OAuth persistence."""

    def test_load_project_env_loads_variables(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            env_file = Path(td) / ".env"
            env_file.write_text("TEST_DOTENV_KEY=sample_val\nGOOGLE_CLIENT_ID=mock-client-id\n", encoding="utf-8")

            os.environ.pop("TEST_DOTENV_KEY", None)
            os.environ.pop("GOOGLE_CLIENT_ID", None)

            loaded = load_project_env(dotenv_path=env_file)
            self.assertTrue(loaded)
            self.assertEqual(os.environ.get("TEST_DOTENV_KEY"), "sample_val")
            self.assertEqual(os.environ.get("GOOGLE_CLIENT_ID"), "mock-client-id")

    def test_load_project_env_preserves_existing_process_env_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            env_file = Path(td) / ".env"
            env_file.write_text("FLASK_CONFIG=production\nGOOGLE_CLIENT_ID=from_file\n", encoding="utf-8")

            os.environ["FLASK_CONFIG"] = "development"
            os.environ["GOOGLE_CLIENT_ID"] = "from_process"

            loaded = load_project_env(dotenv_path=env_file, override=False)
            self.assertTrue(loaded)
            self.assertEqual(os.environ.get("FLASK_CONFIG"), "development")
            self.assertEqual(os.environ.get("GOOGLE_CLIENT_ID"), "from_process")

    def test_load_project_env_allows_explicit_override(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            env_file = Path(td) / ".env"
            env_file.write_text("GOOGLE_CLIENT_ID=from_file_override\n", encoding="utf-8")

            os.environ["GOOGLE_CLIENT_ID"] = "old_process_val"

            loaded = load_project_env(dotenv_path=env_file, override=True)
            self.assertTrue(loaded)
            self.assertEqual(os.environ.get("GOOGLE_CLIENT_ID"), "from_file_override")

    def test_load_project_env_returns_false_for_missing_file(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            missing_path = Path(td) / "nonexistent.env"
            loaded = load_project_env(dotenv_path=missing_path)
            self.assertFalse(loaded)

    def test_app_reads_google_credentials_from_environment(self) -> None:
        os.environ["GOOGLE_CLIENT_ID"] = "env-google-id-12345"
        os.environ["GOOGLE_CLIENT_SECRET"] = "env-google-secret-67890"

        app = create_app(TestingConfig)
        self.assertEqual(app.config["GOOGLE_CLIENT_ID"], "env-google-id-12345")
        self.assertEqual(app.config["GOOGLE_CLIENT_SECRET"], "env-google-secret-67890")
        with app.app_context():
            self.assertTrue(is_google_auth_configured())

    def test_app_google_auth_disabled_when_credentials_missing_or_blank(self) -> None:
        os.environ.pop("GOOGLE_CLIENT_ID", None)
        os.environ.pop("GOOGLE_CLIENT_SECRET", None)

        app = create_app(TestingConfig)
        self.assertIsNone(app.config["GOOGLE_CLIENT_ID"])
        self.assertIsNone(app.config["GOOGLE_CLIENT_SECRET"])
        with app.app_context():
            self.assertFalse(is_google_auth_configured())

        # Also test with whitespace-only values
        os.environ["GOOGLE_CLIENT_ID"] = "   "
        os.environ["GOOGLE_CLIENT_SECRET"] = ""
        app_blank = create_app(TestingConfig)
        with app_blank.app_context():
            self.assertFalse(is_google_auth_configured())

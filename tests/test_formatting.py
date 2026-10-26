"""Unit tests for currency and presentation formatting helpers."""
import unittest

from app import create_app
from app.config import Config
from app.formatting import format_euro


class TestFormatting(unittest.TestCase):
    """Tests for Portuguese euro currency formatting."""

    def test_format_euro_standard_amounts(self) -> None:
        self.assertEqual(format_euro(450), "4,50 €")
        self.assertEqual(format_euro(1200), "12,00 €")
        self.assertEqual(format_euro(99), "0,99 €")
        self.assertEqual(format_euro(5), "0,05 €")
        self.assertEqual(format_euro(0), "0,00 €")

    def test_format_euro_thousands_amounts(self) -> None:
        self.assertEqual(format_euro(1020), "10,20 €")
        self.assertEqual(format_euro(2250), "22,50 €")
        self.assertEqual(format_euro(125000), "1 250,00 €")
        self.assertEqual(format_euro(1000000), "10 000,00 €")

    def test_format_euro_none_and_negative(self) -> None:
        self.assertEqual(format_euro(None), "0,00 €")
        self.assertEqual(format_euro(-450), "-4,50 €")
        self.assertEqual(format_euro(-99), "-0,99 €")

    def test_format_euro_jinja_filter_registration(self) -> None:
        class TestConfig(Config):
            TESTING = True
            CSRF_ENABLED = False

        app = create_app(TestConfig)
        with app.app_context():
            rendered = app.jinja_env.from_string("{{ 450 | format_euro }}").render()
            self.assertEqual(rendered, "4,50 €")


if __name__ == "__main__":
    unittest.main()

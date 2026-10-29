"""Tests for UI reliability, SVG dimensions, and local offline styling (Phase 10.11.2)."""
import re
import unittest
from app import create_app
from app.config import Config


class BaseUITestCase(unittest.TestCase):
    """Base test case for UI and template reliability tests."""

    def setUp(self) -> None:
        class UITestConfig(Config):
            TESTING = True
            SECRET_KEY = "test-ui-reliability-secret-key"
            CSRF_ENABLED = False
            GOOGLE_CLIENT_ID = "mock-google-client-id"
            GOOGLE_CLIENT_SECRET = "mock-google-client-secret"

        self.app = create_app(UITestConfig)
        self.client = self.app.test_client()


class TestUIReliabilityAndStyling(BaseUITestCase):
    """Verifies SVG sizing, local CSS assets, and CDN suppression behavior."""

    def test_local_main_css_is_served_with_correct_mimetype(self) -> None:
        """Verify main.css is served locally and contains essential rules."""
        resp = self.client.get("/static/css/main.css")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("text/css", resp.content_type)
        css_text = resp.get_data(as_text=True)

        # SVG sizing rules
        self.assertIn("svg.w-4", css_text)
        self.assertIn("svg.w-5", css_text)
        self.assertIn("svg.w-6", css_text)
        self.assertIn("svg.w-8", css_text)
        self.assertIn("svg.w-12", css_text)

        # Layout, reset, and navigation rules
        self.assertIn("box-sizing: border-box", css_text)
        self.assertIn(".max-w-7xl", css_text)
        self.assertIn(".verification-card", css_text)
        self.assertIn(".verification-icon-wrap svg", css_text)

    def test_shared_base_template_links_local_main_css(self) -> None:
        """Verify all standard pages link to local main.css."""
        for path in ["/", "/login", "/register", "/cart/", "/contact"]:
            resp = self.client.get(path, follow_redirects=True)
            self.assertEqual(resp.status_code, 200)
            html = resp.get_data(as_text=True)
            self.assertIn('href="/static/css/main.css"', html, f"Missing local CSS on {path}")

    def test_navigation_svgs_have_explicit_dimensions(self) -> None:
        """Verify header SVG icons have explicit HTML width and height attributes."""
        resp = self.client.get("/")
        html = resp.get_data(as_text=True)

        # Brand logo SVG has explicit width="24" height="24"
        self.assertRegex(html, r'<svg[^>]*class="[^"]*w-6 h-6[^"]*"[^>]*width="24"[^>]*height="24"')

        # Cart SVG has explicit width="20" height="20"
        self.assertRegex(html, r'<svg[^>]*class="[^"]*w-5 h-5[^"]*"[^>]*width="20"[^>]*height="20"')

    def test_email_verification_page_renders_cleanly_without_cdn(self) -> None:
        """Verify the email confirmation page suppresses CDN while maintaining local styling."""
        resp = self.client.get("/verify-email/test_token_sample")
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)

        # 1. External Tailwind CDN script is strictly suppressed
        self.assertNotIn("cdn.tailwindcss.com", html)

        # 2. Local CSS is linked to style the shared navigation and base elements
        self.assertIn('href="/static/css/main.css"', html)

        # 3. Verification mail SVG has explicit dimensions preventing expansion
        self.assertRegex(html, r'<svg[^>]*class="[^"]*w-6 h-6[^"]*"[^>]*width="24"[^>]*height="24"')

        # 4. Standalone verification styles and classes are present
        self.assertIn(".verification-card", html)
        self.assertIn(".verification-icon-wrap", html)
        self.assertIn("Verify Your Email Address", html)

    def test_google_login_icons_have_explicit_dimensions(self) -> None:
        """Verify Google login/register buttons have explicit SVG dimensions."""
        for path in ["/login", "/register"]:
            resp = self.client.get(path)
            self.assertEqual(resp.status_code, 200)
            html = resp.get_data(as_text=True)
            self.assertRegex(
                html,
                r'<svg[^>]*class="[^"]*w-4 h-4[^"]*"[^>]*width="16"[^>]*height="16"',
                f"Google SVG missing explicit dimensions on {path}",
            )

    def test_contact_page_has_explicit_svg_dimensions_and_functions(self) -> None:
        """Verify contact page renders with local styling and explicit dimensions."""
        resp = self.client.get("/contact")
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn('href="/static/css/main.css"', html)
        self.assertIn("Contact Us", html)

    def test_all_rendered_svgs_on_core_pages_have_explicit_width_and_height(self) -> None:
        """Audit that all rendered <svg> tags on key storefront pages have width and height attributes."""
        pages = ["/", "/login", "/register", "/contact", "/cart/"]
        for path in pages:
            resp = self.client.get(path, follow_redirects=True)
            self.assertEqual(resp.status_code, 200)
            html = resp.get_data(as_text=True)

            # Find all SVG opening tags
            svg_tags = re.findall(r'<svg[^>]*>', html)
            for tag in svg_tags:
                self.assertIn('width="', tag, f"SVG tag in {path} missing width attribute: {tag}")
                self.assertIn('height="', tag, f"SVG tag in {path} missing height attribute: {tag}")

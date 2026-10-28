"""Unit and integration tests for Contact Page and Customer Support Inbox (Phase 10.11.1)."""
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest

from app import create_app
from app.config import Config
from app.database.connection import create_connection, init_db
from app.models.contact_message import (
    ALLOWED_STATUS_TRANSITIONS,
    MAX_EMAIL_LENGTH,
    MAX_MESSAGE_LENGTH,
    MAX_NAME_LENGTH,
    MAX_SUBJECT_LENGTH,
    ContactMessage,
    ContactMessageStatus,
)
from app.models.user import User, UserRole
from app.repositories.contact_message_repository import ContactMessageRepository
from app.repositories.user_repository import UserRepository
from app.services.auth_service import AuthService
from app.services.contact_service import (
    ContactRateLimiter,
    ContactRateLimitExceededError,
    ContactService,
    ContactStatusTransitionError,
    ContactValidationError,
    contact_rate_limiter,
)


class TestContactModel(unittest.TestCase):
    """Domain model tests for ContactMessage and status transitions."""

    def test_valid_contact_message_creation(self) -> None:
        now = datetime.now(timezone.utc)
        msg = ContactMessage(
            name="Alice Smith",
            email="alice@example.com",
            subject="Question regarding sourdough bread",
            message="Do you bake gluten-free sourdough on weekends?",
            status=ContactMessageStatus.NEW,
            created_at=now,
            updated_at=now,
        )
        self.assertEqual(msg.name, "Alice Smith")
        self.assertEqual(msg.email, "alice@example.com")
        self.assertEqual(msg.subject, "Question regarding sourdough bread")
        self.assertEqual(msg.status, ContactMessageStatus.NEW)
        self.assertIsNone(msg.id)

    def test_contact_message_string_status_converted_to_enum(self) -> None:
        msg = ContactMessage(
            name="Bob",
            email="bob@example.com",
            subject="Subject",
            message="Body text",
            status="read",
        )
        self.assertIsInstance(msg.status, ContactMessageStatus)
        self.assertEqual(msg.status, ContactMessageStatus.READ)

    def test_contact_message_empty_fields_raise_validation_error(self) -> None:
        with self.assertRaises(ValueError):
            ContactMessage(name="", email="bob@example.com", subject="Subj", message="Body")
        with self.assertRaises(ValueError):
            ContactMessage(name="Bob", email="", subject="Subj", message="Body")
        with self.assertRaises(ValueError):
            ContactMessage(name="Bob", email="invalid-email", subject="Subj", message="Body")
        with self.assertRaises(ValueError):
            ContactMessage(name="Bob", email="bob@example.com", subject="", message="Body")
        with self.assertRaises(ValueError):
            ContactMessage(name="Bob", email="bob@example.com", subject="Subj", message="")

    def test_contact_message_field_length_overflow_raises_validation_error(self) -> None:
        with self.assertRaises(ValueError):
            ContactMessage(name="A" * (MAX_NAME_LENGTH + 1), email="a@b.com", subject="Subj", message="Body")
        with self.assertRaises(ValueError):
            ContactMessage(name="Bob", email="a" * (MAX_EMAIL_LENGTH + 1) + "@b.com", subject="Subj", message="Body")
        with self.assertRaises(ValueError):
            ContactMessage(name="Bob", email="a@b.com", subject="S" * (MAX_SUBJECT_LENGTH + 1), message="Body")
        with self.assertRaises(ValueError):
            ContactMessage(name="Bob", email="a@b.com", subject="Subj", message="M" * (MAX_MESSAGE_LENGTH + 1))

    def test_allowed_status_transitions(self) -> None:
        self.assertEqual(
            ALLOWED_STATUS_TRANSITIONS[ContactMessageStatus.NEW],
            {ContactMessageStatus.READ, ContactMessageStatus.RESOLVED},
        )
        self.assertEqual(
            ALLOWED_STATUS_TRANSITIONS[ContactMessageStatus.READ],
            {ContactMessageStatus.NEW, ContactMessageStatus.RESOLVED},
        )
        self.assertEqual(
            ALLOWED_STATUS_TRANSITIONS[ContactMessageStatus.RESOLVED],
            {ContactMessageStatus.NEW, ContactMessageStatus.READ},
        )


class TestContactRepository(unittest.TestCase):
    """Integration tests for ContactMessageRepository using in-memory SQLite."""

    def setUp(self) -> None:
        self.conn = create_connection(":memory:")
        init_db(self.conn)
        self.repo = ContactMessageRepository(conn=self.conn)

    def tearDown(self) -> None:
        self.conn.close()

    def test_create_and_get_by_id(self) -> None:
        msg = self.repo.create(
            name="Charlie Brown",
            email="charlie@example.com",
            subject="Delivery inquiry",
            message="Can you deliver before 9am?",
        )
        self.assertIsNotNone(msg.id)
        self.assertEqual(msg.status, ContactMessageStatus.NEW)

        fetched = self.repo.get_by_id(msg.id)
        self.assertIsNotNone(fetched)
        self.assertEqual(fetched.id, msg.id)
        self.assertEqual(fetched.name, "Charlie Brown")
        self.assertEqual(fetched.email, "charlie@example.com")
        self.assertEqual(fetched.subject, "Delivery inquiry")
        self.assertEqual(fetched.message, "Can you deliver before 9am?")
        self.assertEqual(fetched.status, ContactMessageStatus.NEW)

    def test_get_by_id_nonexistent_returns_none(self) -> None:
        self.assertIsNone(self.repo.get_by_id(999999))

    def test_get_all_ordered_and_filtered(self) -> None:
        m1 = self.repo.create(name="User 1", email="u1@example.com", subject="Sub 1", message="Msg 1")
        m2 = self.repo.create(name="User 2", email="u2@example.com", subject="Sub 2", message="Msg 2")
        m3 = self.repo.create(name="User 3", email="u3@example.com", subject="Sub 3", message="Msg 3")

        # Update m2 to resolved
        self.repo.update_status(m2.id, ContactMessageStatus.RESOLVED.value)

        # Retrieve all
        all_msgs = self.repo.get_all()
        self.assertEqual(len(all_msgs), 3)
        # Should be ordered created_at DESC, id DESC
        self.assertEqual(all_msgs[0].id, m3.id)

        # Filter by resolved
        resolved = self.repo.get_all(status="resolved")
        self.assertEqual(len(resolved), 1)
        self.assertEqual(resolved[0].id, m2.id)

        # Filter by new
        new_msgs = self.repo.get_all(status="new")
        self.assertEqual(len(new_msgs), 2)
        self.assertEqual([m.id for m in new_msgs], [m3.id, m1.id])

    def test_count_and_count_by_status(self) -> None:
        self.assertEqual(self.repo.count(), 0)
        self.assertEqual(self.repo.count_by_status(), {"new": 0, "read": 0, "resolved": 0, "total": 0})

        m1 = self.repo.create(name="U1", email="u1@example.com", subject="S1", message="M1")
        m2 = self.repo.create(name="U2", email="u2@example.com", subject="S2", message="M2")
        self.repo.update_status(m1.id, "read")

        self.assertEqual(self.repo.count(), 2)
        self.assertEqual(self.repo.count("new"), 1)
        self.assertEqual(self.repo.count("read"), 1)
        self.assertEqual(self.repo.count("resolved"), 0)

        counts = self.repo.count_by_status()
        self.assertEqual(counts["new"], 1)
        self.assertEqual(counts["read"], 1)
        self.assertEqual(counts["resolved"], 0)
        self.assertEqual(counts["total"], 2)

        # Specific status argument
        self.assertEqual(self.repo.count_by_status("read"), 1)

    def test_update_status(self) -> None:
        m = self.repo.create(name="U", email="u@example.com", subject="S", message="M")
        original_updated_at = m.updated_at

        # Ensure timestamp difference
        time.sleep(0.01)
        success = self.repo.update_status(m.id, ContactMessageStatus.RESOLVED.value)
        self.assertTrue(success)
        updated = self.repo.get_by_id(m.id)
        self.assertIsNotNone(updated)
        self.assertEqual(updated.status, ContactMessageStatus.RESOLVED)
        self.assertIsNone(original_updated_at)
        self.assertIsInstance(updated.updated_at, datetime)
        self.assertGreaterEqual(updated.updated_at, updated.created_at)


class TestContactRateLimiter(unittest.TestCase):
    """Tests for sliding-window thread-safe contact rate limiter."""

    def setUp(self) -> None:
        self.limiter = ContactRateLimiter(max_attempts=3, window_seconds=2, max_keys=5)

    def test_rate_limiting_threshold(self) -> None:
        ip = "192.168.1.10"
        self.assertFalse(self.limiter.is_rate_limited(ip))

        self.limiter.record_submission(ip)
        self.limiter.record_submission(ip)
        self.assertFalse(self.limiter.is_rate_limited(ip))

        self.limiter.record_submission(ip)
        # 3 attempts reached
        self.assertTrue(self.limiter.is_rate_limited(ip))

    def test_window_expiration(self) -> None:
        ip = "192.168.1.20"
        self.limiter.record_submission(ip)
        self.limiter.record_submission(ip)
        self.limiter.record_submission(ip)
        self.assertTrue(self.limiter.is_rate_limited(ip))

        # Sleep past window
        time.sleep(2.05)
        self.assertFalse(self.limiter.is_rate_limited(ip))

    def test_reset_and_clear(self) -> None:
        ip = "192.168.1.30"
        self.limiter.record_submission(ip)
        self.limiter.record_submission(ip)
        self.limiter.record_submission(ip)
        self.assertTrue(self.limiter.is_rate_limited(ip))

        self.limiter.reset(ip)
        self.assertFalse(self.limiter.is_rate_limited(ip))

        self.limiter.record_submission(ip)
        self.limiter.clear()
        self.assertEqual(len(self.limiter._attempts), 0)

    def test_active_lockouts_are_not_evicted_under_capacity_pressure(self) -> None:
        """Verify that an attacker cannot evict an active IP block by flooding new keys."""
        target_ip = "192.168.1.100"
        # Reach maximum attempts to lock out target_ip
        for _ in range(3):
            self.limiter.record_submission(target_ip)
        self.assertTrue(self.limiter.is_rate_limited(target_ip))

        # Flood limiter with single submissions from other IPs beyond max_keys (max_keys=5)
        for i in range(10):
            self.limiter.record_submission(f"10.0.0.{i}")

        # Target IP lockout must remain intact and NOT evicted
        self.assertTrue(self.limiter.is_rate_limited(target_ip))
        self.assertIn(target_ip, self.limiter._attempts)

    def test_rate_limiter_saturated_with_active_blocks_fails_closed(self) -> None:
        """When capacity is saturated with active blocks, limiter fails closed for untracked IPs."""
        # Fill all 5 slots with active lockouts (3 attempts each)
        for i in range(5):
            ip = f"172.16.0.{i}"
            for _ in range(3):
                self.limiter.record_submission(ip)
            self.assertTrue(self.limiter.is_rate_limited(ip))

        # A brand new untracked IP should fail closed (return True for is_rate_limited)
        new_ip = "172.16.99.99"
        self.assertTrue(self.limiter.is_rate_limited(new_ip))


class TestContactService(unittest.TestCase):
    """Tests for ContactService business logic."""

    def setUp(self) -> None:
        self.conn = create_connection(":memory:")
        init_db(self.conn)
        self.repo = ContactMessageRepository(conn=self.conn)
        self.limiter = ContactRateLimiter(max_attempts=2, window_seconds=300)
        self.service = ContactService(contact_repo=self.repo, rate_limiter=self.limiter, conn=self.conn)

    def tearDown(self) -> None:
        self.conn.close()

    def test_submit_message_success(self) -> None:
        msg = self.service.submit_message(
            name="Diana Prince",
            email="DIANA@Example.COM",
            subject="Store hours",
            message="Are you open on bank holidays?",
            client_ip="10.0.0.1",
        )
        self.assertEqual(msg.name, "Diana Prince")
        self.assertEqual(msg.email, "diana@example.com")  # normalized lowercase
        self.assertEqual(msg.subject, "Store hours")
        self.assertEqual(msg.status, ContactMessageStatus.NEW)

    def test_submit_message_validation_failure(self) -> None:
        with self.assertRaises(ContactValidationError) as ctx:
            self.service.submit_message(
                name="",
                email="not-an-email",
                subject="",
                message="",
                client_ip="10.0.0.1",
            )
        self.assertIn("name", ctx.exception.errors)
        self.assertIn("email", ctx.exception.errors)
        self.assertIn("subject", ctx.exception.errors)
        self.assertIn("message", ctx.exception.errors)

    def test_submit_message_rate_limit_exceeded(self) -> None:
        ip = "10.0.0.2"
        self.service.submit_message("U", "u@example.com", "S", "M", client_ip=ip)
        self.service.submit_message("U", "u@example.com", "S", "M", client_ip=ip)

        with self.assertRaises(ContactRateLimitExceededError):
            self.service.submit_message("U", "u@example.com", "S", "M", client_ip=ip)

    def test_get_message_does_not_mutate_status_on_get(self) -> None:
        """Safe GET semantics: retrieving a message must never modify persistent database state."""
        msg = self.service.submit_message("U", "u@example.com", "S", "M", client_ip="10.0.0.3")
        self.assertEqual(msg.status, ContactMessageStatus.NEW)

        fetched = self.service.get_message(msg.id)
        self.assertIsNotNone(fetched)
        self.assertEqual(fetched.status, ContactMessageStatus.NEW)

        # Confirm database row remains NEW
        raw = self.repo.get_by_id(msg.id)
        self.assertEqual(raw.status, ContactMessageStatus.NEW)

    def test_update_status_transitions(self) -> None:
        msg = self.service.submit_message("U", "u@example.com", "S", "M", client_ip="10.0.0.4")
        # new -> resolved
        res = self.service.update_status(msg.id, "resolved")
        self.assertEqual(res.status, ContactMessageStatus.RESOLVED)

        # resolved -> new (reopen)
        reopened = self.service.update_status(msg.id, "new")
        self.assertEqual(reopened.status, ContactMessageStatus.NEW)

        # same status is idempotent no-op
        same = self.service.update_status(msg.id, "new")
        self.assertEqual(same.status, ContactMessageStatus.NEW)


class TestContactPublicRoutes(unittest.TestCase):
    """Integration tests for public GET/POST /contact endpoints."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_contact_routes.sqlite3"

        class TestRouteConfig(Config):
            DATABASE_PATH = str(self.db_path)
            TESTING = True
            SECRET_KEY = "test-contact-secret"
            CSRF_ENABLED = False

        self.app = create_app(TestRouteConfig)
        self.client = self.app.test_client()
        with self.app.app_context():
            init_db()

        # Clear global rate limiter
        contact_rate_limiter.clear()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()
        contact_rate_limiter.clear()

    def test_get_contact_page_renders_form(self) -> None:
        resp = self.client.get("/contact")
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn("Contact Customer Support", html)
        self.assertIn('name="name"', html)
        self.assertIn('name="email"', html)
        self.assertIn('name="subject"', html)
        self.assertIn('name="message"', html)
        self.assertIn("Send Message", html)

    def test_post_contact_valid_submission_redirects_and_flashes(self) -> None:
        resp = self.client.post(
            "/contact",
            data={
                "name": "Jane Baker",
                "email": "jane@example.com",
                "subject": "Bulk purchase inquiry",
                "message": "Do you offer wholesale sourdough discounts for cafes?",
            },
            follow_redirects=True,
        )
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn("Thank you! Your message has been sent successfully", html)

        # Verify persisted in database
        with self.app.app_context():
            repo = ContactMessageRepository()
            messages = repo.get_all()
            self.assertEqual(len(messages), 1)
            self.assertEqual(messages[0].name, "Jane Baker")
            self.assertEqual(messages[0].email, "jane@example.com")
            self.assertEqual(messages[0].subject, "Bulk purchase inquiry")

    def test_post_contact_validation_errors_return_400_and_preserve_input(self) -> None:
        resp = self.client.post(
            "/contact",
            data={
                "name": "",
                "email": "not-valid-email",
                "subject": "Missing name test",
                "message": "Some message text that should be preserved in textarea.",
            },
        )
        self.assertEqual(resp.status_code, 400)
        html = resp.get_data(as_text=True)
        self.assertIn("Name is required.", html)
        self.assertIn("Please enter a valid email address", html)
        # Verify inputs preserved
        self.assertIn("Missing name test", html)
        self.assertIn("Some message text that should be preserved in textarea.", html)

    def test_post_contact_rate_limiting_returns_429(self) -> None:
        # Submit 5 messages from same IP
        for i in range(5):
            resp = self.client.post(
                "/contact",
                data={
                    "name": f"User {i}",
                    "email": f"u{i}@example.com",
                    "subject": f"Sub {i}",
                    "message": f"Msg {i}",
                },
                environ_base={"REMOTE_ADDR": "198.51.100.42"},
            )
            self.assertEqual(resp.status_code, 302)

        # 6th submission must be rate limited with HTTP 429
        resp = self.client.post(
            "/contact",
            data={
                "name": "Blocked User",
                "email": "blocked@example.com",
                "subject": "Blocked Subject",
                "message": "Blocked message body",
            },
            environ_base={"REMOTE_ADDR": "198.51.100.42"},
        )
        self.assertEqual(resp.status_code, 429)
        html = resp.get_data(as_text=True)
        self.assertIn("Too many submissions", html)

    def test_client_ip_does_not_trust_x_forwarded_for_header(self) -> None:
        """Verify that forged X-Forwarded-For headers cannot bypass rate limiting."""
        # Submit 5 messages with different fake X-Forwarded-For headers from the same socket IP
        for i in range(5):
            resp = self.client.post(
                "/contact",
                data={
                    "name": f"User {i}",
                    "email": f"u{i}@example.com",
                    "subject": f"Sub {i}",
                    "message": f"Msg {i}",
                },
                headers={"X-Forwarded-For": f"203.0.113.{i + 1}"},
                environ_base={"REMOTE_ADDR": "198.51.100.99"},
            )
            self.assertEqual(resp.status_code, 302)

        # 6th submission from 198.51.100.99 with yet another spoofed X-Forwarded-For must be throttled
        resp = self.client.post(
            "/contact",
            data={
                "name": "Spoofer",
                "email": "spoofer@example.com",
                "subject": "Spoofed Sub",
                "message": "Spoofed Msg",
            },
            headers={"X-Forwarded-For": "203.0.113.250"},
            environ_base={"REMOTE_ADDR": "198.51.100.99"},
        )
        self.assertEqual(resp.status_code, 429)

    def test_post_contact_whitespace_only_fields_rejected(self) -> None:
        """Verify that whitespace-only fields are rejected by server validation."""
        resp = self.client.post(
            "/contact",
            data={
                "name": "   \t  ",
                "email": "   \n  ",
                "subject": "   ",
                "message": "     \r\n   ",
            },
        )
        self.assertEqual(resp.status_code, 400)
        html = resp.get_data(as_text=True)
        self.assertIn("Name is required.", html)
        self.assertIn("Email is required.", html)
        self.assertIn("Subject is required.", html)
        self.assertIn("Message is required.", html)

    def test_post_contact_csrf_protection_when_enabled(self) -> None:
        class CSRFConfig(Config):
            DATABASE_PATH = str(self.db_path)
            TESTING = True
            SECRET_KEY = "csrf-active-test-key"
            CSRF_ENABLED = True

        app_csrf = create_app(CSRFConfig)
        client_csrf = app_csrf.test_client()

        # POST without CSRF token must fail with 400
        resp = client_csrf.post(
            "/contact",
            data={
                "name": "Jane",
                "email": "jane@example.com",
                "subject": "Sub",
                "message": "Msg",
            },
        )
        self.assertEqual(resp.status_code, 400)


class TestContactAdminRoutes(unittest.TestCase):
    """Integration tests for store owner customer support inbox routes."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_contact_admin.sqlite3"

        class TestAdminConfig(Config):
            DATABASE_PATH = str(self.db_path)
            TESTING = True
            SECRET_KEY = "test-contact-admin-key"
            CSRF_ENABLED = False

        self.app = create_app(TestAdminConfig)
        self.client = self.app.test_client()

        with self.app.app_context():
            init_db()
            self.user_repo = UserRepository()
            self.contact_repo = ContactMessageRepository()

            pwd_hash = AuthService.hash_password("Password123!")
            self.customer_user = self.user_repo.create(
                User(email="customer@example.com", password_hash=pwd_hash, role=UserRole.CUSTOMER, is_verified=True)
            )
            self.unverified_owner = self.user_repo.create(
                User(email="unverified@example.com", password_hash=pwd_hash, role=UserRole.OWNER, is_verified=False)
            )
            self.verified_owner = self.user_repo.create(
                User(email="owner@example.com", password_hash=pwd_hash, role=UserRole.OWNER, is_verified=True)
            )

            # Seed a message
            self.msg = self.contact_repo.create(
                name="John Doe",
                email="john@example.com",
                subject="Missing item",
                message="My delivery was missing the eggs.",
            )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_anonymous_access_to_inbox_redirects_to_login(self) -> None:
        resp = self.client.get("/admin/messages")
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/login", resp.headers.get("Location", ""))

    def test_customer_access_to_inbox_returns_403(self) -> None:
        with self.client.session_transaction() as sess:
            sess["user_id"] = self.customer_user.id

        resp = self.client.get("/admin/messages")
        self.assertEqual(resp.status_code, 403)

    def test_unverified_owner_access_to_inbox_returns_403(self) -> None:
        with self.client.session_transaction() as sess:
            sess["user_id"] = self.unverified_owner.id

        resp = self.client.get("/admin/messages")
        self.assertEqual(resp.status_code, 403)

    def test_verified_owner_access_to_inbox_returns_200(self) -> None:
        with self.client.session_transaction() as sess:
            sess["user_id"] = self.verified_owner.id

        resp = self.client.get("/admin/messages")
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn("Customer Support Inbox", html)
        self.assertIn("John Doe", html)
        self.assertIn("Missing item", html)

    def test_filter_tabs_in_inbox(self) -> None:
        with self.client.session_transaction() as sess:
            sess["user_id"] = self.verified_owner.id

        resp = self.client.get("/admin/messages?status=resolved")
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn("No messages found", html)

        resp = self.client.get("/admin/messages?status=new")
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn("John Doe", html)

    def test_view_message_detail_does_not_mutate_status_on_get(self) -> None:
        """Safe GET semantics: viewing message details never mutates persistent database state."""
        with self.client.session_transaction() as sess:
            sess["user_id"] = self.verified_owner.id

        # Verify initial status is new
        with self.app.app_context():
            initial = self.contact_repo.get_by_id(self.msg.id)
            self.assertEqual(initial.status, ContactMessageStatus.NEW)

        # GET detail view
        resp = self.client.get(f"/admin/messages/{self.msg.id}")
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn("Support Message #", html)
        self.assertIn("My delivery was missing the eggs.", html)
        self.assertIn("Mark as Read", html)
        self.assertIn("Mark as Resolved", html)

        # Status must STILL be NEW in database (no side effects on GET)
        with self.app.app_context():
            after_view = self.contact_repo.get_by_id(self.msg.id)
            self.assertEqual(after_view.status, ContactMessageStatus.NEW)

    def test_mark_message_read_via_post_action(self) -> None:
        """Marking as read is explicitly performed via CSRF-protected POST."""
        with self.client.session_transaction() as sess:
            sess["user_id"] = self.verified_owner.id

        resp = self.client.post(
            f"/admin/messages/{self.msg.id}/status",
            data={"status": "read"},
            follow_redirects=True,
        )
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn("status updated to", html)
        self.assertIn("read", html)

        with self.app.app_context():
            after_post = self.contact_repo.get_by_id(self.msg.id)
            self.assertEqual(after_post.status, ContactMessageStatus.READ)

    def test_mark_read_from_index_table_redirects_to_index(self) -> None:
        """Quick 'Mark Read' action from index table redirects back to index."""
        with self.client.session_transaction() as sess:
            sess["user_id"] = self.verified_owner.id

        resp = self.client.post(
            f"/admin/messages/{self.msg.id}/status",
            data={"status": "read", "redirect_to": "index"},
            follow_redirects=True,
        )
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn("Customer Support Inbox", html)
        self.assertIn("status updated to", html)

    def test_view_nonexistent_message_returns_404(self) -> None:
        with self.client.session_transaction() as sess:
            sess["user_id"] = self.verified_owner.id

        resp = self.client.get("/admin/messages/999999")
        self.assertEqual(resp.status_code, 404)

    def test_post_update_message_status(self) -> None:
        with self.client.session_transaction() as sess:
            sess["user_id"] = self.verified_owner.id

        resp = self.client.post(
            f"/admin/messages/{self.msg.id}/status",
            data={"status": "resolved"},
            follow_redirects=True,
        )
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn("status updated to", html)
        self.assertIn("resolved", html)

        with self.app.app_context():
            updated = self.contact_repo.get_by_id(self.msg.id)
            self.assertEqual(updated.status, ContactMessageStatus.RESOLVED)

    def test_admin_dashboard_shows_new_messages_metric(self) -> None:
        with self.client.session_transaction() as sess:
            sess["user_id"] = self.verified_owner.id

        resp = self.client.get("/admin")
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn('id="metric-new-messages"', html)
        self.assertIn("Customer Support Inbox", html)


class TestContactDatabaseMigration(unittest.TestCase):
    """Tests confirming schema migration idempotence."""

    def test_migration_is_idempotent(self) -> None:
        conn = create_connection(":memory:")
        # Initial run creates tables
        init_db(conn)
        # Second run must execute without any errors
        init_db(conn)
        # Third run must execute without any errors
        init_db(conn)

        # Check table columns
        cursor = conn.execute("PRAGMA table_info(contact_messages);")
        columns = {row["name"]: row["type"] for row in cursor.fetchall()}
        self.assertIn("id", columns)
        self.assertIn("name", columns)
        self.assertIn("email", columns)
        self.assertIn("subject", columns)
        self.assertIn("message", columns)
        self.assertIn("status", columns)
        self.assertIn("created_at", columns)
        self.assertIn("updated_at", columns)
        conn.close()


if __name__ == "__main__":
    unittest.main()

"""Unit and integration tests for the one-click Windows launcher (start_shop.bat & app.launcher)."""
import os
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from app.config import Config
from app.launcher import (
    check_port_available,
    open_browser_when_ready,
    verify_environment_and_database,
)


class TestLauncherVerificationAndGuards(unittest.TestCase):
    """Test suite verifying safety guards, preflight checks, and browser handling."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root_path = Path(self.temp_dir.name)
        self._orig_db_path = os.environ.get("DATABASE_PATH")
        os.environ.pop("DATABASE_PATH", None)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()
        if self._orig_db_path is not None:
            os.environ["DATABASE_PATH"] = self._orig_db_path
        else:
            os.environ.pop("DATABASE_PATH", None)

    def test_verify_environment_missing_env_raises_file_not_found(self) -> None:
        """Launcher must refuse to start and halt if .env is missing."""
        with self.assertRaises(FileNotFoundError) as ctx:
            verify_environment_and_database(self.root_path)
        self.assertIn(".env", str(ctx.exception))
        self.assertIn("was not found", str(ctx.exception))

    def test_verify_environment_missing_database_raises_file_not_found(self) -> None:
        """Launcher must refuse to start if the configured SQLite database file is missing."""
        # Create .env pointing to a non-existent database file
        env_file = self.root_path / ".env"
        db_file = self.root_path / "instance" / "online_food_shop.sqlite3"
        env_file.write_text(f"DATABASE_PATH={db_file}\nFLASK_CONFIG=development\n", encoding="utf-8")

        with self.assertRaises(FileNotFoundError) as ctx:
            verify_environment_and_database(self.root_path)
        self.assertIn("database file was not found", str(ctx.exception).lower())
        self.assertFalse(db_file.exists(), "Launcher must NOT silently create an empty database file")

    def test_verify_environment_and_database_success(self) -> None:
        """Launcher accepts existing valid .env and existing database without alteration."""
        env_file = self.root_path / ".env"
        inst_dir = self.root_path / "instance"
        inst_dir.mkdir(parents=True, exist_ok=True)
        db_file = inst_dir / "online_food_shop.sqlite3"
        db_file.write_text("DUMMY_DATABASE_PAYLOAD", encoding="utf-8")

        env_file.write_text(f"DATABASE_PATH={db_file}\nFLASK_CONFIG=development\n", encoding="utf-8")

        resolved_db = verify_environment_and_database(self.root_path)
        self.assertEqual(resolved_db, db_file.resolve())
        # Content must remain identical (zero resets or overwrites)
        self.assertEqual(db_file.read_text(encoding="utf-8"), "DUMMY_DATABASE_PAYLOAD")

    def test_no_accidental_demo_database_fallback(self) -> None:
        """Application config defaults to online_food_shop.sqlite3 and never demo_food_shop.sqlite3."""
        cfg = Config()
        self.assertIn("online_food_shop.sqlite3", cfg.DATABASE_PATH)
        self.assertNotIn("demo_food_shop.sqlite3", cfg.DATABASE_PATH)

    def test_check_port_available_free_port(self) -> None:
        """Check returns True when the port is free and unassigned."""
        # Bind and close a socket to find an OS-allocated free port
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            free_port = s.getsockname()[1]

        self.assertTrue(check_port_available("127.0.0.1", free_port))

    def test_check_port_available_occupied_port_raises_os_error(self) -> None:
        """Check raises OSError with clear diagnostic when the port is already occupied."""
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            s.listen(1)
            occupied_port = s.getsockname()[1]

            with self.assertRaises(OSError) as ctx:
                check_port_available("127.0.0.1", occupied_port)
            self.assertIn(f"Port {occupied_port} is already in use", str(ctx.exception))

    def test_open_browser_when_ready_success(self) -> None:
        """Readiness monitor probes server and opens browser when HTTP 200 is received."""
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.__enter__.return_value = mock_resp

        with patch("urllib.request.urlopen", return_value=mock_resp), patch(
            "webbrowser.open"
        ) as mock_browser_open:
            opened = open_browser_when_ready(host="127.0.0.1", port=5000, timeout=2.0, poll_interval=0.01)
            self.assertTrue(opened)
            mock_browser_open.assert_called_once_with("http://127.0.0.1:5000")

    def test_open_browser_when_ready_timeout_never_opens_browser(self) -> None:
        """Readiness monitor times out safely and never opens browser if server failed to start."""
        with patch("urllib.request.urlopen", side_effect=OSError("Connection refused")), patch(
            "webbrowser.open"
        ) as mock_browser_open:
            opened = open_browser_when_ready(host="127.0.0.1", port=5000, timeout=0.1, poll_interval=0.02)
            self.assertFalse(opened)
            mock_browser_open.assert_not_called()

    def test_start_shop_bat_guards_and_integrity(self) -> None:
        """Verify that start_shop.bat contains all required safety checks and no demo regressions."""
        bat_path = Path(__file__).resolve().parent.parent / "start_shop.bat"
        self.assertTrue(bat_path.is_file(), "start_shop.bat must exist in project root")

        content = bat_path.read_text(encoding="utf-8")

        # 1. Working directory resolution
        self.assertIn('cd /d "%~dp0"', content)
        # 2. Critical .env check
        self.assertIn('if not exist ".env"', content)
        # 3. Python 3.10+ check
        self.assertIn("Python 3.10 or newer is required", content)
        # 4. Relocated / broken .venv detection
        self.assertIn(".venv\\Scripts\\python.exe", content)
        self.assertIn("import sys; sys.exit(0)", content)
        # 5. Invokes application launcher
        self.assertIn("-m app.launcher", content)
        # 6. Must NOT invoke seed-demo or create-owner
        self.assertNotIn("seed-demo", content)
        self.assertNotIn("create-owner", content)
        # 7. Must NOT reference demo database as default
        self.assertNotIn("demo_food_shop.sqlite3", content)

    def test_database_data_preserved_and_not_reset(self) -> None:
        """Verify that preflight checks never modify, reset, or drop existing SQLite records."""
        import sqlite3

        env_file = self.root_path / ".env"
        inst_dir = self.root_path / "instance"
        inst_dir.mkdir(parents=True, exist_ok=True)
        db_file = inst_dir / "online_food_shop.sqlite3"

        conn = sqlite3.connect(str(db_file))
        conn.execute("CREATE TABLE mock_customers (id INTEGER PRIMARY KEY, email TEXT);")
        conn.execute("INSERT INTO mock_customers (email) VALUES ('existing_customer@test.com');")
        conn.commit()
        conn.close()

        env_file.write_text(f"DATABASE_PATH={db_file}\nFLASK_CONFIG=development\n", encoding="utf-8")

        # Run preflight verification
        verify_environment_and_database(self.root_path)

        # Confirm data is 100% intact
        conn = sqlite3.connect(str(db_file))
        cur = conn.cursor()
        cur.execute("SELECT email FROM mock_customers;")
        row = cur.fetchone()
        conn.close()

        self.assertIsNotNone(row)
        self.assertEqual(row[0], "existing_customer@test.com")

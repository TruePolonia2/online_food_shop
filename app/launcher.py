"""Application launcher module for one-click Windows and local execution."""
import os
from pathlib import Path
import socket
import sys
import threading
import time
import urllib.request
import webbrowser
from typing import Optional

from app.config import BASE_DIR, Config, load_project_env


def verify_environment_and_database(base_dir: Optional[Path] = None) -> Path:
    """Verify that .env and the configured SQLite database exist before startup.

    Enforces strict data preservation rules:
    - Verifies that .env exists in the project root.
    - Resolves the active database path from application configuration.
    - Verifies that the resolved database file exists on disk.
    - Strictly refuses to create a new database or fallback to demo databases.

    Args:
        base_dir: Optional project root path (defaults to app.config.BASE_DIR).

    Returns:
        The verified Path to the active SQLite database.

    Raises:
        FileNotFoundError: If .env or the active database file does not exist.
    """
    root = Path(base_dir).resolve() if base_dir else BASE_DIR

    env_path = root / ".env"
    if not env_path.is_file():
        raise FileNotFoundError(
            f"Configuration file '.env' was not found in project root ({root}).\n"
            "This launcher runs your transferred original website and requires your original '.env'.\n"
            "To prevent data mismatch, a new .env will not be silently generated."
        )

    # Load environment variables from the verified .env (override=True ensures process reflects verified .env)
    load_project_env(env_path, override=True)

    cfg = Config()
    db_path = Path(cfg.DATABASE_PATH).resolve()

    if not db_path.is_file():
        raise FileNotFoundError(
            f"Expected SQLite database file was not found on disk at:\n  {db_path}\n"
            "The launcher will NOT silently create an empty database or switch to a demo catalog.\n"
            "Please ensure your original SQLite database (e.g. instance/online_food_shop.sqlite3) was transferred."
        )

    return db_path


def check_port_available(host: str = "127.0.0.1", port: int = 5000) -> bool:
    """Verify that the target host and port are not already in use by another process.

    Args:
        host: Host address to check.
        port: Port number to check.

    Returns:
        True if the port is available.

    Raises:
        OSError: If the port is already occupied.
    """
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            s.bind((host, port))
        return True
    except OSError as err:
        raise OSError(
            f"Port {port} is already in use by another application or server.\n"
            f"Please stop any running process using port {port} and run start_shop.bat again."
        ) from err


def open_browser_when_ready(
    host: str = "127.0.0.1",
    port: int = 5000,
    timeout: float = 20.0,
    poll_interval: float = 0.5,
) -> bool:
    """Poll the local server until it responds with HTTP 200, then open the default browser.

    Args:
        host: Host address where the server is binding.
        port: Port number.
        timeout: Maximum seconds to wait before giving up.
        poll_interval: Seconds between probe requests.

    Returns:
        True if the server responded and browser was opened, False if timed out.
    """
    target_url = f"http://{host}:{port}"
    deadline = time.time() + timeout

    while time.time() < deadline:
        time.sleep(poll_interval)
        try:
            req = urllib.request.Request(
                f"{target_url}/",
                headers={"User-Agent": "FoodShop-ReadinessProbe/1.0"},
            )
            with urllib.request.urlopen(req, timeout=1.0) as resp:
                if resp.status == 200:
                    webbrowser.open(target_url)
                    return True
        except Exception:
            continue

    return False


def run_launcher(host: str = "127.0.0.1", port: int = 5000) -> None:
    """Execute preflight checks, launch the server, and open the browser upon readiness.

    Args:
        host: Host loopback interface (defaults to 127.0.0.1).
        port: Port number (defaults to 5000).
    """
    try:
        db_path = verify_environment_and_database()
        print(f"[OK] Verified configuration (.env) and active database ({db_path.name}).")
    except FileNotFoundError as err:
        print(f"\n[ERROR] {err}\n", file=sys.stderr)
        sys.exit(1)

    try:
        check_port_available(host, port)
        print(f"[OK] Port {port} is available.")
    except OSError as err:
        print(f"\n[ERROR] {err}\n", file=sys.stderr)
        sys.exit(1)

    # Start readiness monitor in a daemon thread so it terminates if Flask fails
    monitor_thread = threading.Thread(
        target=open_browser_when_ready,
        args=(host, port),
        daemon=True,
    )
    monitor_thread.start()

    from app import create_app

    app = create_app()

    print("\n" + "=" * 70)
    print("  Online Food Shop is running!")
    print(f"  Local Address:  http://{host}:{port}")
    print("  Debug Mode:     Disabled (Safe Production Mode)")
    print("=" * 70)
    print("\nThe website will open in your default browser once ready.")
    print("To STOP the website, press Ctrl + C or close this window.\n")

    app.run(host=host, port=port, debug=False)


if __name__ == "__main__":
    run_launcher()

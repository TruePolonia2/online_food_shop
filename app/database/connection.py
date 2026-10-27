"""SQLite database connection and lifecycle management for the online food shop."""
from contextlib import contextmanager
from pathlib import Path
import sqlite3
from typing import Generator, Optional, Union

import click
from flask import Flask, current_app, g, has_app_context
from flask.cli import with_appcontext

DEFAULT_SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"


def create_connection(database_path: Union[str, Path]) -> sqlite3.Connection:
    """Create and configure a new SQLite database connection.

    Ensures the instance or target directory exists before opening the file,
    enables foreign key enforcement on the connection, and configures
    sqlite3.Row as the row factory for dictionary-like column access.

    Args:
        database_path: Path to the SQLite database file, or ':memory:'.

    Returns:
        A configured sqlite3.Connection instance with foreign keys enabled.
    """
    path_str = str(database_path)
    if path_str != ":memory:" and not path_str.startswith("file:"):
        db_path = Path(path_str).resolve()
        db_path.parent.mkdir(parents=True, exist_ok=True)
        target = str(db_path)
    else:
        target = path_str

    conn = sqlite3.connect(
        target,
        detect_types=sqlite3.PARSE_DECLTYPES | sqlite3.PARSE_COLNAMES,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


def get_db() -> sqlite3.Connection:
    """Get or create a request-scoped database connection.

    Retrieves the SQLite connection associated with the current application/request
    context stored on Flask's 'g' object. If no connection exists yet in the current
    context, a new one is opened using the database path configured in
    'current_app.config["DATABASE_PATH"]'.

    Returns:
        The active sqlite3.Connection for the current context.

    Raises:
        RuntimeError: If called outside an active Flask application context,
            or if DATABASE_PATH is not configured in current_app.config.
    """
    if not has_app_context():
        raise RuntimeError(
            "get_db() must be called within an active Flask application context. "
            "Outside an application context, use create_connection(database_path) "
            "or push an application context using 'with app.app_context():'."
        )

    if "db" not in g:
        database_path = current_app.config.get("DATABASE_PATH")
        if not database_path:
            raise RuntimeError(
                "DATABASE_PATH is not configured in the Flask application configuration."
            )
        g.db = create_connection(database_path)

    return g.db


def close_db(e: Optional[BaseException] = None) -> None:
    """Close the request-scoped database connection if one exists.

    Registered with Flask's teardown_appcontext hook to guarantee that
    database connections are cleaned up at the end of each request/application context.

    Args:
        e: Optional exception raised during request execution (handled by Flask).
    """
    db = g.pop("db", None)
    if db is not None:
        db.close()


_transaction_depths: dict[int, int] = {}


@contextmanager
def transaction(
    conn: Optional[sqlite3.Connection] = None,
) -> Generator[sqlite3.Connection, None, None]:
    """Context manager for explicit transaction and savepoint handling.

    Supports nested transactions via SQLite SAVEPOINTs. When entering the outermost
    transaction, creates 'sp_0'. Inner transactions create nested savepoints ('sp_1', etc.).
    Releases savepoints on clean exit or rolls back to the savepoint if an exception occurs,
    allowing service-level transactions to coordinate multiple repository operations atomically.

    Args:
        conn: Optional SQLite connection. If omitted, uses the active request connection
            from get_db().

    Yields:
        The active sqlite3.Connection.

    Example:
        with transaction() as conn:
            conn.execute("INSERT INTO categories (name) VALUES (?)", ("Bakery",))
    """
    if conn is None:
        conn = get_db()

    conn_id = id(conn)
    depth = _transaction_depths.get(conn_id, 0)
    _transaction_depths[conn_id] = depth + 1
    sp_name = f"sp_{depth}"

    savepoint_created = False
    try:
        conn.execute(f"SAVEPOINT {sp_name};")
        savepoint_created = True
        yield conn
    except Exception:
        if savepoint_created:
            try:
                conn.execute(f"ROLLBACK TO SAVEPOINT {sp_name};")
            finally:
                conn.execute(f"RELEASE SAVEPOINT {sp_name};")
        raise
    else:
        conn.execute(f"RELEASE SAVEPOINT {sp_name};")
    finally:
        current = _transaction_depths.get(conn_id, 1)
        if current <= 1:
            _transaction_depths.pop(conn_id, None)
        else:
            _transaction_depths[conn_id] = current - 1


def _migrate_users_table_if_needed(conn: sqlite3.Connection) -> None:
    """Safely and idempotently ensure the users table has the password_hash column.

    Non-destructive: Uses ALTER TABLE ADD COLUMN only if password_hash is missing.
    """
    cursor = conn.execute("PRAGMA table_info(users);")
    existing_cols = {row["name"] for row in cursor.fetchall()}
    if not existing_cols:
        return

    if "password_hash" not in existing_cols:
        conn.execute("ALTER TABLE users ADD COLUMN password_hash TEXT;")


def _migrate_orders_table_if_needed(conn: sqlite3.Connection) -> None:
    """Safely and idempotently ensure the orders table has snapshot delivery and user_id columns.

    Non-destructive: Uses ALTER TABLE ADD COLUMN only if columns are missing.
    """
    cursor = conn.execute("PRAGMA table_info(orders);")
    existing_cols = {row["name"] for row in cursor.fetchall()}
    if not existing_cols:
        return

    columns_to_add = [
        ("customer_name", "TEXT"),
        ("customer_email", "TEXT"),
        ("customer_phone", "TEXT"),
        ("customer_address", "TEXT"),
    ]
    for col_name, col_type in columns_to_add:
        if col_name not in existing_cols:
            conn.execute(f"ALTER TABLE orders ADD COLUMN {col_name} {col_type};")

    if "user_id" not in existing_cols:
        conn.execute(
            "ALTER TABLE orders ADD COLUMN user_id INTEGER REFERENCES users(id) ON DELETE SET NULL;"
        )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_orders_user_id ON orders (user_id);")


def _migrate_customers_table_if_needed(conn: sqlite3.Connection) -> None:
    """Safely and idempotently ensure the customers table has the optional user_id column.

    Non-destructive: Uses ALTER TABLE ADD COLUMN only if user_id is missing.
    """
    cursor = conn.execute("PRAGMA table_info(customers);")
    existing_cols = {row["name"] for row in cursor.fetchall()}
    if not existing_cols:
        return

    if "user_id" not in existing_cols:
        conn.execute(
            "ALTER TABLE customers ADD COLUMN user_id INTEGER REFERENCES users(id) ON DELETE SET NULL;"
        )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_customers_user_id ON customers (user_id);")


def _migrate_categories_table_if_needed(conn: sqlite3.Connection) -> None:
    """Safely and idempotently ensure the categories table has the is_active column.

    Non-destructive: Uses ALTER TABLE ADD COLUMN only if is_active is missing.
    """
    cursor = conn.execute("PRAGMA table_info(categories);")
    existing_cols = {row["name"] for row in cursor.fetchall()}
    if not existing_cols:
        return

    if "is_active" not in existing_cols:
        conn.execute(
            "ALTER TABLE categories ADD COLUMN is_active INTEGER NOT NULL DEFAULT 1 CHECK(is_active IN (0, 1));"
        )


def _migrate_products_table_if_needed(conn: sqlite3.Connection) -> None:
    """Safely and idempotently ensure the products table has the image_url column.

    Non-destructive: Uses ALTER TABLE ADD COLUMN only if image_url is missing.
    """
    cursor = conn.execute("PRAGMA table_info(products);")
    existing_cols = {row["name"] for row in cursor.fetchall()}
    if not existing_cols:
        return

    if "image_url" not in existing_cols:
        conn.execute("ALTER TABLE products ADD COLUMN image_url TEXT;")


def _migrate_external_identities_table_if_needed(conn: sqlite3.Connection) -> None:
    """Safely and idempotently ensure the user_external_identities table and indexes exist."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS user_external_identities (
            id INTEGER PRIMARY KEY,
            user_id INTEGER NOT NULL,
            provider TEXT NOT NULL CHECK(length(trim(provider)) > 0),
            provider_user_id TEXT NOT NULL CHECK(length(trim(provider_user_id)) > 0),
            email TEXT,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
            FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE,
            UNIQUE (provider, provider_user_id),
            UNIQUE (user_id, provider)
        );
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_external_identities_user_id ON user_external_identities (user_id);"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_external_identities_provider_sub ON user_external_identities (provider, provider_user_id);"
    )


def init_db(
    conn: Optional[sqlite3.Connection] = None,
    schema_path: Optional[Union[str, Path]] = None,
) -> None:
    """Initialize database tables and indexes from schema.sql.

    Initialization is idempotent and safe to run repeatedly. All DDL statements
    use CREATE TABLE IF NOT EXISTS and CREATE INDEX IF NOT EXISTS, ensuring no tables
    are dropped and no existing data is deleted.

    Args:
        conn: Optional SQLite connection. If omitted, uses the active request connection
            from get_db().
        schema_path: Optional path to an alternative schema SQL file. Defaults to
            app/database/schema.sql.
    """
    if conn is None:
        conn = get_db()

    target_schema = Path(schema_path).resolve() if schema_path else DEFAULT_SCHEMA_PATH

    with open(target_schema, "r", encoding="utf-8") as f:
        schema_sql = f.read()

    conn.executescript(schema_sql)
    _migrate_users_table_if_needed(conn)
    _migrate_orders_table_if_needed(conn)
    _migrate_customers_table_if_needed(conn)
    _migrate_categories_table_if_needed(conn)
    _migrate_products_table_if_needed(conn)
    _migrate_external_identities_table_if_needed(conn)
    conn.commit()


@click.command("init-db")
@with_appcontext
def init_db_command() -> None:
    """Flask CLI command to initialize database schema from schema.sql."""
    init_db()
    click.echo("Initialized SQLite database schema from schema.sql.")


def init_app(app: Flask) -> None:
    """Register database teardown hooks and CLI commands with the Flask application.

    Ensures connections are closed when requests end and registers the 'init-db'
    CLI command. Does NOT automatically run schema initialization on every request.

    Args:
        app: The Flask application instance.
    """
    app.teardown_appcontext(close_db)
    app.cli.add_command(init_db_command)


if __name__ == "__main__":
    from app import create_app

    app = create_app()
    with app.app_context():
        init_db()
        print(f"Database successfully initialized at: {app.config['DATABASE_PATH']}")

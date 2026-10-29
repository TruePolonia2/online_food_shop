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
    existing_cols = {row[1] for row in cursor.fetchall()}
    if not existing_cols:
        return

    if "password_hash" not in existing_cols:
        conn.execute("ALTER TABLE users ADD COLUMN password_hash TEXT;")


def _migrate_orders_table_if_needed(conn: sqlite3.Connection) -> None:
    """Safely and idempotently ensure the orders table has snapshot delivery and user_id columns.

    Non-destructive: Uses ALTER TABLE ADD COLUMN only if columns are missing.
    """
    cursor = conn.execute("PRAGMA table_info(orders);")
    existing_cols = {row[1] for row in cursor.fetchall()}
    if not existing_cols:
        return

    columns_to_add = [
        ("customer_name", "TEXT"),
        ("customer_email", "TEXT"),
        ("customer_phone", "TEXT"),
        ("customer_address", "TEXT"),
        ("payment_method", "TEXT NOT NULL DEFAULT 'standard'"),
        (
            "payment_status",
            "TEXT NOT NULL DEFAULT 'unpaid' CHECK(payment_status IN ('unpaid', 'pending', 'paid', 'failed', 'cancelled'))",
        ),
        ("stripe_session_id", "TEXT"),
        ("checkout_token", "TEXT"),
    ]
    for col_name, col_type in columns_to_add:
        if col_name not in existing_cols:
            conn.execute(f"ALTER TABLE orders ADD COLUMN {col_name} {col_type};")

    if "user_id" not in existing_cols:
        conn.execute(
            "ALTER TABLE orders ADD COLUMN user_id INTEGER REFERENCES users(id) ON DELETE SET NULL;"
        )

    idx_cursor = conn.execute("PRAGMA index_list('orders');")
    existing_indexes = {row[1] for row in idx_cursor.fetchall()}
    if "idx_orders_user_id" not in existing_indexes:
        conn.execute("CREATE INDEX IF NOT EXISTS idx_orders_user_id ON orders (user_id);")
    if "idx_orders_stripe_session_id" not in existing_indexes:
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_orders_stripe_session_id ON orders (stripe_session_id);"
        )
    if "idx_orders_checkout_token" not in existing_indexes:
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_orders_checkout_token ON orders (checkout_token) WHERE checkout_token IS NOT NULL;"
        )


def _migrate_customers_table_if_needed(conn: sqlite3.Connection) -> None:
    """Safely and idempotently ensure the customers table has the optional user_id column.

    Non-destructive: Uses ALTER TABLE ADD COLUMN only if user_id is missing.
    """
    cursor = conn.execute("PRAGMA table_info(customers);")
    existing_cols = {row[1] for row in cursor.fetchall()}
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
    existing_cols = {row[1] for row in cursor.fetchall()}
    if not existing_cols:
        return

    if "description" not in existing_cols:
        conn.execute("ALTER TABLE categories ADD COLUMN description TEXT;")

    if "is_active" not in existing_cols:
        conn.execute(
            "ALTER TABLE categories ADD COLUMN is_active INTEGER NOT NULL DEFAULT 1 CHECK(is_active IN (0, 1));"
        )


def _migrate_products_table_if_needed(conn: sqlite3.Connection) -> None:
    """Safely and idempotently ensure the products table has the image_url column.

    Non-destructive: Uses ALTER TABLE ADD COLUMN only if image_url is missing.
    """
    cursor = conn.execute("PRAGMA table_info(products);")
    existing_cols = {row[1] for row in cursor.fetchall()}
    if not existing_cols:
        return

    if "description" not in existing_cols:
        conn.execute("ALTER TABLE products ADD COLUMN description TEXT;")

    if "is_active" not in existing_cols:
        conn.execute(
            "ALTER TABLE products ADD COLUMN is_active INTEGER NOT NULL DEFAULT 1 CHECK(is_active IN (0, 1));"
        )

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


def _migrate_password_reset_tokens_table_if_needed(conn: sqlite3.Connection) -> None:
    """Safely and idempotently ensure the password_reset_tokens table and index exist."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS password_reset_tokens (
            id INTEGER PRIMARY KEY,
            user_id INTEGER NOT NULL,
            token_hash TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
            expires_at TEXT NOT NULL,
            used_at TEXT,
            FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE
        );
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_password_reset_tokens_user_id ON password_reset_tokens (user_id);"
    )


def _migrate_contact_messages_table_if_needed(conn: sqlite3.Connection) -> None:
    """Safely and idempotently ensure the contact_messages table and indexes exist."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS contact_messages (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL CHECK(length(trim(name)) > 0 AND length(name) <= 100),
            email TEXT NOT NULL CHECK(length(trim(email)) > 0 AND instr(email, '@') > 0 AND length(email) <= 254),
            subject TEXT NOT NULL CHECK(length(trim(subject)) > 0 AND length(subject) <= 150),
            message TEXT NOT NULL CHECK(length(trim(message)) > 0 AND length(message) <= 5000),
            status TEXT NOT NULL DEFAULT 'new' CHECK(status IN ('new', 'read', 'resolved')),
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
            updated_at TEXT
        );
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_contact_messages_status_created_at ON contact_messages (status, created_at DESC);"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_contact_messages_created_at ON contact_messages (created_at DESC);"
    )


def _migrate_account_link_intents_table_if_needed(conn: sqlite3.Connection) -> None:
    """Safely and idempotently ensure the account_link_intents table and indexes exist."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS account_link_intents (
            id INTEGER PRIMARY KEY,
            intent_id TEXT NOT NULL UNIQUE CHECK(length(trim(intent_id)) > 0),
            user_id INTEGER NOT NULL,
            provider TEXT NOT NULL CHECK(length(trim(provider)) > 0),
            provider_user_id TEXT NOT NULL CHECK(length(trim(provider_user_id)) > 0),
            email TEXT NOT NULL CHECK(length(trim(email)) > 0),
            action TEXT NOT NULL DEFAULT 'link' CHECK(action IN ('link', 'delete_account')),
            next_url TEXT,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
            expires_at TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending', 'consumed', 'cancelled')),
            consumed_at TEXT,
            cancelled_at TEXT,
            FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE
        );
        """
    )
    cursor = conn.execute("PRAGMA table_info(account_link_intents);")
    existing_cols = {row[1] for row in cursor.fetchall()}
    if "action" not in existing_cols:
        conn.execute(
            "ALTER TABLE account_link_intents ADD COLUMN action TEXT NOT NULL DEFAULT 'link' CHECK(action IN ('link', 'delete_account'));"
        )

    idx_cursor = conn.execute("PRAGMA index_list('account_link_intents');")
    existing_indexes = {row[1] for row in idx_cursor.fetchall()}
    if "idx_account_link_intents_intent_id" not in existing_indexes:
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_account_link_intents_intent_id ON account_link_intents (intent_id);"
        )
    if "idx_account_link_intents_user_status" not in existing_indexes:
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_account_link_intents_user_status ON account_link_intents (user_id, status);"
        )
    if "idx_account_link_intents_action_status" not in existing_indexes:
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_account_link_intents_action_status ON account_link_intents (user_id, action, status);"
        )


def _migrate_payments_table_if_needed(conn: sqlite3.Connection) -> None:
    """Safely and idempotently ensure the payments table and indexes exist."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS payments (
            id INTEGER PRIMARY KEY,
            order_id INTEGER NOT NULL,
            payment_method TEXT NOT NULL DEFAULT 'stripe',
            status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending', 'paid', 'failed', 'cancelled')),
            amount INTEGER NOT NULL CHECK(amount >= 0),
            currency TEXT NOT NULL DEFAULT 'eur' CHECK(length(trim(currency)) > 0),
            stripe_session_id TEXT UNIQUE,
            stripe_payment_intent_id TEXT,
            idempotency_key TEXT NOT NULL UNIQUE CHECK(length(trim(idempotency_key)) > 0),
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
            updated_at TEXT,
            FOREIGN KEY (order_id) REFERENCES orders (id) ON DELETE RESTRICT
        );
        """
    )
    idx_cursor = conn.execute("PRAGMA index_list('payments');")
    existing_indexes = {row[1] for row in idx_cursor.fetchall()}
    if "idx_payments_order_id" not in existing_indexes:
        conn.execute("CREATE INDEX IF NOT EXISTS idx_payments_order_id ON payments (order_id);")
    if "idx_payments_stripe_session_id" not in existing_indexes:
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_payments_stripe_session_id ON payments (stripe_session_id);"
        )
    if "idx_payments_idempotency_key" not in existing_indexes:
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_payments_idempotency_key ON payments (idempotency_key);"
        )


def _migrate_stripe_webhook_events_table_if_needed(conn: sqlite3.Connection) -> None:
    """Safely and idempotently ensure the stripe_webhook_events table and indexes exist."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS stripe_webhook_events (
            id INTEGER PRIMARY KEY,
            event_id TEXT NOT NULL UNIQUE CHECK(length(trim(event_id)) > 0),
            event_type TEXT NOT NULL CHECK(length(trim(event_type)) > 0),
            livemode INTEGER NOT NULL DEFAULT 0 CHECK(livemode IN (0, 1)),
            status TEXT NOT NULL DEFAULT 'processing' CHECK(status IN ('processing', 'processed', 'failed', 'ignored')),
            order_id INTEGER,
            payment_id INTEGER,
            payload_summary TEXT,
            error_message TEXT,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
            processed_at TEXT,
            FOREIGN KEY (order_id) REFERENCES orders (id) ON DELETE SET NULL,
            FOREIGN KEY (payment_id) REFERENCES payments (id) ON DELETE SET NULL
        );
        """
    )
    idx_cursor = conn.execute("PRAGMA index_list('stripe_webhook_events');")
    existing_indexes = {row[1] for row in idx_cursor.fetchall()}
    if "idx_stripe_webhook_events_event_id" not in existing_indexes:
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_stripe_webhook_events_event_id ON stripe_webhook_events (event_id);"
        )
    if "idx_stripe_webhook_events_order_id" not in existing_indexes:
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_stripe_webhook_events_order_id ON stripe_webhook_events (order_id);"
        )
    if "idx_stripe_webhook_events_status" not in existing_indexes:
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_stripe_webhook_events_status ON stripe_webhook_events (status);"
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
    _migrate_password_reset_tokens_table_if_needed(conn)
    _migrate_contact_messages_table_if_needed(conn)
    _migrate_account_link_intents_table_if_needed(conn)
    _migrate_payments_table_if_needed(conn)
    _migrate_stripe_webhook_events_table_if_needed(conn)
    conn.commit()


@click.command("init-db")
@with_appcontext
def init_db_command() -> None:
    """Flask CLI command to initialize database schema from schema.sql."""
    init_db()
    click.echo("Initialized SQLite database schema from schema.sql.")


def init_app(app: Flask) -> None:
    """Register database teardown hooks, CLI commands, and run startup migrations.

    Ensures connections are closed when requests end, registers the 'init-db'
    CLI command, and applies idempotent database migrations on application startup.
    Auto-migration is bypassed when TESTING=True so automated test cases retain
    full control over isolated test databases.

    Args:
        app: The Flask application instance.
    """
    app.teardown_appcontext(close_db)
    app.cli.add_command(init_db_command)

    if not app.config.get("TESTING"):
        with app.app_context():
            init_db()


if __name__ == "__main__":
    from app import create_app

    app = create_app()
    with app.app_context():
        init_db()
        print(f"Database successfully initialized at: {app.config['DATABASE_PATH']}")

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


@contextmanager
def transaction(
    conn: Optional[sqlite3.Connection] = None,
) -> Generator[sqlite3.Connection, None, None]:
    """Context manager for explicit transaction handling.

    Executes operations within an SQLite transaction. Automatically commits changes
    when exiting cleanly, or issues a rollback if an unhandled exception occurs.

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

    with conn:
        yield conn


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

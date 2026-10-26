"""Unit and integration tests for database schema, connection management, and initialization."""
from datetime import datetime, timezone
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest

from flask import Flask

from app import create_app
from app.config import Config
from app.database.connection import (
    close_db,
    create_connection,
    get_db,
    init_db,
    transaction,
)


class TestConfig(Config):
    """Configuration for testing with an in-memory or temporary database."""

    TESTING = True
    DEBUG = False


class TestDatabaseSchemaAndConstraints(unittest.TestCase):
    """Tests covering schema definition, constraints, foreign keys, and indexes."""

    def setUp(self):
        self.conn = create_connection(":memory:")
        init_db(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_tables_created(self):
        cursor = self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name;"
        )
        table_names = {row["name"] for row in cursor.fetchall()}
        expected_tables = {
            "categories",
            "products",
            "customers",
            "orders",
            "order_items",
            "users",
            "email_verification_tokens",
        }
        self.assertTrue(expected_tables.issubset(table_names))
        # Ensure no cart tables exist in database
        self.assertNotIn("carts", table_names)
        self.assertNotIn("cart_items", table_names)

    def test_indexes_created(self):
        cursor = self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' ORDER BY name;"
        )
        index_names = {row["name"] for row in cursor.fetchall()}
        self.assertIn("idx_products_category_id", index_names)
        self.assertIn("idx_orders_customer_id", index_names)
        self.assertIn("idx_order_items_order_id", index_names)
        self.assertIn("idx_order_items_product_id", index_names)
        self.assertIn("idx_orders_user_id", index_names)
        self.assertIn("idx_customers_user_id", index_names)
        self.assertIn("idx_verification_tokens_user_id", index_names)

    def test_user_constraints(self):
        # Successful insert of customer user
        cursor = self.conn.execute(
            "INSERT INTO users (email, role, is_verified) VALUES (?, ?, ?)",
            ("client@example.com", "customer", 0),
        )
        self.assertIsInstance(cursor.lastrowid, int)

        # Successful insert of owner user
        cursor = self.conn.execute(
            "INSERT INTO users (email, role, is_verified) VALUES (?, ?, ?)",
            ("owner@example.com", "owner", 1),
        )
        self.assertIsInstance(cursor.lastrowid, int)

        # Case-insensitive duplicate email rejected (COLLATE NOCASE)
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "INSERT INTO users (email, role) VALUES (?, ?)",
                ("CLIENT@EXAMPLE.COM", "customer"),
            )

        # Empty email rejected by CHECK constraint
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "INSERT INTO users (email) VALUES (?)",
                ("   ",),
            )

        # Email without @ rejected by CHECK constraint
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "INSERT INTO users (email) VALUES (?)",
                ("no-at-sign",),
            )

        # Invalid role rejected by CHECK constraint
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "INSERT INTO users (email, role) VALUES (?, ?)",
                ("hacker@example.com", "admin"),
            )

        # Invalid is_verified rejected by CHECK constraint
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "INSERT INTO users (email, is_verified) VALUES (?, ?)",
                ("test@example.com", 2),
            )

        # Successful insert with valid password_hash
        cursor = self.conn.execute(
            "INSERT INTO users (email, password_hash) VALUES (?, ?)",
            ("pwd_user@example.com", "scrypt:32768:8:1$salt$hash"),
        )
        self.assertIsInstance(cursor.lastrowid, int)

        # Empty/whitespace password_hash rejected by CHECK constraint
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "INSERT INTO users (email, password_hash) VALUES (?, ?)",
                ("bad_pwd@example.com", "   "),
            )

    def test_verification_tokens_constraints_and_cascade_delete(self):
        # Insert parent user
        user_cursor = self.conn.execute(
            "INSERT INTO users (email) VALUES ('token_parent@example.com')"
        )
        user_id = user_cursor.lastrowid

        # Insert verification token
        token_cursor = self.conn.execute(
            """
            INSERT INTO email_verification_tokens (user_id, token_hash, expires_at)
            VALUES (?, ?, ?)
            """,
            (user_id, "hash_12345", "2026-10-27T12:00:00Z"),
        )
        token_id = token_cursor.lastrowid
        self.assertIsInstance(token_id, int)

        # Duplicate token_hash rejected by UNIQUE constraint
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                """
                INSERT INTO email_verification_tokens (user_id, token_hash, expires_at)
                VALUES (?, ?, ?)
                """,
                (user_id, "hash_12345", "2026-10-27T12:00:00Z"),
            )

        # Non-existent user_id rejected by foreign key constraint
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                """
                INSERT INTO email_verification_tokens (user_id, token_hash, expires_at)
                VALUES (?, ?, ?)
                """,
                (999999, "other_hash", "2026-10-27T12:00:00Z"),
            )

        # Deleting parent user cascades to delete verification token
        self.conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
        token_row = self.conn.execute(
            "SELECT id FROM email_verification_tokens WHERE id = ?",
            (token_id,),
        ).fetchone()
        self.assertIsNone(token_row)

    def test_migrate_users_table_if_needed(self):
        from app.database.connection import _migrate_users_table_if_needed

        # Create isolated connection with legacy schema (users table without password_hash)
        legacy_conn = create_connection(":memory:")
        legacy_conn.execute(
            """
            CREATE TABLE users (
                id INTEGER PRIMARY KEY,
                email TEXT NOT NULL UNIQUE COLLATE NOCASE,
                role TEXT NOT NULL DEFAULT 'customer',
                is_verified INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT '2026-10-26T00:00:00Z'
            );
            """
        )
        legacy_conn.execute(
            "INSERT INTO users (email, role) VALUES ('legacy@example.com', 'customer');"
        )
        legacy_conn.commit()

        # Run migration helper
        _migrate_users_table_if_needed(legacy_conn)

        # Verify password_hash column is now present
        info_cursor = legacy_conn.execute("PRAGMA table_info(users);")
        cols = {row["name"] for row in info_cursor.fetchall()}
        self.assertIn("password_hash", cols)

        # Verify existing row was preserved intact with NULL password_hash
        legacy_user = legacy_conn.execute(
            "SELECT email, password_hash, role FROM users WHERE email = 'legacy@example.com'"
        ).fetchone()
        self.assertEqual(legacy_user["email"], "legacy@example.com")
        self.assertIsNone(legacy_user["password_hash"])

        # Verify new row can be added with password_hash
        legacy_conn.execute(
            "INSERT INTO users (email, password_hash) VALUES ('migrated@example.com', 'scrypt:test')"
        )
        migrated_user = legacy_conn.execute(
            "SELECT email, password_hash FROM users WHERE email = 'migrated@example.com'"
        ).fetchone()
        self.assertEqual(migrated_user["password_hash"], "scrypt:test")

        # Idempotence: running migration again does nothing
        _migrate_users_table_if_needed(legacy_conn)
        legacy_conn.close()

    def test_category_constraints(self):
        # Successful insert
        cursor = self.conn.execute(
            "INSERT INTO categories (name, description) VALUES (?, ?)",
            ("Produce", "Fresh fruit and vegetables"),
        )
        cat_id = cursor.lastrowid
        self.assertIsInstance(cat_id, int)

        # Empty/whitespace name rejected by CHECK constraint
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("INSERT INTO categories (name) VALUES ('   ')")

        # Unique name constraint
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("INSERT INTO categories (name) VALUES ('Produce')")

    def test_product_constraints(self):
        # Valid product insert
        self.conn.execute(
            "INSERT INTO products (name, price, stock_quantity, is_active) VALUES (?, ?, ?, ?)",
            ("Organic Apple", 199, 50, 1),
        )

        # Negative price rejected
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "INSERT INTO products (name, price) VALUES (?, ?)",
                ("Bad Product", -10),
            )

        # Negative stock rejected
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "INSERT INTO products (name, price, stock_quantity) VALUES (?, ?, ?)",
                ("Bad Product", 100, -1),
            )

        # Empty name rejected
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "INSERT INTO products (name, price) VALUES (?, ?)",
                ("   ", 100),
            )

        # Invalid boolean for is_active rejected
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "INSERT INTO products (name, price, is_active) VALUES (?, ?, ?)",
                ("Invalid Active", 100, 2),
            )

    def test_customer_constraints(self):
        # Valid customer
        self.conn.execute(
            "INSERT INTO customers (name, email) VALUES (?, ?)",
            ("Jane Doe", "jane@example.com"),
        )

        # Duplicate email rejected
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "INSERT INTO customers (name, email) VALUES (?, ?)",
                ("Jane Other", "jane@example.com"),
            )

        # Email missing '@' rejected
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "INSERT INTO customers (name, email) VALUES (?, ?)",
                ("Jane", "invalid-email-no-at"),
            )

        # Empty customer name rejected
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "INSERT INTO customers (name, email) VALUES (?, ?)",
                ("  ", "valid@example.com"),
            )

    def test_order_constraints_and_timestamp(self):
        self.conn.execute(
            "INSERT INTO customers (name, email) VALUES (?, ?)",
            ("Customer One", "c1@example.com"),
        )
        cust_id = self.conn.execute("SELECT id FROM customers WHERE email = 'c1@example.com'").fetchone()["id"]

        # Default order creation has status 'pending' and ISO 8601 UTC timestamp
        self.conn.execute("INSERT INTO orders (customer_id) VALUES (?)", (cust_id,))
        order_row = self.conn.execute("SELECT * FROM orders WHERE customer_id = ?", (cust_id,)).fetchone()
        self.assertEqual(order_row["status"], "pending")

        created_at_str = order_row["created_at"]
        self.assertTrue(created_at_str.endswith("Z"))
        parsed_dt = datetime.fromisoformat(created_at_str)
        self.assertIsNotNone(parsed_dt.tzinfo)

        # Valid order statuses
        valid_statuses = ["pending", "confirmed", "paid", "shipped", "delivered", "cancelled"]
        for st in valid_statuses:
            self.conn.execute(
                "INSERT INTO orders (customer_id, status) VALUES (?, ?)",
                (cust_id, st),
            )

        # Invalid order status rejected
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "INSERT INTO orders (customer_id, status) VALUES (?, ?)",
                (cust_id, "unknown_status"),
            )

    def test_order_snapshot_columns_and_idempotent_migration(self):
        # Verify snapshot and user_id columns exist in initialized schema
        cursor = self.conn.execute("PRAGMA table_info(orders);")
        col_names = {row["name"] for row in cursor.fetchall()}
        self.assertTrue(
            {"customer_name", "customer_email", "customer_phone", "customer_address", "user_id"}.issubset(
                col_names
            )
        )

        cust_cursor = self.conn.execute("PRAGMA table_info(customers);")
        cust_cols = {row["name"] for row in cust_cursor.fetchall()}
        self.assertIn("user_id", cust_cols)

        # Test idempotent migration on a connection simulating older tables without snapshot or user_id columns
        old_conn = create_connection(":memory:")
        old_conn.execute(
            """CREATE TABLE customers (
                   id INTEGER PRIMARY KEY,
                   name TEXT NOT NULL,
                   email TEXT NOT NULL UNIQUE,
                   phone TEXT,
                   address TEXT
               );"""
        )
        old_conn.execute(
            """CREATE TABLE orders (
                   id INTEGER PRIMARY KEY,
                   customer_id INTEGER NOT NULL,
                   status TEXT NOT NULL DEFAULT 'pending',
                   created_at TEXT NOT NULL DEFAULT '2026-01-01T00:00:00Z'
               );"""
        )
        # Pre-populate legacy data to verify rows are preserved
        old_conn.execute("INSERT INTO customers (id, name, email) VALUES (1, 'Legacy Customer', 'legacy@example.com')")
        old_conn.execute("INSERT INTO orders (id, customer_id, status) VALUES (10, 1, 'confirmed')")

        # First run: performs migration
        init_db(old_conn)

        migrated_order_cols = {row["name"] for row in old_conn.execute("PRAGMA table_info(orders);").fetchall()}
        self.assertTrue(
            {"customer_name", "customer_email", "customer_phone", "customer_address", "user_id"}.issubset(
                migrated_order_cols
            )
        )

        migrated_cust_cols = {row["name"] for row in old_conn.execute("PRAGMA table_info(customers);").fetchall()}
        self.assertIn("user_id", migrated_cust_cols)

        # Verify legacy data preserved
        order_row = old_conn.execute("SELECT * FROM orders WHERE id = 10").fetchone()
        self.assertEqual(order_row["customer_id"], 1)
        self.assertEqual(order_row["status"], "confirmed")
        self.assertIsNone(order_row["user_id"])

        cust_row = old_conn.execute("SELECT * FROM customers WHERE id = 1").fetchone()
        self.assertEqual(cust_row["name"], "Legacy Customer")
        self.assertIsNone(cust_row["user_id"])

        # Second run: must be safe and idempotent
        init_db(old_conn)

        # Verify foreign keys are enforced on migrated database
        with self.assertRaises(sqlite3.IntegrityError):
            old_conn.execute("INSERT INTO orders (id, customer_id, user_id) VALUES (11, 1, 99999)")

        old_conn.close()

    def test_order_item_constraints_and_price_preservation(self):
        # Setup customer, product, order
        self.conn.execute("INSERT INTO customers (name, email) VALUES (?, ?)", ("Buyer", "buyer@example.com"))
        cust_id = self.conn.execute("SELECT id FROM customers").fetchone()["id"]

        self.conn.execute(
            "INSERT INTO products (name, price, stock_quantity) VALUES (?, ?, ?)",
            ("Olive Oil", 1250, 10),
        )
        prod_id = self.conn.execute("SELECT id FROM products").fetchone()["id"]

        self.conn.execute("INSERT INTO orders (customer_id) VALUES (?)", (cust_id,))
        order_id = self.conn.execute("SELECT id FROM orders").fetchone()["id"]

        # Insert valid order_item capturing snapshot price
        self.conn.execute(
            """INSERT INTO order_items (order_id, product_id, product_name, unit_price, quantity)
               VALUES (?, ?, ?, ?, ?)""",
            (order_id, prod_id, "Olive Oil", 1250, 2),
        )
        item_row = self.conn.execute("SELECT * FROM order_items WHERE order_id = ?", (order_id,)).fetchone()
        self.assertEqual(item_row["unit_price"], 1250)
        self.assertEqual(item_row["quantity"], 2)

        # Negative unit price rejected
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                """INSERT INTO order_items (order_id, product_id, product_name, unit_price, quantity)
                   VALUES (?, ?, ?, ?, ?)""",
                (order_id, prod_id, "Olive Oil", -1, 1),
            )

        # Zero or negative quantity rejected
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                """INSERT INTO order_items (order_id, product_id, product_name, unit_price, quantity)
                   VALUES (?, ?, ?, ?, ?)""",
                (order_id, prod_id, "Olive Oil", 1250, 0),
            )

    def test_foreign_keys_category_on_delete_set_null(self):
        # Create category and product
        self.conn.execute("INSERT INTO categories (name) VALUES ('Pantry')")
        cat_id = self.conn.execute("SELECT id FROM categories WHERE name = 'Pantry'").fetchone()["id"]

        self.conn.execute(
            "INSERT INTO products (name, price, category_id) VALUES (?, ?, ?)",
            ("Rice", 499, cat_id),
        )
        prod = self.conn.execute("SELECT * FROM products WHERE name = 'Rice'").fetchone()
        self.assertEqual(prod["category_id"], cat_id)

        # Deleting category sets product.category_id to NULL
        self.conn.execute("DELETE FROM categories WHERE id = ?", (cat_id,))
        prod_after = self.conn.execute("SELECT * FROM products WHERE name = 'Rice'").fetchone()
        self.assertIsNone(prod_after["category_id"])

    def test_foreign_keys_customer_on_delete_restrict(self):
        self.conn.execute("INSERT INTO customers (name, email) VALUES ('Cust', 'cust@example.com')")
        cust_id = self.conn.execute("SELECT id FROM customers").fetchone()["id"]
        self.conn.execute("INSERT INTO orders (customer_id) VALUES (?)", (cust_id,))

        # Deleting customer with orders is restricted
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("DELETE FROM customers WHERE id = ?", (cust_id,))

    def test_foreign_keys_order_on_delete_cascade(self):
        self.conn.execute("INSERT INTO customers (name, email) VALUES ('Cust', 'cust@example.com')")
        cust_id = self.conn.execute("SELECT id FROM customers").fetchone()["id"]
        self.conn.execute("INSERT INTO products (name, price) VALUES ('Bread', 300)")
        prod_id = self.conn.execute("SELECT id FROM products").fetchone()["id"]

        self.conn.execute("INSERT INTO orders (customer_id) VALUES (?)", (cust_id,))
        order_id = self.conn.execute("SELECT id FROM orders").fetchone()["id"]
        self.conn.execute(
            """INSERT INTO order_items (order_id, product_id, product_name, unit_price, quantity)
               VALUES (?, ?, 'Bread', 300, 1)""",
            (order_id, prod_id),
        )

        # Deleting order cascades to order_items
        self.conn.execute("DELETE FROM orders WHERE id = ?", (order_id,))
        items_count = self.conn.execute("SELECT COUNT(*) as c FROM order_items WHERE order_id = ?", (order_id,)).fetchone()["c"]
        self.assertEqual(items_count, 0)

    def test_foreign_keys_product_on_delete_restrict(self):
        self.conn.execute("INSERT INTO customers (name, email) VALUES ('Cust', 'cust@example.com')")
        cust_id = self.conn.execute("SELECT id FROM customers").fetchone()["id"]
        self.conn.execute("INSERT INTO products (name, price) VALUES ('Milk', 250)")
        prod_id = self.conn.execute("SELECT id FROM products").fetchone()["id"]

        self.conn.execute("INSERT INTO orders (customer_id) VALUES (?)", (cust_id,))
        order_id = self.conn.execute("SELECT id FROM orders").fetchone()["id"]
        self.conn.execute(
            """INSERT INTO order_items (order_id, product_id, product_name, unit_price, quantity)
               VALUES (?, ?, 'Milk', 250, 1)""",
            (order_id, prod_id),
        )

        # Deleting product referenced in order_items is restricted
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("DELETE FROM products WHERE id = ?", (prod_id,))

    def test_foreign_keys_user_on_delete_set_null(self):
        # Insert user
        self.conn.execute("INSERT INTO users (email) VALUES ('user@example.com')")
        user_id = self.conn.execute("SELECT id FROM users WHERE email = 'user@example.com'").fetchone()["id"]

        # Insert customer and order referencing this user
        self.conn.execute(
            "INSERT INTO customers (name, email, user_id) VALUES ('User Profile', 'user@example.com', ?)",
            (user_id,),
        )
        cust_id = self.conn.execute("SELECT id FROM customers WHERE email = 'user@example.com'").fetchone()["id"]

        self.conn.execute("INSERT INTO orders (customer_id, user_id) VALUES (?, ?)", (cust_id, user_id))
        order_id = self.conn.execute("SELECT id FROM orders WHERE customer_id = ?", (cust_id,)).fetchone()["id"]

        # Deleting user sets user_id to NULL on both customer and order without deleting the records
        self.conn.execute("DELETE FROM users WHERE id = ?", (user_id,))

        cust_row = self.conn.execute("SELECT user_id FROM customers WHERE id = ?", (cust_id,)).fetchone()
        self.assertIsNone(cust_row["user_id"])

        order_row = self.conn.execute("SELECT user_id FROM orders WHERE id = ?", (order_id,)).fetchone()
        self.assertIsNone(order_row["user_id"])

        # Foreign key restriction on non-existent user_id
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("INSERT INTO orders (customer_id, user_id) VALUES (?, ?)", (cust_id, 99999))
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "INSERT INTO customers (name, email, user_id) VALUES ('Fake', 'fake@example.com', ?)",
                (99999,),
            )


class TestConnectionAndLifecycle(unittest.TestCase):
    """Tests for connection management, transaction boundaries, and Flask integration."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "nested" / "test_shop.sqlite3"

        class AppConfig(Config):
            DATABASE_PATH = str(self.db_path)
            TESTING = True

        self.app = create_app(AppConfig)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_create_connection_ensures_directory_and_enables_foreign_keys(self):
        self.assertFalse(self.db_path.parent.exists())
        conn = create_connection(self.db_path)
        self.assertTrue(self.db_path.parent.exists())

        # Verify foreign keys pragma is enabled
        fk_status = conn.execute("PRAGMA foreign_keys;").fetchone()[0]
        self.assertEqual(fk_status, 1)

        # Verify row_factory returns sqlite3.Row
        conn.execute("CREATE TABLE t (id INT, val TEXT);")
        conn.execute("INSERT INTO t VALUES (1, 'hello');")
        row = conn.execute("SELECT * FROM t;").fetchone()
        self.assertIsInstance(row, sqlite3.Row)
        self.assertEqual(row["val"], "hello")
        self.assertEqual(row[1], "hello")
        conn.close()

    def test_get_db_within_app_context(self):
        with self.app.app_context():
            db1 = get_db()
            db2 = get_db()
            # Must return the same connection within the same application context
            self.assertIs(db1, db2)

        # Outside application context, get_db raises RuntimeError
        with self.assertRaises(RuntimeError):
            get_db()

    def test_close_db_on_app_context_teardown(self):
        with self.app.app_context():
            db = get_db()
            self.assertIsNotNone(db)

        # Connection should be closed after app context teardown
        # Calling an operation on a closed connection raises ProgrammingError
        with self.assertRaises(sqlite3.ProgrammingError):
            db.execute("SELECT 1;")

    def test_transaction_commit_and_rollback(self):
        with self.app.app_context():
            init_db()

            # Successful transaction commits
            with transaction() as conn:
                conn.execute("INSERT INTO categories (name) VALUES ('Bakery')")

            row = get_db().execute("SELECT name FROM categories WHERE name = 'Bakery'").fetchone()
            self.assertIsNotNone(row)

            # Failing transaction rolls back
            try:
                with transaction() as conn:
                    conn.execute("INSERT INTO categories (name) VALUES ('Beverages')")
                    raise ValueError("Simulated operational failure")
            except ValueError:
                pass

            row = get_db().execute("SELECT name FROM categories WHERE name = 'Beverages'").fetchone()
            self.assertIsNone(row)

    def test_init_db_idempotence_and_preserves_data(self):
        with self.app.app_context():
            init_db()
            db = get_db()
            db.execute("INSERT INTO categories (name) VALUES ('Snacks')")
            db.commit()

            # Second initialization should succeed without error and preserve rows
            init_db()
            row = db.execute("SELECT name FROM categories WHERE name = 'Snacks'").fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row["name"], "Snacks")

    def test_cli_init_db_command(self):
        runner = self.app.test_cli_runner()
        result = runner.invoke(args=["init-db"])
        self.assertEqual(result.exit_code, 0)
        self.assertIn("Initialized SQLite database schema", result.output)

        with self.app.app_context():
            cursor = get_db().execute("SELECT name FROM sqlite_master WHERE type='table';")
            tables = {row["name"] for row in cursor.fetchall()}
            self.assertIn("products", tables)

    def test_transaction_savepoint_creation_failure_does_not_corrupt_state(self):
        closed_conn = create_connection(":memory:")
        closed_conn.close()
        closed_id = id(closed_conn)

        # Attempting transaction on a closed connection fails at SAVEPOINT execution
        with self.assertRaises(sqlite3.ProgrammingError):
            with transaction(closed_conn):
                pass

        # Verify transaction depth for closed_conn was cleaned up and not left corrupted
        from app.database.connection import _transaction_depths
        self.assertNotIn(closed_id, _transaction_depths)

        # Subsequent transaction on a valid connection works normally
        valid_conn = create_connection(":memory:")
        init_db(valid_conn)
        with transaction(valid_conn):
            valid_conn.execute("INSERT INTO categories (name) VALUES ('Clean')")

        row = valid_conn.execute("SELECT name FROM categories WHERE name = 'Clean'").fetchone()
        self.assertIsNotNone(row)
        valid_conn.close()


if __name__ == "__main__":
    unittest.main()

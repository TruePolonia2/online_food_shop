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
        expected_tables = {"categories", "products", "customers", "orders", "order_items"}
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

"""Automated test suite for the 'seed-demo' Flask CLI command."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from app import create_app
from app.config import Config, ProductionConfig
from app.database.connection import get_db, init_db
from app.models.category import Category
from app.models.customer import Customer
from app.models.order import Order
from app.models.product import Product
from app.repositories.category_repository import CategoryRepository
from app.repositories.customer_repository import CustomerRepository
from app.repositories.order_repository import OrderRepository
from app.repositories.product_repository import ProductRepository


class TestSeedDemoCLI(unittest.TestCase):
    """Tests verifying the safety, idempotency, and transactional behavior of seed-demo."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_seed_demo.sqlite3"

        class TestSeedDemoConfig(Config):
            DATABASE_PATH = str(self.db_path)
            TESTING = True
            SECRET_KEY = "test-seed-demo-secret-key"
            CSRF_ENABLED = False

        self.app = create_app(TestSeedDemoConfig)
        self.runner = self.app.test_cli_runner()

        with self.app.app_context():
            init_db()
            self.category_repo = CategoryRepository()
            self.product_repo = ProductRepository()
            self.customer_repo = CustomerRepository()
            self.order_repo = OrderRepository()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()
        # Clean up any potential environment variables
        os.environ.pop("DEMO_MODE", None)
        os.environ.pop("ALLOW_DEMO_SEED", None)

    def test_seed_demo_first_run_success(self) -> None:
        """First execution seeds 4 categories and 8 products with proper attributes."""
        result = self.runner.invoke(args=["seed-demo"])
        self.assertEqual(result.exit_code, 0)
        self.assertIn("Demo catalog seeding completed successfully", result.output)
        self.assertIn("Categories: 4 created, 0 skipped", result.output)
        self.assertIn("Products:   8 created, 0 skipped", result.output)

        with self.app.app_context():
            categories = self.category_repo.get_all()
            self.assertEqual(len(categories), 4)
            cat_names = {c.name for c in categories}
            expected_cats = {
                "Fresh Bakery",
                "Organic Produce",
                "Artisan Dairy",
                "Pantry & Olive Oils",
            }
            self.assertEqual(cat_names, expected_cats)

            products = self.product_repo.get_all()
            self.assertEqual(len(products), 8)

            expected_products = {
                "Artisan Sourdough Loaf": (350, 15, "Fresh Bakery"),
                "Butter Croissant 4-Pack": (420, 20, "Fresh Bakery"),
                "Organic Hass Avocados (Pack of 3)": (299, 25, "Organic Produce"),
                "Sweet Cherry Tomatoes 500g": (210, 30, "Organic Produce"),
                "Farm Fresh Whole Milk 1L": (125, 40, "Artisan Dairy"),
                "Artisan Sheep Cheese 250g": (580, 12, "Artisan Dairy"),
                "Extra Virgin Olive Oil 500ml": (750, 18, "Pantry & Olive Oils"),
                "Organic Raw Honey 350g": (620, 15, "Pantry & Olive Oils"),
            }

            for p in products:
                self.assertIn(p.name, expected_products)
                exp_price, exp_stock, exp_cat_name = expected_products[p.name]
                # Price must be exact integer euro cents
                self.assertEqual(p.price, exp_price)
                self.assertIsInstance(p.price, int)
                # Stock must be positive integer
                self.assertEqual(p.stock_quantity, exp_stock)
                self.assertGreater(p.stock_quantity, 0)
                # Must be active with non-empty description
                self.assertTrue(p.is_active)
                self.assertTrue(p.description and len(p.description) > 10)
                # Category relation must match
                cat = self.category_repo.get_by_id(p.category_id)
                self.assertIsNotNone(cat)
                self.assertEqual(cat.name, exp_cat_name)
                # External image dependency must be None
                self.assertIsNone(p.image_url)

            # Verification of zero fabricated orders
            db = get_db()
            row = db.execute("SELECT COUNT(*) AS count FROM orders").fetchone()
            self.assertEqual(row["count"], 0)

    def test_seed_demo_idempotency_second_run(self) -> None:
        """Executing seed-demo repeatedly produces no duplicates and skips existing items."""
        first_result = self.runner.invoke(args=["seed-demo"])
        self.assertEqual(first_result.exit_code, 0)

        second_result = self.runner.invoke(args=["seed-demo"])
        self.assertEqual(second_result.exit_code, 0)
        self.assertIn("Categories: 0 created, 4 skipped", second_result.output)
        self.assertIn("Products:   0 created, 8 skipped", second_result.output)

        with self.app.app_context():
            categories = self.category_repo.get_all()
            self.assertEqual(len(categories), 4)

            products = self.product_repo.get_all()
            self.assertEqual(len(products), 8)

    def test_seed_demo_non_destructive_preserves_custom_data(self) -> None:
        """Custom pre-existing categories and products are preserved untouched."""
        with self.app.app_context():
            custom_cat = self.category_repo.create(
                Category(name="Special Artisanal Craft", description="Custom category", is_active=True)
            )
            custom_prod = self.product_repo.create(
                Product(
                    name="Handmade Chocolate Truffles",
                    price=999,
                    stock_quantity=10,
                    description="Custom chocolates",
                    category_id=custom_cat.id,
                    is_active=True,
                )
            )

        result = self.runner.invoke(args=["seed-demo"])
        self.assertEqual(result.exit_code, 0)

        with self.app.app_context():
            # 1 custom + 4 demo = 5 categories
            categories = self.category_repo.get_all()
            self.assertEqual(len(categories), 5)
            fetched_cat = self.category_repo.get_by_id(custom_cat.id)
            self.assertIsNotNone(fetched_cat)
            self.assertEqual(fetched_cat.name, "Special Artisanal Craft")

            # 1 custom + 8 demo = 9 products
            products = self.product_repo.get_all()
            self.assertEqual(len(products), 9)
            fetched_prod = self.product_repo.get_by_id(custom_prod.id)
            self.assertIsNotNone(fetched_prod)
            self.assertEqual(fetched_prod.name, "Handmade Chocolate Truffles")
            self.assertEqual(fetched_prod.price, 999)
            self.assertEqual(fetched_prod.stock_quantity, 10)

    def test_seed_demo_refuses_in_production_environment(self) -> None:
        """Command strictly refuses execution in production environments."""
        prod_db_path = Path(self.temp_dir.name) / "prod_test.sqlite3"

        class ProdTestConfig(ProductionConfig):
            DATABASE_PATH = str(prod_db_path)
            TESTING = False
            SECRET_KEY = "a" * 64

        prod_app = create_app(ProdTestConfig)
        with prod_app.app_context():
            init_db()

        prod_runner = prod_app.test_cli_runner()
        result = prod_runner.invoke(args=["seed-demo"])

        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("strictly prohibited in production", result.output)

        with prod_app.app_context():
            cats = CategoryRepository().get_all()
            self.assertEqual(len(cats), 0)

    def test_seed_demo_refuses_in_production_even_with_force(self) -> None:
        """Command strictly refuses execution in production even if --force is passed."""
        prod_db_path = Path(self.temp_dir.name) / "prod_test_force.sqlite3"

        class ProdTestConfig(ProductionConfig):
            DATABASE_PATH = str(prod_db_path)
            TESTING = False
            SECRET_KEY = "b" * 64

        prod_app = create_app(ProdTestConfig)
        with prod_app.app_context():
            init_db()

        prod_runner = prod_app.test_cli_runner()
        result = prod_runner.invoke(args=["seed-demo", "--force"])

        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("strictly prohibited in production", result.output)

    def test_seed_demo_refuses_when_database_contains_orders_without_force(self) -> None:
        """Command refuses to seed if active database already contains orders."""
        with self.app.app_context():
            cust = self.customer_repo.create(Customer(name="John Doe", email="john@example.com"))
            self.order_repo.create(Order(customer_id=cust.id, status="pending"))

        result = self.runner.invoke(args=["seed-demo"])
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("Refusing to seed: the active database contains existing order records", result.output)

        with self.app.app_context():
            # No categories or products should have been created
            self.assertEqual(len(self.category_repo.get_all()), 0)
            self.assertEqual(len(self.product_repo.get_all()), 0)

    def test_seed_demo_allows_when_orders_exist_with_force(self) -> None:
        """Passing --force allows seeding on a non-production database with orders."""
        with self.app.app_context():
            cust = self.customer_repo.create(Customer(name="Jane Doe", email="jane@example.com"))
            order = self.order_repo.create(Order(customer_id=cust.id, status="confirmed"))

        result = self.runner.invoke(args=["seed-demo", "--force"])
        self.assertEqual(result.exit_code, 0)
        self.assertIn("Categories: 4 created", result.output)
        self.assertIn("Products:   8 created", result.output)

        with self.app.app_context():
            # Existing order is still intact
            fetched_order = self.order_repo.get_by_id(order.id)
            self.assertIsNotNone(fetched_order)
            self.assertEqual(fetched_order.status, "confirmed")

    def test_seed_demo_allows_when_orders_exist_with_demo_mode_env(self) -> None:
        """Setting DEMO_MODE=1 allows seeding on a database with orders."""
        with self.app.app_context():
            cust = self.customer_repo.create(Customer(name="Env Customer", email="env@example.com"))
            self.order_repo.create(Order(customer_id=cust.id, status="pending"))

        with patch.dict(os.environ, {"DEMO_MODE": "1"}):
            result = self.runner.invoke(args=["seed-demo"])
            self.assertEqual(result.exit_code, 0)
            self.assertIn("Categories: 4 created", result.output)

    def test_seed_demo_allows_when_orders_exist_with_allow_demo_seed_env(self) -> None:
        """Setting ALLOW_DEMO_SEED=1 allows seeding on a database with orders."""
        with self.app.app_context():
            cust = self.customer_repo.create(Customer(name="Env Customer 2", email="env2@example.com"))
            self.order_repo.create(Order(customer_id=cust.id, status="pending"))

        with patch.dict(os.environ, {"ALLOW_DEMO_SEED": "1"}):
            result = self.runner.invoke(args=["seed-demo"])
            self.assertEqual(result.exit_code, 0)
            self.assertIn("Categories: 4 created", result.output)

    def test_seed_demo_transaction_rollback_on_failure(self) -> None:
        """Transaction failure during product creation rolls back partial insertions."""
        with patch.object(ProductRepository, "create", side_effect=RuntimeError("Simulated write failure")):
            result = self.runner.invoke(args=["seed-demo"])
            self.assertNotEqual(result.exit_code, 0)
            self.assertIn("Database failure during demo seeding", result.output)

        with self.app.app_context():
            # Categories should have been rolled back
            categories = self.category_repo.get_all()
            self.assertEqual(len(categories), 0)
            products = self.product_repo.get_all()
            self.assertEqual(len(products), 0)

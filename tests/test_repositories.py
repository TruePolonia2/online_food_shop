"""Focused tests for repository layer persistence, transactions, and foreign-key behaviors."""
from datetime import datetime, timezone
import sqlite3
import unittest

from app import create_app
from app.config import Config
from app.database.connection import create_connection, init_db
from app.models.category import Category
from app.models.customer import Customer
from app.models.order import Order, OrderStatus
from app.models.order_item import OrderItem
from app.models.product import Product
from app.repositories.category_repository import CategoryRepository
from app.repositories.customer_repository import CustomerRepository
from app.repositories.order_repository import OrderRepository
from app.repositories.product_repository import ProductRepository


class BaseRepositoryTestCase(unittest.TestCase):
    """Base test case providing an isolated in-memory SQLite database for each test."""

    def setUp(self) -> None:
        self.conn = create_connection(":memory:")
        init_db(self.conn)
        self.category_repo = CategoryRepository(self.conn)
        self.product_repo = ProductRepository(self.conn)
        self.customer_repo = CustomerRepository(self.conn)
        self.order_repo = OrderRepository(self.conn)

    def tearDown(self) -> None:
        self.conn.close()


class TestCategoryRepository(BaseRepositoryTestCase):
    """Tests for CategoryRepository operations and foreign key behaviors."""

    def test_create_and_get_by_id(self) -> None:
        cat = Category(name="Produce", description="Fresh fruits and vegetables")
        saved = self.category_repo.create(cat)

        self.assertIsNotNone(saved.id)
        fetched = self.category_repo.get_by_id(saved.id)
        self.assertIsNotNone(fetched)
        self.assertEqual(fetched.id, saved.id)
        self.assertEqual(fetched.name, "Produce")
        self.assertEqual(fetched.description, "Fresh fruits and vegetables")

    def test_get_by_name(self) -> None:
        self.category_repo.create(Category(name="Dairy", description="Milk and cheese"))
        found = self.category_repo.get_by_name("Dairy")
        self.assertIsNotNone(found)
        self.assertEqual(found.name, "Dairy")

        missing = self.category_repo.get_by_name("Nonexistent")
        self.assertIsNone(missing)

    def test_get_by_id_missing_returns_none(self) -> None:
        self.assertIsNone(self.category_repo.get_by_id(9999))

    def test_get_all(self) -> None:
        self.assertEqual(self.category_repo.get_all(), [])

        self.category_repo.create(Category(name="Bakery"))
        self.category_repo.create(Category(name="Beverages"))

        all_cats = self.category_repo.get_all()
        self.assertEqual(len(all_cats), 2)
        self.assertEqual([c.name for c in all_cats], ["Bakery", "Beverages"])

    def test_update(self) -> None:
        cat = self.category_repo.create(Category(name="Snacks", description="Chips"))
        cat.name = "Healthy Snacks"
        cat.description = "Nuts and dried fruit"
        updated = self.category_repo.update(cat)
        self.assertTrue(updated)

        refetched = self.category_repo.get_by_id(cat.id)
        self.assertEqual(refetched.name, "Healthy Snacks")
        self.assertEqual(refetched.description, "Nuts and dried fruit")

        # Updating non-existent ID returns False
        fake_cat = Category(id=9999, name="Ghost")
        self.assertFalse(self.category_repo.update(fake_cat))

    def test_update_none_id_raises_value_error(self) -> None:
        unsaved = Category(name="Unsaved")
        with self.assertRaises(ValueError):
            self.category_repo.update(unsaved)

    def test_delete(self) -> None:
        cat = self.category_repo.create(Category(name="Frozen"))
        deleted = self.category_repo.delete(cat.id)
        self.assertTrue(deleted)
        self.assertIsNone(self.category_repo.get_by_id(cat.id))

        # Deleting already deleted ID returns False
        self.assertFalse(self.category_repo.delete(cat.id))

    def test_delete_sets_product_category_null(self) -> None:
        cat = self.category_repo.create(Category(name="Pantry"))
        prod = self.product_repo.create(
            Product(name="Pasta", price=199, category_id=cat.id)
        )
        self.assertEqual(prod.category_id, cat.id)

        self.category_repo.delete(cat.id)
        refetched_prod = self.product_repo.get_by_id(prod.id)
        self.assertIsNotNone(refetched_prod)
        self.assertIsNone(refetched_prod.category_id)


class TestProductRepository(BaseRepositoryTestCase):
    """Tests for ProductRepository operations, query filters, and constraints."""

    def test_create_and_get_by_id(self) -> None:
        cat = self.category_repo.create(Category(name="Bakery"))
        prod = Product(
            name="Sourdough Loaf",
            price=450,  # $4.50 in cents
            stock_quantity=25,
            description="Artisan crusty bread",
            category_id=cat.id,
            is_active=True,
        )
        saved = self.product_repo.create(prod)
        self.assertIsNotNone(saved.id)

        fetched = self.product_repo.get_by_id(saved.id)
        self.assertIsNotNone(fetched)
        self.assertEqual(fetched.name, "Sourdough Loaf")
        self.assertEqual(fetched.price, 450)
        self.assertEqual(fetched.stock_quantity, 25)
        self.assertEqual(fetched.category_id, cat.id)
        self.assertTrue(fetched.is_active)
        self.assertTrue(fetched.is_in_stock)

    def test_get_by_id_missing_returns_none(self) -> None:
        self.assertIsNone(self.product_repo.get_by_id(9999))

    def test_get_all_and_list_active(self) -> None:
        self.product_repo.create(Product(name="Apple", price=100, is_active=True))
        self.product_repo.create(Product(name="Banana", price=80, is_active=False))
        self.product_repo.create(Product(name="Carrot", price=120, is_active=True))

        active_only = self.product_repo.get_all(include_inactive=False)
        self.assertEqual(len(active_only), 2)
        self.assertEqual([p.name for p in active_only], ["Apple", "Carrot"])

        convenience_active = self.product_repo.list_active()
        self.assertEqual(len(convenience_active), 2)

        all_products = self.product_repo.get_all(include_inactive=True)
        self.assertEqual(len(all_products), 3)
        self.assertEqual([p.name for p in all_products], ["Apple", "Banana", "Carrot"])

    def test_get_by_category_id(self) -> None:
        cat1 = self.category_repo.create(Category(name="Fruits"))
        cat2 = self.category_repo.create(Category(name="Vegetables"))

        self.product_repo.create(Product(name="Apple", price=100, category_id=cat1.id, is_active=True))
        self.product_repo.create(Product(name="Orange", price=150, category_id=cat1.id, is_active=False))
        self.product_repo.create(Product(name="Broccoli", price=200, category_id=cat2.id, is_active=True))

        cat1_active = self.product_repo.get_by_category_id(cat1.id, include_inactive=False)
        self.assertEqual(len(cat1_active), 1)
        self.assertEqual(cat1_active[0].name, "Apple")

        cat1_all = self.product_repo.get_by_category_id(cat1.id, include_inactive=True)
        self.assertEqual(len(cat1_all), 2)

    def test_update(self) -> None:
        prod = self.product_repo.create(Product(name="Milk", price=250, stock_quantity=10))
        prod.name = "Whole Milk"
        prod.price = 280
        prod.stock_quantity = 15
        prod.is_active = False

        updated = self.product_repo.update(prod)
        self.assertTrue(updated)

        refetched = self.product_repo.get_by_id(prod.id)
        self.assertEqual(refetched.name, "Whole Milk")
        self.assertEqual(refetched.price, 280)
        self.assertEqual(refetched.stock_quantity, 15)
        self.assertFalse(refetched.is_active)

    def test_update_none_id_raises_value_error(self) -> None:
        unsaved = Product(name="Unsaved", price=100)
        with self.assertRaises(ValueError):
            self.product_repo.update(unsaved)

    def test_delete(self) -> None:
        prod = self.product_repo.create(Product(name="Eggs", price=399))
        self.assertTrue(self.product_repo.delete(prod.id))
        self.assertIsNone(self.product_repo.get_by_id(prod.id))
        self.assertFalse(self.product_repo.delete(prod.id))

    def test_delete_restricted_by_order_items(self) -> None:
        cust = self.customer_repo.create(Customer(name="Buyer", email="buyer@example.com"))
        prod = self.product_repo.create(Product(name="Coffee", price=1500))

        order = Order(
            customer_id=cust.id,
            items=[OrderItem(product_id=prod.id, product_name=prod.name, unit_price=prod.price, quantity=1)],
        )
        self.order_repo.create(order)

        # Deleting a product that is referenced in an existing order item must be restricted
        with self.assertRaises(sqlite3.IntegrityError):
            self.product_repo.delete(prod.id)


class TestCustomerRepository(BaseRepositoryTestCase):
    """Tests for CustomerRepository operations and constraints."""

    def test_create_and_get_by_id(self) -> None:
        cust = Customer(
            name="Alice Smith",
            email="alice@example.com",
            phone="555-0199",
            address="123 Market St",
        )
        saved = self.customer_repo.create(cust)
        self.assertIsNotNone(saved.id)

        fetched = self.customer_repo.get_by_id(saved.id)
        self.assertIsNotNone(fetched)
        self.assertEqual(fetched.name, "Alice Smith")
        self.assertEqual(fetched.email, "alice@example.com")
        self.assertEqual(fetched.phone, "555-0199")
        self.assertEqual(fetched.address, "123 Market St")

    def test_get_by_email_case_insensitive(self) -> None:
        self.customer_repo.create(Customer(name="Bob", email="bob@example.com"))

        found = self.customer_repo.get_by_email("BOB@EXAMPLE.COM")
        self.assertIsNotNone(found)
        self.assertEqual(found.name, "Bob")

        missing = self.customer_repo.get_by_email("unknown@example.com")
        self.assertIsNone(missing)

    def test_get_by_id_missing_returns_none(self) -> None:
        self.assertIsNone(self.customer_repo.get_by_id(9999))

    def test_get_all(self) -> None:
        self.customer_repo.create(Customer(name="Charlie", email="c@example.com"))
        self.customer_repo.create(Customer(name="Alice", email="a@example.com"))

        customers = self.customer_repo.get_all()
        self.assertEqual(len(customers), 2)
        self.assertEqual([c.name for c in customers], ["Alice", "Charlie"])

    def test_update(self) -> None:
        cust = self.customer_repo.create(Customer(name="Dave", email="dave@example.com"))
        cust.name = "David"
        cust.phone = "555-9999"
        cust.address = "456 Elm St"

        self.assertTrue(self.customer_repo.update(cust))

        refetched = self.customer_repo.get_by_id(cust.id)
        self.assertEqual(refetched.name, "David")
        self.assertEqual(refetched.phone, "555-9999")
        self.assertEqual(refetched.address, "456 Elm St")

    def test_update_none_id_raises_value_error(self) -> None:
        unsaved = Customer(name="Unsaved", email="unsaved@example.com")
        with self.assertRaises(ValueError):
            self.customer_repo.update(unsaved)

    def test_delete(self) -> None:
        cust = self.customer_repo.create(Customer(name="Eve", email="eve@example.com"))
        self.assertTrue(self.customer_repo.delete(cust.id))
        self.assertIsNone(self.customer_repo.get_by_id(cust.id))

    def test_delete_restricted_by_orders(self) -> None:
        cust = self.customer_repo.create(Customer(name="Frank", email="frank@example.com"))
        order = Order(customer_id=cust.id)
        self.order_repo.create(order)

        # Deleting customer with orders must be restricted
        with self.assertRaises(sqlite3.IntegrityError):
            self.customer_repo.delete(cust.id)


class TestOrderRepository(BaseRepositoryTestCase):
    """Tests for OrderRepository atomic creation, queries, transactions, and price snapshots."""

    def setUp(self) -> None:
        super().setUp()
        self.customer = self.customer_repo.create(
            Customer(name="Grace Hopper", email="grace@example.com")
        )
        self.product1 = self.product_repo.create(
            Product(name="Artisan Bread", price=400, stock_quantity=10)
        )
        self.product2 = self.product_repo.create(
            Product(name="Organic Jam", price=650, stock_quantity=15)
        )

    def test_create_order_with_multiple_items_and_get_by_id(self) -> None:
        order = Order(
            customer_id=self.customer.id,
            status=OrderStatus.PENDING,
            items=[
                OrderItem(
                    product_id=self.product1.id,
                    product_name=self.product1.name,
                    unit_price=self.product1.price,
                    quantity=2,
                ),
                OrderItem(
                    product_id=self.product2.id,
                    product_name=self.product2.name,
                    unit_price=self.product2.price,
                    quantity=1,
                ),
            ],
        )

        saved = self.order_repo.create(order)
        self.assertIsNotNone(saved.id)
        self.assertEqual(len(saved.items), 2)
        self.assertIsNotNone(saved.items[0].id)
        self.assertIsNotNone(saved.items[1].id)
        self.assertEqual(saved.items[0].order_id, saved.id)
        self.assertEqual(saved.items[1].order_id, saved.id)

        # Fetch by ID and verify eager loading of items and total calculation
        fetched = self.order_repo.get_by_id(saved.id)
        self.assertIsNotNone(fetched)
        self.assertEqual(fetched.customer_id, self.customer.id)
        self.assertEqual(fetched.status, OrderStatus.PENDING)
        self.assertEqual(len(fetched.items), 2)
        # Total: (2 * 400) + (1 * 650) = 1450 cents ($14.50)
        self.assertEqual(fetched.total, 1450)
        self.assertIsNotNone(fetched.created_at.tzinfo)

    def test_get_by_id_missing_returns_none(self) -> None:
        self.assertIsNone(self.order_repo.get_by_id(9999))

    def test_get_by_customer_id(self) -> None:
        order1 = Order(
            customer_id=self.customer.id,
            items=[OrderItem(product_id=self.product1.id, product_name=self.product1.name, unit_price=400, quantity=1)],
        )
        order2 = Order(
            customer_id=self.customer.id,
            items=[OrderItem(product_id=self.product2.id, product_name=self.product2.name, unit_price=650, quantity=2)],
        )
        self.order_repo.create(order1)
        self.order_repo.create(order2)

        orders = self.order_repo.get_by_customer_id(self.customer.id)
        self.assertEqual(len(orders), 2)
        # Verify items populated for all orders
        self.assertEqual(len(orders[0].items), 1)
        self.assertEqual(len(orders[1].items), 1)

        # Nonexistent customer returns empty list
        self.assertEqual(self.order_repo.get_by_customer_id(9999), [])

    def test_get_all(self) -> None:
        cust2 = self.customer_repo.create(Customer(name="Other", email="other@example.com"))
        self.order_repo.create(Order(customer_id=self.customer.id))
        self.order_repo.create(Order(customer_id=cust2.id))

        all_orders = self.order_repo.get_all()
        self.assertEqual(len(all_orders), 2)

    def test_create_atomic_rollback_on_item_failure(self) -> None:
        # Construct an order where the second item references a nonexistent product_id (FK violation)
        bad_order = Order(
            customer_id=self.customer.id,
            items=[
                OrderItem(
                    product_id=self.product1.id,
                    product_name="Good Product",
                    unit_price=400,
                    quantity=1,
                ),
                OrderItem(
                    product_id=99999,  # Nonexistent product
                    product_name="Bad Product",
                    unit_price=500,
                    quantity=1,
                ),
            ],
        )

        with self.assertRaises(sqlite3.IntegrityError):
            self.order_repo.create(bad_order)

        # Verify that no order was created (atomic transaction rolled back)
        all_orders = self.order_repo.get_all()
        self.assertEqual(len(all_orders), 0)

        # Verify that no order items were left orphaned in the database
        items_count = self.conn.execute("SELECT COUNT(*) as c FROM order_items").fetchone()["c"]
        self.assertEqual(items_count, 0)

    def test_preservation_of_historical_order_item_prices(self) -> None:
        # Create an order with initial price snapshot of 400 cents ($4.00)
        order = Order(
            customer_id=self.customer.id,
            items=[
                OrderItem(
                    product_id=self.product1.id,
                    product_name=self.product1.name,
                    unit_price=self.product1.price,  # 400
                    quantity=3,
                )
            ],
        )
        saved = self.order_repo.create(order)
        self.assertEqual(saved.total, 1200)

        # Subsequently, update the product price in the catalog to 600 cents ($6.00)
        self.product1.price = 600
        self.product_repo.update(self.product1)

        catalog_prod = self.product_repo.get_by_id(self.product1.id)
        self.assertEqual(catalog_prod.price, 600)

        # Historical order item price snapshot must remain intact at 400 cents
        fetched_order = self.order_repo.get_by_id(saved.id)
        self.assertEqual(fetched_order.items[0].unit_price, 400)
        self.assertEqual(fetched_order.total, 1200)

    def test_update_status(self) -> None:
        order = self.order_repo.create(Order(customer_id=self.customer.id))
        self.assertEqual(order.status, OrderStatus.PENDING)

        # Update status using enum
        self.assertTrue(self.order_repo.update_status(order.id, OrderStatus.PAID))
        updated = self.order_repo.get_by_id(order.id)
        self.assertEqual(updated.status, OrderStatus.PAID)

        # Update status using valid string
        self.assertTrue(self.order_repo.update_status(order.id, "shipped"))
        updated2 = self.order_repo.get_by_id(order.id)
        self.assertEqual(updated2.status, OrderStatus.SHIPPED)

        # Updating invalid status raises ValueError
        with self.assertRaises(ValueError):
            self.order_repo.update_status(order.id, "invalid_status")

        # Updating non-existent order returns False
        self.assertFalse(self.order_repo.update_status(9999, OrderStatus.CANCELLED))

    def test_delete_cascades_to_order_items(self) -> None:
        order = Order(
            customer_id=self.customer.id,
            items=[
                OrderItem(
                    product_id=self.product1.id,
                    product_name=self.product1.name,
                    unit_price=self.product1.price,
                    quantity=1,
                )
            ],
        )
        saved = self.order_repo.create(order)
        self.assertEqual(len(self.order_repo.get_by_id(saved.id).items), 1)

        # Deleting the order cascades to remove order_items
        self.assertTrue(self.order_repo.delete(saved.id))
        self.assertIsNone(self.order_repo.get_by_id(saved.id))

        items_remaining = self.conn.execute(
            "SELECT COUNT(*) as c FROM order_items WHERE order_id = ?",
            (saved.id,),
        ).fetchone()["c"]
        self.assertEqual(items_remaining, 0)


class TestRepositoryFlaskContext(unittest.TestCase):
    """Test that repositories default to Flask's get_db() when no connection is provided."""

    def setUp(self) -> None:
        class MemoryConfig(Config):
            TESTING = True
            DATABASE_PATH = ":memory:"

        self.app = create_app(MemoryConfig)

    def test_repositories_default_to_flask_get_db(self) -> None:
        with self.app.app_context():
            init_db()

            cat_repo = CategoryRepository()
            prod_repo = ProductRepository()
            cust_repo = CustomerRepository()
            order_repo = OrderRepository()

            cat = cat_repo.create(Category(name="Apples"))
            self.assertIsNotNone(cat.id)

            prod = prod_repo.create(Product(name="Honeycrisp", price=299, category_id=cat.id))
            self.assertIsNotNone(prod.id)

            cust = cust_repo.create(Customer(name="Tester", email="test@example.com"))
            self.assertIsNotNone(cust.id)

            order = order_repo.create(
                Order(
                    customer_id=cust.id,
                    items=[OrderItem(product_id=prod.id, product_name=prod.name, unit_price=299, quantity=2)],
                )
            )
            self.assertIsNotNone(order.id)
            self.assertEqual(order.total, 598)


if __name__ == "__main__":
    unittest.main()

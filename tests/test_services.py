"""Unit and integration tests for Phase 5 business logic services."""
import sqlite3
import unittest
from unittest.mock import patch

from app.database.connection import create_connection, init_db
from app.models.cart import Cart
from app.models.category import Category
from app.models.customer import Customer
from app.models.order import OrderStatus
from app.models.product import Product
from app.models.user import User
from app.repositories.category_repository import CategoryRepository
from app.repositories.customer_repository import CustomerRepository
from app.repositories.order_repository import OrderRepository
from app.repositories.product_repository import ProductRepository
from app.repositories.user_repository import UserRepository
from app.services.cart_service import CartService
from app.services.category_service import CategoryService
from app.services.customer_service import CustomerService
from app.services.order_service import OrderService
from app.services.product_service import ProductService


class BaseServiceTestCase(unittest.TestCase):
    """Base test case initializing an isolated in-memory SQLite database and service instances."""

    def setUp(self) -> None:
        self.conn = create_connection(":memory:")
        init_db(self.conn)

        self.category_repo = CategoryRepository(self.conn)
        self.product_repo = ProductRepository(self.conn)
        self.customer_repo = CustomerRepository(self.conn)
        self.order_repo = OrderRepository(self.conn)
        self.user_repo = UserRepository(self.conn)

        self.category_service = CategoryService(self.category_repo)
        self.product_service = ProductService(self.product_repo, self.category_repo)
        self.customer_service = CustomerService(self.customer_repo)
        self.cart_service = CartService(self.product_repo)
        self.order_service = OrderService(
            order_repo=self.order_repo,
            product_repo=self.product_repo,
            customer_repo=self.customer_repo,
            user_repo=self.user_repo,
            conn=self.conn,
        )

    def tearDown(self) -> None:
        self.conn.close()


class TestCategoryService(BaseServiceTestCase):
    """Tests for CategoryService validation and operations."""

    def test_create_category_success(self) -> None:
        cat = self.category_service.create_category("Bakery", "Fresh bread and pastries")
        self.assertIsNotNone(cat.id)
        self.assertEqual(cat.name, "Bakery")
        self.assertEqual(cat.description, "Fresh bread and pastries")

    def test_create_category_empty_name_raises(self) -> None:
        with self.assertRaises(ValueError):
            self.category_service.create_category("   ")

    def test_create_category_duplicate_name_raises(self) -> None:
        self.category_service.create_category("Produce")
        with self.assertRaises(ValueError):
            self.category_service.create_category("Produce")

    def test_get_by_id_and_get_all(self) -> None:
        cat = self.category_service.create_category("Dairy")
        fetched = self.category_service.get_by_id(cat.id)
        self.assertIsNotNone(fetched)
        self.assertEqual(fetched.name, "Dairy")

        missing = self.category_service.get_by_id(9999)
        self.assertIsNone(missing)

        all_cats = self.category_service.get_all()
        self.assertEqual(len(all_cats), 1)

    def test_update_category_success(self) -> None:
        cat = self.category_service.create_category("Snack", "Chips")
        updated = self.category_service.update_category(cat.id, "Snacks", "Healthy chips")
        self.assertEqual(updated.name, "Snacks")
        self.assertEqual(updated.description, "Healthy chips")

    def test_update_category_not_found_raises(self) -> None:
        with self.assertRaises(ValueError):
            self.category_service.update_category(9999, "Nonexistent")

    def test_update_category_duplicate_name_raises(self) -> None:
        cat1 = self.category_service.create_category("Drinks")
        self.category_service.create_category("Snacks")

        with self.assertRaises(ValueError):
            self.category_service.update_category(cat1.id, "Snacks")

    def test_delete_category(self) -> None:
        cat = self.category_service.create_category("Meat")
        self.assertTrue(self.category_service.delete_category(cat.id))
        self.assertIsNone(self.category_service.get_by_id(cat.id))

        with self.assertRaises(ValueError):
            self.category_service.delete_category(9999)


class TestProductService(BaseServiceTestCase):
    """Tests for ProductService business rules, stock adjustments, and catalog filters."""

    def setUp(self) -> None:
        super().setUp()
        self.category = self.category_service.create_category("Pantry")

    def test_create_product_success(self) -> None:
        prod = self.product_service.create_product(
            name="Organic Pasta",
            price=299,
            stock_quantity=50,
            description="500g penne",
            category_id=self.category.id,
            is_active=True,
        )
        self.assertIsNotNone(prod.id)
        self.assertEqual(prod.name, "Organic Pasta")
        self.assertEqual(prod.price, 299)
        self.assertEqual(prod.stock_quantity, 50)
        self.assertTrue(prod.is_active)

    def test_create_product_validation_errors(self) -> None:
        # Empty name
        with self.assertRaises(ValueError):
            self.product_service.create_product("  ", price=100)

        # Negative price
        with self.assertRaises(ValueError):
            self.product_service.create_product("Flour", price=-50)

        # Negative stock
        with self.assertRaises(ValueError):
            self.product_service.create_product("Flour", price=100, stock_quantity=-1)

        # Non-existent category
        with self.assertRaises(ValueError):
            self.product_service.create_product("Flour", price=100, category_id=9999)

    def test_get_active_and_all_products(self) -> None:
        self.product_service.create_product("Active Product", price=100, is_active=True)
        self.product_service.create_product("Inactive Product", price=200, is_active=False)

        active = self.product_service.get_active_products()
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0].name, "Active Product")

        all_prods = self.product_service.get_all_products(include_inactive=True)
        self.assertEqual(len(all_prods), 2)

    def test_get_products_by_category(self) -> None:
        self.product_service.create_product("Rice", price=150, category_id=self.category.id)
        prods = self.product_service.get_products_by_category(self.category.id)
        self.assertEqual(len(prods), 1)
        self.assertEqual(prods[0].name, "Rice")

        with self.assertRaises(ValueError):
            self.product_service.get_products_by_category(9999)

    def test_update_product(self) -> None:
        prod = self.product_service.create_product("Oats", price=200, stock_quantity=10)
        updated = self.product_service.update_product(
            prod.id,
            name="Rolled Oats",
            price=250,
            stock_quantity=20,
            description="Organic rolled oats",
            is_active=False,
        )
        self.assertEqual(updated.name, "Rolled Oats")
        self.assertEqual(updated.price, 250)
        self.assertEqual(updated.stock_quantity, 20)
        self.assertFalse(updated.is_active)

    def test_update_stock(self) -> None:
        prod = self.product_service.create_product("Beans", price=120, stock_quantity=10)

        # Increase stock
        res = self.product_service.update_stock(prod.id, 5)
        self.assertEqual(res.stock_quantity, 15)

        # Decrease stock
        res2 = self.product_service.update_stock(prod.id, -10)
        self.assertEqual(res2.stock_quantity, 5)

        # Excessive decrease raises ValueError
        with self.assertRaises(ValueError):
            self.product_service.update_stock(prod.id, -10)

    def test_delete_product(self) -> None:
        prod = self.product_service.create_product("Sugar", price=90)
        self.assertTrue(self.product_service.delete_product(prod.id))
        self.assertIsNone(self.product_service.get_by_id(prod.id))

        with self.assertRaises(ValueError):
            self.product_service.delete_product(9999)

    def test_create_and_update_product_rejects_booleans(self) -> None:
        for val in [True, False]:
            with self.assertRaises(ValueError):
                self.product_service.create_product("Pasta", price=val)
            with self.assertRaises(ValueError):
                self.product_service.create_product("Pasta", price=100, stock_quantity=val)

        prod = self.product_service.create_product("Rice", price=100, stock_quantity=10)
        for val in [True, False]:
            with self.assertRaises(ValueError):
                self.product_service.update_product(prod.id, "Rice", price=val, stock_quantity=10)
            with self.assertRaises(ValueError):
                self.product_service.update_product(prod.id, "Rice", price=100, stock_quantity=val)
            with self.assertRaises(ValueError):
                self.product_service.update_stock(prod.id, val)


class TestCustomerService(BaseServiceTestCase):
    """Tests for CustomerService registration and unique email enforcement."""

    def test_register_customer_success(self) -> None:
        cust = self.customer_service.register_customer(
            name="Alice",
            email="Alice@example.com",
            phone="555-1234",
            address="10 High St",
        )
        self.assertIsNotNone(cust.id)
        self.assertEqual(cust.name, "Alice")
        self.assertEqual(cust.email, "alice@example.com")  # Normalized lowercase

    def test_register_customer_validation_errors(self) -> None:
        with self.assertRaises(ValueError):
            self.customer_service.register_customer("  ", "alice@example.com")

        with self.assertRaises(ValueError):
            self.customer_service.register_customer("Alice", "invalid-email-no-at")

    def test_register_duplicate_email_raises(self) -> None:
        self.customer_service.register_customer("Bob", "bob@example.com")
        with self.assertRaises(ValueError):
            self.customer_service.register_customer("Bob 2", "BOB@EXAMPLE.COM")

    def test_register_customer_concurrent_duplicate_email_integrity_error_converted_to_value_error(self) -> None:
        # Simulate a race condition where get_by_email returns None (pre-check passes),
        # but the database raises a duplicate email IntegrityError on insert.
        self.customer_service.register_customer("Existing User", "racer@example.com")
        with patch.object(self.customer_repo, "get_by_email", return_value=None):
            with self.assertRaises(ValueError) as ctx:
                self.customer_service.register_customer("Concurrent User", "racer@example.com")
            self.assertIn("already exists", str(ctx.exception))

    def test_register_customer_unrelated_integrity_error_is_not_silently_converted(self) -> None:
        # Verify that non-email-uniqueness database errors are not converted to ValueError
        with patch.object(
            self.customer_repo,
            "create",
            side_effect=sqlite3.IntegrityError("CHECK constraint failed: customers"),
        ):
            with self.assertRaises(sqlite3.IntegrityError):
                self.customer_service.register_customer("Valid User", "valid@example.com")

    def test_get_by_email(self) -> None:
        self.customer_service.register_customer("Charlie", "charlie@example.com")
        found = self.customer_service.get_by_email("CHARLIE@EXAMPLE.COM")
        self.assertIsNotNone(found)
        self.assertEqual(found.name, "Charlie")

        self.assertIsNone(self.customer_service.get_by_email("missing@example.com"))
        self.assertIsNone(self.customer_service.get_by_email(""))

    def test_update_customer(self) -> None:
        cust = self.customer_service.register_customer("Diana", "diana@example.com")
        updated = self.customer_service.update_customer(
            cust.id, name="Diana Prince", email="diana.prince@example.com", phone="555-0000"
        )
        self.assertEqual(updated.name, "Diana Prince")
        self.assertEqual(updated.email, "diana.prince@example.com")

    def test_update_customer_duplicate_email_raises(self) -> None:
        self.customer_service.register_customer("User1", "user1@example.com")
        cust2 = self.customer_service.register_customer("User2", "user2@example.com")

        with self.assertRaises(ValueError):
            self.customer_service.update_customer(cust2.id, "User2", "user1@example.com")

    def test_delete_customer(self) -> None:
        cust = self.customer_service.register_customer("Eve", "eve@example.com")
        self.assertTrue(self.customer_service.delete_customer(cust.id))
        self.assertIsNone(self.customer_service.get_by_id(cust.id))

        with self.assertRaises(ValueError):
            self.customer_service.delete_customer(9999)


class TestCartService(BaseServiceTestCase):
    """Tests for in-memory CartService operations, stock checks, and validations."""

    def setUp(self) -> None:
        super().setUp()
        self.product = self.product_service.create_product(
            name="Apple", price=150, stock_quantity=10, is_active=True
        )
        self.inactive_product = self.product_service.create_product(
            name="Rare Spice", price=500, stock_quantity=5, is_active=False
        )

    def test_create_and_add_item_success(self) -> None:
        cart = self.cart_service.create_cart(customer_id=1)
        self.assertTrue(cart.is_empty)

        self.cart_service.add_item(cart, self.product.id, quantity=2)
        self.assertFalse(cart.is_empty)
        self.assertEqual(cart.total_quantity, 2)
        self.assertEqual(cart.total, 300)

    def test_add_item_validation_errors(self) -> None:
        cart = self.cart_service.create_cart()

        # Invalid quantity
        with self.assertRaises(ValueError):
            self.cart_service.add_item(cart, self.product.id, quantity=0)
        with self.assertRaises(ValueError):
            self.cart_service.add_item(cart, self.product.id, quantity=-1)

        # Non-existent product
        with self.assertRaises(ValueError):
            self.cart_service.add_item(cart, 9999, quantity=1)

        # Inactive product
        with self.assertRaises(ValueError):
            self.cart_service.add_item(cart, self.inactive_product.id, quantity=1)

        # Exceeds available stock
        with self.assertRaises(ValueError):
            self.cart_service.add_item(cart, self.product.id, quantity=11)

    def test_add_item_cumulative_stock_check(self) -> None:
        cart = self.cart_service.create_cart()
        self.cart_service.add_item(cart, self.product.id, quantity=6)

        # Second add pushes total from 6 to 11 (exceeds stock 10)
        with self.assertRaises(ValueError):
            self.cart_service.add_item(cart, self.product.id, quantity=5)

    def test_update_item_quantity(self) -> None:
        cart = self.cart_service.create_cart()
        self.cart_service.add_item(cart, self.product.id, quantity=2)

        # Update valid quantity
        self.cart_service.update_item_quantity(cart, self.product.id, quantity=5)
        self.assertEqual(cart.total_quantity, 5)
        self.assertEqual(cart.total, 750)

        # Quantity 0 removes product
        self.cart_service.update_item_quantity(cart, self.product.id, quantity=0)
        self.assertTrue(cart.is_empty)

        # Exceeding stock raises
        with self.assertRaises(ValueError):
            self.cart_service.update_item_quantity(cart, self.product.id, quantity=20)

    def test_validate_cart(self) -> None:
        cart = self.cart_service.create_cart()
        self.cart_service.add_item(cart, self.product.id, quantity=2)
        errors = self.cart_service.validate_cart(cart)
        self.assertEqual(errors, [])

        # Deactivate product in catalog
        self.product.is_active = False
        self.product_repo.update(self.product)
        errors = self.cart_service.validate_cart(cart)
        self.assertEqual(len(errors), 1)
        self.assertIn("no longer active", errors[0])

    def test_cart_service_rejects_booleans(self) -> None:
        cart = self.cart_service.create_cart()
        for val in [True, False]:
            with self.assertRaises(ValueError):
                self.cart_service.add_item(cart, self.product.id, quantity=val)

        self.cart_service.add_item(cart, self.product.id, quantity=2)
        for val in [True, False]:
            with self.assertRaises(ValueError):
                self.cart_service.update_item_quantity(cart, self.product.id, quantity=val)


class TestOrderService(BaseServiceTestCase):
    """Tests for OrderService checkout, atomic stock updates, rollback safety, and order lifecycle."""

    def setUp(self) -> None:
        super().setUp()
        self.customer = self.customer_service.register_customer(
            name="Grace Hopper", email="grace@example.com"
        )
        self.apple = self.product_service.create_product(
            name="Honeycrisp Apple", price=200, stock_quantity=10
        )
        self.bread = self.product_service.create_product(
            name="Sourdough Bread", price=450, stock_quantity=5
        )

    def test_checkout_success(self) -> None:
        cart = self.cart_service.create_cart(self.customer.id)
        self.cart_service.add_item(cart, self.apple.id, quantity=2)  # 2 * 200 = 400
        self.cart_service.add_item(cart, self.bread.id, quantity=1)  # 1 * 450 = 450

        order = self.order_service.checkout(self.customer.id, cart)

        self.assertIsNotNone(order.id)
        self.assertEqual(order.customer_id, self.customer.id)
        self.assertEqual(order.status, OrderStatus.PENDING)
        self.assertEqual(len(order.items), 2)
        self.assertEqual(order.total, 850)  # $8.50 in cents
        self.assertTrue(cart.is_empty)

        # Verify stock was decremented in database
        updated_apple = self.product_service.get_by_id(self.apple.id)
        updated_bread = self.product_service.get_by_id(self.bread.id)
        self.assertEqual(updated_apple.stock_quantity, 8)
        self.assertEqual(updated_bread.stock_quantity, 4)

    def test_checkout_captures_explicit_delivery_snapshot_independently_of_customer_profile(self) -> None:
        cart = self.cart_service.create_cart(self.customer.id)
        self.cart_service.add_item(cart, self.apple.id, quantity=1)

        order = self.order_service.checkout(
            self.customer.id,
            cart,
            customer_name="Recipient Specific",
            customer_email="recipient@example.com",
            customer_phone="912000000",
            customer_address="Delivery Address 789, Coimbra",
        )

        self.assertEqual(order.customer_name, "Recipient Specific")
        self.assertEqual(order.customer_email, "recipient@example.com")
        self.assertEqual(order.customer_phone, "912000000")
        self.assertEqual(order.customer_address, "Delivery Address 789, Coimbra")

        # Mutate the customer profile in the database
        self.customer.name = "Mutated Customer Name"
        self.customer.phone = "999-999-999"
        self.customer.address = "Completely Different Address"
        self.customer_repo.update(self.customer)

        # Refetched order must maintain its historical immutable delivery snapshot
        refetched = self.order_service.get_by_id(order.id)
        self.assertIsNotNone(refetched)
        self.assertEqual(refetched.customer_name, "Recipient Specific")
        self.assertEqual(refetched.customer_email, "recipient@example.com")
        self.assertEqual(refetched.customer_phone, "912000000")
        self.assertEqual(refetched.customer_address, "Delivery Address 789, Coimbra")

    def test_checkout_with_authenticated_user_id_success(self) -> None:
        user = self.user_repo.create(User(email="auth_checkout@example.com"))
        cart = self.cart_service.create_cart(self.customer.id)
        self.cart_service.add_item(cart, self.apple.id, quantity=1)

        order = self.order_service.checkout(
            self.customer.id,
            cart,
            customer_name="Auth Buyer",
            customer_email="auth_checkout@example.com",
            user_id=user.id,
        )

        self.assertIsNotNone(order.id)
        self.assertEqual(order.user_id, user.id)

        # Order is retrievable via get_user_order and get_user_orders
        user_orders = self.order_service.get_user_orders(user.id)
        self.assertEqual(len(user_orders), 1)
        self.assertEqual(user_orders[0].id, order.id)

        fetched_order = self.order_service.get_user_order(order.id, user.id)
        self.assertIsNotNone(fetched_order)
        self.assertEqual(fetched_order.id, order.id)

    def test_checkout_with_nonexistent_user_id_raises(self) -> None:
        cart = self.cart_service.create_cart(self.customer.id)
        self.cart_service.add_item(cart, self.apple.id, quantity=1)

        with self.assertRaises(ValueError) as ctx:
            self.order_service.checkout(self.customer.id, cart, user_id=999999)
        self.assertIn("not found", str(ctx.exception).lower())

    def test_checkout_with_invalid_user_id_type_raises(self) -> None:
        cart = self.cart_service.create_cart(self.customer.id)
        self.cart_service.add_item(cart, self.apple.id, quantity=1)

        for invalid_id in [True, False, -5, 0, "not-an-int"]:
            with self.assertRaises(ValueError):
                self.order_service.checkout(self.customer.id, cart, user_id=invalid_id)

    def test_get_user_orders_validation(self) -> None:
        for invalid_id in [True, False, -1, 0, "string"]:
            with self.assertRaises(ValueError):
                self.order_service.get_user_orders(invalid_id)

    def test_checkout_empty_cart_raises(self) -> None:
        empty_cart = self.cart_service.create_cart()
        with self.assertRaises(ValueError):
            self.order_service.checkout(self.customer.id, empty_cart)

    def test_checkout_nonexistent_customer_raises(self) -> None:
        cart = self.cart_service.create_cart()
        self.cart_service.add_item(cart, self.apple.id, quantity=1)
        with self.assertRaises(ValueError):
            self.order_service.checkout(9999, cart)

    def test_checkout_atomic_rollback_on_insufficient_stock(self) -> None:
        """Verify that if any item fails stock validation during checkout,

        neither the order nor any stock changes are committed to the database.
        """
        cart = self.cart_service.create_cart(self.customer.id)
        # Add 2 apples (stock is 10)
        self.cart_service.add_item(cart, self.apple.id, quantity=2)
        # Add 1 bread (stock is 5)
        self.cart_service.add_item(cart, self.bread.id, quantity=1)

        # Before checkout, simulate another user purchasing all bread so stock drops to 0
        self.bread.stock_quantity = 0
        self.product_repo.update(self.bread)

        # Checkout should fail because bread is now out of stock
        with self.assertRaises(ValueError) as ctx:
            self.order_service.checkout(self.customer.id, cart)

        self.assertIn("Insufficient stock", str(ctx.exception))

        # CRITICAL VERIFICATION: Apple stock must NOT have been decremented (must still be 10)
        apple_after = self.product_service.get_by_id(self.apple.id)
        self.assertEqual(apple_after.stock_quantity, 10)

        # Bread stock remains 0
        bread_after = self.product_service.get_by_id(self.bread.id)
        self.assertEqual(bread_after.stock_quantity, 0)

        # No order record was created
        all_orders = self.order_service.get_all_orders()
        self.assertEqual(len(all_orders), 0)

        # Cart was NOT cleared due to checkout failure
        self.assertEqual(len(cart.items), 2)

    def test_checkout_preserves_purchase_snapshot_price(self) -> None:
        cart = self.cart_service.create_cart()
        self.cart_service.add_item(cart, self.apple.id, quantity=3)  # 3 * 200 = 600

        order = self.order_service.checkout(self.customer.id, cart)
        self.assertEqual(order.items[0].unit_price, 200)
        self.assertEqual(order.total, 600)

        # Product price changes later in catalog
        self.apple.price = 350
        self.product_repo.update(self.apple)

        # Order item snapshot remains at 200 cents
        fetched_order = self.order_service.get_by_id(order.id)
        self.assertEqual(fetched_order.items[0].unit_price, 200)
        self.assertEqual(fetched_order.total, 600)

    def test_update_order_status_and_lifecycle(self) -> None:
        cart = self.cart_service.create_cart()
        self.cart_service.add_item(cart, self.apple.id, quantity=1)
        order = self.order_service.checkout(self.customer.id, cart)

        # Valid transitions
        updated = self.order_service.update_order_status(order.id, OrderStatus.PAID)
        self.assertEqual(updated.status, OrderStatus.PAID)

        updated2 = self.order_service.update_order_status(order.id, "shipped")
        self.assertEqual(updated2.status, OrderStatus.SHIPPED)

        # Invalid status raises ValueError
        with self.assertRaises(ValueError):
            self.order_service.update_order_status(order.id, "bogus_status")

    def test_cancel_order_restores_inventory(self) -> None:
        cart = self.cart_service.create_cart()
        self.cart_service.add_item(cart, self.apple.id, quantity=4)  # leaves 6 in stock
        order = self.order_service.checkout(self.customer.id, cart)

        # Verify stock decreased to 6
        self.assertEqual(self.product_service.get_by_id(self.apple.id).stock_quantity, 6)

        # Cancel order
        cancelled_order = self.order_service.cancel_order(order.id)
        self.assertEqual(cancelled_order.status, OrderStatus.CANCELLED)

        # Verify stock restored to 10
        self.assertEqual(self.product_service.get_by_id(self.apple.id).stock_quantity, 10)

        # Cannot re-update cancelled order
        with self.assertRaises(ValueError):
            self.order_service.update_order_status(order.id, OrderStatus.PAID)

    def test_get_customer_orders(self) -> None:
        cart = self.cart_service.create_cart()
        self.cart_service.add_item(cart, self.apple.id, quantity=1)
        self.order_service.checkout(self.customer.id, cart)

        orders = self.order_service.get_customer_orders(self.customer.id)
        self.assertEqual(len(orders), 1)

        with self.assertRaises(ValueError):
            self.order_service.get_customer_orders(9999)

    def test_delivered_order_is_terminal(self) -> None:
        cart = self.cart_service.create_cart()
        self.cart_service.add_item(cart, self.apple.id, quantity=1)
        order = self.order_service.checkout(self.customer.id, cart)

        # Transition order to DELIVERED
        self.order_service.update_order_status(order.id, OrderStatus.DELIVERED)
        delivered_order = self.order_service.get_by_id(order.id)
        self.assertIsNotNone(delivered_order)
        self.assertEqual(delivered_order.status, OrderStatus.DELIVERED)

        # Re-requesting DELIVERED when already DELIVERED must succeed idempotently
        same_order = self.order_service.update_order_status(order.id, OrderStatus.DELIVERED)
        self.assertEqual(same_order.status, OrderStatus.DELIVERED)
        same_order_str = self.order_service.update_order_status(order.id, "delivered")
        self.assertEqual(same_order_str.status, OrderStatus.DELIVERED)

        # Attempting to transition from DELIVERED to any other status must raise ValueError
        for forbidden_status in [
            OrderStatus.PENDING,
            OrderStatus.CONFIRMED,
            OrderStatus.PAID,
            OrderStatus.SHIPPED,
            OrderStatus.CANCELLED,
        ]:
            with self.assertRaises(ValueError):
                self.order_service.update_order_status(order.id, forbidden_status)

        # Also cancel_order helper must fail on delivered order
        with self.assertRaises(ValueError):
            self.order_service.cancel_order(order.id)

    def test_checkout_failure_at_transaction_exit_preserves_cart(self) -> None:
        from contextlib import contextmanager
        import app.services.order_service as order_service_module

        cart = self.cart_service.create_cart(self.customer.id)
        self.cart_service.add_item(cart, self.apple.id, quantity=2)

        real_tx = order_service_module.transaction

        @contextmanager
        def tx_failing_at_exit(conn=None):
            with real_tx(conn) as active_conn:
                yield active_conn
            # Simulate failure when exiting/committing transaction
            raise sqlite3.OperationalError("Simulated failure during transaction commit/exit")

        with patch("app.services.order_service.transaction", side_effect=tx_failing_at_exit):
            with self.assertRaises(sqlite3.OperationalError):
                self.order_service.checkout(self.customer.id, cart)

        # Transaction failed at exit/commit: cart must NOT be cleared
        self.assertFalse(cart.is_empty)
        self.assertEqual(len(cart.items), 1)
        self.assertEqual(cart.items[0].quantity, 2)


if __name__ == "__main__":
    unittest.main()

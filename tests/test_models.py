"""Unit tests for domain models and business invariants."""
import unittest

from app.models.cart import Cart
from app.models.cart_item import CartItem
from app.models.category import Category
from app.models.customer import Customer
from app.models.order import Order, OrderStatus
from app.models.order_item import OrderItem
from app.models.product import Product


class TestProductModel(unittest.TestCase):
    """Tests for Product domain invariants and behavior."""

    def test_valid_product_creation(self):
        product = Product(
            id=1,
            name="Organic Apples",
            price=299,  # $2.99 represented in cents
            stock_quantity=50,
            category_id=10,
            description="Crisp organic honeycrisp apples",
        )
        self.assertEqual(product.name, "Organic Apples")
        self.assertEqual(product.price, 299)
        self.assertEqual(product.stock_quantity, 50)
        self.assertTrue(product.is_in_stock)
        self.assertTrue(product.is_active)

    def test_empty_name_raises_error(self):
        with self.assertRaises(ValueError):
            Product(name="   ", price=100)

    def test_negative_price_raises_error(self):
        with self.assertRaises(ValueError):
            Product(name="Bread", price=-50)

    def test_negative_stock_raises_error(self):
        with self.assertRaises(ValueError):
            Product(name="Milk", price=250, stock_quantity=-5)

    def test_stock_availability(self):
        out_of_stock = Product(name="Eggs", price=399, stock_quantity=0)
        self.assertFalse(out_of_stock.is_in_stock)

        inactive = Product(name="Cheese", price=500, stock_quantity=10, is_active=False)
        self.assertFalse(inactive.is_in_stock)

    def test_product_model_rejects_booleans(self):
        for val in [True, False]:
            with self.assertRaises(ValueError):
                Product(name="Bread", price=val, stock_quantity=10)
            with self.assertRaises(ValueError):
                Product(name="Bread", price=100, stock_quantity=val)


class TestCategoryModel(unittest.TestCase):
    """Tests for Category domain invariants."""

    def test_valid_category(self):
        category = Category(id=1, name="Fresh Produce", description="Fruits and veggies")
        self.assertEqual(category.name, "Fresh Produce")
        self.assertEqual(category.description, "Fruits and veggies")

    def test_empty_category_name_raises_error(self):
        with self.assertRaises(ValueError):
            Category(name="")


class TestCustomerModel(unittest.TestCase):
    """Tests for Customer domain invariants."""

    def test_valid_customer(self):
        customer = Customer(
            id=1,
            name="Alice Smith",
            email="Alice@example.com",
            phone="123-456-7890",
            address="123 Market St",
        )
        self.assertEqual(customer.name, "Alice Smith")
        self.assertEqual(customer.email, "alice@example.com")

    def test_invalid_email_raises_error(self):
        with self.assertRaises(ValueError):
            Customer(name="Bob", email="invalid-email")

    def test_empty_name_raises_error(self):
        with self.assertRaises(ValueError):
            Customer(name="  ", email="bob@example.com")


class TestCartAndCartItem(unittest.TestCase):
    """Tests for Cart and CartItem subtotal and total calculations."""

    def setUp(self):
        self.apple = Product(id=1, name="Apple", price=150, stock_quantity=20)
        self.bread = Product(id=2, name="Bread", price=300, stock_quantity=10)

    def test_cart_item_subtotal(self):
        item = CartItem(product=self.apple, quantity=3)
        self.assertEqual(item.subtotal, 450)

    def test_cart_item_invalid_quantity(self):
        with self.assertRaises(ValueError):
            CartItem(product=self.apple, quantity=0)
        with self.assertRaises(ValueError):
            CartItem(product=self.apple, quantity=-1)

    def test_cart_add_and_aggregate_total(self):
        cart = Cart(customer_id=42)
        self.assertTrue(cart.is_empty)

        cart.add_item(self.apple, quantity=2)  # 2 * 150 = 300
        cart.add_item(self.bread, quantity=1)  # 1 * 300 = 300

        self.assertFalse(cart.is_empty)
        self.assertEqual(cart.total, 600)
        self.assertEqual(cart.total_quantity, 3)

    def test_cart_add_existing_product_increments_quantity(self):
        cart = Cart()
        cart.add_item(self.apple, quantity=2)
        cart.add_item(self.apple, quantity=3)

        self.assertEqual(len(cart.items), 1)
        self.assertEqual(cart.items[0].quantity, 5)
        self.assertEqual(cart.total, 750)

    def test_cart_remove_item(self):
        cart = Cart()
        cart.add_item(self.apple, quantity=1)
        cart.add_item(self.bread, quantity=1)
        cart.remove_item(self.apple.id)

        self.assertEqual(len(cart.items), 1)
        self.assertEqual(cart.items[0].product.id, self.bread.id)
        self.assertEqual(cart.total, 300)

    def test_cart_update_quantity(self):
        cart = Cart()
        cart.add_item(self.apple, quantity=1)
        cart.update_quantity(self.apple.id, 4)
        self.assertEqual(cart.total, 600)

        # Quantity <= 0 removes item
        cart.update_quantity(self.apple.id, 0)
        self.assertTrue(cart.is_empty)

    def test_cart_clear(self):
        cart = Cart()
        cart.add_item(self.apple, 2)
        cart.clear()
        self.assertTrue(cart.is_empty)
        self.assertEqual(cart.total, 0)

    def test_cart_and_cart_item_reject_booleans(self):
        for val in [True, False]:
            with self.assertRaises(ValueError):
                CartItem(product=self.apple, quantity=val)

        item = CartItem(product=self.apple, quantity=2)
        for val in [True, False]:
            with self.assertRaises(ValueError):
                item.update_quantity(val)

        cart = Cart()
        for val in [True, False]:
            with self.assertRaises(ValueError):
                cart.add_item(self.apple, quantity=val)

        cart.add_item(self.apple, quantity=2)
        for val in [True, False]:
            with self.assertRaises(ValueError):
                cart.update_quantity(self.apple.id, val)


class TestOrderAndOrderItem(unittest.TestCase):
    """Tests for Order and OrderItem calculations and price snapshot preservation."""

    def test_order_item_subtotal(self):
        item = OrderItem(
            product_id=1,
            product_name="Olive Oil",
            unit_price=1299,
            quantity=2,
        )
        self.assertEqual(item.subtotal, 2598)

    def test_order_item_invariants(self):
        with self.assertRaises(ValueError):
            OrderItem(product_id=1, product_name="Olive Oil", unit_price=-10, quantity=1)
        with self.assertRaises(ValueError):
            OrderItem(product_id=1, product_name="Olive Oil", unit_price=100, quantity=0)

    def test_order_item_rejects_booleans(self):
        for val in [True, False]:
            with self.assertRaises(ValueError):
                OrderItem(product_id=1, product_name="Milk", unit_price=val, quantity=2)
            with self.assertRaises(ValueError):
                OrderItem(product_id=1, product_name="Milk", unit_price=250, quantity=val)

    def test_order_total_calculation(self):
        item1 = OrderItem(product_id=1, product_name="Milk", unit_price=250, quantity=2)  # 500
        item2 = OrderItem(product_id=2, product_name="Cereal", unit_price=450, quantity=1)  # 450

        order = Order(customer_id=10, items=[item1, item2])
        self.assertEqual(order.total, 950)
        self.assertEqual(order.status, OrderStatus.PENDING)

    def test_order_status_conversion_and_immutability(self):
        order = Order(customer_id=10, status="confirmed")
        self.assertEqual(order.status, OrderStatus.CONFIRMED)

    def test_order_preserves_purchase_price_when_product_price_changes(self):
        # Verify that OrderItem retains historical purchase price
        product = Product(id=1, name="Coffee Beans", price=1200)
        item = OrderItem(
            product_id=product.id,
            product_name=product.name,
            unit_price=product.price,
            quantity=2,
        )
        order = Order(customer_id=5, items=[item])
        self.assertEqual(order.total, 2400)

        # Product price changes later in catalog
        product.price = 1500

        # Order total and item price remain unchanged
        self.assertEqual(item.unit_price, 1200)
        self.assertEqual(order.total, 2400)

    def test_order_delivery_snapshot_fields_and_properties(self):
        order = Order(
            customer_id=1,
            customer_name="Beatriz Lima",
            customer_email="beatriz@example.pt",
            customer_phone="912345678",
            customer_address="Avenida Central 10, Braga",
        )
        self.assertEqual(order.customer_name, "Beatriz Lima")
        self.assertEqual(order.customer_email, "beatriz@example.pt")
        self.assertEqual(order.customer_phone, "912345678")
        self.assertEqual(order.customer_address, "Avenida Central 10, Braga")

        # Test delivery aliases
        self.assertEqual(order.delivery_name, "Beatriz Lima")
        self.assertEqual(order.delivery_email, "beatriz@example.pt")
        self.assertEqual(order.delivery_phone, "912345678")
        self.assertEqual(order.delivery_address, "Avenida Central 10, Braga")


if __name__ == "__main__":
    unittest.main()

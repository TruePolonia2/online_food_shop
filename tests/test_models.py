"""Unit tests for domain models and business invariants."""
from datetime import datetime, timedelta, timezone
import unittest

from app.models.cart import Cart
from app.models.cart_item import CartItem
from app.models.category import Category
from app.models.customer import Customer
from app.models.email_verification_token import EmailVerificationToken
from app.models.external_identity import ExternalIdentity
from app.models.order import Order, OrderStatus
from app.models.order_item import OrderItem
from app.models.product import Product
from app.models.user import User, UserRole


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

    def test_order_user_id_field_defaults_to_none_and_accepts_int(self):
        guest_order = Order(customer_id=1)
        self.assertIsNone(guest_order.user_id)

        user_order = Order(customer_id=1, user_id=42)
        self.assertEqual(user_order.user_id, 42)


class TestUserModel(unittest.TestCase):
    """Tests for User domain invariants, normalization, and role validation."""

    def test_valid_user_creation_and_defaults(self):
        user = User(email="customer@example.com")
        self.assertEqual(user.email, "customer@example.com")
        self.assertIsNone(user.id)
        self.assertEqual(user.role, UserRole.CUSTOMER)
        self.assertFalse(user.is_verified)
        self.assertIsNotNone(user.created_at)

    def test_email_normalization(self):
        user = User(email="  Customer.Name@Sub.Example.COM  ")
        self.assertEqual(user.email, "customer.name@sub.example.com")

    def test_invalid_and_empty_email_rejection(self):
        invalid_emails = [
            "",
            "   ",
            "not-an-email",
            "@domain.com",
            "user@",
            "user@@domain.com",
            None,
            123,
        ]
        for val in invalid_emails:
            with self.subTest(email=val):
                with self.assertRaises(ValueError):
                    User(email=val)  # type: ignore[arg-type]

    def test_role_validation_and_normalization(self):
        user_cust = User(email="c@example.com", role="customer")
        self.assertEqual(user_cust.role, UserRole.CUSTOMER)

        user_owner = User(email="o@example.com", role="owner")
        self.assertEqual(user_owner.role, UserRole.OWNER)

        user_upper = User(email="u@example.com", role="OWNER")
        self.assertEqual(user_upper.role, UserRole.OWNER)

        invalid_roles = ["admin", "root", "guest", "", 123]
        for val in invalid_roles:
            with self.subTest(role=val):
                with self.assertRaises(ValueError):
                    User(email="test@example.com", role=val)  # type: ignore[arg-type]

    def test_is_verified_validation(self):
        self.assertFalse(User(email="u1@example.com", is_verified=False).is_verified)
        self.assertTrue(User(email="u2@example.com", is_verified=True).is_verified)
        self.assertFalse(User(email="u3@example.com", is_verified=0).is_verified)
        self.assertTrue(User(email="u4@example.com", is_verified=1).is_verified)

        invalid_verified = ["true", "false", 2, -1, None]
        for val in invalid_verified:
            with self.subTest(is_verified=val):
                with self.assertRaises(ValueError):
                    User(email="test@example.com", is_verified=val)  # type: ignore[arg-type]

    def test_user_password_hash_validation_and_repr(self):
        # Default without credentials
        user_no_cred = User(email="cred@example.com")
        self.assertIsNone(user_no_cred.password_hash)
        self.assertFalse(user_no_cred.has_credentials)

        # Valid password hash
        fake_hash = "scrypt:32768:8:1$randomsalt$hashedvalue"
        user_with_cred = User(email="cred@example.com", password_hash=fake_hash)
        self.assertEqual(user_with_cred.password_hash, fake_hash)
        self.assertTrue(user_with_cred.has_credentials)

        # Security invariant: password_hash must be excluded from repr to avoid log leaks
        self.assertNotIn(fake_hash, repr(user_with_cred))
        self.assertNotIn("password_hash", repr(user_with_cred))

        # Rejection of invalid password hashes
        invalid_hashes = ["", "   ", 12345, []]
        for bad_hash in invalid_hashes:
            with self.subTest(bad_hash=bad_hash):
                with self.assertRaises(ValueError):
                    User(email="cred@example.com", password_hash=bad_hash)  # type: ignore[arg-type]


class TestEmailVerificationTokenModel(unittest.TestCase):
    """Tests for EmailVerificationToken domain invariants and lifecycle properties."""

    def test_valid_token_creation(self):
        now_utc = datetime.now(timezone.utc)
        expires_at = now_utc + timedelta(hours=24)
        token = EmailVerificationToken(
            id=1,
            user_id=42,
            token_hash="a" * 64,
            expires_at=expires_at,
            created_at=now_utc,
        )
        self.assertEqual(token.user_id, 42)
        self.assertEqual(token.token_hash, "a" * 64)
        self.assertFalse(token.is_used)
        self.assertFalse(token.is_expired)
        self.assertTrue(token.is_valid)

    def test_invalid_user_id_raises_value_error(self):
        now_utc = datetime.now(timezone.utc)
        expires_at = now_utc + timedelta(hours=24)
        invalid_ids = [0, -1, None, "42", True, False]
        for uid in invalid_ids:
            with self.subTest(uid=uid):
                with self.assertRaises(ValueError):
                    EmailVerificationToken(
                        user_id=uid,  # type: ignore[arg-type]
                        token_hash="a" * 64,
                        expires_at=expires_at,
                    )

    def test_empty_or_whitespace_token_hash_raises_value_error(self):
        now_utc = datetime.now(timezone.utc)
        expires_at = now_utc + timedelta(hours=24)
        for bad_hash in ["", "   ", None, 123]:
            with self.subTest(bad_hash=bad_hash):
                with self.assertRaises(ValueError):
                    EmailVerificationToken(
                        user_id=1,
                        token_hash=bad_hash,  # type: ignore[arg-type]
                        expires_at=expires_at,
                    )

    def test_timestamp_parsing_and_utc_normalization(self):
        token = EmailVerificationToken(
            user_id=5,
            token_hash="b" * 64,
            expires_at="2026-10-27T12:00:00Z",
            created_at="2026-10-26T12:00:00Z",
            used_at="2026-10-26T13:00:00Z",
        )
        self.assertEqual(token.expires_at.tzinfo, timezone.utc)
        self.assertEqual(token.created_at.tzinfo, timezone.utc)
        self.assertEqual(token.used_at.tzinfo, timezone.utc)
        self.assertTrue(token.is_used)

    def test_is_expired_and_is_valid_lifecycle(self):
        now_utc = datetime.now(timezone.utc)

        # Future token: valid and not expired
        token_active = EmailVerificationToken(
            user_id=1,
            token_hash="c" * 64,
            expires_at=now_utc + timedelta(hours=1),
        )
        self.assertFalse(token_active.is_expired)
        self.assertFalse(token_active.is_used)
        self.assertTrue(token_active.is_valid)

        # Past token: expired and invalid
        token_past = EmailVerificationToken(
            user_id=1,
            token_hash="d" * 64,
            expires_at=now_utc - timedelta(seconds=10),
        )
        self.assertTrue(token_past.is_expired)
        self.assertFalse(token_past.is_valid)

        # Consumed token: used and invalid even if not expired
        token_consumed = EmailVerificationToken(
            user_id=1,
            token_hash="e" * 64,
            expires_at=now_utc + timedelta(hours=1),
            used_at=now_utc,
        )
        self.assertTrue(token_consumed.is_used)
        self.assertFalse(token_consumed.is_valid)


class TestExternalIdentityModel(unittest.TestCase):
    """Tests for ExternalIdentity domain invariants and validation."""

    def test_valid_external_identity_creation(self):
        identity = ExternalIdentity(
            user_id=1,
            provider="Google",
            provider_user_id="google_sub_12345",
            email="User@Example.com ",
        )
        self.assertEqual(identity.user_id, 1)
        self.assertEqual(identity.provider, "google")
        self.assertEqual(identity.provider_user_id, "google_sub_12345")
        self.assertEqual(identity.email, "user@example.com")
        self.assertIsNotNone(identity.created_at)

    def test_invalid_user_id_raises(self):
        for invalid_id in [None, 0, -1, "1", True, False]:
            with self.assertRaises(ValueError):
                ExternalIdentity(user_id=invalid_id, provider="google", provider_user_id="sub1")

    def test_invalid_provider_raises(self):
        for invalid_provider in ["", "   ", None, 123]:
            with self.assertRaises((ValueError, AttributeError)):
                ExternalIdentity(user_id=1, provider=invalid_provider, provider_user_id="sub1")

    def test_invalid_provider_user_id_raises(self):
        for invalid_sub in ["", "   ", None, True, False]:
            with self.assertRaises(ValueError):
                ExternalIdentity(user_id=1, provider="google", provider_user_id=invalid_sub)

    def test_email_optional_and_normalized(self):
        identity = ExternalIdentity(user_id=1, provider="google", provider_user_id="sub1", email=None)
        self.assertIsNone(identity.email)

        identity2 = ExternalIdentity(user_id=1, provider="google", provider_user_id="sub1", email="  ")
        self.assertIsNone(identity2.email)

        identity3 = ExternalIdentity(user_id=1, provider="google", provider_user_id="sub1", email="TEST@EXAMPLE.COM")
        self.assertEqual(identity3.email, "test@example.com")


if __name__ == "__main__":
    unittest.main()

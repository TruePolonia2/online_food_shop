"""Order service coordinating checkout workflows, inventory management, and order lifecycle."""
import sqlite3

from app.database.connection import get_db, transaction
from app.models.cart import Cart
from app.models.order import Order, OrderStatus
from app.models.order_item import OrderItem
from app.repositories.category_repository import CategoryRepository
from app.repositories.customer_repository import CustomerRepository
from app.repositories.order_repository import OrderRepository
from app.repositories.product_repository import ProductRepository
from app.repositories.user_repository import UserRepository


class OrderService:
    """Service handling checkout, atomic inventory deduction, and order status transitions."""

    def __init__(
        self,
        order_repo: OrderRepository | None = None,
        product_repo: ProductRepository | None = None,
        customer_repo: CustomerRepository | None = None,
        user_repo: UserRepository | None = None,
        conn: sqlite3.Connection | None = None,
        category_repo: CategoryRepository | None = None,
    ) -> None:
        """Initialize OrderService with injected repositories and connection.

        Args:
            order_repo: OrderRepository instance.
            product_repo: ProductRepository instance.
            customer_repo: CustomerRepository instance.
            user_repo: UserRepository instance.
            conn: Optional database connection for transaction management.
            category_repo: Optional CategoryRepository instance.
        """
        self.order_repo = order_repo or OrderRepository(conn)
        self.product_repo = product_repo or ProductRepository(conn)
        self.customer_repo = customer_repo or CustomerRepository(conn)
        self.user_repo = user_repo or UserRepository(conn)
        self.category_repo = category_repo or CategoryRepository(conn)
        self._conn = conn

    @property
    def conn(self) -> sqlite3.Connection:
        """Get the active database connection."""
        if self._conn is not None:
            return self._conn
        return get_db()

    def get_by_id(self, order_id: int) -> Order | None:
        """Retrieve an order by ID with line items loaded.

        Args:
            order_id: The primary key ID of the order.

        Returns:
            The Order entity if found, None otherwise.
        """
        return self.order_repo.get_by_id(order_id)

    def get_customer_orders(self, customer_id: int) -> list[Order]:
        """Retrieve all orders placed by a specific customer.

        Args:
            customer_id: The ID of the customer.

        Returns:
            A list of Order entities.

        Raises:
            ValueError: If the customer does not exist.
        """
        if self.customer_repo.get_by_id(customer_id) is None:
            raise ValueError(f"Customer with ID {customer_id} not found.")

        return self.order_repo.get_by_customer_id(customer_id)

    def get_all_orders(self) -> list[Order]:
        """Retrieve all orders in the shop.

        Returns:
            A list of Order entities.
        """
        return self.order_repo.get_all()

    def get_user_orders(self, user_id: int) -> list[Order]:
        """Retrieve all orders placed by an authenticated user account.

        Args:
            user_id: The ID of the authenticated user.

        Returns:
            A list of Order entities ordered by newest first.

        Raises:
            ValueError: If user_id is invalid.
        """
        if isinstance(user_id, bool) or not isinstance(user_id, int) or user_id <= 0:
            raise ValueError(f"Invalid user ID: {user_id}")
        return self.order_repo.get_by_user_id(user_id)

    def get_user_order(self, order_id: int, user_id: int) -> Order | None:
        """Retrieve a specific order owned by an authenticated user.

        Args:
            order_id: The ID of the order.
            user_id: The ID of the authenticated user.

        Returns:
            The Order entity if found and owned by user_id, None otherwise.
        """
        if isinstance(order_id, bool) or not isinstance(order_id, int) or order_id <= 0:
            return None
        if isinstance(user_id, bool) or not isinstance(user_id, int) or user_id <= 0:
            return None
        return self.order_repo.get_by_id_for_user(order_id, user_id)

    def checkout(
        self,
        customer_id: int,
        cart: Cart,
        customer_name: str | None = None,
        customer_email: str | None = None,
        customer_phone: str | None = None,
        customer_address: str | None = None,
        user_id: int | None = None,
    ) -> Order:
        """Execute atomic checkout: validates stock, creates order, and decrements inventory.

        Saves each product's unit price as a permanent snapshot in order_items.
        Preserves customer/delivery snapshot on the order independently from the
        customer profile to guarantee privacy and historical accuracy.
        If user_id is supplied, associates the newly created order with the authenticated
        user account; otherwise creates an unclaimed guest order.
        If any item has insufficient stock, is inactive, or fails to persist,
        the entire transaction is rolled back leaving stock and orders unmodified.

        Args:
            customer_id: The purchasing customer ID.
            cart: The in-memory Cart instance containing items to order.
            customer_name: Optional delivery recipient name. Defaults to customer.name.
            customer_email: Optional contact email. Defaults to customer.email.
            customer_phone: Optional contact phone. Defaults to customer.phone.
            customer_address: Optional delivery address. Defaults to customer.address.
            user_id: Optional authenticated user account ID owning the order.

        Returns:
            The newly created and persisted Order entity.

        Raises:
            ValueError: If cart is empty, customer does not exist, user_id is invalid
                or not found, products are inactive, or requested quantities exceed stock.
        """
        if cart.is_empty:
            raise ValueError("Cannot checkout with an empty cart.")

        customer = self.customer_repo.get_by_id(customer_id)
        if customer is None:
            raise ValueError(f"Customer with ID {customer_id} not found.")

        if user_id is not None:
            if isinstance(user_id, bool) or not isinstance(user_id, int) or user_id <= 0:
                raise ValueError(f"Invalid user ID: {user_id}")
            if self.user_repo.get_by_id(user_id) is None:
                raise ValueError(f"User with ID {user_id} not found.")

        # Determine delivery snapshot details, defaulting to customer record if omitted
        snap_name = (
            customer_name.strip()
            if customer_name and customer_name.strip()
            else customer.name
        )
        snap_email = (
            customer_email.strip().lower()
            if customer_email and customer_email.strip()
            else customer.email
        )
        snap_phone = (
            customer_phone.strip()
            if customer_phone and customer_phone.strip()
            else customer.phone
        )
        snap_address = (
            customer_address.strip()
            if customer_address and customer_address.strip()
            else customer.address
        )

        with transaction(self.conn):
            order_items: list[OrderItem] = []

            for cart_item in cart.items:
                product = self.product_repo.get_by_id(cart_item.product.id)
                if product is None:
                    raise ValueError(
                        f"Product '{cart_item.product.name}' is no longer available in the catalog."
                    )

                if not product.is_active:
                    raise ValueError(
                        f"Product '{product.name}' is currently inactive and cannot be purchased."
                    )

                if product.category_id is not None:
                    cat = self.category_repo.get_by_id(product.category_id)
                    if cat is not None and not cat.is_active:
                        raise ValueError(
                            f"Product '{product.name}' belongs to an inactive category and cannot be purchased."
                        )

                if (
                    isinstance(cart_item.quantity, bool)
                    or not isinstance(cart_item.quantity, int)
                    or cart_item.quantity <= 0
                ):
                    raise ValueError(
                        f"Invalid quantity {cart_item.quantity} for product '{product.name}'."
                    )

                if product.stock_quantity < cart_item.quantity:
                    raise ValueError(
                        f"Insufficient stock for '{product.name}'. "
                        f"Available: {product.stock_quantity}, requested: {cart_item.quantity}."
                    )

                # Atomically decrement inventory within the transaction
                product.stock_quantity -= cart_item.quantity
                self.product_repo.update(product)

                # Capture price snapshot at checkout time in minor currency units (cents)
                order_item = OrderItem(
                    product_id=product.id,
                    product_name=product.name,
                    unit_price=product.price,
                    quantity=cart_item.quantity,
                )
                order_items.append(order_item)

            # Persist order and line items with immutable delivery snapshot
            order = Order(
                customer_id=customer_id,
                user_id=user_id,
                status=OrderStatus.PENDING,
                items=order_items,
                customer_name=snap_name,
                customer_email=snap_email,
                customer_phone=snap_phone,
                customer_address=snap_address,
            )
            saved_order = self.order_repo.create(order)

        # Reset in-memory cart only after successful transaction completion
        cart.clear()
        return saved_order

    def update_order_status(
        self, order_id: int, new_status: OrderStatus | str
    ) -> Order:
        """Update an order's lifecycle status.

        If transitioning an active order to CANCELLED, restores inventory stock
        for all line items within the transaction.

        Args:
            order_id: The ID of the order to update.
            new_status: The target OrderStatus enum value or valid string.

        Returns:
            The updated Order entity.

        Raises:
            ValueError: If order not found, invalid status value, or invalid transition.
        """
        order = self.order_repo.get_by_id(order_id)
        if order is None:
            raise ValueError(f"Order with ID {order_id} not found.")

        target_status = (
            new_status
            if isinstance(new_status, OrderStatus)
            else OrderStatus(new_status)
        )

        if order.status == target_status:
            return order

        if order.status == OrderStatus.CANCELLED:
            raise ValueError("Cannot update the status of an already cancelled order.")

        if order.status == OrderStatus.DELIVERED:
            raise ValueError("Cannot update the status of an already delivered order.")

        with transaction(self.conn):
            # If cancelling an active order, restore inventory
            if target_status == OrderStatus.CANCELLED:
                for item in order.items:
                    product = self.product_repo.get_by_id(item.product_id)
                    if product is not None:
                        product.stock_quantity += item.quantity
                        self.product_repo.update(product)

            self.order_repo.update_status(order_id, target_status)
            order.status = target_status
            return order

    def cancel_order(self, order_id: int) -> Order:
        """Cancel an order and return reserved inventory to stock.

        Args:
            order_id: The ID of the order to cancel.

        Returns:
            The cancelled Order entity with status CANCELLED.
        """
        return self.update_order_status(order_id, OrderStatus.CANCELLED)

"""Cart service managing transient shopping cart operations and stock availability."""
from app.models.cart import Cart
from app.repositories.product_repository import ProductRepository


class CartService:
    """Service handling in-memory shopping cart operations and availability validation."""

    def __init__(self, product_repo: ProductRepository | None = None) -> None:
        """Initialize CartService with injected product repository.

        Args:
            product_repo: ProductRepository instance. Defaults to a new instance.
        """
        self.product_repo = product_repo or ProductRepository()

    def create_cart(self, customer_id: int | None = None) -> Cart:
        """Create a new in-memory Cart instance.

        Args:
            customer_id: Optional ID of the customer owning the cart.

        Returns:
            A new Cart entity.
        """
        return Cart(customer_id=customer_id)

    def add_item(self, cart: Cart, product_id: int, quantity: int = 1) -> Cart:
        """Add a product to the cart after validating availability and stock limits.

        Args:
            cart: The Cart instance to update.
            product_id: ID of the product to add.
            quantity: Quantity to add. Defaults to 1.

        Returns:
            The updated Cart entity.

        Raises:
            ValueError: If quantity <= 0, quantity is a boolean/non-integer, product is not found, product is inactive,
                or total requested quantity exceeds available stock.
        """
        if (
            isinstance(quantity, bool)
            or not isinstance(quantity, int)
            or quantity <= 0
        ):
            raise ValueError("Quantity to add must be greater than zero.")

        product = self.product_repo.get_by_id(product_id)
        if product is None:
            raise ValueError(f"Product with ID {product_id} not found.")

        if not product.is_active:
            raise ValueError(f"Product '{product.name}' is currently unavailable.")

        existing_qty = 0
        for item in cart.items:
            if item.product.id == product_id:
                existing_qty = item.quantity
                break

        requested_total = existing_qty + quantity
        if requested_total > product.stock_quantity:
            raise ValueError(
                f"Cannot add {quantity} of '{product.name}'. "
                f"Requested total ({requested_total}) exceeds available stock ({product.stock_quantity})."
            )

        cart.add_item(product, quantity)
        return cart

    def update_item_quantity(self, cart: Cart, product_id: int, quantity: int) -> Cart:
        """Update the quantity of a product in the cart.

        Args:
            cart: The Cart instance.
            product_id: Product ID.
            quantity: New quantity. If 0, removes the product from the cart.

        Returns:
            The updated Cart entity.

        Raises:
            ValueError: If quantity < 0, product is not found, product is inactive,
                or quantity exceeds available stock.
        """
        if (
            isinstance(quantity, bool)
            or not isinstance(quantity, int)
            or quantity < 0
        ):
            raise ValueError("Cart item quantity cannot be negative.")

        if quantity == 0:
            cart.remove_item(product_id)
            return cart

        product = self.product_repo.get_by_id(product_id)
        if product is None:
            raise ValueError(f"Product with ID {product_id} not found.")

        if not product.is_active:
            raise ValueError(f"Product '{product.name}' is currently unavailable.")

        if quantity > product.stock_quantity:
            raise ValueError(
                f"Requested quantity ({quantity}) for '{product.name}' "
                f"exceeds available stock ({product.stock_quantity})."
            )

        cart.update_quantity(product_id, quantity)
        return cart

    def remove_item(self, cart: Cart, product_id: int) -> Cart:
        """Remove a product from the cart.

        Args:
            cart: The Cart instance.
            product_id: ID of the product to remove.

        Returns:
            The updated Cart entity.
        """
        cart.remove_item(product_id)
        return cart

    def clear_cart(self, cart: Cart) -> Cart:
        """Empty all items from the cart.

        Args:
            cart: The Cart instance to clear.

        Returns:
            The emptied Cart entity.
        """
        cart.clear()
        return cart

    def validate_cart(self, cart: Cart) -> list[str]:
        """Validate all items in the cart against current catalog availability and stock.

        Args:
            cart: The Cart instance to check.

        Returns:
            A list of validation error messages. Empty list if cart is fully valid.
        """
        errors: list[str] = []
        for item in cart.items:
            product = self.product_repo.get_by_id(item.product.id)
            if product is None:
                errors.append(f"Product '{item.product.name}' no longer exists.")
            elif not product.is_active:
                errors.append(f"Product '{product.name}' is no longer active.")
            elif item.quantity > product.stock_quantity:
                errors.append(
                    f"Requested quantity ({item.quantity}) for '{product.name}' "
                    f"exceeds available stock ({product.stock_quantity})."
                )
        return errors

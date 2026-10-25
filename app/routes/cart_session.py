"""Session-based cart persistence helpers for the web route layer."""
from flask import session

from app.models.cart import Cart
from app.services.cart_service import CartService
from app.services.product_service import ProductService

SESSION_CART_KEY = "cart"


def get_session_cart_data() -> dict[str, int]:
    """Retrieve raw cart mapping from the Flask session.

    Returns:
        A dictionary mapping string product IDs to integer quantities.
    """
    data = session.get(SESSION_CART_KEY)
    if not isinstance(data, dict):
        return {}
    return data


def load_cart_from_session(
    cart_service: CartService, product_service: ProductService
) -> Cart:
    """Reconstruct an in-memory Cart domain model from the session cart data.

    Validates that each product still exists and is active in the catalog.
    Stale or invalid cart entries are purged from the session automatically.

    Args:
        cart_service: CartService instance to create the cart domain object.
        product_service: ProductService instance to look up current product details.

    Returns:
        A populated Cart domain model instance.
    """
    cart = cart_service.create_cart()
    raw_data = get_session_cart_data()

    cleaned_data: dict[str, int] = {}
    modified = False

    for pid_str, qty in raw_data.items():
        if isinstance(qty, bool) or not isinstance(qty, int) or qty <= 0:
            modified = True
            continue

        try:
            product_id = int(pid_str)
        except (ValueError, TypeError):
            modified = True
            continue

        product = product_service.get_by_id(product_id)
        if product is None or not product.is_active:
            modified = True
            continue

        # Add valid product to domain cart
        cart.add_item(product, qty)
        cleaned_data[str(product.id)] = qty

    if modified or raw_data != cleaned_data:
        session[SESSION_CART_KEY] = cleaned_data
        session.modified = True

    return cart


def save_cart_to_session(cart: Cart) -> None:
    """Serialize the domain Cart items into the Flask session.

    Stores only primitive string product IDs and integer quantities.

    Args:
        cart: The Cart domain model instance to serialize.
    """
    serialized: dict[str, int] = {
        str(item.product.id): item.quantity
        for item in cart.items
        if item.product.id is not None
    }
    session[SESSION_CART_KEY] = serialized
    session.modified = True


def clear_cart_session() -> None:
    """Remove the shopping cart data from the session."""
    session.pop(SESSION_CART_KEY, None)
    session.modified = True

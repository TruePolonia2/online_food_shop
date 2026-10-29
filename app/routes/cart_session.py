"""Session-based cart persistence helpers for the web route layer."""
from flask import session

from app.models.cart import Cart
from app.services.cart_service import CartService
from app.services.product_service import ProductService

SESSION_CART_KEY = "cart"


def count_cart_items(data: object) -> int:
    """Calculate the total quantity of items from raw cart data.

    Safely sums positive integer quantities, explicitly rejecting booleans
    and non-integer values. Does not mutate inputs or query the database.

    Args:
        data: Raw cart mapping, expected to map product IDs to integer quantities.

    Returns:
        The total count of valid items as a non-negative integer.
    """
    if not isinstance(data, dict):
        return 0

    total = 0
    for qty in data.values():
        # In Python, bool is a subclass of int (isinstance(True, int) is True).
        # We must explicitly reject booleans.
        if isinstance(qty, bool) or not isinstance(qty, int):
            continue
        if qty > 0:
            total += qty

    return total


def get_cart_item_count() -> int:
    """Return the total quantity of valid items in the session cart.

    Safe to call within or outside a request context. Does not mutate the session
    or perform database queries.

    Returns:
        The total number of items in the current session cart.
    """
    from flask import has_request_context

    if not has_request_context():
        return 0
    return count_cart_items(session.get(SESSION_CART_KEY))


def get_session_cart_data() -> dict[str, int]:
    """Retrieve raw cart mapping from the Flask session.

    Returns:
        A dictionary mapping string product IDs to integer quantities.
    """
    data = session.get(SESSION_CART_KEY)
    if not isinstance(data, dict):
        return {}
    return data


def extract_sanitized_cart_data(data: object) -> dict[str, int]:
    """Extract a validated, safe cart dictionary mapping string product IDs to positive integer quantities.

    Explicitly rejects booleans, non-integer quantities, non-positive quantities,
    and invalid product ID keys. Does not mutate the input object.

    Args:
        data: Raw cart mapping, typically retrieved from session.

    Returns:
        A sanitized dictionary mapping normalized string product IDs to integer quantities.
    """
    if not isinstance(data, dict):
        return {}

    sanitized: dict[str, int] = {}
    for pid_raw, qty in data.items():
        if isinstance(qty, bool) or not isinstance(qty, int) or qty <= 0:
            continue
        try:
            pid_int = int(pid_raw)
            if pid_int <= 0:
                continue
            sanitized[str(pid_int)] = qty
        except (ValueError, TypeError):
            continue

    return sanitized


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
    """Remove the shopping cart data and associated checkout tokens from the session."""
    session.pop(SESSION_CART_KEY, None)
    session.pop("checkout_token", None)
    session.pop("cart_fingerprint", None)
    session.modified = True

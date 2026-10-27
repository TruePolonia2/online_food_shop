"""Validation and parsing helpers for catalog administration."""
from typing import Any


def parse_euro_to_cents(value: Any) -> int:
    """Parse a human-friendly euro price string into integer cents without binary floating point.

    Accepts formats:
        '12' -> 1200
        '12.5' -> 1250
        '12.50' -> 1250
        '12,50' -> 1250
        '0.99' -> 99
        '0' -> 0

    Rejects:
        - Booleans (True, False)
        - Non-string/non-numeric representations
        - Negative prices
        - More than 2 decimal digits (e.g. '12.555')
        - Multiple decimal separators or malformed characters

    Args:
        value: Input string or integer to parse.

    Returns:
        The price in minor currency units (integer cents).

    Raises:
        ValueError: If the price is missing, malformed, negative, or contains excess precision.
    """
    if isinstance(value, bool):
        raise ValueError("Price cannot be a boolean.")

    if isinstance(value, int):
        if value < 0:
            raise ValueError("Price cannot be negative.")
        return value * 100

    if not isinstance(value, str):
        raise ValueError("Price must be a valid euro amount string.")

    cleaned = value.strip()
    if not cleaned:
        raise ValueError("Price is required.")

    if cleaned.startswith("-"):
        raise ValueError("Price cannot be negative.")

    if "," in cleaned and "." in cleaned:
        raise ValueError("Price cannot contain both commas and dots as decimal separators.")

    normalized = cleaned.replace(",", ".")
    parts = normalized.split(".")

    if len(parts) > 2:
        raise ValueError("Invalid price format.")

    euros_str = parts[0]
    if not euros_str or not euros_str.isdigit():
        raise ValueError("Invalid price format.")

    euros = int(euros_str)

    if len(parts) == 2:
        cents_str = parts[1]
        if not cents_str or not cents_str.isdigit():
            raise ValueError("Invalid price format.")
        if len(cents_str) > 2:
            raise ValueError("Price cannot have more than 2 decimal places.")
        if len(cents_str) == 1:
            cents = int(cents_str) * 10
        else:
            cents = int(cents_str)
    else:
        cents = 0

    total_cents = euros * 100 + cents
    if total_cents < 0:
        raise ValueError("Price cannot be negative.")
    return total_cents


def parse_stock_quantity(value: Any) -> int:
    """Parse inventory stock quantity into a non-negative integer.

    Rejects:
        - Booleans (True, False)
        - Floating point/decimal representations (e.g. '10.5')
        - Negative numbers
        - Non-numeric strings

    Args:
        value: Input string or integer.

    Returns:
        The non-negative integer stock quantity.

    Raises:
        ValueError: If stock quantity is missing, negative, decimal, or malformed.
    """
    if isinstance(value, bool):
        raise ValueError("Stock quantity cannot be a boolean.")

    if isinstance(value, int):
        if value < 0:
            raise ValueError("Stock quantity cannot be negative.")
        return value

    if not isinstance(value, str):
        raise ValueError("Stock quantity must be a non-negative integer.")

    cleaned = value.strip()
    if not cleaned:
        raise ValueError("Stock quantity is required.")

    if not cleaned.isdigit():
        raise ValueError("Stock quantity must be a non-negative whole integer.")

    stock = int(cleaned)
    if stock < 0:
        raise ValueError("Stock quantity cannot be negative.")
    return stock


def validate_product_image_url(url: Any) -> str | None:
    """Validate and sanitize an optional product image URL or local static path.

    Allowed:
        - None or empty string (returns None)
        - Local static paths: starts with '/' (e.g. '/static/images/apple.png')
        - Remote URLs: starts with 'https://' or 'http://'

    Forbidden:
        - Dangerous schemes: 'javascript:', 'data:', 'vbscript:', 'file:'
        - Control or injection characters

    Args:
        url: The image URL or static path.

    Returns:
        Sanitized image URL/path string, or None if omitted.

    Raises:
        ValueError: If the URL uses a dangerous scheme or has an invalid structure.
    """
    if url is None:
        return None

    if not isinstance(url, str):
        raise ValueError("Image URL must be a string or None.")

    cleaned = url.strip()
    if not cleaned:
        return None

    lower = cleaned.lower()
    dangerous_schemes = ("javascript:", "data:", "vbscript:", "file:")
    for scheme in dangerous_schemes:
        if scheme in lower:
            raise ValueError(f"Dangerous URL scheme '{scheme}' is not permitted.")

    if not (cleaned.startswith("/") or lower.startswith("https://") or lower.startswith("http://")):
        raise ValueError(
            "Product image must be a relative static path (e.g. '/static/...') or a valid http(s) URL."
        )

    # Reject quotes and tags that could break HTML attribute rendering
    if any(char in cleaned for char in ('"', "'", "<", ">", "\n", "\r", "\t")):
        raise ValueError("Image path contains forbidden characters.")

    return cleaned

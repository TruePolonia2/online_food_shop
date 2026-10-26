"""Currency and presentation formatting helpers."""


def format_euro(cents: int | None) -> str:
    """Format an integer amount of cents into Portuguese euro currency string.

    Uses a comma as the decimal separator, space as thousands separator,
    and a space before the euro symbol (€).

    Examples:
        450 -> '4,50 €'
        1200 -> '12,00 €'
        99 -> '0,99 €'
        0 -> '0,00 €'
        125000 -> '1 250,00 €'
        -450 -> '-4,50 €'

    Args:
        cents: Monetary value in minor units (cents). If None, defaults to 0.

    Returns:
        Formatted string in Portuguese euro format.
    """
    if cents is None:
        cents = 0

    negative = cents < 0
    abs_cents = abs(cents)
    euros = abs_cents // 100
    remainder = abs_cents % 100

    euros_str = f"{euros:,}".replace(",", " ")
    result = f"{euros_str},{remainder:02d} €"
    if negative:
        return f"-{result}"
    return result

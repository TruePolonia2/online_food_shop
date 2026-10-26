"""HTTP route blueprints and request handlers."""
from app.routes.auth import auth_bp, get_current_user, get_current_user_id, is_safe_redirect_url
from app.routes.cart import cart_bp
from app.routes.catalog import catalog_bp
from app.routes.order import order_bp

__all__ = [
    "auth_bp",
    "cart_bp",
    "catalog_bp",
    "get_current_user",
    "get_current_user_id",
    "is_safe_redirect_url",
    "order_bp",
]


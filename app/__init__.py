"""Application factory for the online food shop application."""
from flask import Flask

from app.config import Config
from app.database.connection import init_app as init_db_app


def create_app(config_class=Config) -> Flask:
    """Create and configure an instance of the Flask application."""
    app = Flask(__name__)
    app.config.from_object(config_class)

    init_db_app(app)

    from app.csrf import init_csrf
    from app.formatting import format_euro

    init_csrf(app)
    app.jinja_env.filters["format_euro"] = format_euro

    from app.routes.cart_session import get_cart_item_count

    @app.context_processor
    def inject_cart_count() -> dict[str, int]:
        count = get_cart_item_count()
        return {
            "cart_item_count": count,
            "cart_count": count,
        }

    from app.routes.cart import cart_bp
    from app.routes.catalog import catalog_bp
    from app.routes.order import order_bp

    app.register_blueprint(catalog_bp)
    app.register_blueprint(cart_bp, url_prefix="/cart")
    app.register_blueprint(order_bp)

    return app

"""Application factory for the online food shop application."""
from flask import Flask

from app.config import Config
from app.database.connection import init_app as init_db_app


def create_app(config_class=Config) -> Flask:
    """Create and configure an instance of the Flask application."""
    app = Flask(__name__)
    app.config.from_object(config_class)

    init_db_app(app)

    from app.routes.cart import cart_bp
    from app.routes.catalog import catalog_bp
    from app.routes.order import order_bp

    app.register_blueprint(catalog_bp)
    app.register_blueprint(cart_bp, url_prefix="/cart")
    app.register_blueprint(order_bp)

    return app

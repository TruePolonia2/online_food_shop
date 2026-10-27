"""Application factory for the online food shop application."""
from flask import Flask

from app.config import CONFIG_MAP, Config, get_config_by_name
from app.database.connection import init_app as init_db_app


def create_app(config_class=None) -> Flask:
    """Create and configure an instance of the Flask application."""
    if config_class is None:
        config_class = get_config_by_name()
    elif isinstance(config_class, str):
        normalized = config_class.strip().lower()
        if normalized in CONFIG_MAP:
            config_class = CONFIG_MAP[normalized]
        elif "." in config_class:
            try:
                from werkzeug.utils import import_string

                config_class = import_string(config_class)
            except (ImportError, AttributeError) as exc:
                raise ValueError(
                    f"Could not import configuration '{config_class}': {exc}"
                ) from exc
        else:
            valid_options = ", ".join(f"'{k}'" for k in sorted(CONFIG_MAP.keys()))
            raise ValueError(
                f"Unknown configuration name '{config_class}'. Valid options are: {valid_options}."
            )

    app = Flask(__name__)
    app.config.from_object(config_class)

    init_db_app(app)

    from app.cli import init_cli
    init_cli(app)

    from app.csrf import init_csrf
    from app.formatting import format_euro
    from app.oauth import init_oauth, is_google_auth_configured

    init_csrf(app)
    init_oauth(app)
    app.jinja_env.filters["format_euro"] = format_euro

    from app.routes.auth import auth_bp, get_current_user
    from app.routes.cart_session import get_cart_item_count

    @app.context_processor
    def inject_template_context() -> dict:
        count = get_cart_item_count()
        return {
            "cart_item_count": count,
            "cart_count": count,
            "current_user": get_current_user(),
            "google_auth_enabled": is_google_auth_configured(),
        }

    from app.routes.account import account_bp
    from app.routes.admin import admin_bp
    from app.routes.cart import cart_bp
    from app.routes.catalog import catalog_bp
    from app.routes.order import order_bp

    app.register_blueprint(catalog_bp)
    app.register_blueprint(cart_bp, url_prefix="/cart")
    app.register_blueprint(order_bp)
    app.register_blueprint(auth_bp)
    app.register_blueprint(account_bp)
    app.register_blueprint(admin_bp)

    return app

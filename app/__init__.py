"""Application factory for the online food shop application."""
from flask import Flask

from app.config import Config


def create_app(config_class=Config) -> Flask:
    """Create and configure an instance of the Flask application."""
    app = Flask(__name__)
    app.config.from_object(config_class)

    return app

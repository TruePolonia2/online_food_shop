"""Configuration settings for the online food shop application."""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


class Config:
    """Base development configuration."""

    SECRET_KEY = os.environ.get("SECRET_KEY", "dev-secret-key-change-in-production")
    DATABASE_PATH = os.environ.get(
        "DATABASE_PATH",
        str(BASE_DIR / "instance" / "online_food_shop.sqlite3"),
    )
    TESTING = False
    DEBUG = True
    CSRF_ENABLED = True

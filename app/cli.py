"""Flask CLI management commands for store administration."""
import os
import sys
import click
from flask import Flask, current_app
from flask.cli import with_appcontext

from app.config import is_production_env
from app.database.connection import get_db, transaction
from app.models.category import Category
from app.models.product import Product
from app.models.user import User, UserRole
from app.repositories.category_repository import CategoryRepository
from app.repositories.product_repository import ProductRepository
from app.repositories.user_repository import UserRepository
from app.services.auth_service import AuthService


@click.command("create-owner")
@click.option(
    "--email",
    prompt="Owner email",
    help="Email address for the store owner account.",
)
@with_appcontext
def create_owner_command(email: str) -> None:
    """Explicitly bootstrap or create a verified store owner account.

    Prompts securely for password without echoing to the terminal. Never accepts
    passwords as command-line arguments. Rejects duplicate emails and will not
    silently promote existing customer accounts.
    """
    cleaned_email = email.strip().lower()
    if not cleaned_email or "@" not in cleaned_email:
        click.echo("Error: A valid user email address is required.", err=True)
        sys.exit(1)

    parts = cleaned_email.split("@")
    if len(parts) != 2 or not parts[0] or not parts[1]:
        click.echo("Error: A valid user email address is required.", err=True)
        sys.exit(1)

    user_repo = UserRepository()
    if user_repo.email_exists(cleaned_email):
        click.echo(
            f"Error: An account with email '{cleaned_email}' already exists. "
            "Existing accounts cannot be automatically promoted to owner. "
            "Please specify a distinct email address.",
            err=True,
        )
        sys.exit(1)

    # Prompt securely for password with confirmation (never via command-line arguments)
    try:
        password = click.prompt("Password", hide_input=True, confirmation_prompt=True)
    except click.Abort:
        click.echo("\nOperation aborted by operator.", err=True)
        sys.exit(1)

    # Validate password against central AuthService policy
    try:
        AuthService.validate_password(password)
    except ValueError as val_err:
        click.echo(f"Error: {val_err}", err=True)
        sys.exit(1)

    # Hash using central AuthService Werkzeug scrypt hashing
    password_hash = AuthService.hash_password(password)

    # Construct owner entity with verified status (administrative trust decision)
    owner_user = User(
        email=cleaned_email,
        password_hash=password_hash,
        role=UserRole.OWNER,
        is_verified=True,
    )

    try:
        with transaction(user_repo.conn):
            created_user = user_repo.create(owner_user)
    except Exception as exc:
        click.echo(f"Error: Database failure while creating owner: {exc}", err=True)
        sys.exit(1)

    click.echo(f"Store owner successfully created: {created_user.email}")


@click.command("seed-demo")
@click.option(
    "--force",
    is_flag=True,
    default=False,
    help="Force seeding on a non-empty database if not in production.",
)
@with_appcontext
def seed_demo_command(force: bool) -> None:
    """Safely seed the catalog with fictional demonstration categories and products.

    - Explicitly invoked (never runs automatically on startup).
    - Idempotent: running repeatedly skips existing categories and products by name.
    - Non-destructive: preserves existing records and never alters schema or orders.
    - Transaction-safe: runs within an atomic transaction with rollback on failure.
    - Environment-guarded: strictly refuses execution in production environments
      or on databases containing existing orders unless explicitly in demo mode.
    """
    # 1. Strict production refusal
    if is_production_env() or current_app.config.get("ENV_NAME") == "production":
        click.echo("Error: 'seed-demo' is strictly prohibited in production environments.", err=True)
        sys.exit(1)

    db = get_db()

    # 2. Safety guard: check if database contains orders or real customer activity
    has_orders = False
    try:
        cursor = db.execute("SELECT COUNT(*) AS count FROM orders")
        row = cursor.fetchone()
        has_orders = bool(row and row["count"] > 0)
    except Exception:
        has_orders = False

    is_demo_mode = (
        force
        or os.environ.get("DEMO_MODE", "").strip().lower() in ("1", "true", "yes")
        or os.environ.get("ALLOW_DEMO_SEED", "").strip().lower() in ("1", "true", "yes")
    )

    if has_orders and not is_demo_mode:
        click.echo(
            "Refusing to seed: the active database contains existing order records.\n"
            "To protect existing customer and order data, 'seed-demo' requires a clean demo\n"
            "database or explicit confirmation. Please configure a dedicated demo database\n"
            "via DATABASE_PATH (e.g. instance/demo_food_shop.sqlite3) or pass --force.",
            err=True,
        )
        sys.exit(1)

    category_repo = CategoryRepository(db)
    product_repo = ProductRepository(db)

    categories_created = 0
    categories_skipped = 0
    products_created = 0
    products_skipped = 0

    # 4 Fictional Categories
    DEMO_CATEGORIES = [
        {
            "name": "Fresh Bakery",
            "description": "Handcrafted artisan breads, sourdoughs, and golden morning pastries baked fresh daily.",
        },
        {
            "name": "Organic Produce",
            "description": "Locally grown seasonal fruits, crisp greens, and organic farm-fresh vegetables.",
        },
        {
            "name": "Artisan Dairy",
            "description": "Traditional pasture-raised cow and sheep milk cheeses, yogurts, and farm butter.",
        },
        {
            "name": "Pantry & Olive Oils",
            "description": "Cold-pressed extra virgin olive oils, wildflower honey, and gourmet Mediterranean staples.",
        },
    ]

    # 8 Realistic Fictional Products with integer cent prices and positive stock
    DEMO_PRODUCTS = [
        {
            "name": "Artisan Sourdough Loaf",
            "category_name": "Fresh Bakery",
            "price": 350,  # 3.50 EUR
            "stock_quantity": 15,
            "description": "Naturally fermented sourdough with a blistered golden crust and light, airy crumb. Baked fresh every morning.",
        },
        {
            "name": "Butter Croissant 4-Pack",
            "category_name": "Fresh Bakery",
            "price": 420,  # 4.20 EUR
            "stock_quantity": 20,
            "description": "Flaky, multi-layered French butter croissants with a delicate, melt-in-the-mouth texture.",
        },
        {
            "name": "Organic Hass Avocados (Pack of 3)",
            "category_name": "Organic Produce",
            "price": 299,  # 2.99 EUR
            "stock_quantity": 25,
            "description": "Creamy, ripe organic Hass avocados harvested at peak freshness. Rich in healthy monounsaturated fats.",
        },
        {
            "name": "Sweet Cherry Tomatoes 500g",
            "category_name": "Organic Produce",
            "price": 210,  # 2.10 EUR
            "stock_quantity": 30,
            "description": "Vibrant sun-ripened cherry tomatoes bursting with natural sweetness and aromatic flavor.",
        },
        {
            "name": "Farm Fresh Whole Milk 1L",
            "category_name": "Artisan Dairy",
            "price": 125,  # 1.25 EUR
            "stock_quantity": 40,
            "description": "Creamy whole milk from pasture-grazed regional dairy cattle, gently pasteurized to preserve natural nutrients.",
        },
        {
            "name": "Artisan Sheep Cheese 250g",
            "category_name": "Artisan Dairy",
            "price": 580,  # 5.80 EUR
            "stock_quantity": 12,
            "description": "Traditional cured Portuguese sheep cheese with a velvety semi-soft paste and distinctive herbal notes.",
        },
        {
            "name": "Extra Virgin Olive Oil 500ml",
            "category_name": "Pantry & Olive Oils",
            "price": 750,  # 7.50 EUR
            "stock_quantity": 18,
            "description": "First cold-extracted extra virgin olive oil from centuries-old Alentejo groves, with a peppery, fruity finish.",
        },
        {
            "name": "Organic Raw Honey 350g",
            "category_name": "Pantry & Olive Oils",
            "price": 620,  # 6.20 EUR
            "stock_quantity": 15,
            "description": "Pure unpasteurized wildflower honey gathered from native Mediterranean blooms. Rich in natural floral aromas.",
        },
    ]

    try:
        with transaction(db):
            category_map: dict[str, int] = {}

            # Seed or retrieve categories
            for cat_data in DEMO_CATEGORIES:
                existing_cat = category_repo.get_by_name(cat_data["name"])
                if existing_cat is not None and existing_cat.id is not None:
                    category_map[cat_data["name"]] = existing_cat.id
                    categories_skipped += 1
                else:
                    new_cat = category_repo.create(
                        Category(
                            name=cat_data["name"],
                            description=cat_data["description"],
                            is_active=True,
                        )
                    )
                    category_map[cat_data["name"]] = new_cat.id
                    categories_created += 1

            # Seed or skip products
            for prod_data in DEMO_PRODUCTS:
                existing_prod = product_repo.get_by_name(prod_data["name"])
                if existing_prod is not None:
                    products_skipped += 1
                else:
                    cat_id = category_map[prod_data["category_name"]]
                    product_repo.create(
                        Product(
                            name=prod_data["name"],
                            price=prod_data["price"],
                            stock_quantity=prod_data["stock_quantity"],
                            description=prod_data["description"],
                            category_id=cat_id,
                            is_active=True,
                            image_url=None,
                        )
                    )
                    products_created += 1

    except Exception as exc:
        click.echo(f"Error: Database failure during demo seeding: {exc}", err=True)
        sys.exit(1)

    click.echo("Demo catalog seeding completed successfully:")
    click.echo(f"  Categories: {categories_created} created, {categories_skipped} skipped (already present)")
    click.echo(f"  Products:   {products_created} created, {products_skipped} skipped (already present)")


def init_cli(app: Flask) -> None:
    """Register application management CLI commands with the Flask application.

    Args:
        app: The Flask application instance.
    """
    app.cli.add_command(create_owner_command)
    app.cli.add_command(seed_demo_command)

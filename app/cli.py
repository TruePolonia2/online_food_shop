"""Flask CLI management commands for store administration."""
import sys
import click
from flask import Flask
from flask.cli import with_appcontext

from app.database.connection import transaction
from app.models.user import User, UserRole
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


def init_cli(app: Flask) -> None:
    """Register application management CLI commands with the Flask application.

    Args:
        app: The Flask application instance.
    """
    app.cli.add_command(create_owner_command)

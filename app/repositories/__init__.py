"""Data access repositories for the online food shop application."""
from app.repositories.category_repository import CategoryRepository
from app.repositories.customer_repository import CustomerRepository
from app.repositories.email_verification_token_repository import EmailVerificationTokenRepository
from app.repositories.order_repository import OrderRepository
from app.repositories.product_repository import ProductRepository
from app.repositories.user_repository import UserRepository

__all__ = [
    "CategoryRepository",
    "CustomerRepository",
    "EmailVerificationTokenRepository",
    "OrderRepository",
    "ProductRepository",
    "UserRepository",
]

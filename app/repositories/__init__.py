"""Data access repositories for the online food shop application."""
from app.repositories.account_link_intent_repository import AccountLinkIntentRepository
from app.repositories.category_repository import CategoryRepository
from app.repositories.contact_message_repository import ContactMessageRepository
from app.repositories.customer_repository import CustomerRepository
from app.repositories.email_verification_token_repository import EmailVerificationTokenRepository
from app.repositories.external_identity_repository import ExternalIdentityRepository
from app.repositories.order_repository import OrderRepository
from app.repositories.password_reset_token_repository import PasswordResetTokenRepository
from app.repositories.payment_repository import PaymentRepository
from app.repositories.product_repository import ProductRepository
from app.repositories.user_repository import UserRepository
from app.repositories.webhook_event_repository import WebhookEventRepository

__all__ = [
    "AccountLinkIntentRepository",
    "CategoryRepository",
    "ContactMessageRepository",
    "CustomerRepository",
    "EmailVerificationTokenRepository",
    "ExternalIdentityRepository",
    "OrderRepository",
    "PasswordResetTokenRepository",
    "PaymentRepository",
    "ProductRepository",
    "UserRepository",
    "WebhookEventRepository",
]

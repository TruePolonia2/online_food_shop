"""Authentication and credential management service."""
from datetime import datetime, timedelta, timezone
import hashlib
import logging
import secrets
from typing import Optional
import sqlite3
from werkzeug.security import check_password_hash, generate_password_hash

from app.database.connection import get_db, transaction
from app.models.email_verification_token import EmailVerificationToken
from app.models.user import User, UserRole
from app.repositories.email_verification_token_repository import EmailVerificationTokenRepository
from app.repositories.user_repository import UserRepository
from app.services.email_sender import EmailSender, InMemoryEmailSender

logger = logging.getLogger(__name__)

DEFAULT_TOKEN_VALIDITY_HOURS = 24
DEFAULT_RESEND_COOLDOWN_SECONDS = 60
MIN_PASSWORD_LENGTH = 8
MAX_PASSWORD_LENGTH = 128


class AuthService:
    """Service managing user registration, credential authentication, and email verification."""

    def __init__(
        self,
        user_repo: Optional[UserRepository] = None,
        token_repo: Optional[EmailVerificationTokenRepository] = None,
        email_sender: Optional[EmailSender] = None,
        conn: Optional[sqlite3.Connection] = None,
        token_validity_hours: int = DEFAULT_TOKEN_VALIDITY_HOURS,
        resend_cooldown_seconds: int = DEFAULT_RESEND_COOLDOWN_SECONDS,
    ) -> None:
        """Initialize AuthService with repositories and email sender.

        Args:
            user_repo: Optional UserRepository. If omitted, uses default.
            token_repo: Optional EmailVerificationTokenRepository. If omitted, uses default.
            email_sender: Optional EmailSender. Defaults to InMemoryEmailSender.
            conn: Optional database connection for transaction coordination.
            token_validity_hours: Hours a verification token remains valid (default 24).
            resend_cooldown_seconds: Minimum seconds between resend requests (default 60).
        """
        self._conn = conn
        self._user_repo = user_repo or UserRepository(conn=conn)
        self._token_repo = token_repo or EmailVerificationTokenRepository(conn=conn)
        self._email_sender = email_sender or InMemoryEmailSender()
        self.token_validity_hours = token_validity_hours
        self.resend_cooldown_seconds = resend_cooldown_seconds

    @property
    def conn(self) -> sqlite3.Connection:
        """Get the active database connection."""
        if self._conn is not None:
            return self._conn
        if self._user_repo is not None:
            return self._user_repo.conn
        return get_db()

    @property
    def user_repo(self) -> UserRepository:
        """Return the user repository."""
        return self._user_repo

    @property
    def token_repo(self) -> EmailVerificationTokenRepository:
        """Return the email verification token repository."""
        return self._token_repo

    @property
    def email_sender(self) -> EmailSender:
        """Return the email sender."""
        return self._email_sender

    @staticmethod
    def validate_password(password: str, confirm_password: Optional[str] = None) -> None:
        """Validate password strength, length, and confirmation matching.

        Args:
            password: The plaintext password to validate.
            confirm_password: Optional confirmation password to verify matching.

        Raises:
            ValueError: If password does not satisfy validation rules.
        """
        if not isinstance(password, str):
            raise ValueError("Password must be a string.")

        if len(password) < MIN_PASSWORD_LENGTH:
            raise ValueError(f"Password must be at least {MIN_PASSWORD_LENGTH} characters long.")

        if len(password) > MAX_PASSWORD_LENGTH:
            raise ValueError(f"Password must not exceed {MAX_PASSWORD_LENGTH} characters.")

        if not password.strip():
            raise ValueError("Password cannot consist solely of whitespace.")

        if confirm_password is not None and password != confirm_password:
            raise ValueError("Passwords do not match.")

    @staticmethod
    def hash_password(password: str) -> str:
        """Hash a plaintext password using scrypt.

        Args:
            password: The plaintext password.

        Returns:
            The cryptographic scrypt hash string.
        """
        return generate_password_hash(password, method="scrypt")

    @staticmethod
    def verify_password(password: str, password_hash: Optional[str]) -> bool:
        """Verify a plaintext password against a stored password hash.

        Args:
            password: The plaintext password to check.
            password_hash: The stored hash (can be None).

        Returns:
            True if password matches hash, False otherwise.
        """
        if not password_hash or not isinstance(password_hash, str) or not password_hash.strip():
            return False
        if not password or not isinstance(password, str):
            return False
        return check_password_hash(password_hash, password)

    @staticmethod
    def hash_token(raw_token: str) -> str:
        """Compute the SHA-256 hash of a raw verification token string.

        Args:
            raw_token: The plaintext token.

        Returns:
            Lower-case hexadecimal SHA-256 hash string.
        """
        return hashlib.sha256(raw_token.strip().encode("utf-8")).hexdigest()

    def register_customer(
        self,
        email: str,
        password: str,
        confirm_password: Optional[str] = None,
    ) -> tuple[User, str]:
        """Register a new customer account with credentials and create a verification token.

        Performs user creation and token persistence inside a single database transaction.
        The verification email is dispatched only AFTER the transaction successfully commits.

        Args:
            email: Desired customer account email.
            password: Desired plaintext password.
            confirm_password: Optional confirmation password.

        Returns:
            A tuple of (persisted User, raw verification token string).

        Raises:
            ValueError: If email is invalid, password does not meet criteria,
                or a user with the given email already exists.
        """
        self.validate_password(password, confirm_password)

        if not email or not isinstance(email, str) or not email.strip() or "@" not in email:
            raise ValueError("A valid user email is required.")

        normalized_email = email.strip().lower()

        if self._user_repo.email_exists(normalized_email):
            raise ValueError(f"User with email '{normalized_email}' already exists.")

        password_hash = self.hash_password(password)
        raw_token = secrets.token_urlsafe(32)
        token_hash = self.hash_token(raw_token)
        now_utc = datetime.now(timezone.utc)
        expires_at = now_utc + timedelta(hours=self.token_validity_hours)

        with transaction(self.conn):
            user = User(
                email=normalized_email,
                password_hash=password_hash,
                role=UserRole.CUSTOMER,
                is_verified=False,
                created_at=now_utc,
            )
            persisted_user = self._user_repo.create(user)

            token = EmailVerificationToken(
                user_id=persisted_user.id,
                token_hash=token_hash,
                created_at=now_utc,
                expires_at=expires_at,
            )
            self._token_repo.create(token)

        # Send verification email ONLY after transaction has cleanly committed
        try:
            email_sent = self._email_sender.send_verification_email(persisted_user.email, raw_token)
            if not email_sent:
                logger.warning(
                    "Email verification delivery failed for newly registered user '%s'.",
                    persisted_user.email,
                )
        except Exception:
            logger.exception(
                "Unexpected error dispatching verification email to '%s'.",
                persisted_user.email,
            )

        return persisted_user, raw_token

    def authenticate(self, email: str, password: str) -> Optional[User]:
        """Authenticate user credentials against stored password hash.

        Rejects users without configured credentials (e.g. password_hash is None).

        Args:
            email: Account email.
            password: Submitted plaintext password.

        Returns:
            The authenticated User if credentials are valid, None otherwise.
        """
        if not email or not isinstance(email, str) or not email.strip():
            return None
        if not password or not isinstance(password, str):
            return None

        normalized_email = email.strip().lower()
        user = self._user_repo.get_by_email(normalized_email)
        if user is None:
            return None

        if not user.password_hash:
            return None

        if not self.verify_password(password, user.password_hash):
            return None

        return user

    def verify_email(self, raw_token: str) -> Optional[User]:
        """Verify a user's email address using a single-use token.

        Safely and atomically marks the token as used and sets user.is_verified = True.

        Args:
            raw_token: The plaintext verification token.

        Returns:
            The verified User entity if token is valid and unconsumed, None otherwise.
        """
        if not raw_token or not isinstance(raw_token, str) or not raw_token.strip():
            return None

        token_hash = self.hash_token(raw_token)
        token = self._token_repo.get_by_token_hash(token_hash)
        if token is None or not token.is_valid:
            return None

        now_utc = datetime.now(timezone.utc)
        with transaction(self.conn):
            # Atomic single-use consumption defends against concurrent verification attempts
            marked = self._token_repo.mark_as_used(token.id, used_at=now_utc)
            if not marked:
                return None

            self._user_repo.set_verified(token.user_id, is_verified=True)
            return self._user_repo.get_by_id(token.user_id)

    def request_verification_resend(self, email: str) -> bool:
        """Generate and dispatch a new verification email for an unverified account.

        Enforces cooldown rate-limiting and invalidates previous pending tokens.
        Protects against concurrent resend attempts by acquiring an immediate write
        lock before reading and updating verification tokens.

        Args:
            email: The account email.

        Returns:
            True if a new verification token was generated and dispatched, False if
            the user does not exist or is already verified.

        Raises:
            ValueError: If a verification email was requested too recently (cooldown active).
        """
        if not email or not isinstance(email, str) or not email.strip():
            return False

        normalized_email = email.strip().lower()
        user = self._user_repo.get_by_email(normalized_email)
        if user is None:
            return False

        if user.is_verified:
            return False

        raw_token = secrets.token_urlsafe(32)
        token_hash = self.hash_token(raw_token)

        with transaction(self.conn):
            # Acquire immediate write lock on tokens table to serialize concurrent resend checks
            # and prevent read-then-write deadlocks in SQLite savepoint transactions.
            self.conn.execute("UPDATE email_verification_tokens SET id = id WHERE 0;")

            latest_token = self._token_repo.get_latest_for_user(user.id)
            if latest_token is not None:
                now_utc = datetime.now(timezone.utc)
                elapsed = (now_utc - latest_token.created_at).total_seconds()
                if elapsed < self.resend_cooldown_seconds:
                    remaining = int(self.resend_cooldown_seconds - elapsed) + 1
                    raise ValueError(
                        f"Please wait {remaining} seconds before requesting a new verification email."
                    )

            now_utc = datetime.now(timezone.utc)
            expires_at = now_utc + timedelta(hours=self.token_validity_hours)

            self._token_repo.invalidate_pending_for_user(user.id, invalidation_time=now_utc)
            new_token = EmailVerificationToken(
                user_id=user.id,
                token_hash=token_hash,
                created_at=now_utc,
                expires_at=expires_at,
            )
            self._token_repo.create(new_token)

        # Dispatch email post-commit
        try:
            self._email_sender.send_verification_email(user.email, raw_token)
        except Exception:
            logger.exception(
                "Unexpected error dispatching resend verification email to '%s'.",
                user.email,
            )

        return True

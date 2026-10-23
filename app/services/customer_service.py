"""Customer service coordinating customer registration, profile updates, and email validation."""
from app.models.customer import Customer
from app.repositories.customer_repository import CustomerRepository


class CustomerService:
    """Service handling customer business rules and validations."""

    def __init__(self, customer_repo: CustomerRepository | None = None) -> None:
        """Initialize CustomerService with injected repository.

        Args:
            customer_repo: CustomerRepository instance. Defaults to a new instance.
        """
        self.customer_repo = customer_repo or CustomerRepository()

    def get_by_id(self, customer_id: int) -> Customer | None:
        """Retrieve a customer by ID.

        Args:
            customer_id: The primary key ID of the customer.

        Returns:
            The Customer entity if found, None otherwise.
        """
        return self.customer_repo.get_by_id(customer_id)

    def get_by_email(self, email: str) -> Customer | None:
        """Retrieve a customer by email.

        Args:
            email: The email address to look up.

        Returns:
            The Customer entity if found, None otherwise.
        """
        if not email or not email.strip():
            return None
        return self.customer_repo.get_by_email(email.strip())

    def get_all(self) -> list[Customer]:
        """Retrieve all customers.

        Returns:
            A list of Customer entities.
        """
        return self.customer_repo.get_all()

    def register_customer(
        self,
        name: str,
        email: str,
        phone: str | None = None,
        address: str | None = None,
    ) -> Customer:
        """Register a new customer with validation and uniqueness checks.

        Args:
            name: Customer name. Must not be empty.
            email: Customer email. Must be valid and unique.
            phone: Optional contact phone number.
            address: Optional physical address.

        Returns:
            The persisted Customer entity with assigned ID.

        Raises:
            ValueError: If name/email is invalid or email is already registered.
        """
        if not name or not name.strip():
            raise ValueError("Customer name cannot be empty.")
        if not email or "@" not in email:
            raise ValueError("A valid customer email is required.")

        cleaned_email = email.strip().lower()
        if self.customer_repo.get_by_email(cleaned_email) is not None:
            raise ValueError(f"Customer with email '{cleaned_email}' already exists.")

        customer = Customer(
            name=name.strip(),
            email=cleaned_email,
            phone=phone.strip() if phone else None,
            address=address.strip() if address else None,
        )
        return self.customer_repo.create(customer)

    def update_customer(
        self,
        customer_id: int,
        name: str,
        email: str,
        phone: str | None = None,
        address: str | None = None,
    ) -> Customer:
        """Update an existing customer's profile.

        Args:
            customer_id: The customer ID.
            name: Updated customer name.
            email: Updated email address.
            phone: Updated phone number.
            address: Updated physical address.

        Returns:
            The updated Customer entity.

        Raises:
            ValueError: If customer not found, invalid details, or email taken by another customer.
        """
        customer = self.customer_repo.get_by_id(customer_id)
        if customer is None:
            raise ValueError(f"Customer with ID {customer_id} not found.")

        if not name or not name.strip():
            raise ValueError("Customer name cannot be empty.")
        if not email or "@" not in email:
            raise ValueError("A valid customer email is required.")

        cleaned_email = email.strip().lower()
        existing = self.customer_repo.get_by_email(cleaned_email)
        if existing is not None and existing.id != customer_id:
            raise ValueError(f"Customer with email '{cleaned_email}' already exists.")

        customer.name = name.strip()
        customer.email = cleaned_email
        customer.phone = phone.strip() if phone else None
        customer.address = address.strip() if address else None

        self.customer_repo.update(customer)
        return customer

    def delete_customer(self, customer_id: int) -> bool:
        """Delete a customer record.

        Args:
            customer_id: The customer ID.

        Returns:
            True if deleted.

        Raises:
            ValueError: If customer does not exist.
        """
        customer = self.customer_repo.get_by_id(customer_id)
        if customer is None:
            raise ValueError(f"Customer with ID {customer_id} not found.")

        return self.customer_repo.delete(customer_id)

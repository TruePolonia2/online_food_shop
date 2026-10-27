"""Product service coordinating catalog operations and inventory validation."""
from app.models.product import Product
from app.repositories.category_repository import CategoryRepository
from app.repositories.product_repository import ProductRepository
from app.services.catalog_validation import validate_product_image_url


class ProductService:
    """Service handling product catalog business rules and validation."""

    def __init__(
        self,
        product_repo: ProductRepository | None = None,
        category_repo: CategoryRepository | None = None,
    ) -> None:
        """Initialize ProductService with injected repositories.

        Args:
            product_repo: ProductRepository instance. Defaults to a new instance.
            category_repo: CategoryRepository instance. Defaults to a new instance.
        """
        self.product_repo = product_repo or ProductRepository()
        self.category_repo = category_repo or CategoryRepository()

    def get_by_id(self, product_id: int) -> Product | None:
        """Retrieve a product by its ID.

        Args:
            product_id: The primary key ID of the product.

        Returns:
            The Product entity if found, None otherwise.
        """
        return self.product_repo.get_by_id(product_id)

    def get_active_products(self) -> list[Product]:
        """Retrieve all currently active catalog products.

        Returns:
            A list of active Product entities sorted by name.
        """
        return self.product_repo.list_active()

    def get_all_products(self, include_inactive: bool = False) -> list[Product]:
        """Retrieve catalog products with optional inactive inclusion.

        Args:
            include_inactive: Whether to include inactive products. Defaults to False.

        Returns:
            A list of Product entities.
        """
        return self.product_repo.get_all(include_inactive=include_inactive)

    def get_products_by_category(
        self, category_id: int, include_inactive: bool = False
    ) -> list[Product]:
        """Retrieve products associated with a specific category.

        Args:
            category_id: The category identifier.
            include_inactive: Whether to include inactive products. Defaults to False.

        Returns:
            A list of matching Product entities.

        Raises:
            ValueError: If the category does not exist.
        """
        if self.category_repo.get_by_id(category_id) is None:
            raise ValueError(f"Category with ID {category_id} not found.")

        return self.product_repo.get_by_category_id(
            category_id, include_inactive=include_inactive
        )

    def create_product(
        self,
        name: str,
        price: int,
        stock_quantity: int = 0,
        description: str | None = None,
        category_id: int | None = None,
        is_active: bool = True,
        image_url: str | None = None,
    ) -> Product:
        """Create a new product after validating domain invariants.

        Args:
            name: Product name. Must be non-empty.
            price: Price in minor currency units (cents). Must be >= 0.
            stock_quantity: Available inventory units. Must be >= 0.
            description: Optional product description.
            category_id: Optional existing category ID.
            is_active: Whether the product is active in the catalog.
            image_url: Optional static path or URL for product image.

        Returns:
            The persisted Product entity with assigned ID.

        Raises:
            ValueError: If name is empty, price/stock is negative, category does not exist,
                or image_url is dangerous.
        """
        if not name or not name.strip():
            raise ValueError("Product name cannot be empty.")
        if isinstance(price, bool) or not isinstance(price, int) or price < 0:
            raise ValueError("Product price must be a non-negative integer.")
        if (
            isinstance(stock_quantity, bool)
            or not isinstance(stock_quantity, int)
            or stock_quantity < 0
        ):
            raise ValueError("Stock quantity must be a non-negative integer.")
        if not isinstance(is_active, bool):
            raise ValueError("Product is_active must be a boolean.")

        if category_id is not None:
            if self.category_repo.get_by_id(category_id) is None:
                raise ValueError(f"Category with ID {category_id} does not exist.")

        sanitized_image_url = validate_product_image_url(image_url)

        product = Product(
            name=name.strip(),
            price=price,
            stock_quantity=stock_quantity,
            description=description.strip() if description else None,
            category_id=category_id,
            is_active=is_active,
            image_url=sanitized_image_url,
        )
        return self.product_repo.create(product)

    def update_product(
        self,
        product_id: int,
        name: str,
        price: int,
        stock_quantity: int,
        description: str | None = None,
        category_id: int | None = None,
        is_active: bool = True,
        image_url: str | None = None,
    ) -> Product:
        """Update an existing product's attributes.

        Args:
            product_id: The ID of the product to update.
            name: Updated product name.
            price: Updated price in cents.
            stock_quantity: Updated stock quantity.
            description: Updated description.
            category_id: Updated category ID.
            is_active: Updated active status.
            image_url: Updated product image URL or static path.

        Returns:
            The updated Product entity.

        Raises:
            ValueError: If product does not exist, values are invalid, or category is not found.
        """
        product = self.product_repo.get_by_id(product_id)
        if product is None:
            raise ValueError(f"Product with ID {product_id} not found.")

        if not name or not name.strip():
            raise ValueError("Product name cannot be empty.")
        if isinstance(price, bool) or not isinstance(price, int) or price < 0:
            raise ValueError("Product price must be a non-negative integer.")
        if (
            isinstance(stock_quantity, bool)
            or not isinstance(stock_quantity, int)
            or stock_quantity < 0
        ):
            raise ValueError("Stock quantity must be a non-negative integer.")
        if not isinstance(is_active, bool):
            raise ValueError("Product is_active must be a boolean.")

        if category_id is not None:
            if self.category_repo.get_by_id(category_id) is None:
                raise ValueError(f"Category with ID {category_id} does not exist.")

        sanitized_image_url = validate_product_image_url(image_url)

        product.name = name.strip()
        product.price = price
        product.stock_quantity = stock_quantity
        product.description = description.strip() if description else None
        product.category_id = category_id
        product.is_active = is_active
        product.image_url = sanitized_image_url

        self.product_repo.update(product)
        return product

    def update_stock(self, product_id: int, quantity_change: int) -> Product:
        """Adjust product stock by a delta (positive to restock, negative to consume).

        Args:
            product_id: The product ID.
            quantity_change: Amount to adjust stock by.

        Returns:
            The updated Product entity.

        Raises:
            ValueError: If product does not exist, quantity_change is a boolean/non-integer, or resulting stock would be negative.
        """
        if isinstance(quantity_change, bool) or not isinstance(quantity_change, int):
            raise ValueError("Quantity change must be an integer.")

        product = self.product_repo.get_by_id(product_id)
        if product is None:
            raise ValueError(f"Product with ID {product_id} not found.")

        new_stock = product.stock_quantity + quantity_change
        if new_stock < 0:
            raise ValueError(
                f"Insufficient stock for product '{product.name}'. "
                f"Available: {product.stock_quantity}, requested change: {quantity_change}."
            )

        product.stock_quantity = new_stock
        self.product_repo.update(product)
        return product

    def delete_product(self, product_id: int) -> bool:
        """Delete a product by ID.

        Args:
            product_id: The product ID to delete.

        Returns:
            True if deleted.

        Raises:
            ValueError: If product not found.
        """
        product = self.product_repo.get_by_id(product_id)
        if product is None:
            raise ValueError(f"Product with ID {product_id} not found.")

        return self.product_repo.delete(product_id)

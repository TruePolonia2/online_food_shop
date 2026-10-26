"""Focused presentation and integration tests for catalog and product detail UI (Phase 7 Step 3)."""
from pathlib import Path
import tempfile
import unittest

from app import create_app
from app.config import Config
from app.database.connection import init_db
from app.models.category import Category
from app.models.product import Product
from app.repositories.category_repository import CategoryRepository
from app.repositories.product_repository import ProductRepository


class TestCatalogUIPresentation(unittest.TestCase):
    """Tests for catalog grid, category filtering, product detail presentation, and price formatting."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_catalog_ui.sqlite3"

        class TestConfig(Config):
            DATABASE_PATH = str(self.db_path)
            TESTING = True
            SECRET_KEY = "test-catalog-ui-secret-key"
            CSRF_ENABLED = True  # Keep CSRF active to verify tokens in forms

        self.app = create_app(TestConfig)
        self.client = self.app.test_client()

        with self.app.app_context():
            init_db()
            self.cat_repo = CategoryRepository()
            self.prod_repo = ProductRepository()

            self.cat_bakery = self.cat_repo.create(Category(name="Bakery"))
            self.cat_produce = self.cat_repo.create(Category(name="Fresh Produce"))

            self.bread = self.prod_repo.create(
                Product(
                    name="Rustic Sourdough Bread",
                    price=450,  # 4,50 €
                    stock_quantity=15,
                    category_id=self.cat_bakery.id,
                    description="Naturally fermented artisanal loaf with a crisp golden crust.",
                    is_active=True,
                )
            )
            self.cheese = self.prod_repo.create(
                Product(
                    name="Artisan Mountain Cheese",
                    price=1299,  # 12,99 €
                    stock_quantity=5,
                    category_id=self.cat_bakery.id,
                    description="Aged farmhouse cheese with rich herbal notes.",
                    is_active=True,
                )
            )
            self.apples = self.prod_repo.create(
                Product(
                    name="Organic Red Apples",
                    price=280,  # 2,80 €
                    stock_quantity=20,
                    category_id=self.cat_produce.id,
                    description="Sweet and crisp orchard apples.",
                    is_active=True,
                )
            )
            self.sold_out_item = self.prod_repo.create(
                Product(
                    name="Seasonal Wild Mushrooms",
                    price=850,  # 8,50 €
                    stock_quantity=0,
                    category_id=self.cat_produce.id,
                    description="Foraged forest mushrooms.",
                    is_active=True,
                )
            )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_catalog_index_renders_products_with_formatted_euro_prices(self) -> None:
        resp = self.client.get("/")
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)

        # Portuguese euro prices
        self.assertIn("4,50 €", html)
        self.assertIn("12,99 €", html)
        self.assertIn("2,80 €", html)
        self.assertIn("8,50 €", html)

        # Product titles and descriptions
        self.assertIn("Rustic Sourdough Bread", html)
        self.assertIn("Artisan Mountain Cheese", html)
        self.assertIn("Naturally fermented artisanal loaf", html)

        # Stock badges
        self.assertIn("In Stock", html)
        self.assertIn("Out of Stock", html)

    def test_catalog_category_navigation_and_filtering(self) -> None:
        # Check all categories navigation
        resp = self.client.get("/")
        html = resp.get_data(as_text=True)
        self.assertIn("All Categories", html)
        self.assertIn("Bakery", html)
        self.assertIn("Fresh Produce", html)

        # Filter by Bakery
        resp_bakery = self.client.get(f"/?category_id={self.cat_bakery.id}")
        html_bakery = resp_bakery.get_data(as_text=True)
        self.assertIn("Rustic Sourdough Bread", html_bakery)
        self.assertIn("Artisan Mountain Cheese", html_bakery)
        self.assertNotIn("Organic Red Apples", html_bakery)
        self.assertIn("Category: Bakery", html_bakery)
        self.assertIn("Clear filter", html_bakery)

    def test_catalog_cards_link_to_product_detail(self) -> None:
        resp = self.client.get("/")
        html = resp.get_data(as_text=True)
        self.assertIn(f'href="/products/{self.bread.id}"', html)
        self.assertIn(f'href="/products/{self.cheese.id}"', html)

    def test_catalog_add_to_cart_form_fields_and_csrf_presence(self) -> None:
        resp = self.client.get("/")
        html = resp.get_data(as_text=True)

        # Add to cart POST form attributes
        self.assertIn('action="/cart/add"', html)
        self.assertIn('method="post"', html)
        self.assertIn('name="csrf_token"', html)
        self.assertIn(f'value="{self.bread.id}"', html)
        self.assertIn('name="quantity"', html)
        self.assertIn("Add to Cart", html)

    def test_catalog_empty_category_renders_empty_state(self) -> None:
        with self.app.app_context():
            empty_cat = self.cat_repo.create(Category(name="Empty Pantry"))
        resp = self.client.get(f"/?category_id={empty_cat.id}")
        html = resp.get_data(as_text=True)
        self.assertIn("No products found.", html)
        self.assertIn("View All Products", html)

    def test_product_detail_presentation_and_euro_price(self) -> None:
        resp = self.client.get(f"/products/{self.bread.id}")
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)

        # Visual hierarchy
        self.assertIn("Rustic Sourdough Bread", html)
        self.assertIn("Bakery", html)
        self.assertIn("4,50 €", html)
        self.assertIn("In Stock (15 available)", html)
        self.assertIn("Description", html)
        self.assertIn("Naturally fermented artisanal loaf", html)

        # Navigation back to catalog
        self.assertIn("Back to Products", html)
        self.assertIn('href="/"', html)

        # Add-to-cart form with CSRF
        self.assertIn('action="/cart/add"', html)
        self.assertIn('name="csrf_token"', html)
        self.assertIn(f'value="{self.bread.id}"', html)
        self.assertIn('name="quantity"', html)
        self.assertIn("Add to Cart", html)

    def test_product_detail_out_of_stock_state(self) -> None:
        resp = self.client.get(f"/products/{self.sold_out_item.id}")
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)

        self.assertIn("Seasonal Wild Mushrooms", html)
        self.assertIn("8,50 €", html)
        self.assertIn("Currently Out of Stock", html)
        self.assertIn("disabled", html)


if __name__ == "__main__":
    unittest.main()

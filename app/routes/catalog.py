"""Catalog routes for browsing products and categories."""
from flask import Blueprint, abort, render_template, request

from app.services.category_service import CategoryService
from app.services.product_service import ProductService

catalog_bp = Blueprint("catalog", __name__)


@catalog_bp.route("/")
def index():
    """Display catalog homepage with product listing and category navigation."""
    category_service = CategoryService()
    product_service = ProductService()

    categories = category_service.get_all()
    selected_category = None
    category_id_str = request.args.get("category_id")

    if category_id_str:
        try:
            category_id = int(category_id_str)
            selected_category = category_service.get_by_id(category_id)
            if selected_category is not None:
                products = product_service.get_products_by_category(category_id)
            else:
                products = product_service.get_active_products()
        except ValueError:
            products = product_service.get_active_products()
    else:
        products = product_service.get_active_products()

    return render_template(
        "catalog/index.html",
        products=products,
        categories=categories,
        selected_category=selected_category,
    )


@catalog_bp.route("/products/<int:product_id>")
def product_detail(product_id: int):
    """Display detail view for a specific active product."""
    product_service = ProductService()
    category_service = CategoryService()

    product = product_service.get_by_id(product_id)
    if product is None or not product.is_active:
        abort(404)

    category = (
        category_service.get_by_id(product.category_id)
        if product.category_id is not None
        else None
    )

    return render_template(
        "catalog/product_detail.html",
        product=product,
        category=category,
    )

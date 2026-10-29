"""Admin blueprint and routes for store owner administration."""
from flask import (
    Blueprint,
    abort,
    flash,
    make_response,
    redirect,
    render_template,
    request,
    url_for,
)

from app.authorization import owner_required
from app.models.contact_message import (
    ALLOWED_STATUS_TRANSITIONS as CONTACT_STATUS_TRANSITIONS,
    ContactMessageStatus,
)
from app.models.order import OrderStatus
from app.repositories.payment_repository import PaymentRepository
from app.services.admin_service import AdminService
from app.services.catalog_validation import (
    parse_euro_to_cents,
    parse_stock_quantity,
    validate_product_image_url,
)
from app.services.category_service import CategoryService
from app.services.contact_service import (
    ContactService,
    ContactStatusTransitionError,
)
from app.services.order_service import (
    ALLOWED_ADMIN_TRANSITIONS,
    OrderConflictError,
    OrderService,
)
from app.services.product_service import ProductService

admin_bp = Blueprint("admin", __name__, url_prefix="/admin")


# =============================================================================
# Dashboard Routes
# =============================================================================


@admin_bp.route("", methods=["GET"])
@admin_bp.route("/", methods=["GET"])
@owner_required
def dashboard():
    """Display the store administration dashboard with aggregate statistics.

    Protected strictly by owner_required decorator: only authenticated, verified
    store owners can access this view.
    """
    admin_service = AdminService()
    stats = admin_service.get_dashboard_stats()
    return render_template("admin/dashboard.html", stats=stats)


@admin_bp.route("/dashboard", methods=["GET"])
@owner_required
def dashboard_alias():
    """Redirect canonical dashboard URL to avoid duplicate implementations.

    Protected strictly by owner_required decorator to ensure unverified or customer
    accounts are rejected before any redirect logic executes.
    """
    return redirect(url_for("admin.dashboard"))


# =============================================================================
# Category Administration Routes
# =============================================================================


@admin_bp.route("/categories", methods=["GET"])
@owner_required
def list_categories():
    """List all store categories including inactive ones."""
    category_service = CategoryService()
    product_service = ProductService()

    categories = category_service.get_all(include_inactive=True)

    # Compute product count per category for administrative insight
    all_products = product_service.get_all_products(include_inactive=True)
    product_counts: dict[int, int] = {}
    uncategorized_count = 0
    for p in all_products:
        if p.category_id is not None:
            product_counts[p.category_id] = product_counts.get(p.category_id, 0) + 1
        else:
            uncategorized_count += 1

    return render_template(
        "admin/categories/index.html",
        categories=categories,
        product_counts=product_counts,
        uncategorized_count=uncategorized_count,
    )


@admin_bp.route("/categories/new", methods=["GET"])
@owner_required
def new_category():
    """Render the category creation form."""
    return render_template(
        "admin/categories/form.html",
        category=None,
        form_data={"name": "", "description": "", "is_active": True},
    )


@admin_bp.route("/categories/new", methods=["POST"])
@owner_required
def create_category():
    """Process category creation submission."""
    name = (request.form.get("name") or "").strip()
    description = (request.form.get("description") or "").strip() or None
    is_active = "is_active" in request.form

    form_data = {
        "name": name,
        "description": description or "",
        "is_active": is_active,
    }

    if not name:
        flash("Category name is required.", "error")
        return (
            render_template(
                "admin/categories/form.html",
                category=None,
                form_data=form_data,
            ),
            400,
        )

    category_service = CategoryService()
    try:
        created = category_service.create_category(
            name=name, description=description, is_active=is_active
        )
        flash(f"Category '{created.name}' created successfully.", "success")
        return redirect(url_for("admin.list_categories"))
    except ValueError as exc:
        flash(str(exc), "error")
        return (
            render_template(
                "admin/categories/form.html",
                category=None,
                form_data=form_data,
            ),
            400,
        )


@admin_bp.route("/categories/<int:category_id>/edit", methods=["GET"])
@owner_required
def edit_category(category_id: int):
    """Render the category edit form."""
    category_service = CategoryService()
    category = category_service.get_by_id(category_id)
    if category is None:
        abort(404)

    form_data = {
        "name": category.name,
        "description": category.description or "",
        "is_active": category.is_active,
    }
    return render_template(
        "admin/categories/form.html",
        category=category,
        form_data=form_data,
    )


@admin_bp.route("/categories/<int:category_id>/edit", methods=["POST"])
@owner_required
def update_category(category_id: int):
    """Process category edit submission."""
    category_service = CategoryService()
    category = category_service.get_by_id(category_id)
    if category is None:
        abort(404)

    name = (request.form.get("name") or "").strip()
    description = (request.form.get("description") or "").strip() or None
    is_active = "is_active" in request.form

    form_data = {
        "name": name,
        "description": description or "",
        "is_active": is_active,
    }

    if not name:
        flash("Category name is required.", "error")
        return (
            render_template(
                "admin/categories/form.html",
                category=category,
                form_data=form_data,
            ),
            400,
        )

    try:
        updated = category_service.update_category(
            category_id=category_id,
            name=name,
            description=description,
            is_active=is_active,
        )
        flash(f"Category '{updated.name}' updated successfully.", "success")
        return redirect(url_for("admin.list_categories"))
    except ValueError as exc:
        flash(str(exc), "error")
        return (
            render_template(
                "admin/categories/form.html",
                category=category,
                form_data=form_data,
            ),
            400,
        )


# =============================================================================
# Product Administration Routes
# =============================================================================


@admin_bp.route("/products", methods=["GET"])
@owner_required
def list_products():
    """List all store products including inactive items."""
    product_service = ProductService()
    category_service = CategoryService()

    products = product_service.get_all_products(include_inactive=True)
    categories = category_service.get_all(include_inactive=True)
    category_map = {c.id: c for c in categories}

    return render_template(
        "admin/products/index.html",
        products=products,
        category_map=category_map,
    )


@admin_bp.route("/products/new", methods=["GET"])
@owner_required
def new_product():
    """Render the product creation form."""
    category_service = CategoryService()
    categories = category_service.get_all(include_inactive=True)

    form_data = {
        "name": "",
        "price": "",
        "stock_quantity": "0",
        "description": "",
        "category_id": "",
        "is_active": True,
        "image_url": "",
    }
    return render_template(
        "admin/products/form.html",
        product=None,
        categories=categories,
        form_data=form_data,
    )


@admin_bp.route("/products/new", methods=["POST"])
@owner_required
def create_product():
    """Process product creation submission."""
    category_service = CategoryService()
    product_service = ProductService()
    categories = category_service.get_all(include_inactive=True)

    raw_name = (request.form.get("name") or "").strip()
    raw_price = request.form.get("price")
    raw_stock = request.form.get("stock_quantity")
    raw_desc = (request.form.get("description") or "").strip() or None
    raw_cat_id = (request.form.get("category_id") or "").strip()
    is_active = "is_active" in request.form
    raw_image_url = (request.form.get("image_url") or "").strip() or None

    form_data = {
        "name": raw_name,
        "price": raw_price or "",
        "stock_quantity": raw_stock or "",
        "description": raw_desc or "",
        "category_id": raw_cat_id,
        "is_active": is_active,
        "image_url": raw_image_url or "",
    }

    # Validate name
    if not raw_name:
        flash("Product name is required.", "error")
        return (
            render_template(
                "admin/products/form.html",
                product=None,
                categories=categories,
                form_data=form_data,
            ),
            400,
        )

    # Validate price
    try:
        price_cents = parse_euro_to_cents(raw_price)
    except ValueError as exc:
        flash(f"Invalid price: {exc}", "error")
        return (
            render_template(
                "admin/products/form.html",
                product=None,
                categories=categories,
                form_data=form_data,
            ),
            400,
        )

    # Validate stock quantity
    try:
        stock_quantity = parse_stock_quantity(raw_stock)
    except ValueError as exc:
        flash(f"Invalid stock quantity: {exc}", "error")
        return (
            render_template(
                "admin/products/form.html",
                product=None,
                categories=categories,
                form_data=form_data,
            ),
            400,
        )

    # Validate category assignment
    category_id: int | None = None
    if raw_cat_id and raw_cat_id != "none":
        try:
            category_id = int(raw_cat_id)
            if category_service.get_by_id(category_id) is None:
                flash(f"Selected category (ID {category_id}) does not exist.", "error")
                return (
                    render_template(
                        "admin/products/form.html",
                        product=None,
                        categories=categories,
                        form_data=form_data,
                    ),
                    400,
                )
        except ValueError:
            flash("Invalid category selection.", "error")
            return (
                render_template(
                    "admin/products/form.html",
                    product=None,
                    categories=categories,
                    form_data=form_data,
                ),
                400,
            )

    # Validate image URL
    try:
        image_url = validate_product_image_url(raw_image_url)
    except ValueError as exc:
        flash(f"Invalid image: {exc}", "error")
        return (
            render_template(
                "admin/products/form.html",
                product=None,
                categories=categories,
                form_data=form_data,
            ),
            400,
        )

    try:
        created = product_service.create_product(
            name=raw_name,
            price=price_cents,
            stock_quantity=stock_quantity,
            description=raw_desc,
            category_id=category_id,
            is_active=is_active,
            image_url=image_url,
        )
        flash(f"Product '{created.name}' created successfully.", "success")
        return redirect(url_for("admin.list_products"))
    except ValueError as exc:
        flash(str(exc), "error")
        return (
            render_template(
                "admin/products/form.html",
                product=None,
                categories=categories,
                form_data=form_data,
            ),
            400,
        )


@admin_bp.route("/products/<int:product_id>/edit", methods=["GET"])
@owner_required
def edit_product(product_id: int):
    """Render the product edit form."""
    product_service = ProductService()
    category_service = CategoryService()

    product = product_service.get_by_id(product_id)
    if product is None:
        abort(404)

    categories = category_service.get_all(include_inactive=True)

    # Format price in euros for human editing
    price_euros = f"{product.price // 100}.{product.price % 100:02d}"

    form_data = {
        "name": product.name,
        "price": price_euros,
        "stock_quantity": str(product.stock_quantity),
        "description": product.description or "",
        "category_id": str(product.category_id) if product.category_id is not None else "",
        "is_active": product.is_active,
        "image_url": product.image_url or "",
    }
    return render_template(
        "admin/products/form.html",
        product=product,
        categories=categories,
        form_data=form_data,
    )


@admin_bp.route("/products/<int:product_id>/edit", methods=["POST"])
@owner_required
def update_product(product_id: int):
    """Process product edit submission."""
    product_service = ProductService()
    category_service = CategoryService()

    product = product_service.get_by_id(product_id)
    if product is None:
        abort(404)

    categories = category_service.get_all(include_inactive=True)

    raw_name = (request.form.get("name") or "").strip()
    raw_price = request.form.get("price")
    raw_stock = request.form.get("stock_quantity")
    raw_desc = (request.form.get("description") or "").strip() or None
    raw_cat_id = (request.form.get("category_id") or "").strip()
    is_active = "is_active" in request.form
    raw_image_url = (request.form.get("image_url") or "").strip() or None

    form_data = {
        "name": raw_name,
        "price": raw_price or "",
        "stock_quantity": raw_stock or "",
        "description": raw_desc or "",
        "category_id": raw_cat_id,
        "is_active": is_active,
        "image_url": raw_image_url or "",
    }

    if not raw_name:
        flash("Product name is required.", "error")
        return (
            render_template(
                "admin/products/form.html",
                product=product,
                categories=categories,
                form_data=form_data,
            ),
            400,
        )

    try:
        price_cents = parse_euro_to_cents(raw_price)
    except ValueError as exc:
        flash(f"Invalid price: {exc}", "error")
        return (
            render_template(
                "admin/products/form.html",
                product=product,
                categories=categories,
                form_data=form_data,
            ),
            400,
        )

    try:
        stock_quantity = parse_stock_quantity(raw_stock)
    except ValueError as exc:
        flash(f"Invalid stock quantity: {exc}", "error")
        return (
            render_template(
                "admin/products/form.html",
                product=product,
                categories=categories,
                form_data=form_data,
            ),
            400,
        )

    category_id: int | None = None
    if raw_cat_id and raw_cat_id != "none":
        try:
            category_id = int(raw_cat_id)
            if category_service.get_by_id(category_id) is None:
                flash(f"Selected category (ID {category_id}) does not exist.", "error")
                return (
                    render_template(
                        "admin/products/form.html",
                        product=product,
                        categories=categories,
                        form_data=form_data,
                    ),
                    400,
                )
        except ValueError:
            flash("Invalid category selection.", "error")
            return (
                render_template(
                    "admin/products/form.html",
                    product=product,
                    categories=categories,
                    form_data=form_data,
                ),
                400,
            )

    try:
        image_url = validate_product_image_url(raw_image_url)
    except ValueError as exc:
        flash(f"Invalid image: {exc}", "error")
        return (
            render_template(
                "admin/products/form.html",
                product=product,
                categories=categories,
                form_data=form_data,
            ),
            400,
        )

    try:
        updated = product_service.update_product(
            product_id=product_id,
            name=raw_name,
            price=price_cents,
            stock_quantity=stock_quantity,
            description=raw_desc,
            category_id=category_id,
            is_active=is_active,
            image_url=image_url,
        )
        flash(f"Product '{updated.name}' updated successfully.", "success")
        return redirect(url_for("admin.list_products"))
    except ValueError as exc:
        flash(str(exc), "error")
        return (
            render_template(
                "admin/products/form.html",
                product=product,
                categories=categories,
                form_data=form_data,
            ),
            400,
        )


# =============================================================================
# Order Administration Routes
# =============================================================================


@admin_bp.route("/orders", methods=["GET"])
@owner_required
def list_orders():
    """List orders with pagination and status filtering.

    Protected strictly by owner_required decorator: only authenticated, verified
    store owners can view administrative order listings.
    """
    order_service = OrderService()

    raw_status = request.args.get("status")
    raw_page = request.args.get("page")

    try:
        page = int(raw_page) if raw_page else 1
    except (ValueError, TypeError):
        page = 1

    orders, total_count, total_pages, active_status = (
        order_service.get_paginated_orders_for_admin(
            status_filter=raw_status,
            page=page,
            per_page=20,
        )
    )

    all_statuses = [s.value for s in OrderStatus]

    response = make_response(
        render_template(
            "admin/orders/index.html",
            orders=orders,
            total_count=total_count,
            page=page,
            total_pages=total_pages,
            active_status=active_status,
            all_statuses=all_statuses,
        )
    )
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, private"
    response.headers["Pragma"] = "no-cache"
    return response


@admin_bp.route("/orders/<int:order_id>", methods=["GET"])
@owner_required
def order_detail(order_id: int):
    """Display administrative order details and available status actions.

    Only verified owners can view order fulfillment details.
    """
    order_service = OrderService()
    order = order_service.get_by_id(order_id)
    if order is None:
        abort(404)

    allowed_transitions = [
        s.value for s in ALLOWED_ADMIN_TRANSITIONS.get(order.status, set())
    ]

    payment_repo = PaymentRepository()
    payments = payment_repo.get_by_order_id(order_id)
    latest_payment = payments[-1] if payments else None

    response = make_response(
        render_template(
            "admin/orders/detail.html",
            order=order,
            allowed_transitions=allowed_transitions,
            latest_payment=latest_payment,
            payments=payments,
        )
    )
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, private"
    response.headers["Pragma"] = "no-cache"
    return response


@admin_bp.route("/orders/<int:order_id>/status", methods=["POST"])
@owner_required
def update_order_status(order_id: int):
    """Transition an order's lifecycle status atomically.

    Requires CSRF token, verifies concurrency via current_status token,
    and enforces conservative status transition rules.
    """
    order_service = OrderService()
    order = order_service.get_by_id(order_id)
    if order is None:
        abort(404)

    expected_status = request.form.get("current_status")
    new_status = request.form.get("new_status")

    if not expected_status or not new_status:
        flash("Missing status transition parameters.", "error")
        allowed_transitions = [
            s.value for s in ALLOWED_ADMIN_TRANSITIONS.get(order.status, set())
        ]
        response = make_response(
            render_template(
                "admin/orders/detail.html",
                order=order,
                allowed_transitions=allowed_transitions,
            ),
            400,
        )
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, private"
        return response

    try:
        updated_order = order_service.admin_transition_order_status(
            order_id=order_id,
            expected_status=expected_status,
            target_status=new_status,
        )
        flash(
            f"Order #{order_id} status updated to '{updated_order.status.value}'.",
            "success",
        )
        return redirect(url_for("admin.order_detail", order_id=order_id))

    except OrderConflictError as exc:
        flash(f"Conflict: {exc}", "error")
        fresh_order = order_service.get_by_id(order_id) or order
        allowed_transitions = [
            s.value for s in ALLOWED_ADMIN_TRANSITIONS.get(fresh_order.status, set())
        ]
        response = make_response(
            render_template(
                "admin/orders/detail.html",
                order=fresh_order,
                allowed_transitions=allowed_transitions,
            ),
            409,
        )
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, private"
        return response

    except ValueError as exc:
        flash(str(exc), "error")
        allowed_transitions = [
            s.value for s in ALLOWED_ADMIN_TRANSITIONS.get(order.status, set())
        ]
        response = make_response(
            render_template(
                "admin/orders/detail.html",
                order=order,
                allowed_transitions=allowed_transitions,
            ),
            400,
        )
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, private"
        return response


# =============================================================================
# Customer Support Inbox Routes
# =============================================================================


@admin_bp.route("/messages", methods=["GET"])
@owner_required
def list_messages():
    """Display paginated list of customer contact messages with status filter tabs.

    Protected strictly by owner_required decorator: only authenticated store owners
    can access customer support messages.
    """
    contact_service = ContactService()
    status_filter = request.args.get("status", "").strip().lower() or None
    page = request.args.get("page", 1, type=int)

    messages, total_count, total_pages = contact_service.list_messages(
        status=status_filter,
        page=page,
        per_page=20,
    )
    status_counts = contact_service.get_status_counts()

    response = make_response(
        render_template(
            "admin/messages/index.html",
            messages=messages,
            current_status=status_filter,
            status_counts=status_counts,
            page=page,
            total_pages=total_pages,
            total_count=total_count,
        )
    )
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, private"
    response.headers["Pragma"] = "no-cache"
    return response


@admin_bp.route("/messages/<int:message_id>", methods=["GET"])
@owner_required
def view_message(message_id: int):
    """View customer contact message details.

    Adheres to safe GET semantics: viewing a message does not modify persistent
    database state. Status changes (such as marking as read) are performed via
    explicit CSRF-protected POST actions.

    Protected strictly by owner_required decorator.
    """
    contact_service = ContactService()
    message = contact_service.get_message(message_id)
    if message is None:
        abort(404)

    allowed_transitions = [
        s.value for s in CONTACT_STATUS_TRANSITIONS.get(message.status, set())
    ]

    response = make_response(
        render_template(
            "admin/messages/detail.html",
            message=message,
            allowed_transitions=allowed_transitions,
        )
    )
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, private"
    response.headers["Pragma"] = "no-cache"
    return response


@admin_bp.route("/messages/<int:message_id>/status", methods=["POST"])
@owner_required
def update_message_status(message_id: int):
    """Update contact message status (e.g. resolve, reopen, mark read/unread).

    Requires CSRF token and owner credentials.
    """
    contact_service = ContactService()
    message = contact_service.get_message(message_id)
    if message is None:
        abort(404)

    new_status = request.form.get("status", "").strip().lower()
    if not new_status:
        flash("Target status is required.", "error")
        return redirect(url_for("admin.view_message", message_id=message_id))

    try:
        updated = contact_service.update_status(message_id, new_status)
        flash(f"Message #{message_id} status updated to '{updated.status.value}'.", "success")
    except (ContactStatusTransitionError, ValueError) as exc:
        flash(str(exc), "error")

    redirect_target = request.form.get("redirect_to")
    if redirect_target == "index":
        return redirect(url_for("admin.list_messages"))
    return redirect(url_for("admin.view_message", message_id=message_id))

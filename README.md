# Online Food Shop

A clean, modular web application for an online food shop built with Python, Flask, SQLite, Jinja2, and Tailwind CSS.

## Features

- **Product Catalog:** Browse active products with category filtering and out-of-stock indicators.
- **Product Details:** Dedicated detail pages with pricing, descriptions, and stock status.
- **Session Shopping Cart:** Add, update quantities, remove items, and clear the cart using Flask session cookies.
- **Catalog Availability Synchronization:** Stale or deactivated products in the cart are automatically purged from the session on reload.
- **Guest Checkout:** Seamless ordering flow with customer and delivery details validation.
- **Atomic Transactions & Inventory Control:** Orders and inventory deductions are executed atomically in SQLite using savepoints; stock depletion triggers an automatic rollback preserving the user's cart.
- **Order-Specific Delivery Snapshots:** Recipient name, email, phone, and address are permanently snapshotted onto each order, ensuring historical accuracy and preventing personal data exposure across sessions.
- **Session-Restricted Order Confirmation:** Order confirmation details are accessible only by the session that placed the order, preventing IDOR enumeration.
- **Integer Monetary Storage:** All prices and totals are stored as integer minor units (cents) to eliminate binary floating-point precision errors, formatted in templates with the `format_euro` filter (e.g. `350` cents rendered as `3,50 €`).
- **CSRF Protection:** Built-in cryptographic CSRF tokens on all state-changing HTTP forms using constant-time HMAC comparison.
- **Hardened Configuration Architecture:** Descriptor-based validation preventing missing or default secret keys in production environments.

## Architecture Overview

The application follows the Flask application factory pattern (`create_app`) with a layered architecture:

- `app/`: Application package root.
  - `app/config.py`: Environment configuration hierarchy (`Config`, `DevelopmentConfig`, `ProductionConfig`, `TestingConfig`), descriptor-based secret validation, and session cookie policies.
  - `app/csrf.py`: Custom zero-dependency CSRF protection module with constant-time HMAC token verification and Jinja helpers.
  - `app/formatting.py`: Euro currency filter (`format_euro`) converting integer cents into European standard notation.
  - `app/models/`: Domain dataclasses and enums (`Product`, `Category`, `Cart`, `CartItem`, `Customer`, `Order`, `OrderItem`, `OrderStatus`).
  - `app/repositories/`: Data access layer executing parameterized SQLite queries (`ProductRepository`, `CategoryRepository`, `CustomerRepository`, `OrderRepository`).
  - `app/services/`: Business logic, validation rules, and transaction coordination (`ProductService`, `CategoryService`, `CartService`, `CustomerService`, `OrderService`).
  - `app/routes/`: Blueprint route handlers:
    - `catalog.py`: Catalog homepage and product detail views.
    - `cart.py`: Cart inspection, item addition, quantity updates, removal, and clearing.
    - `order.py`: Checkout processing and session-isolated order confirmation.
    - `cart_session.py`: Session cart serialization, deserialization, and availability synchronization.
  - `app/database/`: SQLite database connection lifecycle, context-managed transactions with savepoint support (`connection.py`), and idempotent DDL schema with non-destructive migrations (`schema.sql`).
  - `app/templates/`: Semantic Jinja2 HTML templates styled with Tailwind CSS, extending `base.html`.
  - `app/static/`: Static assets (CSS, JavaScript, images).
- `tests/`: Automated test suite covering models, repositories, business services, UI presentation, configuration resolution, and End-to-End user journeys.
- `run.py`: Development server entry point.

## Prerequisites

- **Python 3.10+** (Python 3.14+ supported)

## Installation and Setup

1. **Clone or navigate to the repository directory:**
   ```bash
   cd online_food_shop
   ```

2. **Create and activate a virtual environment:**
   - **Windows (PowerShell):**
     ```powershell
     python -m venv .venv
     .venv\Scripts\Activate.ps1
     ```
   - **Windows (Command Prompt):**
     ```cmd
     python -m venv .venv
     .venv\Scripts\activate.bat
     ```
   - **Linux / macOS:**
     ```bash
     python3 -m venv .venv
     source .venv/bin/activate
     ```

3. **Install application dependencies:**
   ```bash
   pip install -r requirements.txt
   ```

4. **Initialize the SQLite database:**
   Creates tables, indexes, and applies non-destructive schema migrations idempotently without deleting existing data:
   ```bash
   flask --app run.py init-db
   ```
   *Alternative direct invocation:*
   ```bash
   python -m app.database.connection
   ```

## Running the Application

### Development Mode

Start the local development server:
```bash
python run.py
```
Open [http://127.0.0.1:5000](http://127.0.0.1:5000) in your browser.

> **Note:** `python run.py` hardcodes `debug=True` and defaults to `DevelopmentConfig`. It must only be used for local development.

### Production Deployment

> **Warning:** Never use Flask's built-in development server (`python run.py`) or enable debug mode in production environments.

For production, run the application factory through a production WSGI server. Production WSGI servers are separate dependencies and must be installed in your production environment:
- **Linux / macOS:** Install Gunicorn (`pip install gunicorn`) and run:
  ```bash
  gunicorn "app:create_app()"
  ```
  *(or `gunicorn "run:app"`)*
- **Windows:** Install Waitress (`pip install waitress`) and run:
  ```cmd
  waitress-serve --call app:create_app
  ```

#### Production Deployment Checklist

1. Set the configuration environment variable:
   ```bash
   export APP_CONFIG=production
   ```
   *(or `set APP_CONFIG=production` on Windows cmd)*
2. Generate and set a cryptographically secure `SECRET_KEY`:
   ```bash
   export SECRET_KEY=$(python -c "import secrets; print(secrets.token_hex(32))")
   ```
3. Ensure HTTPS is enabled on your reverse proxy or host, as `SESSION_COOKIE_SECURE=True` is enabled by default in production.
4. Run schema initialization:
   ```bash
   flask --app run.py init-db
   ```
5. Start your WSGI server as documented above.

## Configuration Reference

The application selects configuration classes dynamically through `app.config.get_config_by_name()`.

### Environment Precedence

When `create_app()` is called without an explicit configuration class, the active configuration is resolved using the following strict precedence:
1. `APP_CONFIG`
2. `FLASK_CONFIG`
3. `FLASK_ENV`
4. `ENV`
5. Default fallback: `'development'`

### Supported Aliases

- `production`, `prod` &rarr; `ProductionConfig`
- `development`, `dev` &rarr; `DevelopmentConfig`
- `testing`, `test` &rarr; `TestingConfig`

An explicit configuration class or dotted import path (e.g. `create_app("app.config.ProductionConfig")`) can also be passed directly. Unrecognized configuration names raise a `ValueError` immediately at startup.

### Configuration Variables

| Variable | Default (Development) | Default (Production) | Description |
| :--- | :--- | :--- | :--- |
| `APP_CONFIG` / `FLASK_CONFIG` | `None` (resolves to dev) | `production` | Environment name selection. |
| `SECRET_KEY` | `dev-secret-key-change-in-production` | **Required** | Secret key for signing session cookies. In production, startup raises `RuntimeError` if unset, empty, or set to the development placeholder. |
| `FLASK_DEBUG` | `True` | `False` | Enables/disables Flask interactive debugging. |
| `SESSION_COOKIE_SECURE` | `False` | `True` | Restricts cookies to HTTPS connections. |
| `SESSION_COOKIE_HTTPONLY` | `True` | `True` | Prevents client-side scripts from reading session cookies. |
| `SESSION_COOKIE_SAMESITE` | `"Lax"` | `"Lax"` | Protects session cookies against cross-site request forgery. |
| `DATABASE_PATH` | `instance/online_food_shop.sqlite3` | `instance/online_food_shop.sqlite3` | Filesystem path to the SQLite database file. |
| `CSRF_ENABLED` | `True` | `True` | Enables/disables CSRF validation on mutating HTTP methods. |
| `TESTING` | `False` | `False` | Flask testing mode flag (`True` in `TestingConfig`). |

### Generating a Secure Secret Key

To generate a secure 32-byte hexadecimal secret key for production:
```bash
python -c "import secrets; print(secrets.token_hex(32))"
```
Never commit real secrets or `.env` files to version control.

## Running Tests

Run the full automated test suite using `pytest`:
```bash
python -m pytest -v -p no:cacheprovider
```

> **Tip:** The `-p no:cacheprovider` flag prevents `.pytest_cache` file locking issues on Windows and networked or synced folders (such as OneDrive).

## Security Architecture & Known Limitations

### Security Controls

- **CSRF Protection:** State-changing requests (`POST`, `PUT`, `PATCH`, `DELETE`) require a valid session CSRF token. Forms render tokens via `{% from "_macros.html" import csrf_input %}` and `{{ csrf_input() }}`.
- **Session Cookie Hardening:** `HttpOnly` and `SameSite=Lax` are enabled across all environments. In production, `SESSION_COOKIE_SECURE` defaults to `True` to mandate HTTPS transport.
- **Order Delivery Snapshots:** Orders store their own immutable snapshot of recipient name, email, phone, and delivery address. A guest order placed using an email address matching an existing customer does not mutate that customer's stored profile.
- **Order Confirmation Privacy:** The order confirmation page displays only the snapshot data captured on that specific order, never data loaded from customer profiles. Access is restricted to the checkout session (`session["last_order_id"] == order_id`); any other session attempting to access `/orders/<order_id>` receives an `HTTP 404 Not Found`.

### Current Limitations & Unimplemented Features

This application is built for guest checkout and currently **does NOT implement**:
- Customer user authentication (registration, login, logout, password hashing).
- Email verification or password reset workflows.
- Store owner / administrator accounts or backoffice admin routes.
- Product catalog image file upload management.
- Google OAuth or third-party social sign-in.
- Online payment provider integrations (Stripe, PayPal, etc.).

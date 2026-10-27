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
| `EMAIL_BACKEND` | `"memory"` | `"memory"` | Email delivery backend: `"memory"` (in-memory for offline/testing) or `"smtp"` (real delivery). |
| `SMTP_HOST` | `""` | `""` | SMTP relay server hostname (e.g. `smtp.gmail.com`). Required if `EMAIL_BACKEND=smtp`. |
| `SMTP_PORT` | `587` | `587` | SMTP relay server port (`587` for STARTTLS, `465` for implicit TLS/SSL). |
| `SMTP_SECURITY` | `"starttls"` | `"starttls"` | Transport security mode: `"starttls"`, `"ssl"`, or `"none"`. |
| `SMTP_USERNAME` | `""` | `""` | Optional SMTP authentication username / account email. |
| `SMTP_PASSWORD` | `""` | `""` | Optional SMTP authentication password or App Password. |
| `SMTP_FROM_ADDRESS` | `""` | `""` | Sender email address displayed in outgoing messages. Required if `EMAIL_BACKEND=smtp`. |
| `SMTP_TIMEOUT` | `10.0` | `10.0` | Socket connection and read timeout in seconds. |
| `APP_BASE_URL` | `http://127.0.0.1:5000` | `http://127.0.0.1:5000` | Trusted base URL used to construct absolute verification and password reset links (prevents Host header injection). |
| `EMAIL_DEV_CONSOLE_PREVIEW` | `False` | `False` | Prints a formatted console preview of simulated messages when `EMAIL_BACKEND=memory`. |
| `GOOGLE_CLIENT_ID` | `None` | `None` | Google OAuth 2.0 Client ID. Optional; Google sign-in remains disabled if unset. |
| `GOOGLE_CLIENT_SECRET` | `None` | `None` | Google OAuth 2.0 Client Secret. Optional; Google sign-in remains disabled if unset. |
| `TESTING` | `False` | `False` | Flask testing mode flag (`True` in `TestingConfig`). |

### Email Delivery Configuration (Phase 10.11)

The application supports real email delivery for customer account verification and password reset workflows via standard SMTP, with a robust offline mode for development and presentations.

#### Backends: Memory vs SMTP

- **`EMAIL_BACKEND=memory` (Default):**
  - Completely offline. Sent messages are stored in memory without network or SMTP I/O.
  - Used by default in all automated tests and ideal for offline development or school presentations.
  - Set `EMAIL_DEV_CONSOLE_PREVIEW=True` to print formatted email previews directly to the terminal when emails are generated.
- **`EMAIL_BACKEND=smtp`:**
  - Real email delivery using Python's standard `smtplib` and `email.message.EmailMessage`.
  - Supports STARTTLS on port 587 and implicit TLS on port 465 with certificate verification.
  - Refuses to transmit credentials over unencrypted connections (`security="none"`).
  - Emits UTF-8 plain-text messages with trusted absolute links derived strictly from `APP_BASE_URL`.

#### Configuring SMTP Locally

Create or edit your project-local `.env` file (copied from `.env.example`):

```env
# Enable SMTP delivery
EMAIL_BACKEND=smtp
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_SECURITY=starttls
SMTP_USERNAME=your_address@gmail.com
SMTP_PASSWORD=your_16_character_app_password
SMTP_FROM_ADDRESS=your_address@gmail.com
SMTP_TIMEOUT=10
APP_BASE_URL=http://127.0.0.1:5000
```

#### Gmail Setup & App Passwords

When sending through Gmail:
1. Ensure **2-Step Verification** is enabled on your Google account (**Google Account &rarr; Security &rarr; 2-Step Verification**).
2. Go to **App passwords** (**Security &rarr; 2-Step Verification &rarr; App passwords**).
3. Enter an app name (e.g. `Online Food Shop`) and click **Create**.
4. Copy the generated 16-character password into `SMTP_PASSWORD` in your `.env` file (without spaces).
5. Set `SMTP_HOST=smtp.gmail.com`, `SMTP_PORT=587`, `SMTP_SECURITY=starttls`, and set `SMTP_USERNAME` and `SMTP_FROM_ADDRESS` to your Gmail address.

> **Security Warning:** Never commit `.env` or expose credentials. The `.env` file is excluded from Git via `.gitignore`.

#### Offline Development & School Presentation Mode

For offline demonstrations (such as the school presentation without internet or SMTP credentials):
1. Keep `EMAIL_BACKEND=memory` in `.env` (or omit it to use the default).
2. Set `EMAIL_DEV_CONSOLE_PREVIEW=True` in `.env`.
3. When registering a customer or requesting a password reset, the application will display clean, simulated email previews in the server console, including the exact activation or recovery link to click:
   ```text
   ======================================================================
   [SIMULATED EMAIL - DEV PREVIEW]
   To:      customer@example.com
   Subject: Verify your email address
   Type:    verification
   Link:    http://127.0.0.1:5000/verify-email/<token>
   ======================================================================
   ```

#### Troubleshooting Email Delivery

- **Authentication Failed (`SMTPAuthenticationError` / 535):**
  - Verify `SMTP_USERNAME` is your full email address.
  - For Gmail, Yahoo, or Outlook, verify you are using an **App Password**, not your account login password.
  - Check for unintended trailing whitespace in `.env`.
- **TLS / SSL Errors (`SSLError`):**
  - Ensure `SMTP_PORT` and `SMTP_SECURITY` match: port `587` requires `starttls`, and port `465` requires `ssl`.
  - Ensure system CA certificates are up to date.
- **Emails Arriving in Spam / Junk Folders:**
  - When sending from local development environments, public email providers (Gmail, Outlook) may classify messages without SPF/DKIM alignment as spam.
  - Ensure `SMTP_FROM_ADDRESS` exactly matches `SMTP_USERNAME`.
  - Check the Spam or Junk folder in the recipient inbox and mark the message as "Not Spam".
- **Delivery Guarantees:**
  - Note that standard SMTP confirms acceptance by the upstream relay server, not guaranteed inbox delivery by downstream recipient mail filters.
- **Connection Timeouts:**
  - If your local ISP or network blocks outbound port 25 or 587, try port 465 with `SMTP_SECURITY=ssl`.

### Google OAuth 2.0 / OpenID Connect Setup

Google sign-in and registration remain completely disabled until both `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET` are configured. When these variables are omitted or empty, the application starts normally and hides Google authentication options.

To enable Google sign-in:

- **Windows (PowerShell):**
  ```powershell
  $env:GOOGLE_CLIENT_ID = "your-client-id.apps.googleusercontent.com"
  $env:GOOGLE_CLIENT_SECRET = "your-client-secret"
  ```

- **Windows (Command Prompt):**
  ```cmd
  set GOOGLE_CLIENT_ID=your-client-id.apps.googleusercontent.com
  set GOOGLE_CLIENT_SECRET=your-client-secret
  ```

- **Linux / macOS (Bash/Zsh):**
  ```bash
  export GOOGLE_CLIENT_ID="your-client-id.apps.googleusercontent.com"
  export GOOGLE_CLIENT_SECRET="your-client-secret"
  ```

In the Google Cloud Console (APIs & Services &rarr; Credentials):
- **Authorized redirect URI:** `http://localhost:5000/auth/google/callback` (or your production HTTPS domain).

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

Features deferred to subsequent development phases:
- Payment processing integration (Stripe, Phase 10.12).
- Windows packaging and standalone launch scripts (`setup.bat`, `start.bat`, Phase 11.1).
- Product catalog image file upload management.

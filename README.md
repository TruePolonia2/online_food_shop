# Online Food Shop

A clean, maintainable web application for an online food shop, built as a school project.

## Technology Stack

- **Backend:** Python, Flask
- **Database:** SQLite
- **Frontend / Templating:** HTML5, Jinja2
- **Styling:** Tailwind CSS (CDN for development, build pipeline optional)
- **Testing:** pytest

## Architecture Overview

The project follows a layered architecture with strict separation of concerns and the Flask application factory pattern:

- `app/models/`: Domain entities and data structures.
- `app/repositories/`: Database access and query operations.
- `app/services/`: Business rules and coordination logic.
- `app/routes/`: HTTP endpoints and request handling.
- `app/database/`: SQLite schema definitions and migrations/scripts.
- `app/templates/`: Jinja2 presentation templates extending `base.html`.
- `app/static/`: CSS, JavaScript, and image assets.
- `tests/`: Automated unit and integration tests.

> **Project Status:** Database Layer implemented (Phase 3). Domain entities, SQLite schema, connection lifecycle, and CLI database initialization are active. Shop routes and repositories will follow.

## Prerequisites

- **Python 3.10+** (Python 3.14+ supported)
- **Node.js & npm** (optional for Tailwind CSS asset builds; development uses CDN)

## Setup and Running

1. **Clone or open the repository:**
   ```bash
   cd online_food_shop
   ```

2. **Create and activate a virtual environment:**
   - On Windows (PowerShell):
     ```powershell
     python -m venv .venv
     .venv\Scripts\Activate.ps1
     ```
   - On Windows (Command Prompt):
     ```cmd
     python -m venv .venv
     .venv\Scripts\activate.bat
     ```
   - On Linux / macOS:
     ```bash
     python3 -m venv .venv
     source .venv/bin/activate
     ```

3. **Install dependencies:**
   ```bash
   pip install -r requirements.txt
   ```

4. **Initialize the SQLite database:**
   To create or ensure all tables and indexes exist safely and idempotently without deleting existing data:
   ```bash
   flask --app run.py init-db
   ```
   Alternatively, you can run the connection module directly:
   ```bash
   python -m app.database.connection
   ```

5. **Run the automated test suite:**
   ```bash
   pytest
   ```

6. **Start the development server:**
   ```bash
   python run.py
   ```
   Open [http://127.0.0.1:5000](http://127.0.0.1:5000) in your browser.

## Database Design & Conventions

The SQLite database is managed via `app/database/connection.py` and `app/database/schema.sql`:

- **Tables:** `categories`, `products`, `customers`, `orders`, `order_items`. Carts remain transient/in-memory domain models.
- **Identifiers:** Standard `INTEGER PRIMARY KEY`.
- **Foreign Keys:** Enforced on every connection (`PRAGMA foreign_keys = ON;`).
  - `products.category_id`: `ON DELETE SET NULL`
  - `orders.customer_id`: `ON DELETE RESTRICT`
  - `order_items.order_id`: `ON DELETE CASCADE`
  - `order_items.product_id`: `ON DELETE RESTRICT`
- **Monetary Values:** Stored as integer minor currency units (e.g. cents, $10.00 = `1000`) matching domain entities.
- **Timestamps:** ISO 8601 UTC text strings (`YYYY-MM-DDTHH:MM:SSZ`).
- **Connection Scope:** Request-scoped via Flask `g`, automatically closed via `teardown_appcontext`.
- **Transactions:** Handled explicitly with `with transaction():` or standard connection context manager.

-- SQLite Database Schema for Online Food Shop
-- =============================================================================
-- Invariants & Conventions:
-- 1. Identifiers: INTEGER PRIMARY KEY (rowid aliases with automatic 64-bit increment).
-- 2. Foreign Keys: Must be enforced via 'PRAGMA foreign_keys = ON;' on every connection.
-- 3. Monetary Values: Stored as integer minor currency units (e.g., cents, $10.50 = 1050)
--    to prevent floating-point rounding errors, matching the domain model design.
-- 4. Timestamps: Stored as ISO 8601 UTC text strings formatted as 'YYYY-MM-DDTHH:MM:SSZ'
--    using SQLite's strftime('%Y-%m-%dT%H:%M:%SZ', 'now'). This allows chronological
--    string sorting and clean parsing with Python's datetime.fromisoformat.
-- 5. Repeatable Execution: All DDL statements use IF NOT EXISTS to guarantee idempotent,
--    non-destructive initialization without data loss.
-- 6. Cart Persistence: Shopping carts remain transient / in-memory or session-based;
--    no persistent cart tables are created.
-- =============================================================================

PRAGMA foreign_keys = ON;

-- -----------------------------------------------------------------------------
-- 1. categories
-- -----------------------------------------------------------------------------
-- Product categories for catalog navigation and filtering.
CREATE TABLE IF NOT EXISTS categories (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE CHECK(length(trim(name)) > 0),
    description TEXT
);

-- -----------------------------------------------------------------------------
-- 2. products
-- -----------------------------------------------------------------------------
-- Food shop catalog items with price in minor units (cents) and inventory counts.
-- Deleting a category sets category_id to NULL to prevent catalog data loss.
CREATE TABLE IF NOT EXISTS products (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL CHECK(length(trim(name)) > 0),
    price INTEGER NOT NULL CHECK(price >= 0),
    stock_quantity INTEGER NOT NULL DEFAULT 0 CHECK(stock_quantity >= 0),
    description TEXT,
    category_id INTEGER,
    is_active INTEGER NOT NULL DEFAULT 1 CHECK(is_active IN (0, 1)),
    FOREIGN KEY (category_id) REFERENCES categories (id) ON DELETE SET NULL
);

-- Index to optimize catalog queries filtered or sorted by category and foreign key checks
CREATE INDEX IF NOT EXISTS idx_products_category_id ON products (category_id);

-- -----------------------------------------------------------------------------
-- 3. customers
-- -----------------------------------------------------------------------------
-- Customer records. Email is unique and normalized lowercase at the application layer.
CREATE TABLE IF NOT EXISTS customers (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL CHECK(length(trim(name)) > 0),
    email TEXT NOT NULL UNIQUE CHECK(length(trim(email)) > 0 AND instr(email, '@') > 0),
    phone TEXT,
    address TEXT
);

-- -----------------------------------------------------------------------------
-- 4. orders
-- -----------------------------------------------------------------------------
-- Customer orders. Deleting a customer is restricted if they have existing orders
-- to preserve historical financial and order records.
-- Status is restricted to valid OrderStatus enumeration values.
-- Timestamps are recorded in ISO 8601 UTC format.
-- Preserves immutable customer/delivery snapshot captured at checkout time
-- to protect privacy and preserve historical records against profile changes.
CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY,
    customer_id INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending', 'confirmed', 'paid', 'shipped', 'delivered', 'cancelled')),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    customer_name TEXT,
    customer_email TEXT,
    customer_phone TEXT,
    customer_address TEXT,
    FOREIGN KEY (customer_id) REFERENCES customers (id) ON DELETE RESTRICT
);

-- Index to optimize querying customer order history and foreign key checks
CREATE INDEX IF NOT EXISTS idx_orders_customer_id ON orders (customer_id);

-- -----------------------------------------------------------------------------
-- 5. order_items
-- -----------------------------------------------------------------------------
-- Individual line items in an order.
-- Preserves snapshot of product name and unit price (minor currency units) at purchase
-- time so historical orders are immutable against future catalog modifications.
-- Deleting an order cascades to its line items. Deleting a product referenced in an
-- existing order is restricted to preserve purchase history.
CREATE TABLE IF NOT EXISTS order_items (
    id INTEGER PRIMARY KEY,
    order_id INTEGER NOT NULL,
    product_id INTEGER NOT NULL,
    product_name TEXT NOT NULL CHECK(length(trim(product_name)) > 0),
    unit_price INTEGER NOT NULL CHECK(unit_price >= 0),
    quantity INTEGER NOT NULL CHECK(quantity > 0),
    FOREIGN KEY (order_id) REFERENCES orders (id) ON DELETE CASCADE,
    FOREIGN KEY (product_id) REFERENCES products (id) ON DELETE RESTRICT
);

-- Indexes to optimize retrieval of line items by order and foreign key cascade/restrict checks
CREATE INDEX IF NOT EXISTS idx_order_items_order_id ON order_items (order_id);
CREATE INDEX IF NOT EXISTS idx_order_items_product_id ON order_items (product_id);

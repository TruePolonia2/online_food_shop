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
    description TEXT,
    is_active INTEGER NOT NULL DEFAULT 1 CHECK(is_active IN (0, 1))
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
    image_url TEXT,
    FOREIGN KEY (category_id) REFERENCES categories (id) ON DELETE SET NULL
);

-- Index to optimize catalog queries filtered or sorted by category and foreign key checks
CREATE INDEX IF NOT EXISTS idx_products_category_id ON products (category_id);

-- -----------------------------------------------------------------------------
-- 3. users
-- -----------------------------------------------------------------------------
-- User accounts for authentication identity and authorization roles.
-- Email has case-insensitive uniqueness (COLLATE NOCASE) and structural validation.
-- password_hash stores cryptographic hash (scrypt). Nullable to support existing rows
-- without credentials; non-null values must be non-empty strings.
-- Role is restricted to 'customer' or 'owner'.
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    email TEXT NOT NULL UNIQUE COLLATE NOCASE CHECK(length(trim(email)) > 0 AND instr(email, '@') > 0),
    password_hash TEXT CHECK(password_hash IS NULL OR length(trim(password_hash)) > 0),
    role TEXT NOT NULL DEFAULT 'customer' CHECK(role IN ('customer', 'owner')),
    is_verified INTEGER NOT NULL DEFAULT 0 CHECK(is_verified IN (0, 1)),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

-- -----------------------------------------------------------------------------
-- 4. email_verification_tokens
-- -----------------------------------------------------------------------------
-- Single-use tokens for email address verification.
-- Stores SHA-256 hash of the token, never the raw token string.
-- Automatically deleted if the parent user is deleted (ON DELETE CASCADE).
CREATE TABLE IF NOT EXISTS email_verification_tokens (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL,
    token_hash TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    expires_at TEXT NOT NULL,
    used_at TEXT,
    FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE
);

-- Index to optimize querying recent tokens for a user (e.g. cooldown checks)
CREATE INDEX IF NOT EXISTS idx_verification_tokens_user_id ON email_verification_tokens (user_id);

-- -----------------------------------------------------------------------------
-- 4b. password_reset_tokens
-- -----------------------------------------------------------------------------
-- Single-use tokens for secure password reset and account recovery.
-- Stores SHA-256 hash of the token, never the raw token string.
-- Automatically deleted if the parent user is deleted (ON DELETE CASCADE).
CREATE TABLE IF NOT EXISTS password_reset_tokens (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL,
    token_hash TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    expires_at TEXT NOT NULL,
    used_at TEXT,
    FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE
);

-- Index to optimize querying tokens for a user and ordering
CREATE INDEX IF NOT EXISTS idx_password_reset_tokens_user_id ON password_reset_tokens (user_id);

-- -----------------------------------------------------------------------------
-- 5. customers
-- -----------------------------------------------------------------------------
-- Customer records. Email is unique and normalized lowercase at the application layer.
-- user_id is an optional link to an authenticated User account (NULL for guest profiles).
-- Deleting a user sets user_id to NULL to preserve contact/delivery profile history.
CREATE TABLE IF NOT EXISTS customers (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL CHECK(length(trim(name)) > 0),
    email TEXT NOT NULL UNIQUE CHECK(length(trim(email)) > 0 AND instr(email, '@') > 0),
    phone TEXT,
    address TEXT,
    user_id INTEGER,
    FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE SET NULL
);

-- -----------------------------------------------------------------------------
-- 5. orders
-- -----------------------------------------------------------------------------
-- Customer orders. Deleting a customer is restricted if they have existing orders
-- to preserve historical financial and order records.
-- user_id optionally identifies the authenticated user account (NULL for guest orders).
-- Deleting a user account sets user_id to NULL to preserve historical order records.
-- Status is restricted to valid OrderStatus enumeration values.
-- Timestamps are recorded in ISO 8601 UTC format.
-- Preserves immutable customer/delivery snapshot captured at checkout time
-- to protect privacy and preserve historical records against profile changes.
CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY,
    customer_id INTEGER NOT NULL,
    user_id INTEGER,
    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending', 'confirmed', 'paid', 'shipped', 'delivered', 'cancelled')),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    customer_name TEXT,
    customer_email TEXT,
    customer_phone TEXT,
    customer_address TEXT,
    FOREIGN KEY (customer_id) REFERENCES customers (id) ON DELETE RESTRICT,
    FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE SET NULL
);

-- Index to optimize querying customer order history and foreign key checks
CREATE INDEX IF NOT EXISTS idx_orders_customer_id ON orders (customer_id);

-- -----------------------------------------------------------------------------
-- 6. order_items
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

-- -----------------------------------------------------------------------------
-- 7. user_external_identities
-- -----------------------------------------------------------------------------
-- External identity provider links (OAuth / OpenID Connect).
-- Links an external subject identifier (e.g. Google sub) to an internal user.
-- provider is normalized lowercase (e.g. 'google').
-- provider_user_id is the unique subject identifier from the provider.
-- UNIQUE(provider, provider_user_id) ensures one provider account links to at most one user.
-- UNIQUE(user_id, provider) ensures a user has at most one linked account per provider.
-- Deleting a user cascades to remove their external identities.
CREATE TABLE IF NOT EXISTS user_external_identities (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL,
    provider TEXT NOT NULL CHECK(length(trim(provider)) > 0),
    provider_user_id TEXT NOT NULL CHECK(length(trim(provider_user_id)) > 0),
    email TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE,
    UNIQUE (provider, provider_user_id),
    UNIQUE (user_id, provider)
);

CREATE INDEX IF NOT EXISTS idx_external_identities_user_id ON user_external_identities (user_id);
CREATE INDEX IF NOT EXISTS idx_external_identities_provider_sub ON user_external_identities (provider, provider_user_id);

# Windows School Demonstration Guide

**Project:** Flask + SQLite + Jinja2 + Tailwind CSS Online Food Shop  
**Target Environment:** Windows 10 / Windows 11  
**Python Compatibility:** Python 3.10+ (tested with Python 3.14)  

---

## 1. Overview

This guide provides step-by-step instructions for running the **Online Food Shop** project for a classroom demonstration or school presentation on Windows.

The setup is designed to be:
- **Simple:** Uses two single-click batch scripts (`setup_demo.bat` and `start_demo.bat`).
- **Isolated:** Installs all dependencies into a local virtual environment (`.venv`) without modifying global Python installations.
- **Secure:** Binds exclusively to local loopback (`127.0.0.1`), disables debug mode, and prevents external network exposure.
- **Offline-Capable:** Fully functional for storefront browsing, product search/filtering, shopping cart, customer registration, password management, administrative dashboard, and bilingual English/Portuguese localization without requiring active internet or third-party cloud services.

---

## 2. Prerequisites & Python Requirements

1. **Operating System:** Windows 10 or 11 (64-bit).
2. **Python:** Python **3.10** or newer.
   - If not installed, download the official installer from [python.org/downloads/windows/](https://www.python.org/downloads/windows/).
   - **CRITICAL INSTALLATION STEP:** On the first installer screen, you **must check** the box:
     `[x] Add python.exe to PATH`
     before clicking "Install Now".
3. **Hardware / Network:**
   - Internet connection is required **only once** during initial `setup_demo.bat` execution to download Python packages.
   - Once setup is complete, the presentation can run **completely offline**.

---

## 3. Files to Copy vs. What NOT to Include

### What to Include in a School Demo Package
When copying or sharing the project folder onto a presentation machine or USB drive, include:
- `app/` (application source code, models, repositories, routes, templates, static assets)
- `translations/` (English and European Portuguese translation catalogs)
- `tests/` (automated test suite)
- `babel.cfg` (localization configuration)
- `requirements.txt` (pinned dependencies)
- `run.py` (application entry point)
- `setup_demo.bat` (automated one-click setup script)
- `start_demo.bat` (automated one-click launch script)
- `WINDOWS_DEMO_GUIDE.md` (this presentation manual)
- `README.md` (project overview)
- `.env.example` (template configuration)

### What NEVER to Include (Do NOT Commit or Share)
To protect security, confidentiality, and data hygiene:
- **`.env`:** Never copy or commit your local `.env` containing private credentials, real API keys, or live passwords.
- **`instance/online_food_shop.sqlite3`:** Never distribute real development or personal databases that may contain customer names, order histories, or hashed credentials.
- **`.venv/`:** Do not copy virtual environment directories across machines (binaries and Python paths are machine-specific). Let `setup_demo.bat` recreate `.venv` cleanly.
- **`__pycache__/`, `.pytest_cache/`:** Temporary bytecode caches.

---

## 4. First-Time Setup (`setup_demo.bat`)

1. Open File Explorer and navigate to the project root folder.
2. Double-click **`setup_demo.bat`** (or run `setup_demo.bat` in Command Prompt).
3. The script will automatically:
   - Verify Python 3.10+ is detected.
   - Create an isolated `.venv` folder if missing.
   - Install all required libraries (`Flask`, `Flask-Babel`, `Authlib`, `requests`, `python-dotenv`, `stripe`, `pytest`).
   - Compile translation catalogs for English (`en`) and European Portuguese (`pt_PT`).
   - Create a safe local `.env` file from `.env.example` if none exists.
   - Initialize the SQLite database schema idempotently (`CREATE TABLE IF NOT EXISTS`).
4. Once completed, you will see `Demo Setup Completed Successfully!` in the terminal.

---

## 5. Store Owner / Admin Account Preparation

To demonstrate the **Admin Dashboard** (`/admin`), you can create a verified store owner account locally using the supported CLI:

1. Open a Command Prompt in the project folder.
2. Run the following command:
   ```cmd
   .venv\Scripts\python.exe -m flask --app run.py create-owner
   ```
3. Enter the desired owner email (e.g., `owner@school.local` or `admin@foodshop.local`).
4. Enter and confirm a strong password (minimum 8 characters with at least one uppercase letter, one lowercase letter, one digit, and one special character, e.g. `AdminDemo2026!`). The password input will remain hidden while typing.
5. The command will securely hash the password with `scrypt` and create the verified owner account.

---

## 6. Starting the Demonstration (`start_demo.bat`)

1. Double-click **`start_demo.bat`** (or run `start_demo.bat` in Command Prompt).
2. The script will:
   - Activate the local virtual environment.
   - Disable Flask debug mode (`FLASK_DEBUG=0`) to prevent intrusive debugger popups.
   - Bind strictly to `127.0.0.1:5000` (loopback only).
   - Display the URL to open.
3. Open your web browser (Chrome, Edge, Firefox) and navigate to:
   ```text
   http://127.0.0.1:5000
   ```
4. **Verifying the App is Running:**
   - The storefront page will load with the approved circular food-shop emblem, navigation bar, language switcher (`EN` / `PT`), theme toggle, and catalog.
   - Click the theme toggle to showcase dark/light mode switching.
   - Click `PT` / `EN` in the navbar to showcase instant bilingual localization.

---

## 7. Safe Shutdown

To stop the demonstration server safely:
1. Return to the Command Prompt window running `start_demo.bat`.
2. Press **`Ctrl + C`**.
3. When prompted `Terminate batch job (Y/N)?`, type **`Y`** and press **Enter**.
4. The server and all SQLite connections will close cleanly without data corruption.

---

## 8. Offline vs. Online Capabilities

| Feature | Fully Offline | With Internet Only | With External Credentials |
| :--- | :---: | :---: | :---: |
| **Product Catalog & Filtering** | Yes | Yes | Yes |
| **Shopping Cart (Add/Update/Clear)** | Yes | Yes | Yes |
| **Light / Dark Mode Switching** | Yes | Yes | Yes |
| **English / Portuguese (pt-PT) Toggle** | Yes | Yes | Yes |
| **Local Account Registration & Login** | Yes | Yes | Yes |
| **Simulated Email Preview (Console)** | Yes | Yes | Yes |
| **Password Reset (via Console Link)** | Yes | Yes | Yes |
| **Admin Dashboard & Inventory Management** | Yes | Yes | Yes |
| **Order History & Detail Views** | Yes | Yes | Yes |
| **Real SMTP Outbound Emails** | No | Yes | Requires SMTP Host & Password |
| **Google Sign-In / OAuth 2.0** | No | Yes | Requires Google Client ID & Secret |
| **Stripe Hosted Checkout** | No | Yes | Requires Stripe Test Keys (`sk_test_*`) |

### How to Present Offline Without External Services
- **Simulated Email Delivery:** With `EMAIL_BACKEND=memory` (default) and `EMAIL_DEV_CONSOLE_PREVIEW=True`, when a customer registers or requests a password reset, a formatted box is printed directly to the running server terminal containing the exact verification or reset link. You can copy/paste or click this link directly in your browser to demonstrate email verification live on stage!
- **Google Sign-In:** When `GOOGLE_CLIENT_ID` is omitted, Google login buttons are hidden automatically, keeping the UI clean and focusing attention on local authentication.
- **Stripe Checkout:** When Stripe test keys are omitted, the shop continues to run normally; the cart displays items, and checkout explains that card payments are in maintenance mode. If test keys (`sk_test_*`) are configured and internet is present, Stripe hosted checkout works in test mode (using card `4242 4242 4242 4242`).

---

## 9. Demonstration Catalog Seeder (`flask seed-demo`) & Safety Hardening

For an optimal presentation, a fresh demo database needs representative fictional products without copying real customer or order records.

### Safe Catalog Seeding
Run the dedicated CLI command:
```cmd
.venv\Scripts\python.exe -m flask --app run.py seed-demo
```

### Safety Principles & Hardening Rules
1. **Explicit Execution:** Never runs automatically on startup; must be explicitly executed by the presenter.
2. **Idempotent & Non-Destructive:** Running the command repeatedly safely skips already existing categories and products by name without creating duplicates or overwriting custom items.
3. **Fictional Data Only:**
   - **4 Sample Categories:** *Fresh Bakery*, *Organic Produce*, *Artisan Dairy*, *Pantry & Olive Oils*.
   - **8 Realistic Products:** Artisan Sourdough Loaf (3,50 €), Butter Croissant 4-Pack (4,20 €), Organic Hass Avocados (2,99 €), Sweet Cherry Tomatoes (2,10 €), Farm Fresh Whole Milk (1,25 €), Artisan Sheep Cheese (5,80 €), Extra Virgin Olive Oil (7,50 €), Organic Raw Honey (6,20 €). All prices stored as integer euro cents with positive stock and zero external image dependencies.
4. **Zero Fabricated Orders:** The command intentionally creates **zero fake paid, confirmed, or shipped orders**. The Admin dashboard order counter starts at zero, allowing the presenter to demonstrate real-time order placement and lifecycle transitions live during the demo.
5. **Database Isolation (`instance/demo_food_shop.sqlite3`):**
   - In fresh setups, `setup_demo.bat` creates `.env` with `DATABASE_PATH=instance/demo_food_shop.sqlite3`.
   - Your primary development database (`instance/online_food_shop.sqlite3`) remains untouched.
6. **Strict Refusal Guards:**
   - **Production Guard:** Refuses execution immediately if `is_production_env()` or `ENV_NAME="production"`.
   - **Order Guard:** Refuses execution if the active database already contains customer orders, unless `--force` or `DEMO_MODE=1` is explicitly set.
7. **Random Cryptographic `SECRET_KEY`:**
   - `setup_demo.bat` generates a unique 32-byte hex key (`secrets.token_hex(32)`) when creating `.env`.
   - The insecure development placeholder (`dev-secret-key-change-in-production`) is avoided, and secret values are never printed to logs or console output.


---

## 10. Troubleshooting FAQ

- **"Python was not recognized as an internal or external command":**
  - Python was installed without checking "Add python.exe to PATH". Re-run the Python installer, choose "Modify", and check the PATH option.
- **Port 5000 is already in use:**
  - Close any existing Flask servers or background terminal sessions.
  - Or edit `start_demo.bat` to set `set "PORT=5001"`.
- **"Virtual environment not found":**
  - Ensure you ran `setup_demo.bat` first.
- **Language switcher doesn't translate new strings:**
  - `setup_demo.bat` compiles translation catalogs automatically. You can also recompile manually:
    ```cmd
    .venv\Scripts\python.exe -m babel.messages.frontend compile -d translations
    ```

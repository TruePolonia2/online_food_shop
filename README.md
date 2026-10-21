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

> **Project Status:** Initial project structure and configuration (Phase 1). Domain features, routes, database schemas, and shop capabilities will be added in upcoming phases.

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

4. **Start the development server:**
   ```bash
   python run.py
   ```
   Open [http://127.0.0.1:5000](http://127.0.0.1:5000) in your browser.

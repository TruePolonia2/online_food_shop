"""Pytest configuration and test environment isolation fixtures."""
import os

# Deterministically isolate test suites from the project-local .env file.
# Setting FLASK_SKIP_DOTENV=1 ensures tests never inadvertently read developer
# credentials, debug flags, or local overrides from the workspace .env.
os.environ["FLASK_SKIP_DOTENV"] = "1"

"""Set minimum required env vars before any module imports config.settings."""
import os

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("TELEGRAM_USER_ID", "12345")
os.environ.setdefault("DATA_API_URL", "http://data-api-test:8001")
os.environ.setdefault("DATA_API_KEY", "test-key")

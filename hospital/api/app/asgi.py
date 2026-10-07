"""uvicorn entry point: `uvicorn app.asgi:app` (settings from the environment / .env)."""

from app.main import create_checked_app

app = create_checked_app()

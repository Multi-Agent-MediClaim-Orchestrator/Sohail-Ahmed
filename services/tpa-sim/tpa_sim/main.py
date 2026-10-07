"""``uvicorn tpa_sim.main:app --port 8500``."""

from .app import create_app

app = create_app()

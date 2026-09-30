"""Gunicorn entry point for the headless container: server.py's Flask app
plus the /api/v1 layer. Run with `gunicorn wsgi:app` (see Dockerfile)."""
from server import app
from api import init_api

init_api(app)

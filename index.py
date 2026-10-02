"""Vercel entrypoint: Vercel looks for a FastAPI instance named `app` in a root index.py."""

from app.main import app

__all__ = ["app"]

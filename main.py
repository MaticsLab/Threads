"""Entrypoint shim.

Railway's Railpack/Nixpacks builder starts FastAPI apps with
`uvicorn main:app` when no start command is configured; the app itself
lives in app.py. Both `uvicorn app:app` and `uvicorn main:app` work.
"""
from app import app

__all__ = ['app']

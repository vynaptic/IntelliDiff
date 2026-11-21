"""The public HTTP API v1 (FastAPI) and the UI it serves."""

from deltasleuth.api.app import create_app

__all__ = ["create_app"]

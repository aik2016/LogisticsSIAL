"""Railway and other hosts default to ``main:app``; the real app lives in ``app.py``."""
from app import app  # noqa: F401
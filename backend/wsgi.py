"""WSGI entry for production process managers.

Usage::

    gunicorn -b 0.0.0.0:5000 -w 2 -k gthread --threads 4 wsgi:app

Equivalent to importing ``app`` from ``app.py`` / package factory.
Public name ``app`` is stable for deploy configs.
"""

from __future__ import annotations

from app import create_app

app = create_app()

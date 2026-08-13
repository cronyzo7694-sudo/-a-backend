"""WSGI entry — MUST stay cheap to import.

Render scans for an open port within ~5 minutes. If we call create_app()
at import time, file-bank parse + Neon schema blocks gunicorn BEFORE it
binds → "No open ports" / deploy timeout.

`app` is a lazy WSGI wrapper: import is instant, bind happens immediately,
Flask boots on the first request (or when a worker warms up).
"""

from __future__ import annotations

import logging

logger = logging.getLogger("exam_os.wsgi")


class LazyFlaskApp:
    """WSGI callable that builds the real Flask app on first use."""

    def __init__(self) -> None:
        self._app = None

    def _load(self):
        if self._app is None:
            logger.warning("Bootstrapping Flask app (first load)…")
            from app import create_app

            self._app = create_app()
            try:
                from debug_panel import debug_bp, install_debug_handlers

                self._app.register_blueprint(debug_bp)
                install_debug_handlers(self._app)
            except ModuleNotFoundError:
                pass
            except Exception:
                logger.exception("debug panel skipped")
            logger.warning("Flask app ready")
        return self._app

    def __call__(self, environ, start_response):
        return self._load()(environ, start_response)

    def __getattr__(self, name):
        # gunicorn / flask-migrate sometimes poke attributes
        return getattr(self._load(), name)


app = LazyFlaskApp()

"""WSGI entry for production process managers.

Usage:

    gunicorn --bind 0.0.0.0:$PORT wsgi:app

Public name ``app`` is stable for Render/Gunicorn deploy configs.
"""

from __future__ import annotations

import logging

from app import create_app


logger = logging.getLogger("exam_os.wsgi")


# Main Flask application create karo
app = create_app()


# Temporary debug panel attach karo
#
# debug_panel.py isi folder me hona chahiye:
# backend/debug_panel.py
#
# Agar debug_panel.py missing hua to backend start nahi hoga,
# isliye import ko safe rakha gaya hai.
try:
    from debug_panel import debug_bp, install_debug_handlers

    app.register_blueprint(debug_bp)
    install_debug_handlers(app)

    logger.warning(
        "DEBUG PANEL ENABLED: /debug, /debug/routes, "
        "/debug/health, /debug/errors"
    )

except ModuleNotFoundError as exc:
    if exc.name == "debug_panel":
        logger.warning(
            "debug_panel.py not found; starting backend without debug panel"
        )
    else:
        raise

except Exception:
    logger.exception("Failed to register debug panel")
    raise

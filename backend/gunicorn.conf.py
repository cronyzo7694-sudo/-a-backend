import os

bind = f"0.0.0.0:{os.getenv('PORT', '10000')}"

# Never preload — preload imports wsgi + create_app BEFORE bind.
preload_app = False

workers = int(os.getenv("WEB_CONCURRENCY", "1"))
worker_class = "sync"
threads = 1
timeout = int(os.getenv("GUNICORN_TIMEOUT", "120"))
graceful_timeout = 30
keepalive = 5

accesslog = "-"
errorlog = "-"
loglevel = os.getenv("GUNICORN_LOGLEVEL", "info")

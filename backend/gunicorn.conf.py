"""
Gunicorn config tuned for a small (free-tier) box serving a CBT exam API.

Why gthread + many threads:
  * The workload is I/O-bound (DB round-trips), not CPU-bound.
  * Threads give high concurrency with low memory (important on 512MB free tier).
  * A few processes + many threads handles bursts (exam start rush) far better
    than the default 1 sync worker (which serializes every request -> hang).

Override any value with the matching env var on Render.
"""
import os

# Bind to the port Render provides.
bind = f"0.0.0.0:{os.getenv('PORT', '5000')}"

# Processes: keep small on free tier RAM; 2 is a good default.
workers = int(os.getenv("WEB_CONCURRENCY", "2"))

# Threaded workers = cheap concurrency for I/O-bound API.
worker_class = "gthread"
threads = int(os.getenv("GUNICORN_THREADS", "8"))

# Queue backlog so a burst of exam-start requests waits instead of erroring.
backlog = 2048

# Recycle workers occasionally to avoid slow memory creep.
max_requests = int(os.getenv("GUNICORN_MAX_REQUESTS", "1000"))
max_requests_jitter = 200

# Timeouts: generous enough for cold DB, short enough to free stuck workers.
timeout = int(os.getenv("GUNICORN_TIMEOUT", "60"))
graceful_timeout = 30
keepalive = 5

# Preload app so all workers share the imported code (faster boot, less RAM).
preload_app = True

# Logging to stdout/stderr (Render captures these).
accesslog = "-"
errorlog = "-"
loglevel = os.getenv("GUNICORN_LOGLEVEL", "info")

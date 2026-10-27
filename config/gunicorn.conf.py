"""gunicorn settings for production (the image's default command and docker-compose.prod.yml).

Processes scale with the machine and each serves several requests at once, so one slow download (a big Excel
export takes seconds) doesn't hold a third of the server (QA-105). Override with GUNICORN_WORKERS,
GUNICORN_THREADS and GUNICORN_TIMEOUT in .env; each worker process needs about 90 MB.
"""
import multiprocessing
import os

bind = "0.0.0.0:8000"
workers = int(os.environ.get("GUNICORN_WORKERS") or min(2 * multiprocessing.cpu_count() + 1, 9))
worker_class = "gthread"
threads = int(os.environ.get("GUNICORN_THREADS") or 4)
timeout = int(os.environ.get("GUNICORN_TIMEOUT") or 120)
accesslog = "-"

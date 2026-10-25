FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app
# Exact versions with hashes (requirements.lock, compiled from requirements.txt): every build installs what was tested.
COPY requirements.lock .
RUN pip install --require-hashes --no-deps -r requirements.lock
COPY . .

RUN mkdir -p /app/media /app/staticfiles && useradd --create-home app && chown -R app /app
USER app

EXPOSE 8000
CMD ["gunicorn", "config.wsgi:application", "-c", "config/gunicorn.conf.py"]

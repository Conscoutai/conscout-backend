FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    g++ \
    libgl1 \
    libglib2.0-0 \
    poppler-utils \
  && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000

CMD ["sh", "-c", "gunicorn -k uvicorn.workers.UvicornWorker app_main:app --bind 0.0.0.0:${PORT:-8000} --workers ${GUNICORN_WORKERS:-1} --timeout ${GUNICORN_TIMEOUT:-900} --graceful-timeout ${GUNICORN_GRACEFUL_TIMEOUT:-900} --keep-alive ${GUNICORN_KEEP_ALIVE:-120}"]

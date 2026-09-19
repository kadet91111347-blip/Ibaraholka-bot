# Ibaraholka — production Dockerfile
# Multi-stage build for smaller image

FROM python:3.12-slim AS builder

WORKDIR /app

# Install build deps for psycopg2-binary
RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Install Python deps into isolated prefix
COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

# ---
FROM python:3.12-slim

WORKDIR /app

# Runtime deps only (psycopg2 needs libpq)
RUN apt-get update && apt-get install -y --no-install-recommends \
        libpq5 \
    && rm -rf /var/lib/apt/lists/*

# Copy installed packages from builder
COPY --from=builder /install /usr/local

# Copy app
COPY main.py db_adapter.py ./
COPY miniapp ./miniapp

# Persistent SQLite dir (mount a volume here for Render Disk / Railway Volume)
RUN mkdir -p /data && chmod 755 /data

# Non-root user for security
RUN useradd -m -u 1001 ibaraholka && chown -R ibaraholka:ibaraholka /app /data
USER ibaraholka

# Render / Railway inject $PORT; default 10000 for local
ENV PORT=10000
ENV DB_PATH=/data/ibaraholka.db
EXPOSE 10000

# Health check (Render also pings /health)
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request, sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:'+__import__('os').environ.get('PORT','10000')+'/health', timeout=3).status == 200 else 1)"

CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT}"]

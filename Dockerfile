# ---- Build stage ---------------------------------------------------------
FROM python:3.11-slim AS builder

# build-essential covers the rare case where a dependency (or a sub-
# dependency of uvicorn[standard], e.g. uvloop/httptools) has no prebuilt
# wheel for the target architecture (notably arm64/Apple Silicon) and pip
# has to compile it from source.
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build
COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

# ---- Runtime stage ---------------------------------------------------------
FROM python:3.11-slim

# Create a non-root user for defense-in-depth.
RUN useradd --create-home --shell /bin/bash veritas
WORKDIR /app

COPY --from=builder /install /usr/local
COPY app/ ./app/

RUN chown -R veritas:veritas /app
USER veritas

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')" || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]

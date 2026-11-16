# ---- Build stage ---------------------------------------------------------
FROM python:3.11-slim AS builder
RUN apt-get update && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /build
COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

# Generate gRPC stubs from the .proto (grpcio-tools is only needed at build time).
COPY proto/ ./proto/
RUN pip install --no-cache-dir grpcio-tools==1.66.2 \
    && mkdir gen \
    && python -m grpc_tools.protoc -I proto --python_out=gen --grpc_python_out=gen \
       proto/veritas/exam/v1/exam.proto

# ---- Runtime stage ---------------------------------------------------------
FROM python:3.11-slim
RUN useradd --create-home --shell /bin/bash veritas
WORKDIR /app
COPY --from=builder /install /usr/local
COPY --from=builder /build/gen ./gen
COPY app/ ./app/
COPY grpc_service/ ./grpc_service/
# /data holds the shared SQLite file (named volume mounted into both services)
RUN mkdir /data && chown -R veritas:veritas /app /data
USER veritas
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=/app:/app/gen
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')" || exit 1
# --proxy-headers: behind Envoy, request.client.host is the real client (X-Forwarded-For), not Envoy
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]

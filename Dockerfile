# syntax=docker/dockerfile:1.12
# Local and (from M1) Cloud Run image. Multi-stage: uv resolves into a venv, the runtime stage
# copies only the venv and source, and runs as a non-root user.

FROM ghcr.io/astral-sh/uv:0.13.6 AS uv

FROM python:3.12-slim AS build
# The cloud this image is for: installs only that cloud's adapters' dependencies.
ARG CLOUD=gcp
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project --extra "$CLOUD"

FROM python:3.12-slim
RUN useradd --create-home --uid 10001 app
WORKDIR /app
# multilingual-e5-small, int8 ONNX (ADR 0007), pinned by revision and checksum (the same pins as
# adapters/shared/e5.py; a test keeps them in step). Their own early layers, so every image version
# shares them in the registry: ADD takes the file time from the server's Last-Modified header, so
# the layers are identical from build to build.
ADD --checksum=sha256:f80102d3f2a1229f387d3c81909990d8945513e347b0eab049f7de3c6f98c193 --chmod=644 \
    https://huggingface.co/Xenova/multilingual-e5-small/resolve/761b726dd34fb83930e26aab4e9ac3899aa1fa78/onnx/model_quantized.onnx \
    /app/models/e5/model.onnx
ADD --checksum=sha256:0b44a9d7b51c3c62626640cda0e2c2f70fdacdc25bbbd68038369d14ebdf4c39 --chmod=644 \
    https://huggingface.co/Xenova/multilingual-e5-small/resolve/761b726dd34fb83930e26aab4e9ac3899aa1fa78/tokenizer.json \
    /app/models/e5/tokenizer.json
COPY --from=build /app/.venv /app/.venv
COPY core ./core
COPY adapters ./adapters
COPY apps ./apps
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1 PORT=8000 E5_MODEL_DIR=/app/models/e5
USER app
EXPOSE 8000
CMD ["sh", "-c", "uvicorn --factory apps.api.main:create_app --host 0.0.0.0 --port ${PORT}"]

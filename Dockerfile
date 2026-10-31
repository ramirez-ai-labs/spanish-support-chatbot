# Local and (from M1) Cloud Run image. Multi-stage: uv resolves into a venv, the runtime stage
# copies only the venv and source, and runs as a non-root user.

FROM ghcr.io/astral-sh/uv:0.12.20 AS uv

FROM python:3.15-slim AS build
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

FROM python:3.15-slim
RUN useradd --create-home --uid 10001 app
WORKDIR /app
COPY --from=build /app/.venv /app/.venv
COPY core ./core
COPY adapters ./adapters
COPY apps ./apps
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1 PORT=8000
USER app
EXPOSE 8000
CMD ["sh", "-c", "uvicorn --factory apps.api.main:create_app --host 0.0.0.0 --port ${PORT}"]

# syntax=docker/dockerfile:1.12
# Local and (from M1) Cloud Run image. Multi-stage: uv resolves into a venv, the runtime stage
# copies only the venv and source, and runs as a non-root user.

FROM ghcr.io/astral-sh/uv:0.13.6 AS uv

FROM python:3.12-slim AS build
# The cloud this image is for: installs only that cloud's adapters' dependencies.
ARG CLOUD=gcp
COPY --from=uv /uv /usr/local/bin/uv
# UV_HTTP_RETRIES: the spaCy model is a GitHub release download, which times out under load.
ENV UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never UV_HTTP_RETRIES=8
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project --extra "$CLOUD"
# multilingual-e5-small, int8 ONNX (ADR 0007), pinned by revision and checksum (the same pins as
# adapters/shared/e5.py; a test keeps them in step). The directory exists before ADD: newer
# BuildKit applies ADD's --chmod=644 to the parent directories it creates as well, and a directory
# without x can't be entered by the app user.
RUN mkdir -p /layers/models/app/models/e5
ADD --checksum=sha256:f80102d3f2a1229f387d3c81909990d8945513e347b0eab049f7de3c6f98c193 --chmod=644 \
    https://huggingface.co/Xenova/multilingual-e5-small/resolve/761b726dd34fb83930e26aab4e9ac3899aa1fa78/onnx/model_quantized.onnx \
    /layers/models/app/models/e5/model.onnx
ADD --checksum=sha256:0b44a9d7b51c3c62626640cda0e2c2f70fdacdc25bbbd68038369d14ebdf4c39 --chmod=644 \
    https://huggingface.co/Xenova/multilingual-e5-small/resolve/761b726dd34fb83930e26aab4e9ac3899aa1fa78/tokenizer.json \
    /layers/models/app/models/e5/tokenizer.json
# The registry's free tier (0.5 GB) must hold the live image and its rollback, which it can only do
# if they share layers, and layers are shared only when they're byte-identical. So:
# - Bytecode is compiled at its final path with unchecked hashes, so no .pyc records a timestamp.
# - The packages that take the most room get their own layers, so a dependency change only rebuilds
#   the last one. Add a large or rarely changing package here; a package that's gone fails the build.
# - Every file's time is fixed, since a layer's bytes include them.
COPY <<'SPLIT' /split-layers.sh
set -eu
python -m compileall -q --invalidation-mode unchecked-hash .venv/lib
site=.venv/lib/python3.12/site-packages
move() {
  target="/layers/$1/app/$site"; shift
  mkdir -p "$target"
  for package in "$@"; do
    mv "$site/$package" "$target/"
    for info in "$site/$package"-*.dist-info; do
      if [ -e "$info" ]; then mv "$info" "$target/"; fi
    done
  done
}
move nlp spacy es_core_news_md thinc blis
move numeric numpy numpy.libs onnxruntime tokenizers phonenumbers
mkdir -p /layers/python/app
mv .venv /layers/python/app/.venv
find /layers -exec touch -h -d @0 {} +
SPLIT
RUN sh /split-layers.sh

FROM python:3.12-slim
# The base image lags Debian's security fixes. Remove each package once the base image ships it:
# - CVE-2026-109518: apt 3.0.3; fixed in 3.0.4.
# - CVE-2026-85091, CVE-2026-76844: zlib1g 1:1.3.dfsg+really1.3.1-1+b1; fixed in -1+deb13u1.
# About 2 MB compressed, and it differs from build to build, so each stored image keeps its own copy.
RUN apt-get update -qq \
    && apt-get install -y -qq --only-upgrade apt libapt-pkg7.0 zlib1g \
    && rm -rf /var/lib/apt/lists/* /var/cache/apt/* /var/log/apt/* /var/log/dpkg.log /var/log/alternatives.log
RUN useradd --create-home --uid 10001 app
WORKDIR /app
# --link builds each layer on its own, so it doesn't pick up the times of the directories above it.
COPY --link --from=build /layers/models/ /
COPY --link --from=build /layers/nlp/ /
COPY --link --from=build /layers/numeric/ /
COPY --link --from=build /layers/python/ /
COPY --link core ./core
COPY --link adapters ./adapters
COPY --link apps ./apps
# The market list and the docs (a few KB): the API checks each request's market against it, and
# KNOWLEDGE_STORE=memory indexes the docs at startup.
COPY --link knowledge ./knowledge
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1 PORT=8000 E5_MODEL_DIR=/app/models/e5
USER app
EXPOSE 8000
CMD ["sh", "-c", "uvicorn --factory apps.api.main:create_app --host 0.0.0.0 --port ${PORT}"]

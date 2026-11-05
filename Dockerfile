FROM python:3.12-slim@sha256:6d2b0577e6d1632f077331de8e4780535f54839d163ded7abb4b7dd37b5d37ac
ENV PYTHONUNBUFFERED=1 UV_COMPILE_BYTECODE=1
RUN apt-get update && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*
COPY --from=ghcr.io/astral-sh/uv:0.13.6 /uv /bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev
RUN uv run python -c "from winnow.indexing.embed import Embedder; Embedder().embed_one('warmup')"
RUN useradd --uid 1000 --create-home winnow
ENV HOME=/home/winnow UV_CACHE_DIR=/home/winnow/.cache/uv
RUN mkdir -p /home/winnow/.cache/uv /data && chown -R 1000:1000 /home/winnow /data
USER winnow
CMD ["/app/.venv/bin/winnow", "serve", "--host", "0.0.0.0", "--port", "8000"]

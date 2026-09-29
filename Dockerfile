# TradingDots Phase 1 application image.
# UNVERIFIED: base image digests are not pinned yet (no registry access when authored). Pin
# python:3.12-slim-bookworm by digest before any deployment (see docs/security-checklist.md).
FROM python:3.12-slim-bookworm AS build
ENV UV_LINK_MODE=copy UV_COMPILE_BYTECODE=1 PIP_NO_CACHE_DIR=1
RUN pip install uv==0.8.17
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

FROM python:3.12-slim-bookworm
RUN groupadd --gid 10001 tradingdots \
    && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin tradingdots
RUN mkdir /data /review && chown 10001:10001 /data /review
WORKDIR /app
COPY --from=build /app/.venv /app/.venv
COPY app ./app
COPY config ./config
COPY scripts/healthcheck.py scripts/create_admin.py scripts/rotate_admin_password.py ./scripts/
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
USER 10001:10001
# Documentation only: EXPOSE does not publish. Compose never publishes this port.
EXPOSE 8000
CMD ["python", "-m", "app.cli", "serve"]

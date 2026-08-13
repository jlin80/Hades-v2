# syntax=docker/dockerfile:1.7
# ---------------------------------------------------------------------------
# Builder: resolve dependencies into a self-contained virtualenv.
# ---------------------------------------------------------------------------
FROM python:3.12-slim-bookworm AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /build

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:${PATH}"

# Copy only what the build needs, so a source edit does not invalidate the
# dependency layer.
COPY pyproject.toml README.md ./
COPY src ./src

RUN pip install --no-cache-dir .

# ---------------------------------------------------------------------------
# Runtime: no build toolchain, no source tree, non-root.
# ---------------------------------------------------------------------------
FROM python:3.12-slim-bookworm AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:${PATH}"

# A fixed non-root uid keeps ownership predictable across rebuilds and hosts.
RUN groupadd --gid 1000 hades \
    && useradd --uid 1000 --gid 1000 --create-home --shell /usr/sbin/nologin hades

WORKDIR /app

COPY --from=builder /opt/venv /opt/venv

# Migrations are run from the working directory, so alembic.ini and the
# migration scripts ship with the image. The application package itself is
# already installed inside the virtualenv.
COPY --chown=1000:1000 alembic.ini ./alembic.ini
COPY --chown=1000:1000 migrations ./migrations

USER 1000:1000

EXPOSE 8000

CMD ["python", "-m", "hades.main"]

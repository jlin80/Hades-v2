# Python 3.12 slim. No ML extras are installed anywhere in this image — the
# homelab CPU (AMD G-T56N "Bobcat") lacks AVX/SSE4.2, and numpy2/pyarrow wheels
# built for a newer baseline die with SIGILL on it. Keeping the runtime pure
# Python is a deployment constraint, not a preference.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Dependency layer first so source edits do not reinstall the world.
COPY pyproject.toml README.md ./
COPY src/hades/__init__.py src/hades/__init__.py
RUN pip install --no-cache-dir .

COPY src ./src
COPY migrations ./migrations
COPY alembic.ini ./
# The probes and research tools. They are how a result gets reproduced, and the
# only place they can run against the real dataset is the deployed container —
# leaving them out meant `research_report.py` and `sweep_phase7.py` did not exist
# on the one machine that has the data.
COPY scripts ./scripts

RUN pip install --no-cache-dir --no-deps -e . \
    && useradd --uid 1000 --create-home hades \
    && chown -R hades:hades /app
USER hades

EXPOSE 8000
CMD ["python", "-m", "hades"]

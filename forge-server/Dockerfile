FROM python:3.12-slim

WORKDIR /app

# Build deps for psycopg / asyncpg native bits. No docker CLI needed —
# the Python `docker` SDK talks to the socket directly.
RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc libpq-dev curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
 && pip install --no-cache-dir \
        "docker>=7.0,<8" \
        "asyncpg>=0.29"

# Playwright/Chromium was previously installed here (~500 MB). That's a test
# dependency; if you need it for end-to-end tests, run a separate test image.

COPY . .

# Self-bootstrapping entrypoint: waits for Postgres, runs alembic, builds the
# forge-runner image if missing, then exec()s uvicorn. See docker-entrypoint.py.
RUN chmod +x /app/docker-entrypoint.py

# Build-time import smoke test. Fail the BUILD (not the runtime) if any
# transitive import in forge_server.app blows up — e.g. missing
# pydantic[email] for EmailStr, missing optional Supabase deps, a renamed
# module after a refactor. Catches a whole class of "requirements.txt is
# subtly incomplete" bugs that otherwise only surface when the container
# crash-loops in production.
RUN python -c "from forge_server.app import app; print('import smoke test OK')"

EXPOSE 8000

ENTRYPOINT ["python", "/app/docker-entrypoint.py"]

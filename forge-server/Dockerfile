FROM python:3.12-slim

WORKDIR /app

# Install system deps (docker CLI for socket access)
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc libpq-dev curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && \
    playwright install chromium --with-deps

COPY . .

EXPOSE 8000

CMD ["uvicorn", "forge_server.app:app", "--host", "0.0.0.0", "--port", "8000"]

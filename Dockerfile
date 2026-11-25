FROM python:3.12-slim

# Patch vulnerable operating system package
RUN apt-get update && \
    apt-get install -y --only-upgrade libzstd1 && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies
COPY requirements.txt requirements-agent.txt ./
RUN pip install --no-cache-dir -r requirements-agent.txt

# Create non-root user
RUN useradd -m appuser && \
    mkdir /data && \
    chown appuser /data

# Copy application source code
COPY app ./app
COPY static ./static

# Run as non-root user
USER appuser

ENV OPS_MODE=demo OPS_DB=/data/ops.db

EXPOSE 8000

CMD ["uvicorn", "app.server:app", "--host", "0.0.0.0", "--port", "8000"]
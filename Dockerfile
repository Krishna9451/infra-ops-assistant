FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && useradd -m appuser && mkdir /data && chown appuser /data
COPY app ./app
COPY static ./static
USER appuser
ENV OPS_MODE=demo OPS_DB=/data/ops.db
EXPOSE 8000
CMD ["uvicorn", "app.server:app", "--host", "0.0.0.0", "--port", "8000"]

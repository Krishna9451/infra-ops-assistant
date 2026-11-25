FROM python:3.12-slim

# Use HTTPS repositories and patch vulnerability
RUN sed -i 's|http://|https://|g' /etc/apt/sources.list.d/debian.sources && \
    apt-get update --error-on=any && \
    apt-get install -y --only-upgrade libzstd1 && \
    dpkg-query -W libzstd1 && \
    dpkg --compare-versions "$(dpkg-query -W -f='${Version}' libzstd1)" ge "1.5.7+dfsg-1+deb13u1" && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt requirements-agent.txt ./

RUN pip install --no-cache-dir -r requirements-agent.txt

RUN useradd -m appuser && \
    mkdir /data && \
    chown appuser /data

COPY app ./app
COPY static ./static

USER appuser

ENV OPS_MODE=demo OPS_DB=/data/ops.db

EXPOSE 8000

CMD ["uvicorn", "app.server:app", "--host", "0.0.0.0", "--port", "8000"]
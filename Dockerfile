FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends poppler-utils \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV LOGISTICS_DB=/data/logistics.db \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app

RUN chmod +x /app/scripts/entrypoint.sh

EXPOSE 8080

# Railway routes traffic to $PORT; CMD must not hard-code 8000.
CMD ["/bin/sh", "/app/scripts/entrypoint.sh"]
FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends poppler-utils tesseract-ocr tesseract-ocr-eng \
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
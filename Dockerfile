FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends poppler-utils \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV LOGISTICS_DB=/data/logistics.db \
    PYTHONUNBUFFERED=1

RUN chmod +x /app/scripts/entrypoint.sh

EXPOSE 8080

CMD ["/app/scripts/entrypoint.sh"]
FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY config.example.yaml ./config.example.yaml

RUN mkdir -p /app/data

ENV PROXY_CONFIG=/app/data/config.yaml

EXPOSE 8080

CMD ["sh", "-c", "test -f $PROXY_CONFIG || cp /app/config.example.yaml $PROXY_CONFIG; uvicorn app.main:app --host 0.0.0.0 --port 8080 --timeout-graceful-shutdown 130"]

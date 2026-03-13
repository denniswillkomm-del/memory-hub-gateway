FROM python:3.13-slim

WORKDIR /app

COPY pyproject.toml ./
COPY src/ ./src/
COPY migrations/ ./migrations/
COPY allowlist.yaml ./

RUN pip install --no-cache-dir .

ENV GATEWAY_DB_PATH=/data/gateway.db

EXPOSE 8080

CMD uvicorn gateway.app:app --host 0.0.0.0 --port ${PORT:-8080}

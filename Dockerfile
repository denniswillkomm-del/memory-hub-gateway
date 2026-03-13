FROM python:3.13-slim

WORKDIR /app

COPY pyproject.toml ./
COPY src/ ./src/
COPY migrations/ ./migrations/
COPY allowlist.yaml ./

RUN pip install --no-cache-dir . && mkdir -p /data

COPY entrypoint.sh ./
RUN chmod +x entrypoint.sh

ENV GATEWAY_DB_PATH=/data/gateway.db

EXPOSE 8080

CMD ["./entrypoint.sh"]

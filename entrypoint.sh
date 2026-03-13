#!/bin/sh
exec uvicorn gateway.app:app --host 0.0.0.0 --port "${PORT:-8080}"

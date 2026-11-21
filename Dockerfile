FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    NOISE_DB_PATH=/data/noise.db

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

RUN mkdir -p /data
VOLUME ["/data"]

EXPOSE 8000

# 单 worker：写事务由 SQLite 的 BEGIN IMMEDIATE + busy_timeout 串行化，
# 多 worker 也可工作（WAL 支持跨进程），单进程足以保证并发写语义清晰。
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]

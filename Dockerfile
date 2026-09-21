FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DIAG_DB_PATH=/app/data/diagnostics.db \
    PORT=8000

WORKDIR /app

# 构建期预装依赖（启动脚本仍会在首次启动时自检补装）
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN chmod +x docker-entrypoint.sh start.sh

EXPOSE 8000

CMD ["./docker-entrypoint.sh"]

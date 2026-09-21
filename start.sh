#!/usr/bin/env bash
# 本地一键启动：自动安装依赖、初始化数据库、启动 API 服务。
set -euo pipefail
cd "$(dirname "$0")"

PYTHON_BIN="${PYTHON_BIN:-python3}"

if ! "$PYTHON_BIN" -c "import fastapi, uvicorn" >/dev/null 2>&1; then
  echo "[start] 首次启动，自动安装依赖..."
  "$PYTHON_BIN" -m pip install -r requirements.txt
fi

"$PYTHON_BIN" -c "from app.db import init_db; print('[start] 数据库就绪:', init_db())"

echo "[start] API 监听端口 ${PORT:-8000}，文档地址 http://localhost:${PORT:-8000}/docs"
exec "$PYTHON_BIN" -m uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8000}"

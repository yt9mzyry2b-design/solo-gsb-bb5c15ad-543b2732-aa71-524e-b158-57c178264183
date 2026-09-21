#!/usr/bin/env bash
# 容器入口：首次启动自动安装依赖（自检）并初始化数据库，然后启动 API。
set -euo pipefail
cd /app

if ! python -c "import fastapi, uvicorn" >/dev/null 2>&1; then
  echo "[entrypoint] 检测到依赖缺失，自动安装..."
  pip install --no-cache-dir -r requirements.txt
fi

python -c "from app.db import init_db; print('[entrypoint] 数据库就绪:', init_db())"

exec python -m uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8000}"

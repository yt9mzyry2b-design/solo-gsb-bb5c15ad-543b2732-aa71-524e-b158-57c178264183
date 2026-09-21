#!/bin/sh
# 容器入口：首次启动自动建库（幂等），随后执行 CMD。
set -e

echo "[entrypoint] 初始化 SQLite 数据库（若不存在）: ${TILE_SEAM_DB:-/data/tasks.db}"
python - <<'PY'
from app.db import init_db
init_db()
print("[entrypoint] 数据库就绪")
PY

exec "$@"

#!/usr/bin/env bash
# 一条命令在本机启动（无需 Docker）：
#   ./run.sh
# 首次启动会自动创建虚拟环境并安装依赖、初始化数据库；之后启动直接复用。
set -euo pipefail

cd "$(dirname "$0")"

VENV_DIR=".venv"
PYTHON_BIN="${PYTHON_BIN:-python3}"
HOST="${HOST:-0.0.0.0}"
PORT="${PORT:-8000}"

if [ ! -d "$VENV_DIR" ]; then
    echo "[run] 首次启动：创建虚拟环境 $VENV_DIR ..."
    if "$PYTHON_BIN" -m venv "$VENV_DIR" >/dev/null 2>&1; then
        :
    else
        # Debian/Ubuntu 精简镜像可能缺 ensurepip，退化为无 pip 的 venv 后引导
        "$PYTHON_BIN" -m venv --without-pip "$VENV_DIR"
        curl -sS https://bootstrap.pypa.io/get-pip.py -o /tmp/get-pip.py
        "$VENV_DIR/bin/python" /tmp/get-pip.py >/dev/null
    fi
fi

# shellcheck disable=SC1091
. "$VENV_DIR/bin/activate"

if ! python -c "import fastapi, uvicorn" >/dev/null 2>&1; then
    echo "[run] 安装依赖 ..."
    python -m pip install -q --upgrade pip
    python -m pip install -q -r requirements.txt
fi

export TILE_SEAM_DB="${TILE_SEAM_DB:-$(pwd)/data/tasks.db}"
mkdir -p "$(dirname "$TILE_SEAM_DB")"

echo "[run] 初始化数据库: $TILE_SEAM_DB"
python -c "from app.db import init_db; init_db(); print('[run] 数据库就绪')"

echo "[run] 启动服务： http://${HOST}:${PORT}  （Swagger 文档： /docs）"
exec python -m uvicorn app.main:app --host "$HOST" --port "$PORT"

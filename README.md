# 测绘瓦片接缝诊断 API

接收无人机正射瓦片元数据（行列号、坐标范围、分辨率），按容差执行二维矩形
扫描，识别 **覆盖缺口**、**重叠区域** 与 **无法拼接的连通分块**，返回可复现
的诊断 JSON。任务持久化在 SQLite 中，无需任何第三方账号。

## 快速开始（单命令）

```bash
docker compose up --build
```

启动后访问：

- API 文档（Swagger UI）：http://localhost:8000/docs
- 健康检查：http://localhost:8000/health

**访问端口：`8000`**（宿主机端口可在 `docker-compose.yml` 的 `ports` 中修改）。

无 Docker 时本地启动（自动安装依赖并建库）：

```bash
./start.sh          # 等价于：pip install -r requirements.txt && uvicorn 启动
```

首次启动时入口脚本会自检并自动安装依赖、初始化 SQLite 数据库
（默认 `./data/diagnostics.db`，可用环境变量 `DIAG_DB_PATH` 覆盖）。

## 接口一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/health` | 健康检查 |
| POST | `/api/v1/tiles/validate` | 单瓦片元数据校验（结构化错误/告警） |
| POST | `/api/v1/diagnostics` | 提交瓦片集合，执行接缝诊断并入库 |
| GET | `/api/v1/diagnostics` | 任务列表（含摘要，倒序） |
| GET | `/api/v1/diagnostics/{task_id}` | 查询完整诊断结果 |
| GET | `/api/v1/diagnostics/{task_id}/export` | 导出诊断 JSON 文件（附件下载） |

## 使用示例

提交诊断（4 块瓦片缺 1 块，容差 0.01）：

```bash
curl -X POST http://localhost:8000/api/v1/diagnostics \
  -H 'Content-Type: application/json' \
  -d '{
    "job_name": "示例测区",
    "tolerance": 0.01,
    "tiles": [
      {"row":0,"col":0,"min_x":0,"min_y":0,"max_x":100,"max_y":100,"resolution":0.05},
      {"row":0,"col":1,"min_x":100,"min_y":0,"max_x":200,"max_y":100,"resolution":0.05},
      {"row":1,"col":0,"min_x":0,"min_y":100,"max_x":100,"max_y":200,"resolution":0.05}
    ]
  }'
```

单瓦片校验：

```bash
curl -X POST http://localhost:8000/api/v1/tiles/validate \
  -H 'Content-Type: application/json' \
  -d '{"row":0,"col":0,"min_x":0,"min_y":0,"max_x":100,"max_y":100,"resolution":0.3}'
```

导出结果：

```bash
curl -OJ http://localhost:8000/api/v1/diagnostics/<task_id>/export
```

## 诊断结果结构

```json
{
  "task_id": "…", "created_at": "…", "status": "completed",
  "checksum": "sha256…",
  "diagnostic": {
    "tolerance": 0.01,
    "input_hash": "sha256…",
    "summary": {
      "tile_count": 3, "covered_area": 30000, "gap_area": 10000,
      "overlap_area": 0, "coverage_ratio": 0.75,
      "gap_count": 1, "overlap_region_count": 0,
      "component_count": 1, "is_single_component": true, "seamless": false
    },
    "gaps":      [{"bbox": {…}, "width": 100, "height": 100, "area": 10000}],
    "overlaps":  [{"bbox": {…}, "area": 0, "max_depth": 2, "tile_ids": ["…"]}],
    "components":[{"component_id": 0, "tile_count": 3, "tile_ids": ["…"], "bbox": {…}}],
    "warnings":  [{"code": "DUPLICATE_ROW_COL", "message": "…", "tile_ids": ["…"]}]
  }
}
```

## 核心算法说明

1. **容差吸附**：全部瓦片边的 x/y 坐标分别排序聚簇，间距 ≤ `tolerance`
   的坐标吸附到同一代表值，消除接边处的细小裂缝与重叠毛刺；
   `tolerance = 0` 时严格按原始坐标计算。
2. **二维矩形扫描**：吸附后的坐标把外包矩形切分为基本单元格，用二维
   差分数组一次扫描得到每个单元格的覆盖次数：`=0` 为缺口、`≥2` 为重叠。
3. **区域合并**：缺口/重叠单元格经行程压缩 + 并查集合并为最大化连通
   区域，输出外包框、面积与涉及瓦片。
4. **连通分块**：瓦片间按闭矩形相交（含共边/共点）建图求连通分量；
   `component_count > 1` 表示存在无法拼接成整体的孤立分块。

**可复现性**：输入先按 `(row, col, min_y, min_x)` 规范化排序，所有输出
确定性排序并统一舍入；`input_hash` 与 `checksum`（均为 SHA-256）只依赖
诊断内容，相同瓦片集合以任意顺序提交都会得到相同的校验和。

## 运行测试

```bash
pip install -r requirements.txt pytest httpx
python -m pytest tests/ -q
```

## 配置项

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `PORT` | `8000` | API 监听端口 |
| `DIAG_DB_PATH` | `./data/diagnostics.db` | SQLite 数据库文件路径 |

## 项目结构

```
app/
  main.py         FastAPI 应用与路由
  diagnostics.py  核心算法（容差吸附 + 二维扫描 + 连通分块）
  schemas.py      请求模型与单瓦片校验
  db.py           SQLite 持久层（标准库实现）
tests/            算法单元测试 + API 端到端测试
Dockerfile / docker-compose.yml / docker-entrypoint.sh / start.sh
```

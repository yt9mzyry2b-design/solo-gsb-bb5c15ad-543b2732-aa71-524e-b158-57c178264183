# 测绘瓦片接缝诊断 API（Tile Seam Diagnosis）

接收带**行列号、坐标范围和分辨率**的无人机正射瓦片元数据，按容差进行
**二维矩形扫描**，识别：

- **覆盖缺口（gaps）**：连通块内部被瓦片包围的未覆盖区域（洞），含面积与矩形多边形；
- **重叠区域（overlaps）**：物理重叠聚簇、重叠瓦片对、重叠面积/超出面积/覆盖层数；
- **无法拼接的连通分块（components）**：容差下并查集得到的连通分块、各分块面积、分块间最小间距；
- 附带：容差桥接的接缝对、按行列号推断的规则栅格缺失格位、分辨率/像素尺寸/CRS 校验。

结果为**可复现诊断 JSON**：同一输入 + 同一 `schema_version` 必得完全相同的输出
（不含时间戳/随机数；任务时间戳只存在 SQLite 记录里）。任务持久化到 **SQLite**，
默认**无需任何第三方账号或外部服务**。

---

## 一条命令启动

### 方式 A：Docker（推荐，端口 **8000**）

```bash
docker compose up --build
```

首次启动会自动安装依赖并建库（`docker-entrypoint.sh` 幂等执行），数据库位于
命名卷 `tile-seam-data`（容器内 `/data/tasks.db`）。

仅构建/运行单容器：

```bash
docker build -t tile-seam-diagnosis .
docker run --rm -p 8000:8000 -v "$PWD/data:/data" tile-seam-diagnosis
```

### 方式 B：本机 Python（端口 **8000**）

```bash
./run.sh
```

首次运行自动创建 `.venv`、安装依赖（`requirements.txt`）、初始化 `data/tasks.db`，
之后直接复用。需要 Python 3.10+（标准库自带 `venv`/`sqlite3`）。

手动方式：

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000   # 启动时自动建库
```

### 访问端口

| 入口 | 地址 |
| --- | --- |
| API 服务 | <http://localhost:8000> |
| Swagger 交互文档 | <http://localhost:8000/docs> |
| OpenAPI 描述 | <http://localhost:8000/openapi.json> |
| 健康检查 | <http://localhost:8000/health> |

可用环境变量：`TILE_SEAM_DB`（SQLite 路径，默认 `./data/tasks.db`）、
`HOST`、`PORT`（仅 `run.sh`）。

---

## 快速试用

单瓦片校验：

```bash
curl -s -X POST http://localhost:8000/api/v1/tiles/validate \
  -H 'Content-Type: application/json' \
  -d @examples/tile.json
```

执行诊断并保存任务：

```bash
# 规则 2x2：可拼接
curl -s -X POST http://localhost:8000/api/v1/diagnose \
  -H 'Content-Type: application/json' \
  -d @examples/diagnose_perfect.json

# 含重叠 + 缺口 + 分离块的综合场景
curl -s -X POST http://localhost:8000/api/v1/diagnose \
  -H 'Content-Type: application/json' \
  -d @examples/diagnose_overlap_gap.json
```

不落库的即时预览：`POST /api/v1/diagnose/preview`（相同请求体）。

导出任务结果（`json` 为原始诊断 JSON，`geojson` 可直接在 QGIS/geojson.io 打开）：

```bash
curl "http://localhost:8000/api/v1/tasks/<task_id>/export?format=geojson" -o diag.geojson
```

---

## 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/health` | 健康检查、版本、任务数 |
| POST | `/api/v1/tiles/validate` | 单瓦片校验（请求体给单个瓦片对象；批量用 `{"tiles":[...]}`） |
| POST | `/api/v1/diagnose` | 诊断并持久化任务（同输入幂等，返回相同 `task_id`） |
| POST | `/api/v1/diagnose/preview` | 只诊断，不写 SQLite |
| GET | `/api/v1/tasks?limit=&offset=` | 任务列表（摘要，不含完整瓦片） |
| GET | `/api/v1/tasks/{task_id}` | 任务详情（含完整诊断 JSON） |
| GET | `/api/v1/tasks/{task_id}/export?format=json\|geojson` | 导出诊断结果 |
| DELETE | `/api/v1/tasks/{task_id}` | 删除任务 |

### 请求字段

瓦片 `tiles[]`：`tile_id`、`row`、`col`（非负行列号）、
`xmin/ymin/xmax/ymax`（四角坐标，顺序写反也会自动归一化）、
`resolution`（地面采样距离，单位/像素，>0）、`crs`（可空，如 `EPSG:32650`）。

诊断参数：

| 参数 | 默认 | 含义 |
| --- | --- | --- |
| `tolerance` | `0` | 接缝容差（坐标单位）。两矩形最小距离 ≤ 容差即视为可拼接连通 |
| `min_gap_area` | `0` | 小于该面积的缺口被忽略（滤除数值碎屑） |
| `max_pairs` | `100` | 重叠对/接缝对明细最大返回条数（总数仍在统计中给出） |
| `expect_grid` | `true` | 是否按行列号推断规则栅格并报缺失格位 |
| `resolution_rel_tol` | `0.02` | 分辨率相对中位数偏差告警阈值（2%） |

### 诊断 JSON 要点

- `stitchable` / `summary`：是否可拼接为一张整体及原因（多分块 / 内部缺口 / 输入错误）；
- `components[]`：连通分块、成员瓦片、外接矩形、并集面积、内部缺口面积；
- `gaps[]`：缺口（面积 + `polygon_rects` 矩形条带 `[xmin,ymin,xmax,ymax]`）；
- `overlaps[]` 与 `overlap_pairs[]`：重叠聚簇（面积/最大层数/所属分块/多边形）
  与瓦片对（相交面积、重叠宽高、所属分块、聚簇 id）；
- `seam_pairs[]`：宽度 ≤ 容差、被桥接为连通的缝隙对；
- `component_separations[]`：分块之间的最小间距及最近瓦片对；
- `missing_grid_cells`：按行列号推断出的缺失格位及估计中心；
- `statistics`：瓦片/分块/缺口/重叠/面积等汇总；`errors`/`warnings` 为问题清单；
- `reproducibility`：输入指纹 `request_sha256`、`schema_version`、算法说明。

GeoJSON 图层：`tiles`、`gaps`、`overlaps`、`components`；单一 CRS 时带 `source_crs`
（仅声明源坐标系，不做重投影；几何即输入坐标）。

---

## 算法说明

1. **校验归一化**：退化矩形、重复 `tile_id`/行列号、CRS 不一致、分辨率偏差、
   像素跨度非整数；坐标统一归一化为 `min/max`。
2. **扫描线 + 并查集连通**：瓦片按 x 排序，活动集 + y 跨度剪枝生成候选邻居
   （搜索半径取 `max(tolerance, 2×中位瓦片跨幅)`，半径内矩形对无漏检）。
   候选对计算最小欧氏距离 `d` 与相交面积：`d ≤ tolerance` 合并连通；
   相交面积 > 0 记重叠对；`0 < d ≤ tolerance` 记容差桥接接缝；
   跨分块的最近对汇总为分块间距。
3. **全局坐标压缩网格扫描**：用全部瓦片边构成压缩网格，统计每格覆盖层数：
   ≥2 层格元 4-连通聚簇为重叠区域；同时得到各分块并集面积、重叠面积、超出面积。
4. **分块缺口扫描**：对每个连通块用**容差扩张后**的瓦片构建局部压缩网格，
   从块边界泛洪标记外部，剩余未覆盖格元即被包围缺口，4-连通聚簇并按
   `min_gap_area` 过滤。宽度 ≤ 容差的缝已在连通阶段桥接，不会误报为缺口。
5. **规则栅格检查**：在分块的行列号包围盒内报告缺失格位（带估计中心）。

### 可复现性保证

- 输入先做规范化序列化（键排序、瓦片按 `tile_id/row/col` 排序）再取
  SHA-256 得到 `request_id`——打乱瓦片提交顺序也会得到同一任务/结果；
- 分块编号按最小 `tile_id` 确定；所有列表均确定性排序；浮点数统一保留 6 位小数；
- 结果 JSON 不含时间戳与随机数；`task_id` 由 `request_id + schema_version` 派生，
  重复提交同一请求幂等返回同一任务。

### 边界与约定

- 瓦片按**轴对齐矩形**处理（正射瓦片常规情形），未做投影/重投影；
- 仅边/点接触（距离 0、面积 0）天然算作连通；
- 分块间距只统计搜索半径（结果 `params.neighbor_search_radius`）内的最近对；
- `polygon_rects` 超过 500 个条带时截断并置 `polygon_truncated=true`（面积统计不受影响）。

---

## 测试

```bash
. .venv/bin/activate
pip install -r requirements-dev.txt   # 仅测试需要 httpx
python -m unittest discover -s tests -v
```

覆盖：几何工具、规则栅格、中心缺口、分块分离、容差桥接、重叠检测、
面积过滤、分辨率告警、乱序可复现，以及全部 HTTP 接口（含幂等、404、422、GeoJSON 导出）。

## 项目结构

```
tile-seam/
├── app/
│   ├── main.py          # FastAPI 路由（校验/诊断/任务/导出）
│   ├── diagnosis.py     # 核心算法：扫描线+DSU、压缩网格、缺口/重叠聚簇
│   ├── geometry.py      # AABB 相交面积与距离
│   ├── models.py        # Pydantic 请求模型与参数校验
│   ├── export.py        # 诊断 JSON -> GeoJSON
│   └── db.py            # SQLite 任务存储（启动自动建库）
├── tests/               # unittest：算法 + HTTP 端到端
├── examples/            # 单瓦片与诊断请求示例
├── Dockerfile
├── docker-compose.yml
├── docker-entrypoint.sh # 容器首启建库
├── run.sh               # 本机一键：建 venv、装依赖、建库、起服务
├── requirements.txt
└── requirements-dev.txt
```

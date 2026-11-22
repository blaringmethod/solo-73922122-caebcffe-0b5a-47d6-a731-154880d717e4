# 河道水面落差核定 API

水准测线换线后，河道巡测员可通过本服务复核已发的**水面落差核定单**：
按精确到微秒的 UTC 时刻与上下游站号核发落差（把两尺零点高差计入）；换线时在
同一事务内复算全部有效单，自动撤销“算不出”或“数值改变”的单，其余继续有效。

## 1. 领域约定

- **读数**：站号 + UTC 时刻（精确到微秒）+ 整数毫米水尺读数。同站同刻唯一，
  重复录入整单拒绝。`2026-11-22T16:00:00.123456+08:00` 与
  `2026-11-22T08:00:00.123456Z` 是同一时刻，算重复。
- **读数时刻状态**：
  - `confirmed`（已确认）：来自新录入，或由旧整秒记录确认得到；**只有已确认读数可核发**；
  - `pending`（待确认）：旧库整秒读数或补报的旧整秒记录，仍可查询，
    可与同一秒内的精确读数并存，但不能直接参与核发。
- **测线** `(upstream, downstream, d)`：两尺零点的**有向**毫米高差
  `d = 上游尺零点高程 − 下游尺零点高程`；反向查询自动取反。
- **连通组**：测线按无向图连通；BFS 生成树推算各尺相对零点高，
  非树边逐条闭合校核，任一不满足即为**闭环矛盾**，该连通组整体停用。
- **落差公式**（毫米）：
  `落差 = (上游读数 + 上游零点高) − (下游读数 + 下游零点高)`
  等价于 `上游读数 − 下游读数 + 两尺零点高差(上游−下游)`，落差可为负。
- **核定单**：记录核发时的时刻、读数快照、零点高差、落差、测线版本；
  状态 `valid` / `revoked`。撤销单永久保留可查，**撤销后须重新核发**（生成新单号）。
  按单号查询时显示核发当时的时刻与读数，后续录入/确认/换线不会改写快照。

拒发原因（HTTP 422，中文 `detail`）：

1. 两尺未连通（不在同一连通组，或站号未出现在任何测线中）；
2. 所在连通组的测线闭环矛盾；
3. 上游或下游在**该精确时刻**缺已确认读数（若该秒内只有待确认旧读数，会提示先确认）。

## 2. 旧库迁移与确认流程

- 启动时自动识别 v1 库（`readings` 无 `time_status` 列）并一次性迁移：
  - 旧整秒读数规范为 `.000000Z`，**一律标记为待确认**；
  - 已有核定单的 `utc_time` 同步规范为微秒格式，读数快照原样保留；
  - 迁移在一个 `BEGIN IMMEDIATE` 事务内完成，可重复启动（`PRAGMA user_version=2`）。
- 巡测员凭**仪器原记录**，按站号 + 旧整秒调用 `/readings/confirm`，
  给出该行在这一秒内的精确时刻：
  - 精确时刻必须落在旧整秒所在的一秒内，否则 422；
  - 该精确时刻若已有同站读数：**同值合并**（只留一条已确认），**异值拒绝**；
  - 任何失败都回滚，原数据与待确认状态保留。
- 录入与确认即使并发，也都在 `BEGIN IMMEDIATE` 写事务内串行；
  核发在同一类写事务里读取两侧已确认读数，因此只会采用**同一精确时刻**的两站读数。

## 3. 启动（Docker Compose）

```bash
docker compose up --build
# 打开交互文档
open http://localhost:8000/docs      # Linux: xdg-open ...
```

SQLite 数据文件存放在命名卷 `river-fall-data`（容器内 `/data/app.db`），
容器重建不丢数据。健康检查：`GET /health`。

本地直跑（无 Docker 时）：

```bash
python -m pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## 4. /docs 演示脚本（与 Swagger 按钮一一对应）

```bash
B=http://localhost:8000

# ① 录入三站精确到微秒的读数（同一时刻）
curl -X POST $B/readings -H 'Content-Type: application/json' -d '{"readings":[
  {"station":"S1","utc_time":"2026-11-22T08:00:00.123456Z","gauge_mm":1000},
  {"station":"S2","utc_time":"2026-11-22T08:00:00.123456Z","gauge_mm":1020},
  {"station":"S3","utc_time":"2026-11-22T08:00:00.123456Z","gauge_mm":900}]}'

# ② 同一时刻换个时区写法重复录入：整批 409，不写入
curl -i -X POST $B/readings -H 'Content-Type: application/json' -d '{"readings":[
  {"station":"S1","utc_time":"2026-11-22T16:00:00.123456+08:00","gauge_mm":1000}]}'

# ③ 整批录入测线：S1-S2 零点高差 15，S2-S3 为 5
curl -X PUT $B/lines -H 'Content-Type: application/json' -d '{"lines":[
  {"upstream":"S1","downstream":"S2","zero_offset_mm":15},
  {"upstream":"S2","downstream":"S3","zero_offset_mm":5}]}'

# ④ 核发 S1→S3：链式零点高差 15+5=20，落差 (1000+20)-900 = 120
curl -X POST $B/approvals/issue -H 'Content-Type: application/json' -d '
  {"utc_time":"2026-11-22T08:00:00.123456Z","upstream":"S1","downstream":"S3"}'

# ⑤ 补报旧库整秒读数：入库为待确认，可与该秒内精确读数并存
curl -X POST $B/readings/legacy-import -H 'Content-Type: application/json' -d '{"readings":[
  {"station":"S4","utc_time":"2026-11-22T08:00:00Z","gauge_mm":777}]}'
curl "$B/readings?station=S4"

# ⑥ 巡测员凭仪器原记录确认：把 S4 的旧整秒确认到该秒内的精确时刻
curl -X POST $B/readings/confirm -H 'Content-Type: application/json' -d '
  {"station":"S4","legacy_utc_time":"2026-11-22T08:00:00Z",
   "utc_time":"2026-11-22T08:00:00.654321Z","operator":"巡测员-张三"}'
curl "$B/readings?station=S4"

# ⑦ 换线（S2-S3：5→8）：返回被撤单号与原因，事务内一次生效
curl -X PUT $B/lines -H 'Content-Type: application/json' -d '{"lines":[
  {"upstream":"S1","downstream":"S2","zero_offset_mm":15},
  {"upstream":"S2","downstream":"S3","zero_offset_mm":8}]}'

# ⑧ 按号查原数与状态（revoked，原时刻/读数/落差保留，附撤销原因）
curl $B/approvals/1

# ⑨ 重新核发：新高差 23、新落差 123，生成新单号
curl -X POST $B/approvals/issue -H 'Content-Type: application/json' -d '
  {"utc_time":"2026-11-22T08:00:00.123456Z","upstream":"S1","downstream":"S3"}'
```

## 5. 接口一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/readings` | 录入精确时刻读数（批量）；同站同刻（含时区等价写法）返回 409，整单不写入 |
| POST | `/readings/legacy-import` | 补报旧库整秒读数（必须整秒），入库为待确认；重复补报 409 |
| POST | `/readings/confirm` | 确认待确认读数到该秒内的精确时刻；同值合并、异值 409、越界 422 |
| GET | `/readings` | 查读数，可按 `station` / `utc_time` / `time_status` 过滤，按实际精确时刻排序 |
| PUT | `/lines` | **整批替换**测线并复算；返回仍有效单、撤销单（单号+原因）、前后版本号 |
| GET | `/lines` | 查当前测线全集与版本（版本见 `/status`） |
| POST | `/approvals/issue` | 按精确时刻核发落差；不满足条件 422 并说明原因 |
| GET | `/approvals` | 查全部核定单，可按 `?status=valid\|revoked` 过滤 |
| GET | `/approvals/{no}` | 按单号查原数与状态；不存在 404 |
| GET | `/status` | 测线版本、读数（总/已确认/待确认）、有效单/撤销单计数 |
| GET | `/health` | 健康检查 |

## 6. 并发与一致性

- SQLite 开 **WAL + busy_timeout=30s**；所有写事务 `BEGIN IMMEDIATE`，
  写操作在库级串行，读不阻塞写。
- 录入、确认、核发、换线都在各自单个写事务内提交，任一步失败整体回滚。
- 核发只读取**同一精确时刻**的两站 `confirmed` 读数；待确认旧整秒数据不会被采用。
- 换线（删旧线、插新线、版本 +1、逐单复算、撤销）在同一个事务内完成；
  复算只用核定单里的读数快照，不受后续录入/确认影响。
- 并发核发与换线交叉时，核发事务要么在换线提交前完成（随后被复算撤销），
  要么等锁后基于新测线核发，不存在换线后漏撤的旧逻辑单（见实测）。
- 每请求一个 SQLite 连接（线程隔离），uvicorn 多线程下安全。

## 7. 自测

```bash
pip install -r requirements.txt pytest httpx
python -m pytest tests/ -q          # 49 个用例
# 真进程并发（先起服务，再执行）
BASE_URL=http://127.0.0.1:8000 python -I tests/live_concurrency_check.py
BASE_URL=http://127.0.0.1:8000 python -I tests/live_precision_check.py
```

实测结果见文末“交付说明”。

## 8. 目录结构

```
app/
  main.py       FastAPI 路由与异常处理
  schemas.py    Pydantic 模型（strict int、带时区校验、/docs 示例）
  timeutil.py   微秒 UTC 规范文本、整秒截断/解析
  graph.py      连通组/闭环矛盾纯函数与零点高差求解
  service.py    录入/补报/确认/核发、换线复算与撤销的事务编排
  db.py         SQLite 连接、WAL/事务、旧库迁移、表结构与数据访问
tests/          pytest 用例 + 真进程并发检查脚本
Dockerfile, docker-compose.yml, requirements.txt
```

## 9. 交付说明

### 已实现

- 录入/查询改为精确到微秒的 UTC；查询按实际时刻排序；同站同刻重复（含不同
  时区写法）整批 409；同秒不同微秒可分别录入。
- 旧库整秒读数启动时迁移为 `pending`，仍可查询，可与该秒内精确读数并存；
  已有核定单的时刻规范为微秒，快照读数不变。
- 新增 `/readings/legacy-import`（补报，待确认）与 `/readings/confirm`
  （确认到秒内精确时刻；同值合并、异值拒绝、失败保留原状）。
- 核发只采用同一精确时刻的两站已确认读数；缺读数提示待确认记录。
- 核定单按号查询始终返回核发时的时刻与读数快照。
- 提供 Dockerfile + docker-compose.yml 与 `/docs` 演示。

### 已验证

- `python -m pytest tests/ -q`：49 个用例通过（含旧库迁移、补报/确认、合并/冲突、
  时区等价判重、时刻排序、同刻核发、快照不变、并发确认/核发/换线）。
- 真实 uvicorn 进程：
  - `tests/live_concurrency_check.py`：40 次并发核发与换线，无漏撤；
  - `tests/live_precision_check.py`：补报→确认（含异值 409 保留原状）→并发确认/核发，
    所有核定单的上下游读数均与该单时刻成对、无时刻混用，快照不受后续录入影响。
- 手动走通 `/docs` 对应链路：精确录入 → 测线 → 核发 → 换线撤销 → 按号查快照 →
  重新核发 → 补报 → 确认合并。

### 未完成/限制

- 当前环境未安装 Docker，`docker compose up --build` 未实机验证；Dockerfile 与
  Compose 配置按现有镜像约定编写，需在具备 Docker 的机器上执行一次。
- 确认接口没有鉴权/审计登录体系，`operator` 仅作字段记录。
- 旧库迁移为启动时一次性升级，升级后旧版本程序不再兼容（无降级脚本）。

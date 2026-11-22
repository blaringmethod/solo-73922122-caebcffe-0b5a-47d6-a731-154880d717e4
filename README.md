# 河道水面落差核定 API

水准测线换线后，河道巡测员可通过本服务复核已发的**水面落差核定单**：
按时刻与上下游站号核发落差（把两尺零点高差计入）；换线时在同一事务内
复算全部有效单，自动撤销“算不出”或“数值改变”的单，其余继续有效。

## 1. 领域约定

- **读数**：站号 + UTC 时刻 + 整数毫米水尺读数，同站同时刻唯一，重复录入整单拒绝。
- **测线** `(upstream, downstream, d)`：两尺零点的**有向**毫米高差
  `d = 上游尺零点高程 − 下游尺零点高程`；反向查询自动取反。
- **连通组**：测线按无向图连通；BFS 生成树推算各尺相对零点高，
  非树边逐条闭合校核，任一不满足即为**闭环矛盾**，该连通组整体停用。
- **落差公式**（毫米）：
  `落差 = (上游读数 + 上游零点高) − (下游读数 + 下游零点高)`
  等价于 `上游读数 − 下游读数 + 两尺零点高差(上游−下游)`，落差可为负。
- **核定单**：记录核发时的读数快照、零点高差、落差、测线版本；
  状态 `valid` / `revoked`，撤销单永久保留可查，**撤销后须重新核发**（生成新单号）。

拒发原因（HTTP 422，中文 `detail`）：

1. 两尺未连通（不在同一连通组，或站号未出现在任何测线中）；
2. 所在连通组的测线闭环矛盾；
3. 上游或下游缺该时刻读数。

## 2. 启动（Docker Compose）

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

## 3. /docs 演示脚本（与 Swagger 按钮一一对应）

```bash
B=http://localhost:8000

# ① 录入三站读数
curl -X POST $B/readings -H 'Content-Type: application/json' -d '{"readings":[
  {"station":"S1","utc_time":"2026-11-22T08:00:00Z","gauge_mm":1000},
  {"station":"S2","utc_time":"2026-11-22T08:00:00Z","gauge_mm":1020},
  {"station":"S3","utc_time":"2026-11-22T08:00:00Z","gauge_mm":900}]}'

# ② 整批录入测线：S1-S2 零点高差 15，S2-S3 为 5
curl -X PUT $B/lines -H 'Content-Type: application/json' -d '{"lines":[
  {"upstream":"S1","downstream":"S2","zero_offset_mm":15},
  {"upstream":"S2","downstream":"S3","zero_offset_mm":5}]}'

# ③ 核发 S1→S3：链式零点高差 15+5=20，落差 (1000+20)-900 = 120
curl -X POST $B/approvals/issue -H 'Content-Type: application/json' -d '
  {"utc_time":"2026-11-22T08:00:00Z","upstream":"S1","downstream":"S3"}'

# ④ 换线（S2-S3：5→8）：返回被撤单号与原因，事务内一次生效
curl -X PUT $B/lines -H 'Content-Type: application/json' -d '{"lines":[
  {"upstream":"S1","downstream":"S2","zero_offset_mm":15},
  {"upstream":"S2","downstream":"S3","zero_offset_mm":8}]}'

# ⑤ 按号查原数与状态（revoked，原落差 120 保留，附撤销原因）
curl $B/approvals/1

# ⑥ 重新核发：新高差 23、新落差 123，生成新单号
curl -X POST $B/approvals/issue -H 'Content-Type: application/json' -d '
  {"utc_time":"2026-11-22T08:00:00Z","upstream":"S1","downstream":"S3"}'
```

## 4. 接口一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/readings` | 录入读数（批量）；重复返回 409，整单不写入 |
| GET | `/readings` | 查读数，可按 `station` / `utc_time` 过滤 |
| PUT | `/lines` | **整批替换**测线并复算；返回仍有效单、撤销单（单号+原因）、前后版本号 |
| GET | `/lines` | 查当前测线全集与版本（版本见 `/status`） |
| POST | `/approvals/issue` | 核发落差；不满足条件 422 并说明原因 |
| GET | `/approvals` | 查全部核定单，可按 `?status=valid\|revoked` 过滤 |
| GET | `/approvals/{no}` | 按单号查原数与状态；不存在 404 |
| GET | `/status` | 测线版本、测线/读数/有效单/撤销单计数 |
| GET | `/health` | 健康检查 |

## 5. 并发与一致性

- SQLite 开 **WAL + busy_timeout=30s**；所有写事务 `BEGIN IMMEDIATE`，
  写操作在库级串行，读不阻塞写。
- 换线（删旧线、插新线、版本 +1、逐单复算、撤销）在**同一个事务**内提交：
  任一步失败整体回滚，保留原数据；“换线与撤销一起生效”。
- 并发核发与换线交叉时，核发事务要么在换线提交前完成（随后被复算撤销），
  要么等锁后基于新测线核发，不存在换线后漏撤的旧逻辑单（见实测）。
- 每请求一个 SQLite 连接（线程隔离），uvicorn 多线程下安全。

## 6. 自测

```bash
pip install -r requirements.txt pytest httpx
python -m pytest tests/ -q
# 真进程并发（先起服务，再执行）
BASE_URL=http://127.0.0.1:8000 python tests/live_concurrency_check.py
```

实测结果见文末“交付说明”。

## 7. 目录结构

```
app/
  main.py       FastAPI 路由与异常处理
  schemas.py    Pydantic 模型（strict int、带时区 UTC 校验、/docs 示例）
  graph.py      连通组/闭环矛盾纯函数与零点高差求解
  service.py    核发、换线复算与撤销的事务编排
  db.py         SQLite 连接、WAL/事务、表结构与数据访问
tests/          23 个 pytest 用例 + 真进程并发检查脚本
Dockerfile, docker-compose.yml, requirements.txt
```

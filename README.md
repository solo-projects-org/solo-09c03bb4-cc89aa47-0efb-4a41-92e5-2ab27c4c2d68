# 园区能源补传数据 API（Park Energy Backfill API）

网关断网恢复后，电表累计读数会成批补传。本服务接收补传 CSV，校验并幂等入库，
按小时算出**可计费电量**交给计费组复核；迟到读数填补缺口时，前后小时的结果在同一事务里一起更新。

- FastAPI + SQLite（WAL），两个 API 实例共享同一个数据库文件
- `docker compose up` 一键启动两个实例（`:8001` / `:8002`），Swagger 文档开箱即用
- 本地样例覆盖：缺口、迟到填洞、重传去重、异值拒批、表码回退、双实例并发补传

## 快速开始

```bash
docker compose up --build
# Swagger UI: http://localhost:8001/docs  或  http://localhost:8002/docs
```

跑完整演示（含迟到填洞与双实例并发）：

```bash
./scripts/demo.sh
```

本地开发（不用 Docker）：

```bash
pip install -r requirements-dev.txt
DB_PATH=./data/energy.db uvicorn app.main:app --port 8001 &
DB_PATH=./data/energy.db uvicorn app.main:app --port 8002 &
python -m pytest tests/ -q
```

## 数据规则

**CSV 格式**：表头必须是 `meter_id,utc_second,kwh` 三列（UTF-8，可带 BOM）。

- `utc_second`：UTC 整点秒（能被 3600 整除的非负整数）
- `kwh`：电表累计读数，有限且 ≥ 0
- 任一行不合法 → 整批拒绝（HTTP 422），库中已有结果不受影响

**幂等与冲突**：

- 同表同秒**同值**重传：只记一次（响应计入 `rows_deduplicated`）
- 同表同秒**异值**（含批内自相矛盾）：整批拒绝（HTTP 409），不写任何一行

**小时电量**（`hourly_usage`）：

- 每块表的首个读数只作基线，不产生小时电量
- 相邻两条读数恰好相隔 1 小时且未回退 → 该小时 `billable=true`，`kwh_used` 为差值
- 读数缺口内的每个小时 → `billable=false, reason=gap`
- 表码回退对应的小时 → `billable=false, reason=rollback`
- 迟到读数填补缺口时，受影响窗口（含前后相邻小时）在同一事务内重算

## API

| 方法 | 路径 | 说明 |
|---|---|---|
| `POST` | `/batches` | 上传 CSV 批次：multipart 文件（Swagger 文件选择器）或 `text/csv` 原始体 |
| `GET` | `/readings` | 按 `meter_id` + `from_second`/`to_second`（均为整点秒、闭区间）查原始读数 |
| `GET` | `/hourly-usage` | 按表和时间窗查小时电量、`billable` 与 `reason`（`ok`/`gap`/`rollback`） |
| `GET` | `/batches` | 已接受批次的审计列表 |

`POST /batches` 响应示例：

```json
{
  "batch_id": "96a1464398704927a5051026da511afb",
  "received_at": "2026-11-14T20:58:10Z",
  "rows_total": 2,
  "rows_inserted": 2,
  "rows_deduplicated": 0,
  "meters": ["meter-east-01"],
  "hourly_usage_updated": 3
}
```

curl 示例：

```bash
curl -F "file=@samples/batch1_initial_with_gap.csv;type=text/csv" http://localhost:8001/batches
curl -X POST -H "content-type: text/csv" --data-binary @samples/batch2_late_gap_fill.csv http://localhost:8002/batches
curl "http://localhost:8001/hourly-usage?meter_id=meter-east-01&from_second=1793491200&to_second=1793509200"
```

## Swagger 导入本地样例

打开 `http://localhost:8001/docs` → `POST /batches` → **Try it out** → 文件选择器依次上传：

1. `samples/batch1_initial_with_gap.csv` — 00/01/02/05 点读数，02–05 出现 3 小时缺口
2. `GET /hourly-usage?meter_id=meter-east-01` — 可见 02/03/04 点 `reason=gap`
3. `samples/batch2_late_gap_fill.csv` — 迟到的 03/04 点读数；再查可见缺口各小时已变为可计费
4. `samples/batch3_retransmit_same_values.csv` — 同值重传，`rows_inserted=0`、`rows_deduplicated=2`
5. `samples/batch4_conflicting_value.csv` — 异值冲突，HTTP 409，已存数据不变
6. `samples/batch5_meter_rollback.csv` — `meter-west-02` 表码回退，对应小时不可计费
7. `samples/batch6a_concurrent.csv` / `batch6b_concurrent.csv` — 分别发往两个实例（演示并发补传同一表）

## 并发与一致性设计

两个容器（`api-a`、`api-b`）挂载同一个 named volume，共享 `/data/energy.db`：

- **WAL 模式 + `busy_timeout=30s`**：读写不互斥，写者排队而不是报 `database is locked`
- **每批一个 `BEGIN IMMEDIATE` 事务**：批次行、原始读数、重算的小时电量一起提交或一起回滚。
  写者被串行化，因此两个实例同时补传同一表时，成功批次全部保留、不会互相覆盖
- **查询只读已提交快照**：不会看到"新读数配旧用量"的中间态
- **校验失败（422）或冲突（409）发生在提交前**：此前完整结果照常可查，失败批次不留痕迹
- `(meter_id, utc_second)` 主键兜底，同值重传天然幂等

## 项目结构

```
app/
  main.py    # FastAPI 路由与请求校验
  logic.py   # CSV 解析、入库事务、小时电量窗口重算
  db.py      # SQLite 连接（WAL、busy_timeout）与建表
samples/     # 本地样例 CSV（缺口/迟到填洞/重传/冲突/回退/并发）
scripts/demo.sh   # 端到端演示：跑通全部业务场景
tests/test_app.py # 18 个 pytest 用例（含多线程并发同表补传）
docker-compose.yml # api-a:8001 + api-b:8002 共享 named volume
```

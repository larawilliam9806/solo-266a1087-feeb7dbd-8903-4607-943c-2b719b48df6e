# 噪声小时等效声级监测 API

监测员按测点登记仪器读数与校准单，服务按 **UTC 小时**计算等效声级 Leq，并给出主表 / 备表各计入的秒数。
技术栈：FastAPI + SQLite（WAL），支持 `docker compose` 一键启动。

## 1. 业务规则

1. **读数登记**：按编号登记，填写测点、主备角色（`primary` / `backup`）、左闭右开的 UTC 整秒区间 `[start, end)` 和段内恒定分贝值。
2. **区间互斥**：同一仪器（测点 + 角色）的读数区间互不重叠；校准单的有效区间同样互不重叠（相邻允许，因为区间左闭右开）。
3. **校准叠加**：取读数与**有效校准单**重叠的时段，把段内恒定分贝值加上校准偏移；校准单同仪器互不重叠，任一秒最多命中一个偏移。
4. **主备合并**：逐秒选择数据源——主表优先，主表未覆盖的秒由有效备表补；未被任何仪器覆盖的秒不参与平均。
5. **等效声级**：按计入时长对声能平均后换回分贝：
   `Leq = 10·lg( Σ(10^(0.1·Li)·ti) / Σti )`，无任何覆盖时报无数据（单小时查询返回 404）。
6. **读数补交 / 重传**：读数可补交任意历史时刻；同号只接受原样重传（幂等 200），同号内容不同返回 409。
7. **校准单变更**：可修改起止时刻与偏移（测点、角色不可变），可撤回（软删除，撤回幂等）；已撤回编号不可复活。
8. **接反更正**：两处测点主表通道接反时，按更正号登记两个测点与左闭右开区间。区间内主表读数换回各自测点，再按实际测点取校准偏移；备表读数和校准单始终按登记测点核算。更正可在读数补交前登记，已有和补交的主表读数跨越更正边界时均分段归属。同一测点的更正时段允许相邻、重叠拒绝；测点对无方向。同号同内容重传幂等，同号内容有变报 409。原始读数查询与同号重传仍以报送内容为准。
9. **增量重算**：每次写入只重算涉及的测点和小时（登记更正重算两个测点，补交主表读数连带更正对方测点）；原始数据与小时结果在**同一事务**内提交，失败则一起回滚。
10. **并发写入**：写事务以 `BEGIN IMMEDIATE` 串行化，配合 `busy_timeout` 等待锁；成功的变更全部计入，失败时原数据与小时结果保持不变。

## 2. 目录结构

```
.
├── app/
│   ├── main.py        # FastAPI 路由与异常映射
│   ├── services.py    # 领域逻辑：登记、重叠校验、校准叠加、主备合并、接反更正、小时重算
│   ├── database.py    # SQLite 连接、建表、写事务
│   ├── schemas.py     # Pydantic 请求 / 响应模型
│   ├── config.py      # 环境变量配置
│   └── time_utils.py  # UTC 整秒解析与受影响小时计算
├── tests/             # pytest 用例（37 个，含并发场景）
├── Dockerfile
├── docker-compose.yml
└── requirements.txt
```

## 3. 启动

### docker compose（推荐）

```bash
docker compose up -d --build
# 服务监听 http://localhost:8000，SQLite 文件存放在 noise-data 卷的 /data/noise.db
curl http://localhost:8000/health
```

交互式文档：`http://localhost:8000/docs`（Swagger UI）。

### 本地运行

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
NOISE_DB_PATH=./data/noise.db uvicorn app.main:app --reload
```

### 环境变量

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `NOISE_DB_PATH` | `/data/noise.db` | SQLite 数据库文件路径 |
| `NOISE_BUSY_TIMEOUT_MS` | `10000` | 写锁等待上限（毫秒） |
| `NOISE_MAX_HOUR_RANGE` | `744` | 区间查询最多返回的小时数 |

## 4. API

所有时间均为带 `Z` 后缀的 UTC ISO-8601 整秒（也接受纪元秒整数），区间一律左闭右开。

### 4.1 读数

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `PUT` | `/readings/{number}` | 登记读数；同号原样重传幂等，内容不同 409，同仪器区间重叠 409 |
| `GET` | `/readings?point=` | 列表，可按测点过滤 |

```bash
curl -X PUT http://localhost:8000/readings/R-0001 \
  -H 'Content-Type: application/json' \
  -d '{"point":"P-101","role":"primary",
       "start":"2026-11-21T08:00:00Z","end":"2026-11-21T08:30:00Z","db":60.0}'
```

### 4.2 校准单

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `PUT` | `/calibrations/{number}` | 登记或修改（仅起止时刻、偏移可改；测点/角色变更 409） |
| `POST` | `/calibrations/{number}/withdraw` | 撤回（幂等） |
| `GET` | `/calibrations?point=&include_withdrawn=` | 列表 |

```bash
curl -X PUT http://localhost:8000/calibrations/C-0001 \
  -H 'Content-Type: application/json' \
  -d '{"point":"P-101","role":"primary",
       "start":"2026-11-21T08:00:00Z","end":"2026-11-21T08:15:00Z","offset":3.0}'

curl -X POST http://localhost:8000/calibrations/C-0001/withdraw
```

### 4.3 接反更正

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `PUT` | `/corrections/{number}` | 登记主表通道接反更正；同号同内容（同一对测点 + 同一区间）重传幂等，内容不同 409，同一测点时段重叠 409 |
| `GET` | `/corrections?point=` | 列表，可按测点过滤（匹配更正任一端） |

```bash
curl -X PUT http://localhost:8000/corrections/SW-0001 \
  -H 'Content-Type: application/json' \
  -d '{"point_a":"P-101","point_b":"P-102",
       "start":"2026-11-21T08:00:00Z","end":"2026-11-21T09:00:00Z"}'
```

区间内两个测点的主表读数互换归属，校准偏移按换回后的实际测点叠加；备表读数与校准单仍按登记测点核算。
测点对无方向（`point_a` / `point_b` 按字典序归一化存储），两个测点必须不同。

### 4.4 小时结果

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/points/{point}/hours/{hour}` | 单小时；`hour` 必须 UTC 整点，无覆盖返回 404 |
| `GET` | `/points/{point}/hours?start=&end=` | 整点区间查询（左闭右开），只返回有覆盖的小时 |

响应字段：

```json
{
  "point": "P-101",
  "hour": "2026-11-21T08:00:00Z",
  "leq_db": 57.40362689494244,
  "covered_seconds": 3600,
  "primary_seconds": 1800,
  "backup_seconds": 1800
}
```

### 4.5 错误码

| 状态码 | 含义 |
| --- | --- |
| 400 | 区间 `start >= end`、hour 非整点、更正两端测点相同等语义错误 |
| 404 | 编号不存在 / 该小时无覆盖（无数据） |
| 409 | 同号内容不同、同仪器区间重叠、更正时段重叠、校准单已撤回或试图改测点/角色 |
| 422 | 请求体字段缺失或时间格式不合法 |
| 503 | 写锁等待超时，事务已回滚，可安全重试 |

## 5. 关键设计

- **存储**：读数、校准单、更正、小时结果四张表；时间统一存 UTC 纪元秒整数，区间重叠用半开判定 `existing.start < new.end AND existing.end > new.start`。
- **接反更正**：更正只影响小时核算，不改写原始读数。重算某测点某小时时，本测点主表读数先扣除更正窗口，再把对方测点主表读数落在窗口内的片段换回，校准偏移按归属测点叠加；同一测点的更正时段互不重叠，保证任一秒的归属无歧义。
- **增量重算**：写入时按区间求出所有触及的 UTC 小时（校准修改取新旧窗口并集；更正取两个测点的窗口小时；主表读数补交连带更正对方测点的重叠小时），逐小时删除旧结果后基于当前全量原始数据重算，无覆盖则不写结果行。
- **逐秒合并**：小时内 3600 个秒槽，主表数据优先占位，备表只填空槽；校准在切片阶段完成偏移叠加。
- **事务与并发**：WAL 模式 + `BEGIN IMMEDIATE` + `busy_timeout`，每个请求独立连接；原始数据变更与小时重算同事务提交或回滚。

## 6. 测试

```bash
pip install -r requirements.txt pytest httpx
pytest -q
```

覆盖：Leq 声能平均、主备优先与补缺口、校准叠加/修改/撤回、区间互斥、原样重传、补交、无数据 404、
测点隔离、失败回滚、接反更正（换归属、跨边界分段、按实际测点校准、备表不动、先更后补、相邻/重叠、幂等重传），
以及多线程并发写（互不重叠全部成功、互斥区间恰有一方成功且旧结果不被破坏）。

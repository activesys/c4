# C4_FUN_00067 测试方案

> **对应功能**：`c4/docs/specification/c4_function.md` C4_FUN_00067
> **对应需求**：C4_RS_00094
> **设计参考**：`c4/docs/design/c4_influxdb_client.md` §2, §3, §5, §6

C4_FUN_00067：Agent 生成 InfluxDB 写入 MCP 服务的配置文件后，启动 MCP 服务，MCP 服务根据配置文件将共享内存中的数据写入 InfluxDB。

---

## 1. 测试目标

验证 `c4_influxdb_client` 在收到 Agent 的 `start` 工具调用后：

1. 通过 `config_path` 参数获取配置文件路径并读取 `c4_influxdb_client` 配置段
2. 校验配置有效性（实例级：url 格式 / token·org·bucket 必填 / batch_size·flush_interval 取值；point 级：type 枚举 / measurement 非空 / field·tags 键名 / shm_id 实例内唯一）
3. 以 `O_RDONLY` 附加已有共享内存并校验 magic
4. 构建 `shm_id → (measurement, field, type, tags)` 映射索引
5. 为每个配置实例启动一个 goroutine，**异步**发起 HTTP 写入循环
6. **`start` 不等待与 InfluxDB 的连接建立**——所有实例均已启动即返回 `"success"`，InfluxDB 不可达/写入失败属运行时事件（记日志 + 重试），不导致 `start` 返回错误
7. 各错误码正确返回

> **与 `c4_modbus_client` 的关键差异**：modbus 的 `start` 要求「全部连接成功才 success、任一失败 tear down 并返回 `CONNECT_FAILED`」；`c4_influxdb_client` 的 `start` **不等待连接**（见 [c4_influxdb_client.md §5.1](c4_influxdb_client.md) 与 [c4_architecture.md §3.3.1](c4_architecture.md) 返回时机语义），故**无 `CONNECT_FAILED` 错误码**，取而代之的是「InfluxDB 不可达时 `start` 仍返回 `success`」（TC13）。

---

## 2. 测试架构

```
c4/test/c4_fun_00067/
├── README.md              # 本文件
├── conftest.py            # 公共 fixture（复用 c4_fun_00062，新增真实 InfluxDB + SUT）
├── shm_helpers.py         # 共享内存操作（复用 c4_fun_00012，扩展写入 helper，见 §2.6）
└── test_start.py          # TC1~TC13
```

### 2.1 被测对象 (SUT)

Go 编译的 `c4_influxdb_client` 二进制，通过 Python `subprocess.Popen` 启动，
走 **MCP stdio JSON-RPC** 协议（同 c4_fun_00062）。

### 2.2 测试基础设施：真实 InfluxDB 1.8.10

测试使用**真实 InfluxDB 1.8.10**（`/home/wangbo/backup/influxdb/influxdb-1.8.10-1/usr/bin/influxd`）
作为下游，通过其 HTTP API 验证数据写入：

| 组件 | 角色 | 说明 |
|------|------|------|
| `c4_shm_manager` | 共享内存创建/分配 | `create_shm` + `adjust_shm`（复用 c4_fun_00062 的 `shm_mgr_client`） |
| `influxd` (1.8.10) | 下游数据库 | 独立端口 + 临时数据目录启动，无认证（`auth-enabled=false`） |
| `c4_influxdb_client` | SUT | Reader，读共享内存 → 编码 line protocol → 写入 InfluxDB |

```
Python mmap 模拟 Writer（写 DataBlock）→ 共享内存 → c4_influxdb_client（Reader）→ HTTP line protocol → 真实 InfluxDB 1.8.10
```

> 启动测试（本套件）主要验证 `start` 返回值与错误码，**不依赖数据写入**；
> InfluxDB 仅需「可达」（TC13 验证「不可达时 start 仍 success」）。数据写入验证见 c4_fun_00016。

### 2.3 InfluxDB 1.8.10 启动与准备

- **启动**：`influxd run -config <临时配置>`，配置含 `[meta]`/`[data]`/`[http]` 段，
  `bind-address` 用独立动态端口、`auth-enabled=false`
- **就绪判定**：轮询 `GET /ping` 返回 `204`
- **数据库准备**：每个 TC 通过 `POST /query` 的 `CREATE DATABASE <db>` 创建独立 database
- **写入端点**：InfluxDB 1.8.10 同时支持 legacy `/write?db=` 与 v2 兼容 `/api/v2/write?org&bucket`；
  本测试用 `/api/v2/write`（bucket 填 database 名，org 填任意非空值（被忽略）），与设计文档 §2.2 一致
- **精度**：**必须带 `precision=ms`**——InfluxDB 1.x 默认纳秒，不带则毫秒时间戳被误解析
  （实测 `1768848814264` 无 precision 显示为 1970 年，带 `precision=ms` 显示为 2026 年）

### 2.4 shm_id 分配与模拟 Writer

`c4_influxdb_client` 是 **Reader**，不分配 shm_id——其 point 用 `key`（`{service_id}.{point_id}`）
引用上游 Writer 的采集点，由 `c4_shm_manager.adjust_shm` 回填相同 shm_id。

本套件配置一个**占位 Writer**（`c4_modbus_client` 单点，**不实际启动**，仅让 `adjust_shm`
为其分配 shm_id 并回填 influxdb 的 key 引用）。数据由测试用 Python **mmap 直接写共享内存**
模拟（同 c4_fun_00055 用 mmap 写 block 模拟 Writer 激活的先例）。

### 2.5 conftest.py 导出 fixture 契约

| fixture | scope | 职责 |
|---------|-------|------|
| `shm_mgr_client` | function | 复用 c4_fun_00062——启动 c4_shm_manager，MCP initialize |
| `isolated_shm` | function | 复用 c4_fun_00062——shm 隔离/清理 |
| `prepare_environment` | function | 复用 c4_fun_00062——生成配置 → create_shm → adjust_shm → 关闭，返回 config_path |
| `influxdb` | session | 启动真实 InfluxDB 1.8.10（独立端口 + 临时数据目录），返回 `http://127.0.0.1:<port>` |
| `create_database` | function | 在 influxdb 上创建独立 database，返回 db 名，teardown 时 DROP |
| `start_influxdb_client` | function | 启动 c4_influxdb_client 子进程（MCP initialize），返回 MCP 客户端句柄 |
| `_run_adjust_shm` | helper | 复用 c4_fun_00062——启动独立 c4_shm_manager → `adjust_shm` → 关闭（供 00068 Stop-Start 二次调整，re-export 自 `_c62._run_adjust_shm`） |

> `c4_fun_00067` 是 influxdb 三套件的**主套件**，上述 fixture 由 `c4_fun_00016`、`c4_fun_00068`
> 通过 `importlib.util` 复用（复用方式见各 README §5.1）。

### 2.6 shm_helpers.py 写入 helper

本套件扩展共享内存 helper，新增**写入**能力（模拟 Writer；读能力复用 c4_fun_00012）：

```python
def write_shm_block(shm_path: str, shm_id: int, data_type: int, value, timestamp: int) -> int:
    """模拟 Writer 写入一个 Data Block（32 字节，本机序），返回新 write_seq。

    - magic @0：保持不变（由 shm_manager create_shm 写入 0xC4DA7A00）
    - state @4：首次写入置 1（激活）
    - type  @7：data_type（ASFP2 枚举）
    - write_seq @8：取当前值，+1（奇数，写中）→ 写 value/timestamp → +1（偶数，稳定），返回偶数
    - timestamp @16：本机序
    - value @24：本机序低位字节，高位补零
    """
```

---

## 3. 测试配置模板

### 3.1 c4_influxdb_client 配置（SUT）

```json
{
    "c4_shm_manager": {"writer": ["c4_modbus_client"], "reader": ["c4_influxdb_client"]},
    "c4_modbus_client": [{
        "name": "占位采集", "id": "fake_writer",
        "ip": "127.0.0.1", "port": 502,
        "t0": 5, "t1": 5, "retries": 1,
        "coils_quantity_max": 2000, "registers_quantity_max": 125,
        "hton_register": 1, "hton_total": 0, "timer": 1000,
        "points": [{"id": "pt1", "uid": 1, "addr": 0, "fun": 3, "type": 5, "swap": 0, "shm_id": 0}]
    }],
    "c4_influxdb_client": [{
        "name": "启动测试入库", "id": "test_influx",
        "url": "http://127.0.0.1:<influxdb_port>",
        "token": "test-token",
        "org": "activesys",
        "bucket": "<database>",
        "precision": "ms",
        "batch_size": 5000,
        "flush_interval": 100,
        "timer": 100,
        "gzip": 0,
        "t0": 5,
        "retries": 1,
        "points": [
            {"key": "fake_writer.pt1", "measurement": "wind_turbine", "field": "windspeed", "type": "float", "tags": {"site": "hnals"}, "shm_id": 0}
        ]
    }]
}
```

> 测试用 `timer=100`、`flush_interval=100`（100ms）加速数据流；`t0=5`、`retries=1` 缩短失败路径。
> `token` 在 1.8（无认证）下被忽略，但配置仍提供（SUT 校验必填）。
> **占位 Writer `c4_modbus_client` 不启动**——仅让 `adjust_shm` 为其 point 分配 shm_id，回填 influxdb 的 key 引用。

### 3.2 期望的 shm_id 分配

1 个占位 writer point → `adjust_shm` 分配 `fake_writer.pt1` → shm_id=1，
influxdb 的 `key: "fake_writer.pt1"` 被回填 shm_id=1。

---

## 4. 测试用例

### TC1: 基本启动 — 单实例单点

- **前置**：influxdb 已启动 + `create_database`，`prepare_environment`（create_shm + adjust_shm），SUT MCP initialize
- **操作**：调用 `start`（传入 config_path）
- **预期**：`start` 返回 `"success"`（`isError: false`）
- **说明**：`start` 仅要求 goroutine 启动即返回 success（不等待与 InfluxDB 的连接建立）

### TC2: 多实例启动 — 3 个实例

- **前置**：1 个 influxdb；c4 配置 3 个 influxdb 实例（`id` 分别为 `db1`/`db2`/`db3`，均指向同一 influxdb，各 1 个 point，占位 writer 配 3 个 point）
- **操作**：调用 `start`
- **预期**：返回 `"success"`
- **说明**：3 个 goroutine 各自独立写入

### TC3: 空实例列表 — 0 个实例

- **前置**：配置 `"c4_influxdb_client": []`（空数组），**但 `prepare_environment` 仍运行**——
  占位 writer（`c4_modbus_client`）保留在 `writer` 列表（**非空**），`create_shm` + `adjust_shm`
  正常完成，共享内存已创建（否则 `adjust_shm` 可能因 writer 列表为空而报错）
- **操作**：调用 `start`
- **预期**：返回 `"success"`（无实例需启动，但仍需 shm_open + mmap + magic 校验）

### TC4: 重复调用 start → ALREADY_RUNNING

- **前置**：TC1 已成功启动
- **操作**：再次调用 `start`（同一 SUT 进程，无间隔 `stop`）
- **预期**：`isError: true`，`content[0].text` 以 `ALREADY_RUNNING` 开头

### TC5: start 未调用前调用 stop → 幂等 success

- **前置**：启动 SUT，MCP initialize 完成，但未调用 `start`
- **操作**：调用 `stop`
- **预期**：返回 `"success"`（`isError: false`）——`stop` 幂等

### TC6: config_path 缺失 → CONFIG_PATH_MISSING

- **前置**：启动 SUT，MCP initialize，共享内存正常
- **操作**：调用 `start`，提供 `instance_id` 但不提供 `config_path` 参数
- **预期**：`isError: true`，错误码 `CONFIG_PATH_MISSING`

### TC7: 配置文件格式错误 → CONFIG_PARSE_ERROR

- **前置**：共享内存正常。`pytest.mark.parametrize` 子场景：
  - (a) JSON 语法错误：`{invalid json`
  - (b) 合法 JSON 但缺 key：`{"c4_shm_manager": {...}}`（无 `c4_influxdb_client` 段）
- **操作**：调用 `start`（传入对应 config_path）
- **预期**：`isError: true`，错误码 `CONFIG_PARSE_ERROR`

### TC8: 共享内存不存在 → SHM_OPEN_FAILED

- **前置**：不创建共享内存（跳过 create_shm + adjust_shm），但**手工将配置中各 point 的
  `shm_id` 置为非 0 值**（如 1，模拟 adjust_shm 已回填），influxdb 已启动
- **操作**：调用 `start`
- **预期**：`isError: true`，错误码 `SHM_OPEN_FAILED`
- **说明**：跳过 adjust_shm 会让 shm_id 停留为 0，而 start 的校验顺序是「先校验配置
  （含 shm_id）再 shm_open」——shm_id=0 会先触发 `SHM_ID_NOT_ASSIGNED`。故须手工置非 0
  shm_id，才能让配置校验通过、进入 `shm_open` 失败分支

### TC9: 共享内存 magic 损坏 → SHM_CORRUPTED

- **前置**：c4_shm_manager 已创建 shm，Python 将 Header magic 改为 `0xDEADBEEF`
- **操作**：调用 `start`
- **预期**：`isError: true`，错误码 `SHM_CORRUPTED`

### TC10: shm_id 未分配（=0）→ SHM_ID_NOT_ASSIGNED

- **前置**：c4_shm_manager 完成 `create_shm`，但**跳过 `adjust_shm`**（配置中 shm_id 仍为 0），influxdb 已启动
- **操作**：调用 `start`
- **预期**：`isError: true`，错误码 `SHM_ID_NOT_ASSIGNED`

### TC11: 实例级字段非法 → INVALID_CONFIG

- **前置**：共享内存正常，influxdb 已启动。`pytest.mark.parametrize` 子场景：

  | 子场景 | 非法字段 | 配置 |
  |--------|---------|------|
  | (a) url 缺失 | 无 `url` 字段 | 删除 url |
  | (b) url 格式错 | `url: "not-a-url"` | 非法 URL |
  | (c) token 缺失 | 无 `token` 字段 | 删除 token |
  | (d) token 为空 | `token: ""` | 空字符串（必填 = 非空） |
  | (e) org 缺失 | 无 `org` 字段 | 删除 org |
  | (f) bucket 缺失 | 无 `bucket` 字段 | 删除 bucket |
  | (g) batch_size ≤ 0 | `batch_size: 0` | 非法取值 |
  | (h) flush_interval < 0 | `flush_interval: -1` | 非法取值 |

- **操作**：调用 `start`
- **预期**：`isError: true`，错误码 `INVALID_CONFIG`，消息指明具体字段与取值

### TC12: point 字段非法 → INVALID_POINT

- **前置**：共享内存正常，influxdb 已启动。`pytest.mark.parametrize` 子场景：

  | 子场景 | 非法字段 | 配置 |
  |--------|---------|------|
  | (a) type 非法 | `type: "string"` | 非 float/int/uint/bool |
  | (b) measurement 为空 | `measurement: ""` | 空字符串 |
  | (c) tag 键名非法 | `tags: {"bad.key": "v"}` | 键含 `.`（违反 `[a-zA-Z_]+`） |
  | (d) field 键名非法 | `field: "bad-key"` | 键含 `-`（违反 `[a-zA-Z_]+`） |
  | (e) shm_id 实例内重复 | 两个 point 均 `shm_id: 1` | 同实例 shm_id 重复 |

- **操作**：调用 `start`
- **预期**：`isError: true`，错误码 `INVALID_POINT`，消息指明具体字段与取值

### TC13: InfluxDB 不可达 — start 仍返回 success（不等待连接）

- **前置**：共享内存正常（create_shm + adjust_shm 完成）。配置 `url` 指向**无 influxd 监听的端口**
- **操作**：调用 `start`
- **预期**：`start` 返回 `"success"`（`isError: false`）
- **说明**：验证设计文档 §5.1「start 不等待连接」。写入失败是运行时事件（记日志 + `retries`
  次重试），**不导致 `start` 返回错误**——这是与 modbus 的 `CONNECT_FAILED` 语义的根本差异。

---

## 5. 实现注意

### 5.1 复用 c4_fun_00062 的基础 fixture

`conftest.py` 通过 `importlib.util` 复用 `c4_fun_00062/conftest.py` 的 fixture
（`shm_mgr_client`、`isolated_shm`、`prepare_environment`、`McpClient` 及各类 helper
`_find_*_binary`/`_free_port`/`_assert_mcp_success`/`_assert_mcp_error`），复用方式同
c4_fun_00012 复用 c4_fun_00062。新增：

- `influxdb`（session）：启动真实 InfluxDB 1.8.10（见 §5.2），返回 URL
- `create_database`（function）：创建独立 database，teardown DROP
- `start_influxdb_client`：启动 c4_influxdb_client（`_find_influxdb_client_binary` 查找/编译，
  编译失败 `pytest.skip`）
- 配置工厂：`_make_c4_config` / `_make_influx_instance` / `_make_influx_point` / `_make_placeholder_writer`
- **helper re-export**：`_run_adjust_shm = _c62._run_adjust_shm`（供 00068 复用，见 §2.5）

> **注意**：`c4_influxdb_client` 是 Reader，不依赖 redis / modbusd / iec104d / 授权。
> 复用 `prepare_environment` 时**不需要** `license_env` / `redis_server` fixture。

### 5.2 InfluxDB 1.8.10 启动

- 二进制：默认 `/home/wangbo/backup/influxdb/influxdb-1.8.10-1/usr/bin/influxd`，可用 env
  `INFLUXD_PATH` 覆盖；`influx` CLI 同目录
- **前置要求**：influxd 版本须为 **1.8+**（支持 v2 兼容 `/api/v2/write`）。CI / 其他主机须
  提供 `INFLUXD_PATH` 环境变量指向 influxd 二进制（或放置于默认路径）；两者皆无则
  `pytest.skip`（不因环境缺失而误报失败）
- 临时配置（独立动态端口 + 临时 meta/data/wal 目录 + `auth-enabled=false` + `reporting-disabled`）：
  ```toml
  reporting-disabled = true
  [meta]
    dir = "<tmp>/meta"
  [data]
    dir = "<tmp>/data"
    wal-dir = "<tmp>/wal"
  [http]
    bind-address = "127.0.0.1:<port>"
    auth-enabled = false
  ```
- 启动：`setsid nohup influxd run -config <cfg> > log 2>&1 < /dev/null &`（脱离测试进程，
  避免 shell 等待后台进程）
- 就绪：轮询 `GET /ping` 返回 `204`（超时 10s）
- teardown：SIGTERM 终止进程 + 清理临时目录

### 5.3 数据库准备与查询验证

- 创建 database：`POST /query` body `q=CREATE DATABASE <db>`（返回 200）
- 查询验证（c4_fun_00016 使用）：`POST /query?db=<db>` body `q=SELECT * FROM <measurement>`
  或 `q=SHOW FIELD KEYS FROM <measurement>`；解析 JSON `results[0].series[0]`
- 每个 TC 独立 database 名（如 `test_<tc>_<uuid>`），teardown `DROP DATABASE`

### 5.4 占位 Writer 说明

- 占位 `c4_modbus_client` **不启动**（无 modbusd、无 redis），仅作为 `adjust_shm` 的 shm_id
  分配载体——`adjust_shm` 为其 point 分配 shm_id=1，并将 influxdb point 的 `key` 引用回填为同一 shm_id
- 占位 writer point 的 `type` 字段取值不影响测试（mock writer 不实际写入）；实际写入由
  `write_shm_block`（§2.6）按测试需要的 type/value 直接写

### 5.5 隔离性

- 每个 TC 独立 `instance_id`（共享内存路径不冲突）、独立 database
- fixture teardown：关闭 SUT、DROP database、shm_unlink、删除临时配置

### 5.6 禁止事项

- **不得调用 `status` 工具**：该接口后续有调整，测试用例中不得使用
- 验证手段仅限：`start`/`stop` 返回值、共享内存 mmap 读写、InfluxDB HTTP 查询结果

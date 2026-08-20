# C4_FUN_00016 测试方案

> **对应功能**：`c4/docs/specification/c4_function.md` C4_FUN_00016
> **对应需求**：C4_RS_00094
> **设计参考**：`c4/docs/design/c4_influxdb_client.md` §4

C4_FUN_00016：C4 将采集到的数据写入 InfluxDB 数据库（从共享内存读取数据点，按 line protocol 编码后批量写入）。

---

## 1. 测试目标

验证 `c4_influxdb_client` 在 `start` 成功后的数据写入通路（Python mmap 模拟 Writer + 真实 InfluxDB 1.8.10）：

1. 从共享内存读取已订阅 shm_id 的数据（`write_seq > last_seen` 过滤新数据）
2. **采集类型 → 入库类型转换**：跟随采集类型（auto）与显式指定 `type` 两种路径
3. line protocol 编码：measurement / tags / field / timestamp 正确拼装
4. **类型编码**：float / int（`i` 后缀）/ bool（`true`/`false`）写入 InfluxDB 后字段类型正确
5. **显式转换**：INT32 → float 时字段类型为 **float**（而非 integer）
6. **tag 转义**：tag value 含逗号 / 空格 / 等号 / 中文时正确入库
7. 非数值类型（STRING 等）block 被跳过、不写入
8. 数据变化后（`write_seq` 递增）写入新值
9. timestamp 精度（`precision=ms` 透传毫秒时间戳，时间正确）
10. measurement（表名）按 point 配置正确映射（不同 point 落到不同表）
11. 整数类型缺省（auto）→ integer（与显式 `"float"` 形成对照）

---

## 2. 测试架构

```
c4/test/c4_fun_00016/
├── README.md              # 本文件
├── conftest.py            # 复用 c4_fun_00067 的 fixture
├── shm_helpers.py         # 复用 c4_fun_00067 的写入 helper
└── test_write.py          # TC1~TC11
```

### 2.1 被测对象 (SUT)

Go 编译的 `c4_influxdb_client` 二进制，通过 MCP stdio JSON-RPC 控制（同 c4_fun_00067）。

### 2.2 测试基础设施与数据流

复用 c4_fun_00067 的完整数据流（见其 README §2.2）：

```
Python mmap 模拟 Writer（write_shm_block）→ 共享内存 → c4_influxdb_client（Reader）→ HTTP line protocol → 真实 InfluxDB 1.8.10
```

### 2.3 fixture 复用

`conftest.py` 通过 `importlib.util` 复用 `c4_fun_00067/conftest.py` 的 fixture
（`shm_mgr_client`、`isolated_shm`、`prepare_environment`、`influxdb`、`create_database`、
`start_influxdb_client`），复用方式同 c4_fun_00066 复用 c4_fun_00065。

### 2.4 数据写入与验证

测试用 `write_shm_block`（c4_fun_00067 §2.6）向共享内存写入已知 type/value/timestamp，
`c4_influxdb_client` 轮询（`timer=100ms`）读入 → 编码 → flush（`flush_interval=100ms`）→
写入 InfluxDB。断言依据 **InfluxDB 查询结果**（非捕获 HTTP 请求）：

- **字段类型**：`SHOW FIELD KEYS FROM <measurement>` 返回 field 的 `fieldType`
  （`float`/`integer`/`boolean`），验证类型转换正确性
- **字段值**：`SELECT * FROM <measurement>` 返回写入的值
- **tag 值**：查询结果中的 tag 列
- **时间戳**：查询结果中的 `time` 列（验证 `precision=ms`）

### 2.5 查询验证 helper

```python
def query_influx(url: str, db: str, q: str) -> list:
    """POST /query?db=<db> 执行查询，返回 results[0].series（无结果返回 []）。"""
    # q 如 "SELECT * FROM <m>" / "SHOW FIELD KEYS FROM <m>"

def field_type(url: str, db: str, measurement: str, field: str) -> str | None:
    """SHOW FIELD KEYS 查询指定 field 的 fieldType（float/integer/boolean），不存在返回 None。"""

def query_latest(url: str, db: str, measurement: str, field: str):
    """SELECT 查询指定 field 的最新值（含 tag 列与 time 列）。"""
```

---

## 3. 测试配置模板

### 3.1 c4_influxdb_client 配置（SUT）

同 c4_fun_00067 §3.1（`timer=100`、`flush_interval=100`、`precision=ms`、`gzip=0`），
points 按各 TC 需求调整（`type` 字段、`measurement`/`field`/`tags`）。

### 3.2 采集类型 → 入库类型 → InfluxDB 字段类型（断言依据）

设计文档 §4.4.1。测试涉及的采集类型（`write_shm_block` 写入的 block.type）与 InfluxDB 字段类型：

| block.type（采集） | 枚举值 | point.type（入库） | InfluxDB fieldType | 说明 |
|-------------------|--------|-------------------|-------------------|------|
| FLOAT32 | 10 | 缺省（auto） | `float` | 浮点裸写 |
| INT32 | 5 | 缺省（auto） | `integer` | 跟随采集类型，`i` 后缀 |
| INT32 | 5 | `"float"` | `float` | **显式转 float**（非 integer） |
| INT32 | 5 | `"int"` | `integer` | `i` 后缀 |
| BOOLEAN | 0 | 缺省（auto） | `boolean` | `true`/`false` |

> **InfluxDB 1.x 与 2.x 的类型语义差异**（重要）：
> - 1.x 中裸数字（无小数点无后缀）默认解析为 **float**；2.x 中默认解析为 **integer**。
>   设计文档 §4.4.1 的「float 编码必须含小数点（`25.0`）」是针对 **2.x 目标**的规则——
>   在 1.x 下此规则无害（`25` 与 `25.0` 都是 float），但**无法通过 1.x 的 fieldType 区分
>   SUT 是否真的加了小数点**。故本套件 TC2 仅验证「显式转 float 后 fieldType=float
>   （非 integer）」，不验证小数点字面形式。
> - 1.x **不支持 unsigned（`u` 后缀）**——`25u` 会返回 `400 invalid number`。设计文档的
>   `"uint"` 类型（2.x 特性）**无法在 1.8 环境验证**，见 §5.4 边界说明。

---

## 4. 测试用例

### TC1: 基本写入 — FLOAT32 跟随采集类型

- **配置**：1 point，`type` 缺省（auto），`measurement: "wind_turbine"`, `field: "windspeed"`, `tags: {"site": "hnals"}`
- **操作**：`write_shm_block(shm_id=1, type=10, value=12.5, timestamp=1768848814264)` → 等待写入
- **预期**：`field_type("wind_turbine", "windspeed") == "float"`，`query_latest` 值 ≈ `12.5`，tag `site=hnals`
- **说明**：FLOAT32 跟随采集类型 → float

### TC2: 显式转换 INT32 → float（字段类型为 float 而非 integer）

- **配置**：1 point，`type: "float"`，采集 INT32
- **操作**：`write_shm_block(type=5, value=25)` → 等待写入
- **预期**：`field_type(..., "windspeed") == "float"`（**不是 integer**），值 = `25`
- **说明**：验证「采集类型与入库类型解耦」——INT32 采集 + `type:"float"` 入库，字段类型为
  float。若 SUT 错误地按采集类型直接编码（`25i`），fieldType 会是 `integer`，本测试即失败

### TC3: 整数 int 后缀 → integer

- **配置**：1 point，`type: "int"`，采集 INT32
- **操作**：`write_shm_block(type=5, value=25)` → 等待写入
- **预期**：`field_type(..., "windspeed") == "integer"`，值 = `25`
- **说明**：`type:"int"` → 编码 `25i` → InfluxDB 存储为 integer

### TC4: 布尔类型 → boolean

- **配置**：1 point，`type` 缺省（auto），采集 BOOLEAN
- **操作**：
  1. `write_shm_block(type=0, value=1)` → 用 `wait_value` 轮询等待查询到 `true`
  2. `write_shm_block(type=0, value=0)`（write_seq 递增）→ 用 `wait_value` 等待查询到 `false`
- **预期**：`field_type(..., "status") == "boolean"`，值依次为 `true` → `false`
- **说明**：BOOLEAN 跟随采集类型 → bool，编码 `true`/`false`。中间态 `true` 必须用轮询观察
  （不依赖固定 sleep），否则 reader 可能只看到最终 `false`

### TC5: 多类型混合（多 point 同实例）

- **配置**：3 point（同实例）：`windspeed`（FLOAT32 缺省）、`temperature`（INT32 `type:"float"`）、
  `status`（BOOLEAN 缺省）；占位 writer 配 3 个 point（shm_id=1/2/3）
- **操作**：分别写 3 个 block（type=10 value=12.5 / type=5 value=25 / type=0 value=1）→ 等待写入
- **预期**：三个 field 的 fieldType 分别为 `float`/`float`/`boolean`，值正确
- **说明**：验证同一实例内多类型 point 各自独立转换、独立编码

### TC6: tag 转义（中文 / 逗号 / 空格 / 等号）

- **配置**：1 point，`tags: {"site": "华能,阿拉善", "region": "I 区", "eq": "a=b"}`
- **操作**：写数据 → 等待写入 → `SELECT * FROM <measurement>`
- **预期**：查询结果的 tag 列中 `site`=`华能,阿拉善`、`region`=`I 区`、`eq`=`a=b`（特殊字符被正确转义并入库）
- **说明**：逗号 / 空格 / 等号须转义为 `\,` / `\ ` / `\=`，InfluxDB 解析后还原；中文无需转义

### TC7: 数据变化（值更新）

- **配置**：1 point，采集 FLOAT32
- **操作**：
  1. `write_shm_block(type=10, value=1.5, timestamp=T)` → 用 `wait_value` 等待查询到 `1.5`
  2. `write_shm_block(type=10, value=2.5, timestamp=T)`（**同一固定 timestamp T**，write_seq 递增）→ 用 `wait_value` 等待查询到 `2.5`
- **预期**：最终查询值 = `2.5`
- **说明**：两次写用**同一固定 timestamp**——同 measurement+tag+field+timestamp 会 UPSERT
  为单点，避免 `query_latest` 依赖「最新值 = 最大时间戳」的排序语义。`write_seq > last_seen`
  过滤保证 reader 只读新数据（写 `2.5` 时不重复写 `1.5`）

### TC8: 非数值类型跳过

- **配置**：占位 writer 配 2 point；influxdb 配 1 个正常 point（引用 shm_id=1）+ 1 个
  映射到 STRING 的 point（引用 shm_id=2，`measurement` 独立）
- **操作**：
  1. `write_shm_block(shm_id=1, type=10, value=3.5)` → 等待，确认 shm_id=1 的 field 入库
  2. `write_shm_block(shm_id=2, type=12, value=...)`（STRING，非数值）→ 等待 ≥1 轮询周期
- **预期**：shm_id=2 对应的 measurement **无数据**（`field_type` 返回 None 或 `SELECT` 无结果）
- **说明**：非数值类型（STRING/BLOB/BITSTRING/LARGE_DATA_BLOCK）block 静默跳过、不写入（§4.2 步骤 3）

### TC9: timestamp 精度（precision=ms 透传）

- **配置**：1 point，`precision: "ms"`（默认），采集 FLOAT32
- **操作**：`write_shm_block(type=10, value=4.5, timestamp=1768848814264)` → 等待写入 → 查询
- **预期**：查询结果 time 的**年份 == 2026**（约 `1768848814264` 毫秒对应的 UTC 时间），
  且 time 在 `2026-01-19T18:53:34Z` ±1 天内
- **说明**：`precision=ms` 让 InfluxDB 按毫秒解释时间戳。断言年份（而非仅「非 1970」）以
  区分 `precision=s` 等错误精度（会渲染为 ~58000 年，同样是「非 1970」）。若 SUT 漏带
  precision（默认纳秒），time 显示为 1970 年——本测试即失败（验证 §2.2 的 precision 透传）

### TC10: 多个 measurement（不同 point 不同表）

- **配置**：2 point，`measurement` 分别为 `wind_turbine` 和 `transformer`
- **操作**：分别写 2 个 block → 等待写入 → 分别查询两个 measurement
- **预期**：两个 measurement 各有正确 field 与值
- **说明**：验证 measurement（表名）按 point 配置正确映射，不同 point 落到不同表

### TC11: 整数缺省（auto）→ integer

- **配置**：1 point，`type` 缺省（auto），采集 INT32
- **操作**：`write_shm_block(type=5, value=25)` → 等待写入
- **预期**：`field_type(..., "windspeed") == "integer"`，值 = `25`
- **说明**：INT32 跟随采集类型（auto）→ 编码 `25i` → InfluxDB 存储为 integer。
  若 SUT 错误地把 auto 路径的整数类型编码为 float（裸数字 `25`），fieldType 会是 `float`，
  本测试即失败（与 TC2 的「显式 float」形成对照——同一 INT32 采集，`type` 缺省与
  `type:"float"` 产生不同入库类型）

---

## 5. 实现注意

### 5.1 复用 c4_fun_00067 的 fixture

`conftest.py` 通过 `importlib.util` 复用 `c4_fun_00067/conftest.py` 的 fixture：

```python
import importlib.util, os

_src = os.path.join(os.path.dirname(__file__), "../c4_fun_00067/conftest.py")
_spec = importlib.util.spec_from_file_location("c4_fun_00067_conftest", _src)
_c67 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_c67)

shm_mgr_client = _c67.shm_mgr_client
isolated_shm = _c67.isolated_shm
prepare_environment = _c67.prepare_environment
influxdb = _c67.influxdb
create_database = _c67.create_database
start_influxdb_client = _c67.start_influxdb_client
```

`shm_helpers.py` 同样通过 `importlib.util` 复用 `c4_fun_00067/shm_helpers.py`，获取
`write_shm_block`（写 helper）及 `read_shm_block` / `read_write_seq` 等读 helper——
方式与 conftest.py 复用一致，本套件**不复制实现、仅复用**。

### 5.2 写入等待策略（关键时序）

数据流验证需等待 ≥1 个 **SUT 轮询周期**（`timer=100ms`）+ ≥1 个 **flush 周期**
（`flush_interval=100ms`）。正向断言（查询到期望值 / 字段类型）用轮询重试
（间隔 50ms，最长 3s），不依赖固定 sleep：

```python
def wait_field_type(url, db, measurement, field, expected_type, timeout=3.0, interval=0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if field_type(url, db, measurement, field) == expected_type:
            return
        time.sleep(interval)
    raise RuntimeError(f"field {measurement}.{field} did not become {expected_type} within {timeout}s")

def wait_value(url, db, measurement, field, expected_value, timeout=3.0, interval=0.05):
    """轮询等待指定 field 的最新值达到期望值（用于观察中间态，如 TC4 的 true→false）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if query_latest(url, db, measurement, field) == expected_value:
            return
        time.sleep(interval)
    raise RuntimeError(f"field {measurement}.{field} did not become {expected_value} within {timeout}s")
```

### 5.3 value 写入的字节格式

`write_shm_block` 按采集类型把 value 写入 8 字节 value 字段的低位字节（本机序），高位补零
（c4_fun_00012 README §2.4 读取区间表）。本套件需支持的采集类型：

| type 枚举 | 名称 | 写入格式 | 有效字节 |
|-----------|------|---------|---------|
| 0 | BOOLEAN | `=B` | 1 |
| 5 | INT32 | `=i` | 4 |
| 10 | FLOAT32 | `=f` | 4 |
| 12 | STRING | （非数值，跳过测试用） | — |

> 浮点值（FLOAT32）断言用近似比较（`pytest.approx`），因 float 经 InfluxDB 存储后可能有精度损失。

### 5.4 测试边界（InfluxDB 1.x 环境无法覆盖的场景）

| 场景 | 说明 |
|------|------|
| `"uint"` 类型（`u` 后缀） | 1.x **不支持** unsigned——`25u` 返回 `400 invalid number`。设计文档的 `"uint"` 是 2.x 特性，**需 2.x 环境验证**，本套件不覆盖（若要覆盖，可验证「type:uint 时 SUT 写 25u 且 1.x 返回 400、SUT 判定不可重试丢弃」，但那测的是错误处理而非 uint 编码） |
| 「float 编码带小数点」的字面验证 | 1.x 裸数字也是 float，无法通过 fieldType 区分 `25` vs `25.0`。此规则是 2.x 兼容要求，1.x 下无害且不可观测 |
| gzip 压缩 | 测试配置 `gzip=0`（关闭）；`gzip=1` 的正确性在 1.x 下可独立验证（InfluxDB 支持 gzip 请求体），非必需 |
| batch_size 批量（5000 行） | 测试点数少，靠 `flush_interval` 触发 flush；`batch_size` 触发的批量路径不在此集成测试覆盖（需 5000 点，代价过高） |

### 5.5 隔离性

- 每个 TC 独立 `instance_id`、独立 database
- fixture teardown：关闭 SUT、DROP database、shm_unlink、删除临时配置

### 5.6 禁止事项

- **不得用 Python 实现 line protocol 编码**（仅通过查询结果做断言）
- **不得调用 `status` 工具**
- 验证手段仅限：`start`/`stop` 返回值、共享内存 mmap 读写、InfluxDB HTTP 查询结果

# C4_FUN_00013 测试方案

> **对应功能**：`c4/docs/specification/c4_function.md` C4_FUN_00013
> **对应需求**：C4_RS_00091
> **设计参考**：`c4/docs/design/c4_iec104_client.md` §4, §5

C4_FUN_00013：C4 通过 IEC 60870-5-104 协议（以太网）采集远动设备的数据。

---

## 1. 测试目标

验证 `c4_iec104_client` 的事件驱动数据采集通路（真实设备端 `iec104d` + Redis + `redis_tool`）：

1. 主站建立 TCP 连接 + STARTDT 激活 + 按 `gi_timer` 周期总召唤（GI）+ 按 `it_timer` 周期累计量召唤（IT）
2. 三类数据类型正确解析：遥信单点（M_SP_NA_1 → BOOLEAN）、遥测短浮点（M_ME_NC_1 → FLOAT32）、遥脉累计量（M_IT_NA_1 → INT32）
3. 带时标类型（with_cp56time2a=1）正确解析（值字段相同，仅时标不同）
4. 按 `(instance, ioa) → shm_id` 映射写入共享内存（`write_seq` 递增、value 正确、block.type 正确）
5. 数据变化后（redis_tool 改写值）主站更新共享内存
6. 多实例各自独立采集

---

## 2. 测试架构

```
c4/test/c4_fun_00013/
├── README.md              # 本文件
├── conftest.py            # 复用 c4_fun_00065 的 fixture
└── test_acquisition.py    # TC1~TC8
```

### 2.1 被测对象 (SUT)

Go 编译的 `c4_iec104_client` 二进制，通过 MCP stdio JSON-RPC 控制（同 c4_fun_00065）。

### 2.2 测试基础设施与数据流

复用 c4_fun_00065 的完整数据流（见其 README §2.2）：

```
redis_tool ──写值──> Redis ──MGET──> iec104d ──IEC104响应──> c4_iec104_client ──seqlock──> 共享内存
```

数据流验证：redis_tool 向 Redis 写入已知值 → iec104d 读入 → 主站总召（GI，上送 YX/YC）/
累计量召唤（IT，上送 YM）→ 主站解析 → 写入共享内存 → Python mmap 读取并比对。

### 2.3 fixture 复用

`conftest.py` 通过 `importlib.util` 复用 `c4_fun_00065/conftest.py` 的 fixture
（复用方式见 c4_fun_00066 README §5.1）。

### 2.4 共享内存验证

Python 通过 `mmap` 读取 `/dev/shm/{instance_id}`，Data Block 布局（32 字节）同 c4_fun_00012 §2.3。
**value 读取**（设计文档 §5.2：值以本机序写入 value 字段低位字节，高位补零）：

| type | 枚举值 | 有效字节 | struct 格式 | 读取区间 |
|------|--------|---------|-------------|---------|
| BOOLEAN | 0 | 1B | `=B` | `shm[24:25]` |
| INT32 | 5 | 4B | `=i` | `shm[24:28]` |
| FLOAT32 | 10 | 4B | `=f` | `shm[24:28]` |

**数据流验证**：记录 `write_seq` 为 `seq_before`，等待 ≥1 个采集周期（GI 或 IT）后重读，
断言 `write_seq > seq_before`、`value` 等于期望值、`block.type` 等于期望的 ASFP2 枚举值。

---

## 3. 测试配置模板

### 3.1 iec104d 配置（设备端）

三类数据点的 104addr 范围（见 c4_fun_00065 §2.4）：

| 类型 | Redis key | 104addr | 上送类型标识（with_cp56time2a=0） |
|------|----------|---------|----------------------------------|
| 遥信 YX | `TF_TEST_DI001` | 1 | M_SP_NA_1 (1) |
| 遥测 YC | `TF_TEST_AI001` | 16385 | M_ME_NC_1 (13) |
| 遥脉 YM | `TF_TEST_AI101` | 25601 | M_IT_NA_1 (15) |

```json
{
    "engine": {"pwd": "/tmp/c4_test/iec104d_tc1", "stop_check": 100},
    "log": {"dir": "log", "file": "log.log", "level": 1, "debug_time": 300, "size": 128},
    "iec104": {
        "ip": "127.0.0.1", "port": <port>,
        "k": 12, "w": 8,
        "t0": 30, "t1": 15, "t2": 10, "t3": 20,
        "modules": 32768,
        "common_address": 1,
        "with_cp56time2a": 0,
        "acquisition_of_events_timer": 100,
        "cyclic_data_transmission_timer": 0,
        "timer": 100
    },
    "redis": {
        "ip": "127.0.0.1", "port": 6379, "dbid": 0, "auth": "",
        "with_timestamp": 1, "precision": 6
    },
    "points": [
        {"key": "TF_TEST_DI001", "104addr": 1},
        {"key": "TF_TEST_AI001", "104addr": 16385},
        {"key": "TF_TEST_AI101", "104addr": 25601}
    ]
}
```

### 3.2 c4_iec104_client 配置（SUT）

```json
{
    "c4_shm_manager": {"writer": ["c4_iec104_client"], "reader": ["c4_asfp2_client"]},
    "c4_iec104_client": [{
        "name": "采集测试主变", "id": "acq_transformer",
        "ip": "127.0.0.1", "port": <port>,
        "k": 12, "w": 8,
        "t0": 5, "t1": 5, "t2": 3, "t3": 5,
        "modules": 32768,
        "common_address": 1,
        "ioa_size": 3,
        "discard_cp56time2a": 0,
        "ignore_qds": 0,
        "it_timer": 100,
        "gi_timer": 100,
        "points": [
            {"id": "di_1", "addr": 1, "shm_id": 0},
            {"id": "ai_1", "addr": 16385, "shm_id": 0},
            {"id": "ai_101", "addr": 25601, "shm_id": 0}
        ]
    }],
    "c4_asfp2_client": []
}
```

> 测试用 `gi_timer=100`（100ms）加速总召、`it_timer=100`（100ms）加速累计量召唤。
> 遥信（YX）+ 遥测（YC）经**总召 GI** 采集；遥脉（YM）经**累计量召唤 IT** 采集——iec104d
> 响应总召时仅上送 YX+YC（累计量是独立召唤命令，符合 IEC 60870-5-104 标准）。

### 3.3 期望的 shm_id 分配

3 个 point → `adjust_shm` 按配置顺序依次分配：
`di_1(addr=1)` → shm_id=1，`ai_1(addr=16385)` → shm_id=2，`ai_101(addr=25601)` → shm_id=3。

---

## 4. 测试用例

### TC1: 遥信单点（YX，M_SP_NA_1 → BOOLEAN）

- **配置**：1 point `addr: 1`（遥信），iec104d `with_cp56time2a: 0`
- **操作**：`redis_tool` 写 `TF_TEST_DI001 = 1` → 等待采集
- **预期**：shm block（shm_id=1）`write_seq` 递增，`block.type = 0`（BOOLEAN），
  `value = 1`（`=B` 读 `shm[24:25]`）
- **说明**：iec104d 上送 M_SP_NA_1（单点遥信），主站提取 `siq.spi` → BOOLEAN

### TC2: 遥测短浮点（YC，M_ME_NC_1 → FLOAT32）

- **配置**：1 point `addr: 16385`（遥测），iec104d `with_cp56time2a: 0`
- **操作**：`redis_tool` 写 `TF_TEST_AI001 = 1.5` → 等待采集
- **预期**：shm block `block.type = 10`（FLOAT32），`value = 1.5`（`=f` 读 `shm[24:28]`）
- **说明**：iec104d 上送 M_ME_NC_1（短浮点），主站提取 `value` → FLOAT32

### TC3: 遥脉累计量（YM，M_IT_NA_1 → INT32）

- **配置**：1 point `addr: 25601`（遥脉），iec104d `with_cp56time2a: 0`；c4 保留 `it_timer: 100`
  （可加 `gi_timer: 0` 隔离 IT 通道，避免 GI/IT 交替）
- **操作**：`redis_tool` 写 `TF_TEST_AI101 = 1000` → 等待采集
- **预期**：shm block `block.type = 5`（INT32），`value = 1000`（`=i` 读 `shm[24:28]`）
- **说明**：iec104d 经累计量召唤（IT）上送 M_IT_NA_1（累计量），主站提取 `bcr.counter_reading` → INT32。
  遥脉仅经 IT 通道采集（依赖 `it_timer>0`），不随总召 GI 上送

### TC4: 带时标类型（with_cp56time2a=1，M_SP_TB_1/M_ME_TF_1 → 对应类型）

- **配置**：2 point（`addr: 1` 遥信 + `addr: 16385` 遥测），iec104d `with_cp56time2a: 1`
- **操作**：`redis_tool` 写 `TF_TEST_DI001 = 1`、`TF_TEST_AI001 = 2.5` → 等待采集
- **预期**：
  - 遥信 block `type = 0`（BOOLEAN），`value = 1`
  - 遥测 block `type = 10`（FLOAT32），`value = 2.5`
- **说明**：带时标类型（M_SP_TB_1/M_ME_TF_1）的值字段与无时标类型（M_SP_NA_1/M_ME_NC_1）
  相同，仅多 7 字节 CP56Time2a 时标；主站提取值字段一致，映射到相同 block.type。
  `discard_cp56time2a=0` 时用设备时标、`=1` 时用本地接收时间，均不影响 value 提取。

### TC5: 多类型混合（遥信 + 遥测 + 遥脉同测）

- **配置**：3 point（§3.2），iec104d `with_cp56time2a: 0`
- **操作**：`redis_tool` 写 `TF_TEST_DI001 = 0`、`TF_TEST_AI001 = 3.75`、`TF_TEST_AI101 = 2000` → 等待采集
- **预期**：三个 block 各自 `value` 分别为 0、3.75、2000，`block.type` 分别为 0/10/5
- **说明**：验证主站在同一连接上正确处理多种类型标识的 ASDU（iec104d 分别经总召 GI 上送
  YX/YC、经累计量召唤 IT 上送 YM）

### TC6: 周期采集（总召 GI + 累计量召唤 IT）

- **配置**：3 point（§3.2），`gi_timer: 100, it_timer: 100`
- **操作**：
  1. `redis_tool` 写三个 key 的初始值 → 等待采集，记录三个 block 的 `write_seq`
  2. 等待 ≥1 个采集周期，确认 `write_seq` 持续递增
- **预期**：遥信/遥测 block（shm_id=1/2）随总召 GI 周期递增，遥脉 block（shm_id=3）随累计量召唤 IT 周期递增（数据持续刷新）
- **说明**：验证 §4.7——总召 GI（主站周期发 C_IC_NA_1，iec104d 响应 YX+YC）与
  累计量召唤 IT（主站周期发 C_CI_NA_1，iec104d 响应 YM）两条周期采集通道

### TC7: 数据变化（值更新）

- **配置**：1 point `addr: 16385`（遥测），iec104d `with_cp56time2a: 0`
- **操作**：
  1. `redis_tool` 写 `TF_TEST_AI001 = 1.5` → 等待采集，确认 `value = 1.5`
  2. `redis_tool` 改写 `TF_TEST_AI001 = 2.5` → 等待采集（≥1 个 iec104d 读入周期 + 总召周期）
  3. 读 `value`
- **预期**：最终 `value = 2.5`（新值覆盖旧值）
- **说明**：验证数据变化后主站更新共享内存（iec104d 读入新值 → 总召上送 → 主站覆盖写入）

### TC8: 多实例独立采集

- **配置**：1 个 iec104d（**含 `16385` + `16386` 两个遥测点**，如 `TF_TEST_AI001→16385`、
  `TF_TEST_AI002→16386`），c4 配置 2 个实例（`id` 分别为 `dev1`/`dev2`，均指向
  同一 iec104d，各 1 个 point，dev1 采 `addr: 16385`、dev2 采 `addr: 16386`）
- **操作**：`redis_tool` 写 `TF_TEST_AI001`、`TF_TEST_AI002` 两个 key 对应值 → 等待采集
- **预期**：两个实例各自写入自己的 shm block（不同 shm_id），`value` 正确
- **说明**：验证多 goroutine 各自独立连接、独立采集、独立写入

---

## 5. 实现注意

### 5.1 复用 c4_fun_00065 的 fixture

同 c4_fun_00066 README §5.1（`importlib.util` 复用 `c4_fun_00065/conftest.py`）。

### 5.2 采集等待策略（关键时序）

数据流验证需等待 ≥1 个 **iec104d 读入周期**（`timer=100ms`）+ ≥1 个 **主站采集周期**
（总召 `gi_timer=100ms` / 累计量召唤 `it_timer=100ms`）。正向断言（`write_seq` 递增 /
`value` 达到期望值）用轮询重试（间隔 50ms，最长 3s），不依赖固定 sleep：

> GI 与 IT 互斥（设计 §4.7）：`gi_timer=it_timer=100` 时二者交替，YM 实际刷新周期约 2×`it_timer`
> （~200ms），仍远小于 3s 轮询超时，不影响断言。

```python
def wait_shm_value(shm_path, shm_id, expected_value, data_type, timeout=3.0, interval=0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if read_shm_value(shm_path, shm_id, data_type) == expected_value:
            return
        time.sleep(interval)
    raise RuntimeError(f"shm value did not reach {expected_value} within {timeout}s")
```

> 浮点值（FLOAT32）断言用近似比较（`pytest.approx` 或 `abs(a-b) < 1e-6`）。

### 5.3 遥信值域

遥信（YX）的 Redis 值写 **0 或 1**（单点遥信 spi 只有 0/1 两个状态）；iec104d 将非零
double 值上送为 spi=1、零值上送为 spi=0。遥测（YC）写浮点值，遥脉（YM）写整数值
（double 经 `bcr.counter_reading` 隐式截断为 int32）。**遥脉仅经累计量召唤（IT，`it_timer>0`）
采集，不随总召 GI 上送**。

### 5.4 测试边界（无法通过 iec104d 覆盖的场景）

| 场景 | 说明 |
|------|------|
| `ignore_qds` 负向验证 | iec104d 上送的 QDS 品质恒为有效（IV=0），**无法通过 iec104d 制造 IV=1 的无效点**。`ignore_qds=1` 与 `ignore_qds=0` 在有效数据下行为一致（均写入），「IV=1 点被跳过」的负向断言需单元测试或专用模拟器覆盖，不在本集成测试范围 |
| `discard_cp56time2a` 时标精确值 | CP56Time2a 由 iec104d 从 Redis timestamp 编码，主站解码后与本地时间差异受时钟精度影响，**仅验证 value 正确、不断言 timestamp 精确值**（timestamp 字段的存在性/单调性可作弱断言） |
| 步位/双点/归一化/标度化类型 | iec104d 仅上送 YX（单点）/YC（短浮点）/YM（累计量）三类，**不上送** M_DP_NA_1/M_ST_NA_1/M_ME_NA_1/M_ME_NB_1。这些类型的编解码正确性需单元测试覆盖 |

### 5.5 隔离性

- 每个 TC 独立 `instance_id`、独立 iec104d 端口、独立 Redis key 前缀
- fixture teardown：关闭 SUT、关闭 iec104d、shm_unlink、删除临时配置、清理 Redis key

### 5.6 禁止事项

- **不得调用 `status` 工具**
- 验证手段仅限：`start` 返回值、`stop` 返回值、共享内存 mmap 读取

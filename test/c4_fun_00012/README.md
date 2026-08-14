# C4_FUN_00012 测试方案

> **对应功能**：`c4/docs/specification/c4_function.md` C4_FUN_00012
> **对应需求**：C4_RS_00090
> **设计参考**：`c4/docs/design/c4_modbus_client.md` §4, §5

C4_FUN_00012：C4 通过 Modbus/TCP 协议（以太网）采集工业设备的寄存器数据。

---

## 1. 测试目标

验证 `c4_modbus_client` 的轮询采集数据通路（真实设备端 `modbusd` + Redis + `redis_tool`）：

1. 4 个读功能码（0x01 Read Coils / 0x02 Read Discrete Inputs / 0x03 Read Holding Registers / 0x04 Read Input Registers）正确构造请求并解析响应
2. 各数据类型（BOOLEAN/BIT/INT16/UINT16/INT32/UINT32/FLOAT32）正确解码
3. 字节序规则（`hton_register` + `swap`）正确处理四种设备字节序（ABCD/BADC/CDAB/DCBA）
4. 解析后的值按 Seqlock 协议写入共享内存（`write_seq` 递增、value 正确）
5. 连接断开（modbusd 停止）后重连恢复
6. timer 周期轮询、每次写入覆盖前值

---

## 2. 测试架构

```
c4/test/c4_fun_00012/
├── README.md              # 本文件
├── conftest.py            # 公共 fixture（复用 c4_fun_00062 的 redis+modbusd+授权 fixture）
├── shm_helpers.py         # 共享内存读写工具函数（复用 c4_fun_00057）
└── test_acquisition.py    # TC1~TC13
```

### 2.1 被测对象 (SUT)

Go 编译的 `c4_modbus_client` 二进制，通过 MCP stdio JSON-RPC 控制（同 c4_fun_00062）。

### 2.2 复用设备端 fixture

复用 `c4_fun_00062/conftest.py` 的 `license_env`、`start_modbusd`、`write_redis` fixture：

```
redis_tool ──写值──> Redis ──MGET──> modbusd ──Modbus响应──> c4_modbus_client ──seqlock──> 共享内存
```

数据流验证：redis_tool 向 Redis 写入已知值 → modbusd 读入 DAM → c4_modbus_client
轮询读回 → 写入共享内存 → Python mmap 读取并比对。

### 2.3 共享内存验证

Python 通过 `mmap` 读取 `/dev/shm/c4_{instance_id}`，Data Block 布局（32 字节）：

| 字段 | 偏移 | 大小 | Python struct |
|------|------|------|---------------|
| magic | 0 | 4B | `>I` |
| state | 4 | 1B | `>B` |
| type | 7 | 1B | `>B` |
| write_seq | 8 | 8B | `>Q` |
| timestamp | 16 | 8B | `>Q` |
| value | 24 | 8B | 见 §2.4 |

**value 读取**（设计文档 §5.2：值以大端写入 `value` 字段低位字节，高位补零）：

| type | 枚举值 | 有效字节 | struct 格式 | 读取区间 |
|------|--------|---------|-------------|---------|
| BOOLEAN / BIT | 0 / 15 | 1B | `>B` | `shm[24:25]` |
| INT16 | 3 | 2B | `>h` | `shm[24:26]` |
| UINT16 | 4 | 2B | `>H` | `shm[24:26]` |
| INT32 | 5 | 4B | `>i` | `shm[24:28]` |
| UINT32 | 6 | 4B | `>I` | `shm[24:28]` |
| FLOAT32 | 10 | 4B | `>f` | `shm[24:28]` |

**数据流验证**：记录 `write_seq` 为 `seq_before`，等待 ≥1 个轮询周期后重读，
断言 `write_seq > seq_before` 且 `value` 等于期望值。

---

## 3. 测试配置模板

### 3.1 modbusd 配置（设备端，单点 FLOAT32）

```json
{
    "engine": {"pwd": "/tmp/c4_test/modbusd_acq", "stop_check": 100},
    "log": {"dir": "log", "file": "log.log", "level": 1, "debug_time": 300, "size": 128},
    "modbus": {"ip": "127.0.0.1", "port": <port>, "hton_register": 1, "hton_total": 0, "swap": 1, "timer": 100},
    "redis": {"ip": "127.0.0.1", "port": 6379, "dbid": 0, "auth": "", "with_timestamp": 1, "precision": 6},
    "points": [
        {"key": "MB_AI_001", "modbusaddr": 1000, "funcode": 2, "type": 10}
    ]
}
```

### 3.2 c4_modbus_client 配置（SUT）

```json
{
    "c4_shm_manager": {"writer": ["c4_modbus_client"], "reader": ["c4_asfp2_client"]},
    "c4_modbus_client": [{
        "name": "采集测试设备", "id": "acq_device",
        "ip": "127.0.0.1", "port": <port>,
        "t0": 5, "t1": 5, "retries": 3,
        "coils_quantity_max": 2000, "registers_quantity_max": 125,
        "hton_register": 1, "hton_total": 0, "timer": 100,
        "points": [
            {"id": "pt_a", "uid": 1, "addr": 1000, "fun": 3, "type": 10, "swap": 2, "shm_id": 0}
        ]
    }],
    "c4_asfp2_client": []
}
```

### 3.3 字节序映射（关键）

modbusd 与 c4_modbus_client 各有自己的字节序配置。**在 Intel/小端机**上，
modbusd 配置、线上字节序、c4 配置三者对应关系（FLOAT32/INT32/UINT32，`hton_total=0`）：

| modbusd `hton_register` | modbusd `swap` | 线上字节序（V=1.5f=0x3FC00000） | c4 `hton_register` | c4 `swap` |
|:---:|:---:|--------|:---:|:---:|
| 1 | 1 | ABCD（标准大端，高字在前）`3F C0 00 00` | 1 | 2 |
| 0 | 1 | BADC（寄存器内字节交换）`C0 3F 00 00` | 0 | 2 |
| 1 | 0 | CDAB（低字在前）`00 00 3F C0` | 1 | 0 |
| 0 | 0 | DCBA（完全反转）`00 00 C0 3F` | 0 | 0 |

> **关键规则**：modbusd 与 c4 的 `hton_register` 语义同向（modbusd 执行 `htons`，
> c4 执行 `ntohs`），故**必须相等**（同为 1 或同为 0）。单寄存器类型（INT16/UINT16）
> 仅受 `hton_register` 影响；多寄存器类型（INT32/UINT32/FLOAT32）另受 `swap` 影响
> （modbusd `swap` 为高低字顺序，c4 `swap` 为 `_swap_byte` 字镜像，上表已桥接）。

### 3.4 期望的 shm_id 分配

N 个 point → `adjust_shm` 依次分配 shm_id=1..N。

---

## 4. 测试用例

### TC1: fun=3 读保持寄存器 — 单寄存器 UINT16

- **配置**：1 point `fun:3, type:4(UINT16)`，c4 `hton_register:1`；modbusd `hton_register:1`（§3.3 单寄存器规则），`type:4`
- **操作**：redis_tool 写 `MB_PT_001 = 4660`（0x1234）
- **预期**：shm block（shm_id=1）`write_seq` 递增，`value = 0x1234`（`>H` 读 `shm[24:26]`）

### TC2: fun=3 读保持寄存器 — FLOAT32（ABCD 标准大端）

- **配置**：modbusd `hton_register:1, swap:1`（→ ABCD），c4 `hton_register:1, swap:2`，point `fun:3, type:10(FLOAT32)`
- **操作**：redis_tool 写 `MB_AI_001 = 1.5`
- **预期**：`value = 1.5`（`>f` 读 `shm[24:28]`）
- **说明**：验证 §3.3 映射 ABCD（标准大端）→ c4 `hton_register=1, swap=2` 解码正确

### TC3: 字节序 BADC（寄存器内字节交换）

- **配置**：modbusd `hton_register:0, swap:1`（→ BADC），c4 `hton_register:0, swap:2`，point `type:10(FLOAT32)`
- **操作**：redis_tool 写 `MB_AI_001 = 1.5`
- **预期**：`value = 1.5`
- **说明**：modbusd 不逐寄存器转网络序（BADC 寄存器内字节交换），c4 以 `hton_register=0` + `swap=2` 反转字序还原

### TC4: 字节序 CDAB（低字在前）

- **配置**：modbusd `hton_register:1, swap:0`（→ CDAB），c4 `hton_register:1, swap:0`，point `type:10(FLOAT32)`
- **操作**：redis_tool 写 `MB_AI_001 = 1.5`
- **预期**：`value = 1.5`

### TC5: 字节序 DCBA（完全反转）

- **配置**：modbusd `hton_register:0, swap:0`（→ DCBA），c4 `hton_register:0, swap:0`，point `type:10(FLOAT32)`
- **操作**：redis_tool 写 `MB_AI_001 = 1.5`
- **预期**：`value = 1.5`

### TC6: 多类型覆盖（INT32 / UINT32 / FLOAT32）

- **配置**：3 point，均 `fun:3`，modbusd `hton_register:1, swap:1`（ABCD），c4 `hton_register:1, swap:2`：
  - INT32（type=5）addr=1000~1001，redis 写 `100000`
  - UINT32（type=6）addr=1002~1003，redis 写 `305419896`（0x12345678）
  - FLOAT32（type=10）addr=1004~1005，redis 写 `2.5`
- **预期**：各 block `value` 分别为 100000、305419896、2.5
- **注意**：均为跨 2 寄存器（多寄存器）类型，按 §3.3 映射配置字节序；单寄存器类型
  （INT16/UINT16）见 TC1

### TC7: fun=1 读线圈 — BIT

- **配置**：1 point `fun:1, type:15(BIT)`；modbusd point `funcode:0, type:15`，addr=0
- **操作**：redis_tool 写 `MB_COIL_000 = 1`
- **预期**：`value = 1`（`>B` 读 `shm[24:25]`）
- **说明**：线圈位打包 LSB 优先，地址 0 位于首字节 bit0

### TC8: fun=2 读离散输入 — BOOLEAN

- **配置**：1 point `fun:2, type:0(BOOLEAN)`；modbusd point `funcode:1, type:0`，addr=0
- **操作**：redis_tool 写 `MB_DI_000 = 0`
- **预期**：`value = 0`

### TC9: fun=4 读输入寄存器 — UINT16

- **配置**：1 point `fun:4, type:4(UINT16)`，modbusd `hton_register:1`、c4 `hton_register:1`（单寄存器匹配）；modbusd point `funcode:3, type:4`，addr=500
- **操作**：redis_tool 写 `MB_IR_500 = 48879`（0xBEEF）
- **预期**：`value = 0xBEEF`

### TC10: 相邻 point 采集（连续区间，隐式覆盖批处理合并）

- **配置**：2 point 均 `fun:3, type:10(FLOAT32)`，addr=1000（span 2）、addr=1002（span 2），
  连续区间 [1000, 1003]；modbusd 对应 2 point（modbusaddr=1000, 1002）
- **操作**：redis_tool 写 `MB_AI_1000 = 1.5`、`MB_AI_1002 = 2.5`
- **预期**：两个 block `value` 分别为 1.5、2.5
- **说明**：相邻 point 触发批处理合并路径（§4.5），数据仍正确读回

### TC11: 不相邻 point 采集（拆分路径）

- **配置**：2 point `fun:3, type:4(UINT16)`，addr=1000、addr=2000（不相邻）
- **操作**：redis_tool 写两个 key 对应值
- **预期**：两个 block `value` 正确
- **说明**：不相邻 point 拆分为多个批次（§4.5），数据仍正确

### TC12: modbusd 停止后重启 — 连接断开重连恢复

- **配置**：1 point `fun:3`，`t1=2, retries=2`
- **操作**：
  1. 等待 ≥1 轮询周期，确认 `write_seq` 递增
  2. 停止 modbusd（SIGINT）→ 等待，确认 `write_seq` 停止递增（连接断开）
  3. 重启 modbusd（同配置、同端口）
  4. 等待 ≥1 轮询周期
- **预期**：重启后 `write_seq` 恢复递增，`value` 正确
- **说明**：验证 §4.4 连接断开 → 重连成功 → 恢复轮询

### TC13: timer 周期轮询 — 多次写入覆盖

- **配置**：1 point `fun:3, type:4(UINT16)`，modbusd `hton_register:1`、c4 `hton_register:1`（单寄存器匹配），`timer=100`
- **操作**：
  1. 记录 `write_seq` 为 `seq_1`，调用 `wait_write_seq_advanced(seq_before=seq_1)` 等待下一周期写入，记录递增后的 `seq_2`
  2. redis_tool 改写 key 值为新值，调用 `wait_write_seq_advanced(seq_before=seq_2)` 等待再下一周期写入，记录递增后的 `seq_3` 并读取 `value`
- **预期**：`seq_1 < seq_2 < seq_3`（每周期写入一次），最终 `value` = 新值
- **说明**：验证 §5.3 按 timer 周期轮询，每次写入覆盖前值

---

## 5. 实现注意

### 5.1 轮询等待策略

数据流验证需等待 ≥1 个轮询周期（timer=100ms）+ modbusd DAM 刷新周期（modbus.timer=100ms）。
**正向断言**（`write_seq` 递增）用**轮询重试**（间隔 50ms，最长 3s），不依赖固定 sleep；
**负向断言**（`write_seq` 不变，见 c4_fun_00063 §5.2）需有界等待：

```python
def wait_write_seq_advanced(shm_path, shm_id, seq_before, timeout=3.0, interval=0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if read_write_seq(shm_path, shm_id) > seq_before:
            return
        time.sleep(interval)
    raise RuntimeError(f"write_seq did not advance within {timeout}s")
```

### 5.2 redis_tool 写值与 modbusd 刷新时序

- redis_tool 写值后，modbusd 需 **modbus.timer 周期（100ms）** 后才将新值刷入 DAM。
  修改 Redis 值后（TC13）应等待 ≥1 个 modbus.timer 周期再断言
- redis_tool 写值命令不带 `-n`（写二进制结构体，与 `with_timestamp=1` 匹配）

### 5.3 字节序断言

- shm 中 value 为大端（§2.3 表），`struct.unpack` 用 `>` 前缀
- 字节序用例（TC2~TC5）断言 `value == 1.5`（近似比较，FLOAT32 精度内）
- modbusd 的 `hton_total` 在 FLOAT32 类型下不生效（仅 INT32/UINT32 生效），测试统一设 0

### 5.4 类型支持边界

modbusd 2.3.0 仅实现 DAM 写入：BOOLEAN/BIT、INT8/UINT8、INT16/UINT16、INT32/UINT32、
FLOAT16/FLOAT32。**INT64/UINT64/FLOAT64 未实现**，无法通过 modbusd 测试——这些类型的
编解码正确性需 c4_modbus_client 单元测试覆盖，不在本集成测试范围。

### 5.5 批处理验证边界

批处理（§4.5）是内部优化，modbusd 为黑盒且对任何合法请求都正确响应，**无法在集成层
直接观察批次的起始地址/数量**。TC10/TC11 仅验证"相邻/不相邻 point 数据正确读回"
（隐式覆盖合并/拆分路径的正确性），批次数量的精确断言留待单元测试。

### 5.6 隔离性

- 每个 TC 独立 `instance_id`、独立 modbusd 端口、独立 Redis key 前缀
- fixture teardown 清理：关闭 SUT、关闭 modbusd、shm_unlink、删除临时配置、清理 Redis key

### 5.7 禁止事项

- **不得调用 `status` 工具**
- 验证手段仅限：`start`/`stop` 返回值、共享内存 mmap 读取

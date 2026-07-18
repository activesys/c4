# C4_FUN_00042 测试方案

> **对应功能**：`docs/specification/c4_function.md` C4_FUN_00042
> **对应需求**：C4_RS_00095
> **设计参考**：`docs/design/c4_asfp2_server.md`，`docs/specification/asfp2_specification.md`

C4_FUN_00042：C4 接收 ASFP2 数据 — 作为服务端监听连接，接收 ASFP2 数据包，解析后写入共享内存。

---

## 1. 测试目标

验证 `c4_asfp2_server` 在 `start` 成功后：
1. 监听配置端口，接受 TCP 连接
2. 正确解析 ASFP2 数据包，按 `addr → shm_id` 映射写入共享内存
3. 过滤变长类型（STRING / BLOB / BITSTRING / LARGE_DATA_BLOCK）
4. 正确处理多版本（2.0.0 / 2.1.0 / 2.1.1）
5. 正确处理 BIT 压缩模式

---

## 2. 测试架构

```
c4/test/c4_fun_00042/
├── README.md              # 本文件
├── conftest.py            # 复用 c4_fun_00057 fixtures + asfp2_client 工具
├── shm_helpers.py         # 共享内存读写工具函数（同 c4_fun_00057）
└── test_receive.py        # TC1~TC15
```

### 2.1 被测对象 (SUT)

Go 编译的 `c4_asfp2_server` 二进制，通过 MCP stdio JSON-RPC 协议控制。
SUT 启动流程：`prepare_environment` → `start_asfp2_server` → `start` → 端口监听。

### 2.2 测试方法

使用 `/usr/local/bin/asfp2_client` 作为 ASFP2 数据发生器。Python 测试代码通过 `subprocess.run`
执行 `asfp2_client`，连接到 SUT 监听端口并发送数据，退出后通过 `mmap` 读取共享内存验证。

**交互时序**：

```
1. prepare_environment（创建 shm + 配置）+ start_asfp2_server + start
2. Python subprocess.run(["asfp2_client", ...])  → 连接 SUT 端口 → 发送数据 → 退出
3. Python mmap 读取 shm → 验证 Data Block 内容
```

### 2.3 asfp2_client 关键参数

| 参数 | 说明 |
|------|------|
| `-s 127.0.0.1 -p PORT` | 服务器地址和端口 |
| `-b KEY -e KEY` | key 范围（begin < end，最小 2 个 key） |
| `-B VAL -E VAL` | 数据值范围（begin < end） |
| `-t N` | 发送轮数（0=无限），测试用 `-t 1` |
| `-z N` | 每包数据项数（0=最大），测试用 `-z 1` |
| `--type N` | 数据类型（0=BOOLEAN, 4=UINT16, 6=UINT32, 10=FLOAT32, 12=STRING, 15=BIT 等） |
| `--ts-start MS` | 合成时间戳（毫秒） |
| `--nks` | 禁用 KEY_SEQUENCE |
| `--nsdt` | 禁用 SAME_DATA_TYPE |
| `--nstp` | 禁用 SAME_TIMESTAMP |
| `-P 7` | 扩展格式（v2.1.0，Length 4B） |
| `-P 8` | 扩展格式（v2.1.1，FLOAT 网络序，Length 4B） |
| `--i0 10 --i1 10` | 发包间隔（ms） |

**默认行为**：KEY_SEQUENCE、SAME_DATA_TYPE、SAME_TIMESTAMP 均开启，
数据值从 `-B` 到 `-E` 按 key 顺序分配。

### 2.4 共享内存验证

Python 通过 `shm_helpers.read_shm_block(shm_path, shm_id)` 读取 Data Block，
验证 `state`、`type`、`timestamp`、`value` 字段。

- `value` 为 8 字节大端存储，按原类型解析：
  - UINT16 → `struct.unpack(">H", value[6:8])`
  - UINT32 → `struct.unpack(">I", value[4:8])`
   - FLOAT32 → `struct.unpack("f", value[4:8])`（`-P 0`/`-P 7` 为本机序，`-P 8`（v2.1.1）用 `>f`）
  - BOOLEAN/BIT → `data_raw[31] & 1`（读 raw 字节最低位，非 unpacked int）
- `type` 对应 ASFP2_TYPE_* 枚举值
- `timestamp` 为 Unix 毫秒时间戳（大端 8 字节）
- `state` 首次写入后应变为 `1`

---

## 3. 测试配置模板

复用 C4_FUN_00057 §3.1 配置模板，points 中 `addr` 需与 asfp2_client 发送的 key 范围匹配。

### 3.1 标准配置（2 key）

```json
{
    "c4_shm_manager": {"writer": ["c4_asfp2_server"], "reader": ["c4_asfp2_client"]},
    "c4_asfp2_server": [{
        "name": "test", "id": "test_receiver", "port": 9000,
        "t1": 0, "t2": 0, "forward_kack": 255, "inverse_keep": 0,
        "points": [
            {"id": "p1", "addr": 1000, "shm_id": 0},
            {"id": "p2", "addr": 1001, "shm_id": 0}
        ]
    }],
    "c4_asfp2_client": []
}
```

### 3.2 多 key 配置（5 key，用于 BIT 压缩和 key_range 测试）

```json
{
    "c4_shm_manager": {"writer": ["c4_asfp2_server"], "reader": ["c4_asfp2_client"]},
    "c4_asfp2_server": [{
        "name": "test", "id": "test_receiver", "port": 9000,
        "t1": 0, "t2": 0, "forward_kack": 255, "inverse_keep": 0,
        "points": [
            {"id": "p0", "addr": 1000, "shm_id": 0},
            {"id": "p1", "addr": 1001, "shm_id": 0},
            {"id": "p2", "addr": 1002, "shm_id": 0},
            {"id": "p3", "addr": 1003, "shm_id": 0},
            {"id": "p4", "addr": 1004, "shm_id": 0}
        ]
    }],
    "c4_asfp2_client": []
}
```

### 3.3 期望的 shm_id 分配

`adjust_shm` 计算 `writer_points = N`，`max_points = 2N`。分配后 addr 按出现顺序递增：addr=1000 → shm_id=1，addr=1001 → shm_id=2，依此类推。

---

## 4. 测试用例

### TC1: 基本数据接收 — UINT16

- **前置**：标准配置（2 key：addr=1000→shm_id=1, addr=1001→shm_id=2）
- **操作**：执行
  ```
  asfp2_client -s 127.0.0.1 -p 9000 -t 1 -z 1 -b 1000 -e 1001
               -B 100 -E 200 --type 4 --ts-start 1000000
               --i0 10 --i1 10
  ```
- **预期**：
  - shm_id=1：`state=1, type=4, timestamp≈1000000`，value 在 [100, 200] 范围内
  - shm_id=2：`state=1, type=4`，value 在 [100, 200] 范围内
- **说明**：默认属性全部开启（KEY_SEQUENCE + SAME_DATA_TYPE + SAME_TIMESTAMP），
  验证接收端正确解析带有 Mutable 的 v2.0.0 数据包

### TC2: 无属性优化 — 逐项独立编码（`--nks --nsdt --nstp`）

- **前置**：标准配置
- **操作**：禁用全部属性
  ```
  asfp2_client -s 127.0.0.1 -p 9000 -t 1 -z 1 -b 1000 -e 1001
               -B 100 -E 200 --type 4 --ts-start 1000000
               --nks --nsdt --nstp --i0 10 --i1 10
  ```
- **预期**：shm_id=1、shm_id=2 的 `state=1, type=4`，数据正确写入
- **说明**：每个 data item 携带独立的 type + key + timestamp，Mutable 不出现

### TC3: key_range — 多 key 连续发送

- **前置**：多 key 配置（5 key：addr=1000~1004→shm_id=1~5）
- **操作**：
  ```
  asfp2_client -s 127.0.0.1 -p 9000 -t 1 -b 1000 -e 1004
               -B 100 -E 500 --type 4 --i0 10 --i1 10
  ```
- **预期**：shm_id=1~5 的 `state=1, type=4`，所有 block 均有数据写入
- **说明**：默认每包 `-z` 为 0（最大），多个 key 的 data item 在同一包中按 KEY_SEQUENCE 发送

### TC4: 变长类型过滤 — STRING 被丢弃

- **前置**：标准配置
- **操作**：发送 STRING 类型数据
  ```
  asfp2_client -s 127.0.0.1 -p 9000 -t 1 -z 1 -b 1000 -e 1001
               -B 100 -E 200 --type 12 --i0 10 --i1 10
  ```
- **预期**：shm_id=1、shm_id=2 的 `state=0`（无任何数据写入）
- **说明**：SAME_DATA_TYPE 有效且 type=STRING(12)，整个包被丢弃

### TC5: BOOLEAN 类型 — 隐式 BIT 压缩模式

- **前置**：多 key 配置（5 key）
- **操作**：
  ```
  asfp2_client -s 127.0.0.1 -p 9000 -t 1 -z 5 -b 1000 -e 1004
               --type 0 --i0 10 --i1 10
  ```
- **预期**：shm_id=1~5 的 `state=1, type=0`（BOOLEAN），数据已写入
- **说明**：默认属性全开启且 type=BOOLEAN(0)。
  若 asfp2_client 也设置了 SAME_STATUS，则触发 BIT 压缩模式（每 bit 一个值）；
  若未设置 SAME_STATUS，则为普通 BOOLEAN 模式（每 byte 一个值）。
  无论哪种模式，接收端都应正确解析。

### TC6: BIT 类型

- **前置**：多 key 配置（5 key）
- **操作**：
  ```
  asfp2_client -s 127.0.0.1 -p 9000 -t 1 -z 5 -b 1000 -e 1004
               --type 15 --i0 10 --i1 10
  ```
- **预期**：shm_id=1~5 的 `state=1, type=15`（BIT），数据已写入

### TC7: FLOAT32 类型

- **前置**：标准配置
- **操作**：
  ```
  asfp2_client -s 127.0.0.1 -p 9000 -t 1 -z 1 -b 1000 -e 1001
               -B 100 -E 200 --type 10 --i0 10 --i1 10
  ```
- **预期**：shm_id=1 的 `state=1, type=10`（FLOAT32），数据已写入
- **说明**：asfp2_client 默认使用 v2.0.0（本机序 FLOAT），接收端对 <2.1.1 版本不做字节 swap

### TC8: v2.1.0 扩展格式（`-P 7`）

- **前置**：标准配置
- **操作**：
  ```
  asfp2_client -s 127.0.0.1 -p 9000 -t 1 -z 1 -b 1000 -e 1001
               -B 100 -E 200 --type 4 -P 7 --i0 10 --i1 10
  ```
- **预期**：shm_id=1、shm_id=2 的 `state=1, type=4`，数据正确写入
- **说明**：`-P 7` 使用 ASFP2 2.1.0 扩展格式（Flag=`"ASFPV210"`，Length 4B），
  验证接收端能正确拆分 Length/Attribute

### TC9: LARGE_DATA_BLOCK 类型 — 被过滤

- **前置**：标准配置。使用 `tmp_path` fixture 创建测试文件（内容任意，如 `tmp_path / "test_blob.bin"`）
- **操作**：
  ```
  asfp2_client -s 127.0.0.1 -p 9000 -t 1 -z 1 -b 1000 -e 1001
               -F /tmp/test_blob.bin --i0 10 --i1 10
  ```
- **预期**：shm_id=1、shm_id=2 的 `state=0`（LARGE_DATA_BLOCK 类型被丢弃）
- **说明**：LARGE_DATA_BLOCK(type=16) 为变长类型，无法存入 8B value 字段

### TC10: 多连接并发

- **前置**：标准配置
- **操作**：使用 Python `concurrent.futures.ThreadPoolExecutor` 同时启动 3 个 asfp2_client 进程，
  分别连接同一端口，发送**不同 key** 的数据：
  ```
  # 连接 1: -b 1000 -e 1001, value range 100-200
  # 连接 2: -b 2000 -e 2001, value range 300-400
  # 连接 3: -b 3000 -e 3001, value range 500-600
  ```
- **前置**：配置需包含全部 6 个 key（addr=1000/1001, 2000/2001, 3000/3001 → shm_id=1~6）
- **预期**：所有 asfp2_client 进程正常退出（exit code 0），shm_id=1~6 的 `state=1`
- **数据隔离验证**：
  - shm_id=1~2 的 value 在 [100, 200] 范围内（连接 1）
  - shm_id=3~4 的 value 在 [300, 400] 范围内（连接 2）
  - shm_id=5~6 的 value 在 [500, 600] 范围内（连接 3）
- **说明**：使用非重叠 key 范围，可验证每个连接的数据独立写入正确的 shm_id

### TC11: BLOB/BITSTRING 变长类型过滤

- **前置**：标准配置
- **操作**：分别发送 BLOB 和 BITSTRING 类型数据
- **子场景**（`pytest.mark.parametrize`）：

  | 子场景 | 类型 | --type | 预期 |
  |--------|------|--------|------|
  | (a) BLOB | BLOB(13) | `--type 13` | shm_id=1、2 的 `state=0`（整个包被丢弃） |
  | (b) BITSTRING | BITSTRING(14) | `--type 14` | 同上 |

- **操作命令**：
  ```
  asfp2_client -s 127.0.0.1 -p PORT -t 1 -z 1 -b 1000 -e 1001
               -B 100 -E 200 --type <13|14> --i0 10 --i1 10
  ```
- **说明**：与 TC4（STRING）一起覆盖规格要求的全部 4 种变长类型过滤

### TC12: UINT64 — 8 字节整型边界

- **前置**：标准配置
- **操作**：
  ```
  asfp2_client -s 127.0.0.1 -p PORT -t 1 -z 1 -b 1000 -e 1001
               -B 100 -E 200 --type 8 --i0 10 --i1 10
  ```
- **预期**：shm_id=1 的 `state=1, type=8`（UINT64），value 在 [100, 200] 范围内
- **说明**：UINT64 占满 8 字节 value 字段，验证边界 packing 正确，无字节偏移错误

### TC13: v2.1.1 整数类型 — 基本解析

- **前置**：标准配置
- **操作**：
  ```
  asfp2_client -s 127.0.0.1 -p 10300 -t 1 -z 1 -b 1000 -e 1001
               -B 100 -E 200 --type 4 -P 8 --i0 10 --i1 10
  ```
- **预期**：shm_id=1、shm_id=2 的 `state=1, type=4`，数据正确写入
- **说明**：`-P 8` 使用 ASFP2 2.1.1 Flag（`ASFPV211`），验证接收端能正确识别
  v2.1.1 版本的 Header 并正常解析（整数类型与低版本无差异，始终网络序）

### TC14: v2.1.1 FLOAT32 网络序

- **前置**：标准配置
- **操作**：
  ```
  asfp2_client -s 127.0.0.1 -p 10400 -t 1 -z 1 -b 1000 -e 1001
               -B 100 -E 200 --type 10 -P 8 --i0 10 --i1 10
  ```
- **预期**：shm_id=1 的 `state=1, type=10`（FLOAT32），数据已写入
- **说明**：v2.1.1 的 FLOAT 类型使用**网络序（大端）**传输，与 v2.0.0/v2.1.0 的
  本机序不同。接收端应做 byte swap 转为本机序后再写入 shm，
  Python 侧用 `struct.unpack("f", ...)` 验证。
  对比 TC7（v2.0.0 本机序），两者在 shm 中的字节序一致，验证方式也一致。

### TC15: v2.1.0 FLOAT32

- **前置**：标准配置
- **操作**：
  ```
  asfp2_client -s 127.0.0.1 -p 10500 -t 1 -z 1 -b 1000 -e 1001
               -B 100 -E 200 --type 10 -P 7 --i0 10 --i1 10
  ```
- **预期**：shm_id=1 的 `state=1, type=10`（FLOAT32），数据已写入
- **说明**：v2.1.0 的 FLOAT 使用本机序（与 v2.0.0 相同），但 Header 为 4B Length。
  验证接收端正确将 v2.1.0 版本识别为 <2.1.1，
  不做 FLOAT 字节 swap，防止与 v2.1.1 网络序混淆。

---

## 5. 实现注意

### 5.1 复用 C4_FUN_00057 fixtures

- 通过 `sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../c4_fun_00057"))` 引用 `c4_fun_00057` 的 `conftest.py`
- 需要引入的 fixtures：`prepare_environment`、`start_asfp2_server`、`isolated_shm`
- 需要引入的工具：`shm_helpers.read_shm_block`、`shm_helpers.shm_path`、`shm_helpers.read_shm_header`
- **重要**：每个 TC 在调用 `start_asfp2_server.call_tool("start", ...)` 时，必须传入 `on_request=_roots_callback([{"uri": f"file://{config_path}"}])` 以响应 SUT 的 `roots/list` 请求。`_roots_callback` 定义参考 c4_fun_00057/conftest.py 中的同名函数

### 5.2 asfp2_client 进程管理

- `subprocess.run([...], capture_output=True, timeout=10)` 执行 asfp2_client
- 检查 `returncode == 0` 确认数据发送成功
- 发送失败（连接被拒等）应标记为测试失败，而非通过验证

### 5.3 等待策略

- asfp2_client 退出后，需短暂等待（`time.sleep(0.5)`）确保 SUT 已完成共享内存写入
- 写入频率由发包频率决定，单轮 `-t 1` 在 100ms 内完成

### 5.4 端口管理

- 每个测试用例使用独立的 port（TC1: 9000, TC2: 9100, …），
  或每个测试用例自行启动独立的 SUT 实例（function-scope fixtures 保证隔离）

### 5.5 共享内存清理

- `isolated_shm` fixture 确保每个测试用例的 shm 不冲突
- 测试结束后 `shm_unlink` 清理

### 5.6 值验证策略

asfp2_client 的值分配由 `-B` 和 `-E` 定义的数据范围决定，按 key 顺序线性映射。
对于验证，优先确认：
- `state == 1`（数据已写入）
- `type` 正确
- `timestamp > 0`（合理时间戳）
- `value` 在 `[-B, -E]` 范围内（宽松验证）

如需要精确值验证，将 `-B` 和 `-E` 设为相同值。

### 5.7 已知限制

- asfp2_client 支持 v2.0.0（`-P 0`，默认）、v2.1.0（`-P 7`）、v2.1.1（`-P 8`）三种协议版本。
  TC7（`-P 0`）、TC15（`-P 7`）、TC14（`-P 8`）分别覆盖了三种版本的 FLOAT32 解码路径。
  FLOAT 在所有版本下最终以本机序写入 shm，Python 验证统一使用 `struct.unpack("f", ...)`。
- asfp2_client 的 SAME_STATUS 属性行为未文档化，BIT 压缩模式的触发条件不能完全控制。
  如 TC5 未触发压缩模式，仅验证普通 BOOLEAN 解码路径。

# C4_FUN_00053 测试方案

> **对应功能**：`docs/specification/c4_function.md` C4_FUN_00053
> **对应需求**：C4_RS_00096
> **设计参考**：`docs/design/c4_architecture.md` §2.2, §3.3

C4_FUN_00053 有两条分支：

| 分支 | 行为 | 测试文件 |
|------|------|---------|
| **配置文件不存在或为 null** | 创建默认 10 万点 | `test_no_config.py` (TC1~TC8) |
| **配置文件存在** | 按 config 点数 ×2 + shm_id 分配 | `test_with_config.py` (TC9~TC23) |

---

## 1. 测试目标

验证 c4_shm_manager 在配置文件不存在或为 null 时，正确创建默认 10 万点 POSIX 共享内存空间。

### 触发默认模式的三种场景

| 场景 | roots/list 返回 | 文件状态 |
|------|-----------------|---------|
| 无配置文件注册 | `[]` | — |
| 路径指向的文件不存在 | `["file:///tmp/no_such.json"]` | 文件不存在 (ENOENT) |
| 文件内容为空 JSON | `["file:///tmp/empty.json"]` | 文件内容 `{}` |

### 默认模式下的期望值

| 项目 | 期望值 | 来源 |
|------|--------|------|
| shm 路径 | `/dev/shm/c4_{instance_id}` | §2.5.1 |
| shm 文件大小 | `(100000 + 1) × 32 = 3,200,032` 字节 | §2.2 |
| 返回结果 | `"success"` | §3.3.2 |

**Header 字段（32 字节，偏移 0x00）**：

| 字段 | 偏移 | 大小 | 期望值 | 字节序 |
|------|------|------|--------|--------|
| magic | 0 | 4B | `0xC4DA7A00` | 大端 |
| version | 4 | 2B | `1` | 大端 |
| remap_version | 6 | 2B | `0` | — |
| point_count | 8 | 4B | `0` | — |
| max_points | 12 | 4B | `100000` | 大端 |
| global_write_seq | 16 | 8B | `0` | — |
| reserved | 24 | 8B | `0` | — |

> `remap_version`、`point_count`、`global_write_seq`、`reserved` 为 ftruncate 零填充后的默认值 0（§2.2.1 初始化规则）。

**Data Block 字段（每个 32 字节，偏移 = shm_id × 32）**：

| 字段 | 偏移 | 大小 | 期望值 |
|------|------|------|--------|
| magic | 0 | 4B | `0xC4DA7A00` |
| state | 4 | 1B | `0` |
| reserved | 5 | 2B | `0` |
| type | 7 | 1B | `0` |
| write_seq | 8 | 8B | `0` |
| timestamp | 16 | 8B | `0` |
| value | 24 | 8B | `0` |

---

## 2. 测试架构

```
c4/test/c4_fun_00053/
├── README.md              # 本文件
├── conftest.py            # 公共 fixture（MCP 客户端 + 隔离清理）
├── shm_helpers.py         # shm 读写工具函数
├── test_no_config.py      # 分支 1: 配置文件不存在或为 null (TC1~TC8)
└── test_with_config.py    # 分支 2: 配置文件存在 (TC9~TC23)
```

### 2.1 被测对象 (SUT)

Go 编译的 `c4_shm_manager` 二进制，通过 Python `subprocess.Popen` 启动，
走 **MCP stdio JSON-RPC** 协议：

- Python 向 SUT 的 stdin 写入 JSON-RPC 请求
- Python 从 SUT 的 stdout 读取 JSON-RPC 响应
- stderr 保留给 SUT 日志输出

### 2.2 MCP 交互序列

```
1. initialize (握手，声明 roots 能力)
2. tools/list → 获取工具列表，确认 create_shm 存在
3. tools/call create_shm({instance_id}) → SUT 发起 roots/list
4. Python 根据测试场景返回对应的 roots/list 应答
5. SUT 执行创建 → 返回 create_shm 结果
```

### 2.3 共享内存验证

Python 通过 `os.open("/dev/shm/c4_{id}", os.O_RDONLY)` + `mmap` 直接读取共享内存，
用 `struct.unpack` 按偏移解析各字段。

---

## 3. 分支 1: 配置文件不存在或为 null

**测试文件**：`test_no_config.py`（TC1~TC8）

### TC1: roots/list 返回空 → 创建 100k 默认 shm

- **前置**：无配置文件注册
- **操作**：调用 `create_shm({instance_id: "test_tc1"})`，roots/list 返回 `{"roots": []}`
- **预期**：返回 `"success"`
- **清理**：`shm_unlink("/c4_test_tc1")`

### TC2: roots/list 返回路径，文件不存在 → 创建 100k 默认 shm

- **前置**：确保 `/tmp/c4_no_such_config.json` 不存在
- **操作**：调用 `create_shm({instance_id: "test_tc2"})`，roots/list 返回 `{"roots": [{"uri": "file:///tmp/c4_no_such_config.json"}]}`
- **预期**：返回 `"success"`
- **清理**：`shm_unlink("/c4_test_tc2")`

### TC3: roots/list 返回路径，文件内容为空 JSON → 创建 100k 默认 shm

- **前置**：创建 `/tmp/c4_empty_config.json`，内容 `{}`
- **操作**：调用 `create_shm({instance_id: "test_tc3"})`，roots/list 返回 `{"roots": [{"uri": "file:///tmp/c4_empty_config.json"}]}`
- **预期**：返回 `"success"`
- **额外验证**：`/tmp/c4_empty_config.json` 未被修改（内容仍为 `{}`）
- **清理**：`shm_unlink("/c4_test_tc3")`，删除 `/tmp/c4_empty_config.json`

### TC4: 重复创建 → SHM_ALREADY_EXISTS

- **前置**：TC1 或 TC2 或 TC3 已成功创建 shm
- **操作**：对同一 `instance_id` 再次调用 `create_shm`
- **预期**：返回 `isError: true`，错误码 `SHM_ALREADY_EXISTS`
- **清理**：`shm_unlink`

### TC5: Header 全字段校验

- **前置**：TC1 成功后
- **操作**：打开 `/dev/shm/c4_test_tc1`，读取前 32 字节
- **预期**：各字段值与 §1 中 Header 表一致
- **额外验证**：`os.fstat` 文件大小 = `3,200,032` 字节

### TC6: Data Block 初始化校验

- **前置**：TC1 成功后
- **操作**：读取 shm_id=1（偏移 32）、shm_id=50000（偏移 1,600,000）、shm_id=100000（偏移 3,200,000）各 32 字节
- **预期**：三个块均满足 §1 中 Data Block 表
- **说明**：首尾和中间抽检，覆盖完整范围

### TC7: 创建后通过 query_status 读取 shm 一致性

- **前置**：TC1 成功后
- **操作**：通过 MCP 调用 `query_status`
- **预期**：返回 `{"magic": "valid", "version": 1, "remap_version": 0, "point_count": 0, "max_points": 100000, "free_blocks": 100000, "global_write_seq": 0}`
- **注意**：`query_status` 的 MCP 响应为嵌套 JSON——`result.content[0].text` 是 JSON 字符串，需二次 `json.loads` 解析

### TC8: roots/list MCP 调用失败 → 返回 CONFIG_PATH_MISSING

- **前置**：无
- **操作**：调用 `create_shm({instance_id: "test_tc8"})`，Python 对 roots/list 请求响应 `{"jsonrpc": "2.0", "id": "...", "error": {"code": -32601, "message": "Method not found"}}`
- **预期**：`create_shm` 返回 `isError: true`，`content[0].text` 以 `CONFIG_PATH_MISSING:` 开头
- **清理**：`shm_unlink("/c4_test_tc8")`（预防性）

> **测试顺序依赖**：TC4 依赖 TC1~TC3 之一已成功创建 shm。TC5、TC6、TC7 依赖 TC1。
> 若 TC1 失败，TC4~TC7 均会被跳过。建议为 TC4~TC7 各自独立创建/销毁 shm，
> 或使用 `pytest-dependency` 显式标记依赖关系。

---

## 4. 实现注意

这些注意事项适用于所有测试用例。

### 4.1 MCP JSON-RPC 协议细节

- 每行一个 JSON 对象（行分隔），不是流式 JSON
- `initialize` 握手：Python 发送 `initialize` 请求 → 读取 SUT 的 `initialize` 响应（含 `serverInfo` 和 capabilities）→ 发送 `initialized` 通知
- initialize 请求需包含 `capabilities.roots.listChanged: true`
- `create_shm` 调用期间，SUT 通过 **stdout** 发送 `roots/list` 请求，Python 须从 SUT 的 stdout 读取该请求，并向 SUT 的 **stdin** 写入对应的 `roots/list` 应答
- `query_status` 返回的 MCP 响应中，`result.content[0].text` 是 JSON 字符串，需二次 `json.loads` 解析

### 4.2 共享内存清理

- 测试结束后必须 `shm_unlink`（Python: `os.unlink("/dev/shm/c4_{id}")` 或通过 ctypes 调用 `shm_unlink`）
- 建议在 `conftest.py` 的 fixture **setup** 中先尝试 `shm_unlink` 预防性清理（防止前一次运行崩溃残留 shm 导致 `O_EXCL` 失败）
- fixture **teardown** 中再次清理，确保无论测试成败都释放共享内存

### 4.3 字节序

- `magic`、`version`、`max_points` 及所有多字节字段存储为大端（网络字节序）
- Python `struct.unpack` 使用 `>` 前缀，各字段格式速查：

| 字段 | Python struct | 说明 |
|------|--------------|------|
| magic（4B） | `>I` | unsigned int |
| version（2B） | `>H` | unsigned short |
| remap_version（2B） | `>H` | unsigned short |
| point_count（4B） | `>I` | unsigned int |
| max_points（4B） | `>I` | unsigned int |
| global_write_seq（8B） | `>Q` | unsigned long long |
| write_seq（8B） | `>Q` | unsigned long long |
| timestamp（8B） | `>Q` | unsigned long long |
| value（8B） | `>Q` | unsigned long long |
| state（1B） | `>B` | unsigned char |
| type（1B） | `>B` | unsigned char |

> 值为 0 的字段同样按大端存储（`\x00` 填充位相同，不影响校验），统一使用上表格式即可。

### 4.4 SUT 编译

- 测试前需编译 `c4/mcp/shm_manager` 为二进制
- 建议 conftest.py 自动检测二进制是否存在，不存在则 `go build`

---

## 5. 分支 2: 配置文件存在

**测试文件**：`test_with_config.py`（TC9~TC23）

### 算法概述（§3.3.1）

```
1. 解析 config → 读取 writer[] / reader[] → 任一为空则报错
2. Σ 各 Writer 的 points 数量 → writer_points
3. max_points = writer_points × 2（2 倍余量供后续扩容）
4. shm_open → ftruncate → mmap → 初始化
5. 逐 point 分配 shm_id（从 1 递增），key = "{service_id}.{point_id}"
   → 重复 key → DUPLICATE_KEY 报错
6. 回填 Reader：按 key 匹配 Writer 的 shm_id
   → 未知 key → UNKNOWN_READER_KEY 报错
7. 配置文件写回磁盘（含已分配的 shm_id）
```

### 测试配置模板

```json
{
    "c4_shm_manager": {"writer": ["c4_modbus_client"], "reader": ["c4_asfp2_client"]},
    "c4_modbus_client": [{
        "id": "device1",
        "points": [
            {"id": "temp",  "uid": 1, "addr": 1000, "shm_id": 0},
            {"id": "press", "uid": 1, "addr": 1002, "shm_id": 0}
        ]
    }],
    "c4_asfp2_client": [{
        "points": [
            {"key": "device1.temp",  "addr": 100, "shm_id": 0},
            {"key": "device1.press", "addr": 101, "shm_id": 0}
        ]
    }]
}
```

### TC9: 单 Writer 2x 分配

- **配置**：1 个 modbus_client，2 个 point
- **预期**：
  - 返回 `"success"`
  - `max_points = 2 × 2 = 4`
  - `point_count = 0`（Writer 尚未激活，所有 block state=0）
  - shm 文件大小 = `(4 + 1) × 32 = 160` 字节
  - Header 校验：`point_count=0, max_points=4`
  - Block[1] (device1.temp)：`magic=0xC4DA7A00, state=0`
  - Block[2] (device1.press)：`magic=0xC4DA7A00, state=0`
  - Block[3~4]：空闲（`magic=0xC4DA7A00, state=0`）

### TC10: 多 Writer 聚合

- **配置**：modbus 2 点 + iec104 3 点
- **预期**：
  - `writer_points = 2 + 3 = 5`
  - `max_points = 5 × 2 = 10`
  - `point_count = 0`（Writer 尚未激活）
  - shm 文件大小 = `(10 + 1) × 32 = 352` 字节

### TC11: Reader 回填

- **配置**：modbus 2 点（temp/press）+ asfp2 2 点引用同 key
- **验证**：创建后读取回填的配置文件
  - `asfp2_client[0].points[0].shm_id` = `modbus_client[0].points[0].shm_id`（均为 1）
  - `asfp2_client[0].points[1].shm_id` = `modbus_client[0].points[1].shm_id`（均为 2）
- **说明**：同一 key 可出现在多个 Reader 中，实现一对多数据流

### TC12: writer 为空 → CONFIG_MISSING_SECTION

- **配置**：`"writer": []`
- **预期**：`isError: true`，错误码 `CONFIG_MISSING_SECTION`

### TC13: reader 为空 → CONFIG_MISSING_SECTION

- **配置**：`"reader": []`
- **预期**：`isError: true`，错误码 `CONFIG_MISSING_SECTION`

### TC14: 重复 key → DUPLICATE_KEY

- **配置**：同一 service 类型下两个实例使用相同的 `id: "device1"`，且 point.id 均为 `"temp"`，导致 key `"device1.temp"` 重复
- **预期**：`isError: true`，错误码 `DUPLICATE_KEY`

### TC15: Reader key 不存在 → UNKNOWN_READER_KEY

- **配置**：Writer 有 `device1.temp`，Reader 引用 `device1.unknown`
- **预期**：`isError: true`，错误码 `UNKNOWN_READER_KEY`

### TC16: 配置文件回填

- **配置**：同 TC9（modbus 2 点 + asfp2 2 引用）
- **操作**：`create_shm` → 创建后重新读取配置文件
- **验证**：
  - 所有 `shm_id: 0` 已变为具体值
  - Writer `device1.temp` → `shm_id: 1`，`device1.press` → `shm_id: 2`
  - Reader `device1.temp` → `shm_id: 1`，`device1.press` → `shm_id: 2`
  - 其他字段（uid, addr, key 等）未被修改

### TC17: query_status 交叉验证

- **前置**：TC9 成功后
- **操作**：调用 `query_status`
- **预期**：`point_count=0, max_points=4, free_blocks=4`

### TC18: config JSON 格式错误 → 报错

- **配置**：配置文件内容为 `{invalid json`
- **预期**：`isError: true`，错误码 `CONFIG_PARSE_ERROR` 或 `CONFIG_MISSING_SECTION`

### TC19: 缺少 c4_shm_manager 段 → CONFIG_MISSING_SECTION

- **配置**：JSON 仅有 `c4_modbus_client` 段，无 `c4_shm_manager` 顶层 key
- **预期**：`isError: true`，错误码 `CONFIG_MISSING_SECTION`

### TC20: 分支 2 重复创建 → SHM_ALREADY_EXISTS

- **配置**：同 TC9（modbus 2 点），第一次创建成功后再次调用 `create_shm`
- **预期**：第二次返回 `isError: true`，错误码 `SHM_ALREADY_EXISTS`

### TC21: writer 引用的 service 类型在 config 中无对应 section

- **配置**：`"writer": ["c4_unknown_service"]`，有 reader
- **预期**：`isError: true` 或贡献 0 points（待设计决策确认）

### TC22: Reader backfill 保留额外字段

- **配置**：Reader point 含 `valid`、`coeff`、`base` 等非 shm_id 字段
- **预期**：创建后 config 写回，这些字段值未被修改

### TC23: 分支 2 Data Block 边界验证

- **前置**：TC9 成功后
- **操作**：读取 shm_id=1, 2, 4 的 Data Block
- **预期**：Block[1~2] `magic=0xC4DA7A00, state=0`，Block[4]（边界）同样

---

## 6. 实现注意（分支 2 补充）

### 6.1 配置文件 JSON 结构

`c4_shm_manager` 段的 `writer` 列表是关键——它指定了哪些顶层 key 是 Writer。
配置文件中未被 `writer`/`reader` 列出的顶层 key 应被忽略（不报错）。

### 6.2 临时配置文件

测试需动态生成配置文件写入 `/tmp`，`roots/list` 返回该路径。
创建 shm 后验证文件内容已被 `c4_shm_manager` 回填更新。

### 6.3 配置写入幂等

`create_shm` 应保留配置文件中未涉及的字段和结构不变，
仅回填 `points` 数组中 `shm_id` 字段的值。

### 6.4 point_count 语义

`point_count` 在 Header 中表示**已激活**（`state=1`）的 Data Block 数量（§2.2.1）。
创建共享内存后，所有 Block 的 `state` 均为 `0`（Writer 尚未写入），因此 `point_count = 0`。
Writer 首次写入时自行将 `state` 置 `1`，`point_count` 随之变化。

### 6.5 配置写回原子性

写入配置文件时应先写临时文件再 `rename`，防止进程崩溃导致配置文件损坏。
若 shm 创建成功但配置写回失败，应 `shm_unlink` 回滚已创建的共享内存。

### 6.6 c4_asfp2_server 的处理

`c4_asfp2_server` 作为 Writer 出现在 `writer` 列表中，但该服务无静态 points 列表
（接收端按反向映射动态分配）。解析时贡献 0 points，不应报错。

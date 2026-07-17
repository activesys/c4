# C4_FUN_00057 测试方案

> **对应功能**：`docs/specification/c4_function.md` C4_FUN_00057
> **对应需求**：C4_RS_00095
> **设计参考**：`docs/design/c4_asfp2_server.md`

C4_FUN_00057：Agent 生成 ASFP2 接收 MCP 服务的配置文件后，启动 MCP 服务，MCP 服务根据配置文件启动多个 ASFP2 Server。

---

## 1. 测试目标

验证 `c4_asfp2_server` 在收到 Agent 的 `start` 工具调用后：
1. 通过 `roots/list` 获取配置文件路径
2. 读取并校验配置
3. 附加已有共享内存
4. 构建 `addr → shm_id` 映射索引
5. 为每个配置实例启动 goroutine 监听对应端口
6. 返回正确的结果

---

## 2. 测试架构

```
c4/test/c4_fun_00057/
├── README.md              # 本文件
├── conftest.py            # 公共 fixture（MCP 客户端 + 环境准备 + 清理）
├── shm_helpers.py         # 共享内存读写工具函数
└── test_start.py          # TC1~TC10
```

### 2.1 被测对象 (SUT)

Go 编译的 `c4_asfp2_server` 二进制，通过 Python `subprocess.Popen` 启动，
走 **MCP stdio JSON-RPC** 协议：

- Python 向 SUT 的 stdin 写入 JSON-RPC 请求
- Python 从 SUT 的 stdout 读取 JSON-RPC 响应
- stderr 保留给 SUT 日志输出

### 2.2 测试前置条件

每个测试用例需要先准备好共享内存和配置文件。准备步骤：

```
1. 生成包含 c4_asfp2_server 配置段的 JSON 配置文件
2. 启动 c4_shm_manager → MCP initialize
3. 调用 create_shm 创建共享内存
4. 调用 adjust_shm 为 c4_asfp2_server 的 points 分配 shm_id（回填配置文件）
5. 关闭 c4_shm_manager（释放端口，仅需共享内存和配置文件保留）
6. 启动 c4_asfp2_server → MCP initialize
7. 获取工具列表，确认 start/pause/resume/status 均已注册
8. 调用 start 工具
```

### 2.3 MCP 交互序列（start 工具调用期间）

```
1. Python 发送 tools/call {name: "start", arguments: {}}
2. SUT 通过 stdout 发送 roots/list 请求
3. Python 向 SUT 的 stdin 写入 roots/list 应答（含配置文件路径）
4. SUT 读取配置 → shm_open → mmap → 启动 goroutine
5. SUT 返回 start 结果
```

### 2.4 共享内存验证

Python 通过 `os.open("/dev/shm/c4_{id}", os.O_RDONLY)` + `mmap` 直接读取共享内存，
用 `struct.unpack` 按偏移解析 Header 字段。

### 2.5 端口监听验证

Python 通过 `socket.create_connection(("127.0.0.1", port), timeout=1)` 验证端口
是否已被 SUT 监听。连接成功即确认 goroutine 已启动，随后关闭测试连接。

---

## 3. 测试配置模板

### 3.1 配置文件结构

```json
{
    "c4_shm_manager": {
        "writer": ["c4_asfp2_server"],
        "reader": ["c4_asfp2_client"]
    },
    "c4_asfp2_server": [
        {
            "name": "接收测试数据服务",
            "id": "test_receiver",
            "port": 9000,
            "t1": 0,
            "t2": 0,
            "forward_kack": 255,
            "inverse_keep": 0,
            "points": [
                {"id": "point_a", "addr": 1000, "shm_id": 0},
                {"id": "point_b", "addr": 1001, "shm_id": 0}
            ]
        }
    ],
    "c4_asfp2_client": []
}
```

### 3.2 期望的 shm_id 分配

`c4_asfp2_server` 有 2 个 point（addr=1000, 1001），`c4_asfp2_client` 有 0 个。
`adjust_shm` 计算 `writer_points = 2`，`max_points = 4`。
分配后：`point_a` → shm_id=1，`point_b` → shm_id=2。

---

## 4. 测试用例

### TC1: 基本启动 — 单实例 2 point

- **前置**：配置 1 个 Server 实例、port=9000、2 个 point（配置模板见 §3.1），
  c4_shm_manager 已完成 create_shm + adjust_shm
- **操作**：启动 `c4_asfp2_server`，MCP initialize，调用 `start`
- **预期**：
  - 返回 `"success"`（`isError: false`）
  - 端口 9000 已监听（`socket.create_connection` 成功）
- **额外验证**：
  - `c4_asfp2_server` 内部已构建 addr→shm_id 索引（通过后续 `status` 工具间接验证）
  - 无法直接验证索引内容，但 `start` 成功 + 端口已监听即可证明加载正常

### TC2: 多实例启动 — 3 个不同端口

- **前置**：配置 3 个 Server 实例，port 分别为 9000、9001、9002，各 1 个 point
  ```
  c4_asfp2_server: [
    {"id":"r1", "port":9000, "points":[{"id":"p1","addr":1000,"shm_id":0}]},
    {"id":"r2", "port":9001, "points":[{"id":"p2","addr":2000,"shm_id":0}]},
    {"id":"r3", "port":9002, "points":[{"id":"p3","addr":3000,"shm_id":0}]}
  ]
  ```
- **预期**：
  - 返回 `"success"`
  - 端口 9000、9001、9002 均已监听
- **验证**：3 个 goroutine 各自独立监听 — 关闭一个端口的测试连接不影响其他端口

### TC3: 空实例列表 — 0 个 Server

- **前置**：配置 `"c4_asfp2_server": []`（空数组），有 reader 但无 writer
- **操作**：调用 `start`
- **预期**：返回 `"success"`（无实例需启动，无端口需监听，但仍需 shm_open + mmap）
- **注意**：`adjust_shm` 的 writer_points=0 时，shm 是默认 10 万点大小。asfp2_server 的 start 不依赖 point_count，仅需 shm 存在且 magic 有效。

### TC4: 重复调用 start → ALREADY_STARTED

- **前置**：TC1 已成功启动
- **操作**：再次调用 `start`（同一 SUT 进程）
- **预期**：`isError: true`，错误码 `ALREADY_STARTED`

### TC5: start 未调用前调用 pause/status → SERVICE_NOT_READY

- **前置**：启动 `c4_asfp2_server`，MCP initialize 完成，但未调用 `start`
- **操作**：分别调用 `pause`、`status`（`resume` 同理，任选一个验证）
- **预期**：均返回 `isError: true`，错误码 `SERVICE_NOT_READY`

### TC6: 端口重复 → PORT_CONFLICT

- **前置**：配置 2 个 Server 实例，均指定 `port: 9000`
- **操作**：调用 `start`
- **预期**：`isError: true`，错误码 `PORT_CONFLICT`
- **验证**：端口 9000 未被监听（SUT 应在检测到冲突后不启动任何 goroutine）

### TC7: 共享内存不存在 → SHM_OPEN_FAILED

- **前置**：不创建共享内存（跳过 create_shm + adjust_shm），配置文件正常
- **操作**：调用 `start`
- **预期**：`isError: true`，错误码 `SHM_OPEN_FAILED`

### TC8: 共享内存 magic 损坏 → SHM_CORRUPTED

- **前置**：c4_shm_manager 已创建 shm，但 Python 直接修改 Header magic 为 0xDEADBEEF
- **操作**：调用 `start`
- **预期**：`isError: true`，错误码 `SHM_CORRUPTED`

### TC9: roots/list 超时 → CONFIG_PATH_MISSING

- **前置**：启动 `c4_asfp2_server`，MCP initialize
- **操作**：调用 `start`，Python 对 `roots/list` 请求**不响应**（模拟超时）
- **预期**：SUT 在超时（如 5s）后返回 `isError: true`，错误码 `CONFIG_PATH_MISSING`
- **注意**：Python 需设置 `start` 调用超时略大于 SUT 的 roots/list 超时，避免测试误报

### TC10: 配置文件格式错误 → CONFIG_PARSE_ERROR

- **前置**：传入格式错误的配置文件。共享内存正常创建。
- **操作**：调用 `start`
- **预期**：`isError: true`，错误码 `CONFIG_PARSE_ERROR`
- **子场景**（`pytest.mark.parametrize`）：

  | 子场景 | 配置内容 | 触发条件 |
  |--------|---------|---------|
  | (a) JSON 语法错误 | `{invalid json` | JSON 解析失败 |
  | (b) 合法 JSON 但缺 key | `{"c4_shm_manager": {...}}` | `c4_asfp2_server` 顶层 key 不存在 |

---

## 5. 实现注意

### 5.1 前置条件准备

测试环境下需要 c4_shm_manager 先完成 create_shm + adjust_shm，然后关闭 c4_shm_manager
（共享内存和配置文件保留在磁盘上）。这是因为 c4_asfp2_server 不创建共享内存，
只附加已有共享内存。conftest.py 应提供 `prepare_environment` fixture 封装此流程。

### 5.2 MCP JSON-RPC 协议细节

- 每行一个 JSON 对象（行分隔），不是流式 JSON
- `initialize` 握手：Python 发送 `initialize` → 读取 SUT 的 `initialize` 响应 → 发送 `initialized` 通知
- initialize 请求需包含 `capabilities.roots.listChanged: true`
- `start` 调用期间，SUT 通过 **stdout** 发送 `roots/list` 请求，Python 从 stdout 读取该请求，
  并向 SUT 的 **stdin** 写入对应的 `roots/list` 应答
- `roots/list` 应答示例：`{"jsonrpc":"2.0","id":<request_id>,"result":{"roots":[{"uri":"file:///tmp/config.json"}]}}`

### 5.3 端口冲突处理

TC6 需要确保端口在测试前未被占用。建议 conftest.py 的 `isolated_port` fixture
使用动态端口（端口号 0 → OS 分配），或 manual 指定高位端口（如 50000+），
并在 teardown 中确认端口已释放。

### 5.4 共享内存操作

- Python 通过 `mmap` 直接读写共享内存进行验证
- 修改 Header magic（TC8）：`mmap[0:4] = struct.pack(">I", 0xDEADBEEF)`
- 路径：`/dev/shm/c4_{instance_id}`
- 测试结束后必须 `shm_unlink`，建议在 fixture teardown 中清理

### 5.5 SUT 编译

- 测试前需编译 `c4/mcp/c4_asfp2_server` 为二进制
- 建议 conftest.py 自动检测二进制是否存在，不存在则 `go build`

### 5.6 隔离性

- 每个测试用例使用独立的 `instance_id`（如 `test_tc1`、`test_tc2`），
  确保共享内存路径不冲突
- 每个测试用例使用独立端口，避免 TC2 与 TC1 的 9000 端口冲突
- fixture teardown 中清理共享内存、关闭 SUT 进程、删除临时配置文件

### 5.7 字节序

共享内存中所有多字节字段为大端存储。Python `struct.unpack` 使用 `>` 前缀：

| 字段 | 格式 |
|------|------|
| magic | `>I` |
| version | `>H` |
| point_count | `>I` |
| max_points | `>I` |

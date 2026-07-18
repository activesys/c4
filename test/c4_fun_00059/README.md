# C4_FUN_00059 测试方案

> **对应功能**：`c4/docs/specification/c4_function.md` C4_FUN_00059
> **对应需求**：C4_RS_00095
> **设计参考**：`c4/docs/design/c4_asfp2_client.md` §3

C4_FUN_00059：Agent 生成 ASFP2 发送 MCP 服务的配置文件后，启动 MCP 服务，MCP 服务根据配置文件启动多个 ASFP2 Client。

---

## 1. 测试目标

验证 `c4_asfp2_client` 在收到 Agent 的 `start` 工具调用后：
1. 通过 `roots/list` 获取配置文件路径
2. 读取并校验配置（shm_id 合法性、addr 合法性等）
3. 以 O_RDONLY 模式附加已有共享内存
4. 构建 `shm_id → asfp2_key` 正向映射索引
5. 为每个配置实例启动 goroutine 并连接目标服务器
6. 操作原子性——全部成功或全部回滚
7. 返回正确的结果或错误码

---

## 2. 测试架构

```
c4/test/c4_fun_00059/
├── README.md              # 本文件
├── conftest.py            # 公共 fixture（MCP 客户端 + 环境准备 + 清理）
├── shm_helpers.py         # 共享内存读写工具函数（复用 c4_fun_00057）
└── test_start.py          # TC1~TC11
```

### 2.1 被测对象 (SUT)

Go 编译的 `c4_asfp2_client` 二进制，通过 MCP stdio JSON-RPC 协议控制。

### 2.2 测试二进制

| 二进制 | 用途 |
|--------|------|
| `c4_shm_manager` | 创建 SHM，分配 shm_id |
| `c4_asfp2_client` | **SUT** |
| `asfp2_server` | 连接目标 — 在配置的 port 上监听，验证 SUT 的 TCP 连接可达 |

### 2.3 测试前置条件

```
1. 生成包含 c4_asfp2_client 配置段的 JSON 配置文件
2. 启动 c4_shm_manager → create_shm → adjust_shm → 回填 shm_id → 关闭
3. 在配置的目标端口启动 asfp2_server（验证连接用）
4. 启动 c4_asfp2_client → MCP initialize
5. 调用 start 工具
```

### 2.4 连接验证

- **成功**：`start` 返回 `"success"` → SUT 的 TCP Dial 已成功（`asfp2_server` 侧可见连接）
- **CONNECT_FAILED**：使用不可达 IP（192.0.2.0/24 TEST-NET-1）→ 验证返回错误码
- **原子性**：部分实例成功、部分失败 → 全部回滚 → `asfp2_server` 侧无残留连接

---

## 3. 测试配置模板

### 3.1 标准配置（单实例 2 points）

```json
{
    "c4_shm_manager": {
        "writer": ["c4_modbus_client"],
        "reader": ["c4_asfp2_client"]
    },
    "c4_modbus_client": [
        {
            "name": "模拟数据源", "id": "mock_writer",
            "ip": "127.0.0.1", "port": 502,
            "hton_register": 1, "hton_total": 0,
            "t0": 30, "t1": 10, "retries": 10,
            "coils_quantity_max": 2000, "registers_quantity_max": 125,
            "timer": 1000,
            "points": [
                {"id": "pt_a", "uid": 1, "addr": 1000, "fun": 3, "type": 4, "swap": 2, "shm_id": 0},
                {"id": "pt_b", "uid": 1, "addr": 1002, "fun": 3, "type": 4, "swap": 2, "shm_id": 0}
            ]
        }
    ],
    "c4_asfp2_client": [
        {
            "name": "测试发送端",
            "ip": "127.0.0.1", "port": 9900,
            "t0": 30, "t1": 0, "t2": 0,
            "smart": 1, "forward_kack": 255, "inverse_keep": 0,
            "timer": 100,
            "points": [
                {"key": "mock_writer.pt_a", "addr": 1000, "shm_id": 0},
                {"key": "mock_writer.pt_b", "addr": 1001, "shm_id": 0}
            ]
        }
    ]
}
```

### 3.2 多实例配置

3 个 client 实例，分别连接 port 9901、9902、9903。

### 3.3 不可达配置

```json
{"c4_asfp2_client": [{"ip": "192.0.2.1", "port": 9999, "t0": 5, ...}]}
```

### 3.4 部分不可达配置（原子性验证）

实例 1 可达（127.0.0.1:9901），实例 2 不可达（192.0.2.1:9999）。

### 3.5 期望的 shm_id 分配

writer_points=2，max_points=4。pt_a → shm_id=1，pt_b → shm_id=2。

---

## 4. 测试用例

### TC1: 基本启动 — 单实例

- **前置**：标准配置。`asfp2_server -p 9900 &` 已监听
- **操作**：启动 SUT，MCP initialize，调用 `start`
- **预期**：返回 `"success"`。`asfp2_server` 侧可见 TCP 连接

### TC2: 多实例启动

- **前置**：3 实例配置。`asfp2_server` 分别在 9901/9902/9903 监听
- **操作**：调用 `start`
- **预期**：返回 `"success"`。3 个 `asfp2_server` 均可见连接

### TC3: 空实例列表

- **前置**：`"c4_asfp2_client": []`（空数组）。SHM 正常创建（writer 端仍有 points）
- **操作**：调用 `start`
- **预期**：返回 `"success"`（无实例需启动，调用 `shm_open` + `mmap` 后直接返回）
- **注意**：若 SHM 不存在（连创建都不做），预期返回 `SHM_OPEN_FAILED`。本 TC 验证 normal 路径（SHM 存在但无 client 实例）

### TC4: 重复调用 start → ALREADY_RUNNING

- **前置**：TC1 已成功启动
- **操作**：再次调用 `start`
- **预期**：`isError: true`，错误码 `ALREADY_RUNNING`

### TC5: start 未调用前调用 stop/status → SERVICE_NOT_READY

- **前置**：MCP initialize 完成，但未调用 `start`
- **操作**：调用 `stop`、`status`
- **预期**：均返回 `isError: true`，`SERVICE_NOT_READY`

### TC6: shm_id 未分配 → SHM_ID_NOT_ASSIGNED

- **前置**：配置中所有 point 的 shm_id=0（跳过 adjust_shm），SHM 正常创建
- **操作**：调用 `start`
- **预期**：`isError: true`，`SHM_ID_NOT_ASSIGNED`

### TC7: 配置文件格式错误 → CONFIG_PARSE_ERROR

- **前置**：SHM 正常。配置为无效 JSON 或缺 `c4_asfp2_client` key
- **操作**：调用 `start`
- **预期**：`isError: true`，`CONFIG_PARSE_ERROR`

### TC8: roots/list 超时 → CONFIG_PATH_MISSING

- **前置**：MCP initialize 完成
- **操作**：调用 `start`，Python 不响应 `roots/list`
- **预期**：超时后返回 `isError: true`，`CONFIG_PATH_MISSING`

### TC9: 共享内存不存在 → SHM_OPEN_FAILED

- **前置**：未创建 SHM（跳过 create_shm）。配置正常
- **操作**：调用 `start`
- **预期**：`isError: true`，`SHM_OPEN_FAILED`

### TC10: 共享内存 magic 损坏 → SHM_CORRUPTED

- **前置**：SHM 已创建，Python 通过 mmap 修改 Header magic 为 0xDEADBEEF
- **操作**：调用 `start`
- **预期**：`isError: true`，`SHM_CORRUPTED`

### TC11: 连接失败 — 原子性回滚

- **前置**：部分不可达配置（§3.4）。`asfp2_server -p 9901 &` 已监听
- **操作**：调用 `start`
- **预期**：
  - 返回 `isError: true`，`CONNECT_FAILED`
  - 可达实例（port 9901）的连接被 tear down → `asfp2_server` 侧无活跃连接

---

## 5. 实现注意

### 5.1 连接验证

- 通过 `asfp2_server` 的 stdout 输出确认连接（输出 "client connected" 等类似信息）
- 原子性验证（TC11）：检查 `asfp2_server` 侧连接在 SUT 失败后断开了

### 5.2 SMH magic 修改（TC10）

Python 通过 mmap + `struct.pack(">I", 0xDEADBEEF)` 修改 Header 前 4 字节。

### 5.3 隔离性

- 每个 TC 使用独立的 `instance_id` 和端口
- fixture teardown 清理 SHM、SUT 进程、`asfp2_server` 进程、临时配置文件

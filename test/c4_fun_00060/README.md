# C4_FUN_00060 测试方案

> **对应功能**：`c4/docs/specification/c4_function.md` C4_FUN_00060
> **对应需求**：C4_RS_00095
> **设计参考**：`c4/docs/design/c4_asfp2_client.md` §3.2

C4_FUN_00060：ASFP2 发送 MCP 服务支持停止和重启 — Agent 可停止全部发送实例，配置文件调整后重新启动。重启时自动重读配置，根据变化调整读取点和链接地址/端口。

---

## 1. 测试目标

验证 `c4_asfp2_client` 的 Stop-Start 协议：

1. `stop` 在运行状态返回 `"success"` 并释放 TCP 连接
2. `stop` 在未启动状态返回 `SERVICE_NOT_READY`
3. `start` 在已运行状态返回 `ALREADY_RUNNING`
4. 简单重启（`stop` → `start`）后连接恢复
5. 完整 Stop-Start 协议（`stop` → `adjust_shm` → `start`）
6. 多次 `stop`/`start` 循环正确
7. 连续两次 `stop` 返回 `SERVICE_NOT_READY`
8. 重启时配置变更（新 IP/port/points）生效
9. `start` 失败后修正配置恢复成功
10. At-least-once 语义：重启后可能重复发送

---

## 2. 测试架构

```
c4/test/c4_fun_00060/
├── README.md              # 本文件
├── conftest.py            # 公共 fixture（复用 c4_fun_00059 fixture + 扩展）
├── shm_helpers.py         # 共享内存读写工具函数（复用 c4_fun_00057）
└── test_stop_restart.py   # TC1~TC10
```

### 2.1 被测对象 (SUT)

Go 编译的 `c4_asfp2_client` 二进制，通过 MCP stdio JSON-RPC 协议控制。

### 2.2 测试二进制

| 二进制 | 用途 |
|--------|------|
| `c4_shm_manager` | 创建 SHM，分配 shm_id |
| `c4_asfp2_client` | **SUT** |
| `asfp2_server` | 连接目标 — 验证 TCP 连接建立/释放 |
| `asfp2_client` | 数据注入（TC10 at-least-once 验证） |
| `c4_asfp2_server` | 注入端接收（TC10） |

### 2.3 连接验证

- `stop` 后通过 `asfp2_server` 侧连接断开确认释放
- `start` 后通过 `asfp2_server` 侧连接恢复确认重启成功

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
            "name": "停止重启测试客户端",
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

### 3.2 变更后配置（新 IP + 新 port + 新 point）

基于 §3.1，ip 改为 `127.0.0.2`，port 改为 9901，新增 point（addr=2000）。

---

## 4. 测试用例

### TC1: stop — 运行中停止，连接释放

- **前置**：标准配置。`asfp2_server -p 9900 &` 已监听。SUT 已 start
- **操作**：
  1. 确认 `asfp2_server` 侧有连接
  2. 调用 `stop`
  3. 确认 `asfp2_server` 侧连接断开
- **预期**：`stop` 返回 `"success"`。连接已释放

### TC2: stop — 未启动时调用

- **前置**：MCP initialize 完成，`start` 从未调用
- **操作**：调用 `stop`
- **预期**：`isError: true`，`SERVICE_NOT_READY`

### TC3: start — 已运行时重复调用

- **前置**：`start` 已成功
- **操作**：再次调用 `start`
- **预期**：`isError: true`，`ALREADY_RUNNING`

### TC4: 简单重启（stop → start，无配置变更）

- **前置**：标准配置。SUT 已 start，port 9900 有连接
- **操作**：
  1. `stop` → 确认返回 `"success"`，连接断开
  2. `start`（同一 config_path）→ 确认返回 `"success"`
  3. 确认 port 9900 重新连接
- **预期**：重启后连接恢复

### TC5: 完整 Stop-Start 协议（stop → adjust_shm → start）

- **前置**：标准配置。SUT 已 start
- **操作**：
  1. `stop`
  2. 启动 `c4_shm_manager` → `adjust_shm` → 关闭
  3. `start`
  4. 确认 port 9900 重新连接
- **预期**：三方协议全链路正确

### TC6: 多次 stop/start 循环

- **前置**：标准配置。SUT 已 start
- **操作**：`stop` → `start`（第 1 轮）→ `stop` → `start`（第 2 轮）→ `stop` → `start`（第 3 轮）
- **预期**：全部 6 次调用返回 `"success"`，每轮重启后连接均恢复

### TC7: double-stop — 连续两次 stop

- **前置**：SUT 已 start
- **操作**：`stop`（成功）→ `stop`
- **预期**：第一次 `"success"`，第二次 `SERVICE_NOT_READY`

### TC8: 重启时配置变更生效

- **前置**：标准配置（ip=127.0.0.1, port=9900，2 points）。SUT 已 start
- **操作**：
  1. `stop`
  2. 修改配置：ip → 127.0.0.2，port → 9901，新增 point（addr=2000）。`adjust_shm`
  3. 在 127.0.0.2:9901 启动 `asfp2_server`（若 127.0.0.2 不可用则用其他可达的 loopback 别名，或验证 `start` 返回 `CONNECT_FAILED`）
  4. `start`（新 config_path）
  5. 验证：
     - 新地址 127.0.0.2:9901 的 `asfp2_server` 收到连接
     - 旧地址 127.0.0.1:9900 的 `asfp2_server` 无连接
- **预期**：配置变更（ip + port + point）均已生效

### TC9: start 失败后错误恢复

- **前置**：标准配置。SUT 已 start
- **操作**：
  1. `stop`
  2. 配置改为不可达目标（192.0.2.1:9999）→ `adjust_shm`
  3. `start` → 预期 `CONNECT_FAILED`
  4. 配置改回标准配置 → `adjust_shm`
  5. `start` → 预期 `"success"`，port 9900 连接恢复
- **预期**：失败后允许修正配置重试，无需重启 SUT 进程

### TC10: At-least-once 语义 — 重启后数据重复发送

- **前置**：需数据注入链路。
  1. `c4_shm_manager` → SHM
  2. `c4_asfp2_server` (port A) + `c4_asfp2_client` (SUT, → port B) 复用 FUN_00047 架构
  3. `asfp2_client` 注入数据到 port A → SHM → SUT 读取 → 发送到 port B
  4. `asfp2_server -p B` 接收验证
- **操作**：
  1. 首轮注入 → 验证 `asfp2_server` (port B) 收到数据
  2. `stop` SUT → `start` SUT（同配置，**无新注入**）
  3. 验证 `asfp2_server` (port B) 再次收到相同数据
- **预期**：重启后 `last_seen` 归零，同一数据被再次发送（at-least-once）
- **说明**：验证 §3.2 声明的语义——重启时重复发送 stop 前最近一次成功发送的数据

---

## 5. 实现注意

### 5.1 连接验证

- `asfp2_server` 作为连接目标，stdout 输出可用于确认连接建立/断开
- `socket.create_connection` 也可用于快速验证端口可达

### 5.2 TC10 数据注入链路

TC10 需要完整数据路径，复用 C4_FUN_00047 的编排模式：
- `asfp2_client` (CLI) → `c4_asfp2_server` (接收) → SHM → SUT → `asfp2_server` (验证)

### 5.3 adjust_shm 调用

TC5/TC8/TC9 需独立启动 `c4_shm_manager` 调用 `adjust_shm`。

### 5.4 隔离性

- 每个 TC 使用独立的 `instance_id` 和端口号
- fixture teardown 清理 SHM、SUT、`asfp2_server`、临时配置文件

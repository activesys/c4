# C4_FUN_00058 测试方案

> **对应功能**：`c4/docs/specification/c4_function.md` C4_FUN_00058
> **对应需求**：C4_RS_00095
> **设计参考**：`c4/docs/design/c4_asfp2_server.md` §3.3，`c4/docs/design/c4_architecture.md` §3.3.1

C4_FUN_00058：Agent 停止全部 ASFP2 接收端口实例，配置调整后重启。重启时 MCP 服务自动重新读取配置文件。

---

## 1. 测试目标

验证 `c4_asfp2_server` 的 Stop-Start 协议：

1. `stop` 在运行状态返回 `"success"` 并释放端口
2. `stop` 在未启动状态返回 `SERVICE_NOT_READY`
3. `start` 在已运行状态返回 `ALREADY_RUNNING`
4. `stop` → `start` 简单重启后端口重新监听、数据流恢复
5. `stop` → `c4_shm_manager.adjust_shm()` → `start` 完整 Stop-Start 协议正确执行
6. 多次 `stop` / `start` 循环，每次均正确
7. 重启后配置校验（端口冲突检测）仍然生效
8. 连续两次 `stop`（double-stop）返回 `SERVICE_NOT_READY`
9. 重启时配置变更（新端口 / 新 point）生效
10. `start` 失败（PORT_CONFLICT）后，用修正配置恢复启动成功

---

## 2. 测试架构

```
c4/test/c4_fun_00058/
├── README.md              # 本文件
├── conftest.py            # 公共 fixture（复用 c4_fun_00057 的 fixture + 扩展）
├── shm_helpers.py         # 共享内存读写工具函数（复用 c4_fun_00057）
└── test_stop_restart.py   # TC1~TC10
```

### 2.1 被测对象 (SUT)

Go 编译的 `c4_asfp2_server` 二进制，通过 Python `subprocess.Popen` 启动，
走 **MCP stdio JSON-RPC** 协议。

### 2.2 测试前置条件

复用 `c4_fun_00057/conftest.py` 的 `prepare_environment` 和 `start_asfp2_server` fixture。

```
1. prepare_environment(config, instance_id)
   → 启动 c4_shm_manager → create_shm → adjust_shm → 回填 shm_id
   → 关闭 c4_shm_manager → 返回 (config_path, instance_id)
2. start_asfp2_server fixture 启动 c4_asfp2_server 子进程
3. 测试用例调用 start_asfp2_server.call_tool("start", {}, on_request=...)
4. 测试用例调用 start_asfp2_server.call_tool("stop", {})
```

### 2.3 MCP 交互序列

**`start` 调用期间**：
```
1. Python 发送 tools/call {name: "start", arguments: {}}
2. SUT 通过 stdout 发送 roots/list 请求
3. Python 向 SUT 的 stdin 写入 roots/list 应答（含配置文件路径）
4. SUT 读取配置 → shm_open → mmap → 启动 goroutine
5. SUT 返回 start 结果
```

**`stop` 调用期间**：
```
1. Python 发送 tools/call {name: "stop", arguments: {}}
2. SUT 关闭所有 listener → 销毁实例 → munmap + close shm
3. SUT 返回 stop 结果
```

### 2.4 端口监听验证

Python 通过 `socket.create_connection(("127.0.0.1", port), timeout=1)` 验证端口
是否已被 SUT 监听。若端口未被监听，抛出 `ConnectionRefusedError`。

端口释放检测使用**轮询重试**（100ms 间隔，最长 3s），避免 OS 端口回收时间不可靠：

```python
def wait_port_released(port: int, timeout: float = 3.0, interval: float = 0.1):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            s = socket.create_connection(("127.0.0.1", port), timeout=interval)
            s.close()
        except (ConnectionRefusedError, OSError):
            return  # 端口已释放
        time.sleep(interval)
    raise RuntimeError(f"Port {port} not released within {timeout}s")
```

### 2.5 数据流验证（重启后）

重启后通过 `asfp2_client` 向 SUT 端口发送 ASFP2 数据包，Python 通过 `mmap`
直接读取共享内存。验证分两步：

1. **发送前**：读取目标 shm block 的 `write_seq` 字段，记录为 `seq_before`
2. **发送后**：再次读取同一 block 的 `write_seq`，验证 `write_seq > seq_before`

`write_seq` 递增即证明数据已通过完整路径：端口接受 → ASFP2 解析 → shm 写入。
同时验证 block 的 `magic == 0xC4DA7A00` 和 `state == 1`。

---

## 3. 测试配置模板

### 3.1 标准配置（单实例 2 点）

```json
{
    "c4_shm_manager": {
        "writer": ["c4_asfp2_server"],
        "reader": ["c4_asfp2_client"]
    },
    "c4_asfp2_server": [
        {
            "name": "停止重启测试服务",
            "id": "test_stop_restart",
            "port": 9000,
            "t1": 0,
            "t2": 0,
            "forward_kack": 255,
            "inverse_keep": 0,
            "points": [
                {"id": "pt_a", "addr": 1000, "shm_id": 0},
                {"id": "pt_b", "addr": 1001, "shm_id": 0}
            ]
        }
    ],
    "c4_asfp2_client": []
}
```

### 3.2 端口冲突配置

```json
{
    "c4_shm_manager": {
        "writer": ["c4_asfp2_server"],
        "reader": ["c4_asfp2_client"]
    },
    "c4_asfp2_server": [
        {
            "name": "实例1",
            "id": "conflict_r1",
            "port": 9000,
            "t1": 0, "t2": 0,
            "forward_kack": 255, "inverse_keep": 0,
            "points": [{"id": "p1", "addr": 1000, "shm_id": 0}]
        },
        {
            "name": "实例2",
            "id": "conflict_r2",
            "port": 9000,
            "t1": 0, "t2": 0,
            "forward_kack": 255, "inverse_keep": 0,
            "points": [{"id": "p2", "addr": 2000, "shm_id": 0}]
        }
    ],
    "c4_asfp2_client": []
}
```

### 3.3 期望的 shm_id 分配

标准配置下 `c4_asfp2_server` 有 2 个 point（addr=1000, 1001），
`adjust_shm` 计算 `writer_points=2`，`max_points=4`。
分配后：`pt_a(addr=1000)` → shm_id=1，`pt_b(addr=1001)` → shm_id=2。

---

## 4. 测试用例

### TC1: stop — 运行中停止，端口释放

- **前置**：已按 §3.1 标准配置完成 prepare_environment，`start` 调用成功，端口 9000 已监听
- **操作**：
  1. `socket.create_connection(("127.0.0.1", 9000))` 确认端口可连接 → 关闭连接
  2. 调用 `stop`，无参数
  3. 轮询等待端口释放（`wait_port_released(9000)`，按 §2.4）
- **预期**：
  - `stop` 返回 `"success"`（`isError: false`）
  - 端口 9000 已释放（轮询在 3s 内检测到 `ConnectionRefusedError`）

### TC2: stop — 未启动时调用

- **前置**：`c4_asfp2_server` 已 MCP initialize，但 `start` 从未调用过
- **操作**：调用 `stop`，无参数
- **预期**：`isError: true`，`content[0].text` 以 `SERVICE_NOT_READY` 开头
- **说明**：`stop` 只有在 `start` 成功过才允许调用

### TC3: start — 已运行时重复调用

- **前置**：已按 §3.1 标准配置完成 prepare_environment，`start` 调用成功
- **操作**：再次调用 `start`（同一 SUT 进程，无间隔 `stop`）
- **预期**：`isError: true`，`content[0].text` 以 `ALREADY_RUNNING` 开头
- **说明**：`start` 在 `stop` 之前不得重复调用

### TC4: 简单重启（stop → start，无配置变更）

- **前置**：已按 §3.1 标准配置完成 prepare_environment，`start` 调用成功
- **操作**：
  1. 调用 `stop` → 确认返回 `"success"`
  2. 调用 `start`（无参数，`on_request` 返回同一 config_path）
  3. `socket.create_connection(("127.0.0.1", 9000))` 确认端口重新监听 → 关闭连接
  4. 通过 `mmap` 读取 shm block shm_id=1（addr=1000）和 shm_id=2（addr=1001），
     记录 `write_seq` 为 `seq_before`
  5. 运行 `asfp2_client -s 127.0.0.1 -p 9000 -b 1000 -e 1001 -t 3 -d 16`
  6. 再次读取同一 block 的 `write_seq`，验证 `write_seq > seq_before`
- **预期**：
  - `stop` 返回 `"success"`
  - `start` 返回 `"success"`（`isError: false`）
  - 端口 9000 可连接
  - shm block `magic == 0xC4DA7A00`，`state == 1`，`write_seq` 已递增
- **说明**：重启后 `start` 重新 `shm_open` + `mmap`，数据通路恢复正常。

### TC5: 完整 Stop-Start 协议（stop → adjust_shm → start）

- **前置**：已按 §3.1 标准配置完成 prepare_environment，`start` 调用成功
- **操作**：
  1. 调用 `stop` → 确认返回 `"success"`
  2. 启动 `c4_shm_manager`，MCP initialize，调用 `adjust_shm`（无参数）
     → `adjust_shm` 通过 `roots/list` 获取同一 config_path → 返回 `"success"`
  3. 关闭 `c4_shm_manager`
  4. 调用 `start`（无参数，`on_request` 返回同一 config_path）
  5. `socket.create_connection(("127.0.0.1", 9000))` 确认端口重新监听 → 关闭连接
   6. 通过 `mmap` 读取 shm block shm_id=1（addr=1000）和 shm_id=2（addr=1001），
     记录 `write_seq` 为 `seq_before`
   7. 运行 `asfp2_client -s 127.0.0.1 -p 9000 -b 1000 -e 1001 -t 3 -d 16`
   8. 再次读取同一 block 的 `write_seq`，验证 `write_seq > seq_before`
- **预期**：
  - `stop` 返回 `"success"`
  - `adjust_shm` 返回 `"success"`
  - `start` 返回 `"success"`
  - 端口 9000 可连接，`write_seq` 递增证明 ASFP2 数据正常写入共享内存
- **说明**：验证完整的 Stop-Start 三方协议：stop → shm_manager 调整 → start，三步全链路正确

### TC6: 多次 stop/start 循环

- **前置**：已按 §3.1 标准配置完成 prepare_environment，`start` 调用成功
- **操作**：
  1. `stop` → 验证 `"success"`
  2. `start` → 验证 `"success"` + 端口 9000 可连接
  3. `stop` → 验证 `"success"`
  4. `start` → 验证 `"success"` + 端口 9000 可连接
  5. 通过 `mmap` 读取 shm block shm_id=1（addr=1000）和 shm_id=2（addr=1001），
     记录 `write_seq` 为 `seq_before`
  6. 运行 `asfp2_client -s 127.0.0.1 -p 9000 -b 1000 -e 1001 -t 3 -d 16`
  7. 再次读取同一 block 的 `write_seq`，验证 `write_seq > seq_before`
- **预期**：
  - 全部 4 次 stop/start 调用均返回 `"success"`
  - 每次 `start` 后端口 9000 均可连接
  - 第 2 轮重启后 ASFP2 数据正常写入共享内存（`write_seq` 递增）
- **说明**：验证 stop/start 循环的幂等性——每次重启后数据通路均恢复正常

### TC7: 重启后端口冲突检测

- **前置**：已按 §3.1 标准配置完成 prepare_environment，`start` 调用成功
- **操作**：
  1. 调用 `stop` → 确认返回 `"success"`
  2. 准备端口冲突配置（§3.2）：两个实例均为 `port: 9000`，
     通过 `prepare_environment` 写入新的配置文件
  3. 启动 `c4_shm_manager` → `create_shm`（若需要）→ `adjust_shm` → 关闭
   4. 调用 `start`，`on_request` 返回新 config_path
- **预期**：
  - `start` 返回 `isError: true`，`content[0].text` 以 `PORT_CONFLICT` 开头
- **说明**：重启时的 `start` 仍需完整校验配置（包括端口唯一性），与首次启动行为一致。

### TC8: double-stop — 连续两次 stop

- **前置**：已按 §3.1 标准配置完成 prepare_environment，`start` 调用成功
- **操作**：
  1. 调用 `stop` → 确认返回 `"success"`
  2. 再次调用 `stop`（SUT 已回到初始状态）
- **预期**：
  - 第一次 `stop` 返回 `"success"`
  - 第二次 `stop` 返回 `isError: true`，`content[0].text` 以 `SERVICE_NOT_READY` 开头
- **说明**：`stop` 后 SUT 回到"初始化完成但未启动"的状态，与 `start` 从未调用时一致，
  因此第二次 `stop` 应返回 `SERVICE_NOT_READY`

### TC9: 重启时配置变更生效

- **前置**：已按 §3.1 标准配置完成 prepare_environment，`start` 调用成功（端口 9000，addr=1000,1001）
- **操作**：
  1. 调用 `stop` → 确认返回 `"success"`
  2. 修改配置：将 port 从 9000 改为 **9001**，保留原有 2 个 point（addr=1000,1001），
     新增一个 point（addr=2000, shm_id=0）。通过 `prepare_environment` 写入新配置文件
  3. 启动 `c4_shm_manager` → `adjust_shm` → 关闭（会为新 point 分配 shm_id，已有点 shm_id 不变）
  4. 调用 `start`，`on_request` 返回新 config_path
  5. 验证**端口 9001** 已监听（`socket.create_connection`）
  6. 验证**端口 9000** 已释放（`wait_port_released`）
  7. 验证**旧 point（addr=1000,1001）数据流**：
     a. 记录 shm_id=1（addr=1000）和 shm_id=2（addr=1001）的 `write_seq` 为 `seq_before`
     b. 运行 `asfp2_client -s 127.0.0.1 -p 9001 -b 1000 -e 1001 -t 3 -d 16`
     c. 验证 `write_seq > seq_before`
  8. 验证**新 point（addr=2000）数据流**：
     a. 解析配置文件，找到 addr=2000 分配的 `shm_id`（§5.8），记录其 `write_seq` 为 `seq_before`
     b. 运行 `asfp2_client -s 127.0.0.1 -p 9001 -b 2000 -e 2000 -t 3 -d 16`
     c. 验证 `write_seq > seq_before`
- **预期**：
  - `start` 返回 `"success"`
  - 新端口 9001 监听，旧端口 9000 释放
  - 旧 point（addr=1000,1001）在新端口上数据正常写入
  - 新 point（addr=2000）数据正常写入
- **说明**：验证 C4_FUN_00058 的核心语义——"重启时 MCP 服务自动重新读取配置文件，
  根据配置变化调整接收点和端口"，且已有采集点不受影响

### TC10: start 失败后错误恢复

- **前置**：已按 §3.1 标准配置完成 prepare_environment，`start` 调用成功
- **操作**：
  1. 调用 `stop` → 确认返回 `"success"`
  2. 准备**端口冲突配置**（§3.2），通过 `prepare_environment` 写入 → `adjust_shm`
  3. 调用 `start`，`on_request` 返回冲突 config_path → **预期失败**，返回 `PORT_CONFLICT`
  4. 准备**修正配置**（单实例，port=9000，1 point），通过 `prepare_environment` 写入 → `adjust_shm`
   5. 再次调用 `start`，`on_request` 返回修正后 config_path
  6. 验证端口 9000 已监听
- **预期**：
  - 步骤 3 `start` 返回 `isError: true`，`PORT_CONFLICT`
  - 步骤 5 `start` 返回 `"success"`
  - 端口 9000 可连接
- **说明**：`start` 失败后 SUT 应保持在"初始化完成但未启动"状态，
  允许 Agent 修正配置后重试——不需要重启 SUT 进程

---

## 5. 实现注意

### 5.1 复用 c4_fun_00057 的 fixture

`conftest.py` 通过 `importlib.util` 复用 `c4_fun_00057/conftest.py` 的 fixture，
与 `c4_fun_00042` 的模式一致：

```python
import importlib.util, os

_src = os.path.join(os.path.dirname(__file__), "../c4_fun_00057/conftest.py")
_spec = importlib.util.spec_from_file_location("c4_fun_00057_conftest", _src)
_c57 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_c57)

prepare_environment = _c57.prepare_environment
start_asfp2_server = _c57.start_asfp2_server
isolated_shm = _c57.isolated_shm
```

### 5.2 shm_id 固定性

`adjust_shm` 保证已有点的 `shm_id` 不变。标准配置（§3.1）中 addr=1000 → shm_id=1，
addr=1001 → shm_id=2。TC4/TC5/TC6 均可依赖此分配验证数据流。

### 5.3 测试隔离（function-scoped SUT）

每个测试用例独立启动自己的 `c4_asfp2_server` 子进程（function scope），
避免共享 SUT 导致的测试顺序依赖。这与 `c4_fun_00057` 的 fixture 模式一致。

- `prepare_environment` 和 `start_asfp2_server` 均为 function scope
- 每个 TC 自包含完整的 start → 操作 → stop 生命周期
- 不同 TC 之间无状态依赖，pytest 可任意排序
- `prepare_environment` 的 instance_id 按 TC 独立命名（如 `test_tc1`、`test_tc8`）

### 5.4 端口释放检测（轮询重试）

验证端口释放使用轮询重试（100ms 间隔，最长 3s），不依赖固定 sleep。
conftest.py 应提供 `wait_port_released(port, timeout=3.0, interval=0.1)` helper。

### 5.5 adjust_shm 调用

TC5 和 TC7 需要在 `stop` 和 `start` 之间调用 `c4_shm_manager.adjust_shm`。
应单独启动 `c4_shm_manager` 子进程、MCP initialize、调用 `adjust_shm`、关闭进程。
conftest.py 可提供 `run_adjust_shm(config_path)` helper 封装此流程。

### 5.6 asfp2_client 数据发送

TC4/TC5 通过 asfp2_client 验证数据流。命令格式：

```bash
asfp2_client -s 127.0.0.1 -p <port> -b <begin_addr> -e <end_addr> -t <count> -d <data_size>
```

`-t 3` 发送 3 个数据包，`-d 16` 使用 16 位整数类型。conftest.py 可提供
`run_asfp2_client(port, begin_addr, end_addr)` helper 封装此流程。

### 5.7 隔离性

- 每个测试用例使用独立的 `instance_id`（如 `test_tc1`、`test_tc2`），确保共享内存路径不冲突
- TC1–TC6 均使用标准配置 port 9000；function-scoped teardown 确保端口在用例间释放，顺序执行无冲突
- TC7 PORT_CONFLICT 配置也使用 port 9000，依赖 TC 间顺序执行和 teardown 释放
- TC9 配置变更后使用 port 9001，不与其他 TC 冲突
- fixture teardown 中清理共享内存、关闭 SUT 进程、删除临时配置文件

### 5.8 shm_id 查找（TC9）

TC9 需要查找新 point（addr=2000）被 `adjust_shm` 分配的 `shm_id`。方法：
`adjust_shm` 执行后会回填配置文件中各 point 的 `shm_id` 字段。在调用 `start` 前，
从 disk 上的配置 JSON 读取 `c4_asfp2_server[0].points` 数组，找到 `addr==2000` 的
point，取其 `shm_id` 即为目标 shm block 的偏移。

### 5.9 禁止事项

- **不得调用 `status` 工具**：该接口后续有调整，测试用例中不得使用
- 验证手段仅限：`start` 返回值、`stop` 返回值、端口连接检测、共享内存 mmap 读取

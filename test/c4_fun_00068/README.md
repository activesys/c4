# C4_FUN_00068 测试方案

> **对应功能**：`c4/docs/specification/c4_function.md` C4_FUN_00068
> **对应需求**：C4_RS_00094
> **设计参考**：`c4/docs/design/c4_influxdb_client.md` §3.2, §5.1

C4_FUN_00068：InfluxDB 写入 MCP 服务支持停止和重启 — Agent 可停止 InfluxDB 写入的全部实例，配置调整后重启，重启时 MCP 服务自动重新读取配置文件。

---

## 1. 测试目标

验证 `c4_influxdb_client` 的 Stop-Start 协议：

1. `stop` 在运行状态返回 `"success"` 并销毁全部实例（**尽力 flush 当前缓冲** + 关闭 HTTP 连接 + munmap 共享内存）
2. `stop` 在未启动状态（`start` 从未成功）幂等返回 `"success"`
3. `start` 在已运行状态返回 `ALREADY_RUNNING`
4. `stop` → `start` 简单重启后数据流恢复（InfluxDB 重新收到数据）
5. `stop` → `c4_shm_manager.adjust_shm()` → `start` 完整 Stop-Start 协议正确执行
6. 多次 `stop` / `start` 循环，每次均正确
7. 重启后配置变更（新 point）生效
8. **stop 时 flush 缓冲**——flush_interval 未到、数据尚在缓冲时，`stop` 尽力 flush，缓冲数据被写入 InfluxDB

---

## 2. 测试架构

```
c4/test/c4_fun_00068/
├── README.md              # 本文件
├── conftest.py            # 复用 c4_fun_00067 的 fixture
├── shm_helpers.py         # 复用 c4_fun_00067 的写入 helper
└── test_stop_restart.py   # TC1~TC8
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
`start_influxdb_client`），复用方式同 c4_fun_00016。

### 2.4 数据流验证（重启后）

influxdb 是 Reader，**不写共享内存**（`write_seq` 由测试模拟的 Writer 控制），数据流验证
依据 **InfluxDB 查询结果**：

1. **写入正常**：查询到期望的 field 值/类型
2. **stop 后**：再写数据后查询，InfluxDB **不再新增**（实例销毁，写入循环停止）
3. **start 后**：查询恢复新增（写入循环重建）

---

## 3. 测试配置模板

### 3.1 c4_influxdb_client 配置（SUT）

同 c4_fun_00067 §3.1（`timer=100`、`flush_interval=100`、`t0=5`、`retries=1`）。
TC8（stop 时 flush）改用 `flush_interval: 10000`（10s，让数据停留缓冲、不自动 flush）。

### 3.2 期望的 shm_id 分配

1 个占位 writer point → shm_id=1，influxdb point `key: "fake_writer.pt1"` 回填 shm_id=1。

---

## 4. 测试用例

### TC1: stop — 运行中停止

- **前置**：`prepare_environment` 完成，`start` 成功，写数据后 InfluxDB 已查到数据
- **操作**：
  1. 调用 `stop`，无参数
  2. 写新数据（`write_shm_block`，write_seq 递增）→ 等待 ≥ 2×`timer`（200ms）→ 查询
- **预期**：
  - `stop` 返回 `"success"`（`isError: false`）
  - 等待后 InfluxDB **无新增数据**（实例已销毁，写入循环停止）
- **说明**：`stop` 销毁全部实例并关闭 HTTP 写入循环

### TC2: stop — 未启动时调用（幂等）

- **前置**：`c4_influxdb_client` 已 MCP initialize，但 `start` 从未调用过
- **操作**：调用 `stop`，无参数
- **预期**：返回 `"success"`（`isError: false`）——`stop` 幂等

### TC3: start — 已运行时重复调用

- **前置**：`start` 调用成功
- **操作**：再次调用 `start`（同一 SUT 进程，无间隔 `stop`）
- **预期**：`isError: true`，`content[0].text` 以 `ALREADY_RUNNING` 开头

### TC4: 简单重启（stop → start，无配置变更）

- **前置**：`start` 成功，写数据后 InfluxDB 已查到数据
- **操作**：
  1. 调用 `stop` → 确认返回 `"success"`
  2. 调用 `start`（传入同一 config_path）
  3. 写新数据（新 value）→ 等待查询到新值
- **预期**：
  - `stop` 返回 `"success"`
  - `start` 返回 `"success"`（`isError: false`）
  - 重启后查询到**新的** field 值（数据流恢复）
- **说明**：重启后 `start` 重新 `shm_open` + `mmap`，写入循环恢复正常

### TC5: 完整 Stop-Start 协议（stop → adjust_shm → start）

- **前置**：`start` 成功，写数据后 InfluxDB 已查到数据
- **操作**：
  1. 调用 `stop` → 确认返回 `"success"`
  2. 启动 `c4_shm_manager`，调用 `adjust_shm`（同一 config_path）→ 返回 `"success"` → 关闭
  3. 调用 `start`（传入同一 config_path）
  4. 写新数据 → 等待查询到新值
- **预期**：
  - `stop` 返回 `"success"`
  - `adjust_shm` 返回 `"success"`
  - `start` 返回 `"success"`
  - 查询到新数据
- **说明**：验证完整 Stop-Start 三方协议：stop → shm_manager 调整 → start，三步全链路正确

### TC6: 多次 stop/start 循环

- **前置**：`start` 调用成功
- **操作**：
  1. `stop` → 验证 `"success"`
  2. `start` → 验证 `"success"`
  3. 重复 `stop` → `start` 共 2 轮
  4. 最后一轮 `start` 后写数据，验证查询到数据
- **预期**：
  - 全部 stop/start 调用均返回 `"success"`
  - 最后一轮重启后数据流恢复正常
- **说明**：验证 stop/start 循环的幂等性——每次重启后写入循环均恢复正常

### TC7: 重启时配置变更生效

- **前置**：占位 writer 预配 2 point（`pt1`/`pt2`）；`start` 成功（c4 配置 1 个 influxdb point，
  引用 `pt1`）
- **操作**：
  1. 调用 `stop` → 确认返回 `"success"`
  2. 修改 c4 配置：新增 influxdb point（引用 `pt2`，`shm_id: 0`），写新配置文件
  3. 启动 `c4_shm_manager` → `adjust_shm`（为 `pt2` 分配 shm_id=2，回填新 point）→ 关闭
  4. 调用 `start`（传入新 config_path）
  5. 分别写 shm_id=1、shm_id=2 的数据 → 等待查询到两行
- **预期**：
  - `start` 返回 `"success"`
  - 旧 point（shm_id=1）数据正常写入
  - 新 point（shm_id=2）数据正常写入
- **说明**：验证 C4_FUN_00068 核心语义——"重启时 MCP 服务自动重新读取配置文件，根据配置
  变化调整写入点"，已有写入点不受影响

### TC8: stop 时 flush 缓冲

- **前置**：配置 `flush_interval: 10000`（10s，数据不会自动 flush），`start` 成功
- **操作**：
  1. `write_shm_block(type=10, value=8.8)` → 等待 **≥3×`timer`（≥300ms）**，确保数据
     已被 SUT 读入缓冲（但因 `flush_interval=10s` **尚未自动 flush**）
  2. 查询 InfluxDB，确认**尚无数据**（数据停留在缓冲，`flush_interval` 生效的 sanity check）
  3. 调用 `stop` → **stop 尽力 flush 缓冲**
  4. 查询 InfluxDB
- **预期**：
  - `stop` 返回 `"success"`
  - 查询到 `value=8.8` 的数据（stop 时缓冲被 flush，写入 InfluxDB）
- **说明**：验证设计文档 §5.1「stop 尽力 flush 当前缓冲」。stop 仅 flush **已读入缓冲**的
  数据——尚未从共享内存读入的数据（或 flush 失败的数据）在 stop 时**丢失在所难免**，
  属 best-effort 语义，不在本测试验证范围

---

## 5. 实现注意

### 5.1 复用 c4_fun_00067 的 fixture

`conftest.py` 通过 `importlib.util` 复用 `c4_fun_00067/conftest.py` 的 fixture，
复用方式与 c4_fun_00016 完全一致（见其 README §5.1）。

### 5.2 adjust_shm 调用（TC5、TC7）

TC5 和 TC7 需要在 `stop` 和 `start` 之间调用 `c4_shm_manager.adjust_shm`。
单独启动 `c4_shm_manager` 子进程 → MCP initialize → 调用 `adjust_shm` → 关闭进程
（复用 c4_fun_00062 的 `_run_adjust_shm` helper）。

### 5.3 数据流验证（轮询重试 + 负向等待）

- **正向断言**（查询到新值）用轮询重试（间隔 50ms，最长 3s）
- **负向断言**（TC1：stop 后无新增）用「固定等待 ≥ 2×`timer` 后断言无新增」，
  不轮询（负向断言无自然终态，固定等待避免假阴性）
- 数据流验证需等待 SUT 轮询周期（`timer=100ms`）+ flush 周期（`flush_interval=100ms`），
  故正向等待超时须 ≥ 3 个周期（建议 3s）

### 5.4 TC8 的 flush_interval 隔离与「stop 丢数」语义

TC8 用 `flush_interval=10000`（10s）保证数据在 `stop` 前不会自动 flush。
写数据后**等待 ≥3×`timer`（≥300ms）**确保数据已读入 SUT 缓冲，再查询确认 InfluxDB
**尚无数据**（仅作 `flush_interval` 生效的 sanity check，**不证明**数据在缓冲中——
Reader 不写共享内存，无外部信号可观察缓冲状态），最后调用 `stop` 验证 flush。

> **stop 丢数语义**：stop 仅 flush **已读入缓冲**的数据；尚未从共享内存读入的数据
> 在 stop 时**丢失在所难免**（属 best-effort 语义）。TC8 通过足够长的等待（≥3×`timer`）
> 把「未读入」的概率压到可忽略，聚焦验证「已读入的数据被 flush」这一核心行为。
> 这是 influxdb 特有的「stop 尽力 flush」语义（iec104 无此行为）。

### 5.5 隔离性

- 每个 TC 独立 `instance_id`、独立 database
- fixture teardown：关闭 SUT、DROP database、shm_unlink、删除临时配置

### 5.6 禁止事项

- **不得调用 `status` 工具**
- 验证手段仅限：`start`/`stop` 返回值、共享内存 mmap 读写、InfluxDB HTTP 查询结果

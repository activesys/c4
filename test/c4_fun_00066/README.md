# C4_FUN_00066 测试方案

> **对应功能**：`c4/docs/specification/c4_function.md` C4_FUN_00066
> **对应需求**：C4_RS_00091
> **设计参考**：`c4/docs/design/c4_iec104_client.md` §3.3

C4_FUN_00066：Agent 停止 IEC104 采集 MCP 服务的全部实例，配置调整后重启。重启时 MCP 服务自动重新读取配置文件。

---

## 1. 测试目标

验证 `c4_iec104_client` 的 Stop-Start 协议：

1. `stop` 在运行状态返回 `"success"` 并销毁全部实例（先发 STOPDT act 尽力停用数据传输，再关闭 TCP 连接）
2. `stop` 在未启动状态（`start` 从未成功）幂等返回 `"success"`
3. `start` 在已运行状态返回 `ALREADY_RUNNING`
4. `stop` → `start` 简单重启后数据流恢复
5. `stop` → `c4_shm_manager.adjust_shm()` → `start` 完整 Stop-Start 协议正确执行
6. 多次 `stop` / `start` 循环，每次均正确
7. 重启后配置变更（新 point）生效
8. 重启后数据采集恢复正常（`write_seq` 恢复递增、value 正确）

---

## 2. 测试架构

```
c4/test/c4_fun_00066/
├── README.md              # 本文件
├── conftest.py            # 复用 c4_fun_00065 的 fixture
└── test_stop_restart.py   # TC1~TC8
```

### 2.1 被测对象 (SUT)

Go 编译的 `c4_iec104_client` 二进制，通过 MCP stdio JSON-RPC 控制（同 c4_fun_00065）。

### 2.2 测试基础设施与数据流

复用 c4_fun_00065 的完整数据流（见其 README §2.2）：

```
redis_tool ──写值──> Redis ──MGET──> iec104d ──IEC104响应──> c4_iec104_client ──seqlock──> 共享内存
```

### 2.3 fixture 复用

`conftest.py` 通过 `importlib.util` 复用 `c4_fun_00065/conftest.py` 的 fixture
（`license_env`、`redis_server`、`write_redis`、`start_iec104d`、`prepare_environment`、
`start_iec104_client`），复用方式同 c4_fun_00012 复用 c4_fun_00062。

### 2.4 数据流验证（重启后）

重启后通过共享内存 `write_seq` 递增验证数据通路恢复：

1. **采集正常**：`write_seq` 持续递增（iec104d 上送 → 主站写入 shm）
2. **stop 后**：`write_seq` 停止递增（实例销毁，连接关闭，不再接收/写入）
3. **start 后**：`write_seq` 恢复递增（数据通路重建）

`write_seq` 递增即证明数据已通过完整路径：iec104d 上送 → 主站解析 → shm 写入。
同时验证 block 的 `magic == 0xC4DA7A00` 和 `state == 1`。

---

## 3. 测试配置模板

### 3.1 iec104d 配置（设备端）

同 c4_fun_00065 §3.1（单点遥测 `104addr: 16385`，`with_cp56time2a: 0`，`timer: 100`，
`acquisition_of_events_timer: 100`）。

### 3.2 c4_iec104_client 配置（SUT）

同 c4_fun_00065 §3.2（`gi_timer: 100`，`it_timer: 0`，1 个 point `addr: 16385`）。

### 3.3 期望的 shm_id 分配

1 个 point → `adjust_shm` 分配 `pt_a` → shm_id=1。

---

## 4. 测试用例

### TC1: stop — 运行中停止

- **前置**：按 c4_fun_00065 §3 完成准备，`start` 调用成功，数据采集正常（`write_seq` 递增）
- **操作**：
  1. 记录 shm_id=1 的 `write_seq` 为 `seq_before`，等待一轮确认递增
  2. 调用 `stop`，无参数
- **预期**：
  - `stop` 返回 `"success"`（`isError: false`）
  - 调用 `stop` 后，`write_seq` 停止递增（实例已销毁，连接关闭）
- **说明**：`stop` 销毁全部实例并释放连接

### TC2: stop — 未启动时调用（幂等）

- **前置**：`c4_iec104_client` 已 MCP initialize，但 `start` 从未调用过
- **操作**：调用 `stop`，无参数
- **预期**：返回 `"success"`（`isError: false`）——`stop` 幂等
- **说明**：与 `c4_asfp2_server`（返回 `SERVICE_NOT_READY`）不同，`c4_iec104_client` 的
  `stop` 幂等（同 `c4_modbus_client`）

### TC3: start — 已运行时重复调用

- **前置**：`start` 调用成功
- **操作**：再次调用 `start`（同一 SUT 进程，无间隔 `stop`）
- **预期**：`isError: true`，`content[0].text` 以 `ALREADY_RUNNING` 开头

### TC4: 简单重启（stop → start，无配置变更）

- **前置**：`start` 调用成功，数据采集正常
- **操作**：
  1. 调用 `stop` → 确认返回 `"success"`，`write_seq` 停止递增
  2. 调用 `start`（传入同一 config_path）
  3. 记录 `write_seq` 为 `seq_before`，`wait_write_seq_advanced` 等待递增
  4. 读 shm_id=1 的 `value` 比对期望值
- **预期**：
  - `stop` 返回 `"success"`
  - `start` 返回 `"success"`（`isError: false`）
  - 重启后 `write_seq` 恢复递增，`value` 正确
- **说明**：重启后 `start` 重新 `shm_open` + `mmap`，数据通路恢复正常

### TC5: 完整 Stop-Start 协议（stop → adjust_shm → start）

- **前置**：`start` 调用成功，数据采集正常
- **操作**：
  1. 调用 `stop` → 确认返回 `"success"`
  2. 启动 `c4_shm_manager`，调用 `adjust_shm`（同一 config_path）→ 返回 `"success"` → 关闭
  3. 调用 `start`（传入同一 config_path）
  4. 记录 `write_seq` 为 `seq_before`，等待递增，读 `value` 比对
- **预期**：
  - `stop` 返回 `"success"`
  - `adjust_shm` 返回 `"success"`
  - `start` 返回 `"success"`
  - `write_seq` 递增，`value` 正确
- **说明**：验证完整的 Stop-Start 三方协议：stop → shm_manager 调整 → start，三步全链路正确

### TC6: 多次 stop/start 循环

- **前置**：`start` 调用成功
- **操作**：
  1. `stop` → 验证 `"success"`
  2. `start` → 验证 `"success"`
  3. 重复 `stop` → `start` 共 2 轮
  4. 最后一轮 `start` 后验证数据流恢复（`write_seq` 递增）
- **预期**：
  - 全部 stop/start 调用均返回 `"success"`
  - 最后一轮重启后数据采集恢复正常
- **说明**：验证 stop/start 循环的幂等性——每次重启后数据通路均恢复正常

### TC7: 重启时配置变更生效

- **前置**：iec104d **预配两点**（`TF_TEST_AI001→16385`、`TF_TEST_AI002→16386`，均写 Redis key）；
  `start` 调用成功（c4 配置 1 个 point `addr: 16385`）
- **操作**：
  1. 调用 `stop` → 确认返回 `"success"`
  2. 修改 c4 配置：新增一个 point `addr: 16386`（`shm_id: 0`），写新配置文件
     （iec104d 侧点表已含 16386，**不随 c4 restart 变更**）
  3. 启动 `c4_shm_manager` → `adjust_shm`（为新 point 分配 shm_id）→ 关闭
  4. 调用 `start`（传入新 config_path）
  5. 记录新旧 point 的 `write_seq`，等待递增，读 `value` 比对
- **预期**：
  - `start` 返回 `"success"`
  - 旧 point（shm_id=1）数据正常写入
  - 新 point（shm_id=2）数据正常写入
- **说明**：验证 C4_FUN_00066 核心语义——"重启时 MCP 服务自动重新读取配置文件，根据配置变化调整采集点"，已有采集点不受影响

### TC8: 重启后数据流恢复（含值变化）

- **前置**：`start` 调用成功，`redis_tool` 已写 `TF_TEST_AI001 = 1.5`，采集到 `value = 1.5`
- **操作**：
  1. 调用 `stop` → `start`（简单重启）
  2. 记录 `write_seq` 为 `seq_before`，等待递增
  3. `redis_tool` 改写 `TF_TEST_AI001 = 2.5`，等待新一轮采集
  4. 读 shm_id=1 的 `value`
- **预期**：重启后数据采集恢复，`value = 2.5`（新值）
- **说明**：验证重启后不仅通路恢复，且能持续采集**更新后的**数据

---

## 5. 实现注意

### 5.1 复用 c4_fun_00065 的 fixture

`conftest.py` 通过 `importlib.util` 复用 `c4_fun_00065/conftest.py` 的 fixture，
复用方式与 c4_fun_00012 复用 c4_fun_00062 一致：

```python
import importlib.util, os

_src = os.path.join(os.path.dirname(__file__), "../c4_fun_00065/conftest.py")
_spec = importlib.util.spec_from_file_location("c4_fun_00065_conftest", _src)
_c65 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_c65)

license_env = _c65.license_env
redis_server = _c65.redis_server
write_redis = _c65.write_redis
start_iec104d = _c65.start_iec104d
prepare_environment = _c65.prepare_environment
start_iec104_client = _c65.start_iec104_client
```

### 5.2 adjust_shm 调用（TC5、TC7）

TC5 和 TC7 需要在 `stop` 和 `start` 之间调用 `c4_shm_manager.adjust_shm`。
单独启动 `c4_shm_manager` 子进程 → MCP initialize → 调用 `adjust_shm` → 关闭进程
（复用 c4_fun_00062 的 `_run_adjust_shm` helper）。

### 5.3 数据流验证（轮询重试）

- **正向断言**（`write_seq` 递增）用轮询重试（间隔 50ms，最长 3s），不依赖固定 sleep
- 数据流验证需等待 iec104d 读入周期（`timer=100ms`）+ 主站总召周期（`gi_timer=100ms`），
  故 `wait_write_seq_advanced` 的超时须 ≥ 3 个周期（建议 3s）

### 5.4 隔离性

- 每个 TC 独立 `instance_id`、独立 iec104d 端口、独立 Redis key 前缀
- fixture teardown：关闭 SUT、关闭 iec104d、shm_unlink、删除临时配置、清理 Redis key

### 5.5 禁止事项

- **不得调用 `status` 工具**
- 验证手段仅限：`start` 返回值、`stop` 返回值、共享内存 mmap 读取

# C4_FUN_00063 测试方案

> **对应功能**：`c4/docs/specification/c4_function.md` C4_FUN_00063
> **对应需求**：C4_RS_00090
> **设计参考**：`c4/docs/design/c4_modbus_client.md` §3.3

C4_FUN_00063：Modbus/TCP 采集 MCP 服务支持停止和重启 — Agent 可停止全部 Modbus/TCP Client 实例，配置文件调整后重新启动。重启时自动重读配置，根据配置变化调整采集地址和端口。

---

## 1. 测试目标

验证 `c4_modbus_client` 的 Stop-Start 协议：

1. `stop` 在运行状态返回 `"success"` 并停止轮询（释放 TCP 连接）
2. `stop` 在未启动状态幂等返回 `"success"`
3. `start` 在已运行状态返回 `ALREADY_RUNNING`
4. 简单重启（`stop` → `start`）后连接恢复、数据流恢复
5. 完整 Stop-Start 协议（`stop` → `adjust_shm` → `start`）
6. 多次 `stop`/`start` 循环正确
7. 连续两次 `stop`（double-stop）幂等
8. 重启时配置变更（新端口 / 新 point）生效
9. `start` 失败（CONNECT_FAILED）后修正配置恢复成功

---

## 2. 测试架构

```
c4/test/c4_fun_00063/
├── README.md              # 本文件
├── conftest.py            # 公共 fixture（复用 c4_fun_00062 的 redis+modbusd+授权 fixture）
├── shm_helpers.py         # 共享内存读写工具函数（复用 c4_fun_00057）
└── test_stop_restart.py   # TC1~TC9
```

### 2.1 被测对象 (SUT)

Go 编译的 `c4_modbus_client` 二进制，通过 MCP stdio JSON-RPC 控制（同 c4_fun_00062）。

### 2.2 复用设备端 fixture

复用 `c4_fun_00062/conftest.py` 的 `license_env`、`start_modbusd`、`write_redis` fixture。

### 2.3 停止/重启验证手段

- **轮询停止验证**：`stop` 后 shm block 的 `write_seq` 停止递增（轮询停止，连接已释放）。
  等待 2~3 个轮询周期（timer=100ms）后重读，断言 `write_seq` 不变
- **轮询恢复验证**：`start` 后 `write_seq` 恢复递增（数据流恢复）
- **连接建立验证**：`start` 返回 `"success"` 即证明连接已建立（§3.2 全部连接成功才 success）

---

## 3. 测试配置模板

### 3.1 标准配置（单实例 2 points）

**modbusd.json（设备端）**：

```json
{
    "engine": {"pwd": "/tmp/c4_test/modbusd_sr", "stop_check": 100},
    "log": {"dir": "log", "file": "log.log", "level": 1, "debug_time": 300, "size": 128},
    "modbus": {"ip": "127.0.0.1", "port": <port>, "hton_register": 1, "hton_total": 0, "swap": 0, "timer": 100},
    "redis": {"ip": "127.0.0.1", "port": 6379, "dbid": 0, "auth": "", "with_timestamp": 1, "precision": 6},
    "points": [
        {"key": "MB_PT_001", "modbusaddr": 1000, "funcode": 2, "type": 4},
        {"key": "MB_PT_002", "modbusaddr": 1002, "funcode": 2, "type": 4}
    ]
}
```

**c4_modbus_client.json（SUT）**：

```json
{
    "c4_shm_manager": {"writer": ["c4_modbus_client"], "reader": ["c4_asfp2_client"]},
    "c4_modbus_client": [{
        "name": "停止重启测试设备", "id": "sr_device",
        "ip": "127.0.0.1", "port": <port>,
        "t0": 5, "t1": 5, "retries": 3,
        "coils_quantity_max": 2000, "registers_quantity_max": 125,
        "hton_register": 1, "hton_total": 0, "timer": 100,
        "points": [
            {"id": "pt_a", "uid": 1, "addr": 1000, "fun": 3, "type": 4, "swap": 0, "shm_id": 0},
            {"id": "pt_b", "uid": 1, "addr": 1002, "fun": 3, "type": 4, "swap": 0, "shm_id": 0}
        ]
    }],
    "c4_asfp2_client": []
}
```

> 单寄存器 UINT16（type=4）仅受 `hton_register` 影响：modbusd 与 c4 的
> `hton_register` 必须一致（此处均 1），`swap` 均 0，避免跨寄存器字节序映射
> （见 c4_fun_00012 §3.3）带来的混淆。

### 3.2 变更后配置（新端口 + 新 point）

基于 §3.1，双侧同步变更：

- **modbusd 侧**：端口改为新端口 P2，points 新增
  `{"key": "MB_PT_003", "modbusaddr": 2000, "funcode": 2, "type": 4}`；
  redis_tool 写入 `MB_PT_003` 对应值
- **c4 侧**：`c4_modbus_client` 实例端口改为 P2，points 新增
  `{"id": "pt_c", "uid": 1, "addr": 2000, "fun": 3, "type": 4, "swap": 0, "shm_id": 0}`

### 3.3 期望的 shm_id 分配

2 个 point → shm_id=1（pt_a）、shm_id=2（pt_b）。新增 point 经 `adjust_shm`
分配 shm_id=3（已有点 shm_id 不变）。

---

## 4. 测试用例

### TC1: stop — 运行中停止，轮询停止

- **前置**：标准配置（§3.1）。modbusd 已启动，SUT 已 `start`，`write_seq` 在递增
- **操作**：
  1. 记录 `write_seq` 为 `seq_0`，轮询重试（间隔 50ms，最长 3s）确认 `write_seq > seq_0`（轮询进行中）
  2. 调用 `stop`
  3. 记录 `write_seq` 为 `seq_1`，等待 3 个轮询周期（300ms）后重读
- **预期**：
  - `stop` 返回 `"success"`
  - `write_seq` 保持 `seq_1` 不变（轮询已停止，连接已释放）
- **说明**：`stop` 关闭 TCP 连接、销毁实例，轮询随之停止

### TC2: stop — 未启动时调用

- **前置**：SUT MCP initialize 完成，`start` 从未调用
- **操作**：调用 `stop`
- **预期**：`isError: false`，返回 `"success"`（stop 幂等）

### TC3: start — 已运行时重复调用

- **前置**：标准配置，`start` 已成功
- **操作**：再次调用 `start`（无间隔 `stop`）
- **预期**：`isError: true`，`content[0].text` 以 `ALREADY_RUNNING` 开头

### TC4: 简单重启（stop → start，无配置变更）

- **前置**：标准配置，SUT 已 `start`
- **操作**：
  1. 调用 `stop` → `"success"`
  2. 调用 `start`（同一 config_path）→ `"success"`
  3. 记录 `write_seq` 为 `seq_before`，等待 ≥1 轮询周期
- **预期**：`start` 返回 `"success"`，`write_seq > seq_before`（数据流恢复）
- **说明**：`start` 在 `stop` 之后可再次调用，与首次启动复用同一逻辑

### TC5: 完整 Stop-Start 协议（stop → adjust_shm → start）

- **前置**：标准配置，SUT 已 `start`
- **操作**：
  1. 调用 `stop` → `"success"`
  2. 启动 `c4_shm_manager` → `adjust_shm(instance_id, config_path)` → `"success"` → 关闭
  3. 调用 `start`（同一 config_path）
  4. 记录 `write_seq` 为 `seq_before`，等待 ≥1 轮询周期
- **预期**：三步全链路正确，`write_seq > seq_before`（数据流恢复）
- **说明**：验证 §3.3 完整三方协议 stop → adjust_shm → start

### TC6: 多次 stop/start 循环

- **前置**：标准配置，SUT 已 `start`
- **操作**：`stop` → `start`（第 1 轮）→ `stop` → `start`（第 2 轮）→ `stop` → `start`（第 3 轮），
  每轮 `start` 后等待并确认 `write_seq` 恢复递增
- **预期**：全部 6 次调用返回 `"success"`，每轮重启后轮询均恢复

### TC7: double-stop — 连续两次 stop

- **前置**：标准配置，SUT 已 `start`
- **操作**：`stop`（成功）→ 再次 `stop`
- **预期**：两次均返回 `"success"`（stop 幂等）

### TC8: 重启时配置变更生效

- **前置**：标准配置（modbusd 端口 P1，2 points）。SUT 已 `start`
- **操作**：
  1. `stop` → `"success"`
  2. 修改配置（§3.2）：modbusd 端口改为 P2（在 P2 启动新 modbusd，或复用改端口），
     新增 point（addr=2000, shm_id=0）。启动 `c4_shm_manager` → `adjust_shm` → 关闭
     （新 point 分配 shm_id=3）
  3. `start`（新 config_path）
  4. 验证：返回 `"success"`；旧 point（addr=1000/1002）与新 point（addr=2000）的
     `write_seq` 均恢复递增
- **预期**：配置变更（端口 + 新 point）均已生效，采集恢复正常
- **说明**：验证 C4_FUN_00063 核心语义——"重启时自动重读配置，调整采集地址和端口"

### TC9: start 失败后错误恢复

- **前置**：标准配置，SUT 已 `start`
- **操作**：
  1. `stop` → `"success"`
  2. 配置改为不可达目标（`ip: "192.0.2.1", port: 502`）→ `adjust_shm`
  3. `start` → 预期 `isError: true`，`CONNECT_FAILED`
  4. 配置改回标准配置（可达 modbusd）→ `adjust_shm`
  5. `start` → 预期 `"success"`，轮询恢复（`write_seq` 递增）
- **预期**：失败后允许修正配置重试，无需重启 SUT 进程
- **说明**：`start` 失败后 SUT 回到未启动状态（§3.2 tear down），Agent 可修正配置重试

---

## 5. 实现注意

### 5.1 复用 c4_fun_00062 的 fixture

`conftest.py` 通过 `importlib.util` 复用 `c4_fun_00062/conftest.py` 的
`license_env`、`start_modbusd`、`write_redis`、`prepare_environment`、
`start_modbus_client` fixture（模式同 `c4_fun_00058` 复用 `c4_fun_00057`）：

```python
import importlib.util, os
_src = os.path.join(os.path.dirname(__file__), "../c4_fun_00062/conftest.py")
_spec = importlib.util.spec_from_file_location("c4_fun_00062_conftest", _src)
_c62 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_c62)
license_env = _c62.license_env
start_modbusd = _c62.start_modbusd
write_redis = _c62.write_redis
prepare_environment = _c62.prepare_environment
start_modbus_client = _c62.start_modbus_client
```

### 5.2 轮询停止/恢复验证

- 轮询恢复（正向断言）：`start` 后轮询重试（间隔 50ms，最长 3s）等待 `write_seq` 递增
- 轮询停止（负向断言）：`stop` 后等待 3 个轮询周期（300ms），断言 `write_seq` 不变——
  负向断言（无递增即成功）无法用轮询重试表达，只能用有界等待

### 5.3 shm_id 查找（TC8 新 point）

`adjust_shm` 回填配置文件中各 point 的 `shm_id`。TC8 在 `start` 前从磁盘配置 JSON
读取 `c4_modbus_client[0].points`，找到 `addr==2000` 的 point 取其 `shm_id`。

### 5.4 字节序一致

本方案统一用单寄存器 UINT16（type=4），modbusd 与 c4 的 `hton_register` 一致（均 1），
`swap` 均 0，避免跨寄存器字节序映射（见 c4_fun_00012 §3.3）带来的混淆。

### 5.5 隔离性

- 每个 TC 独立 `instance_id`、独立 modbusd 端口、独立 Redis key 前缀
- fixture teardown：关闭 SUT、关闭 modbusd、shm_unlink、删除临时配置、清理 Redis key
- 不同 TC 无状态依赖，pytest 可任意排序

### 5.6 禁止事项

- **不得调用 `status` 工具**
- 验证手段仅限：`start`/`stop` 返回值、共享内存 mmap 读取

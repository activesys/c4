# C4_FUN_00062 测试方案

> **对应功能**：`c4/docs/specification/c4_function.md` C4_FUN_00062
> **对应需求**：C4_RS_00090
> **设计参考**：`c4/docs/design/c4_modbus_client.md` §3, §6

C4_FUN_00062：Agent 生成 Modbus/TCP 采集 MCP 服务的配置文件后，经 Unix socket 调用该服务的
`start` 工具拉起数据路径实例（MCP 服务进程为常驻系统服务，不由 Agent 拉起——见
c4_architecture.md §3.1.1），MCP 服务根据配置文件启动多个 Modbus/TCP Client。

---

## 1. 测试目标

验证 `c4_modbus_client` 在收到 Agent 的 `start` 工具调用后：

1. 通过 `config_path` 参数获取配置文件路径并读取 `c4_modbus_client` 配置段
2. 校验配置有效性（shm_id 合法性、fun/addr/type/swap 合法性、point 区间不重叠）
3. 以 `O_RDWR` 附加已有共享内存并校验 magic
4. 构建 `(uid, fun, addr) → shm_id` 映射索引
5. 为每个配置实例启动一个 goroutine，`net.Dial` 主动连接 Modbus/TCP 设备（modbusd）
6. 实例进入运行态后 `start` 即返回 `"success"`；设备不可达不作为 tool 错误，由实例按 T0 周期后台重拨（连接成功不作为返回条件）
7. 各错误码正确返回

---

## 2. 测试架构

```
c4/test/c4_fun_00062/
├── README.md              # 本文件
├── conftest.py            # 公共 fixture（redis + modbusd + 授权 + SUT + 清理）
├── shm_helpers.py         # 共享内存读写工具函数（复用 c4_fun_00057）
└── test_start.py          # TC1~TC12
```

### 2.1 被测对象 (SUT)

Go 编译的 `c4_modbus_client` 二进制，通过 Python `subprocess.Popen` 启动，
走 **MCP stdio JSON-RPC** 协议。

### 2.2 测试基础设施：redis + modbusd + redis_tool

`c4_modbus_client` 是**主动连接的主站**（`net.Dial`），测试用真实的生产组件
`modbusd`（Modbus TCP 从站）+ Redis + `redis_tool` 构成设备端数据源：

```
redis_tool ──写值──> Redis ──MGET──> modbusd ──Modbus响应──> c4_modbus_client ──seqlock──> 共享内存
```

| 组件 | 角色 | 命令/说明 |
|------|------|----------|
| `redis-server` | 数据缓存 | Redis 服务，127.0.0.1:6379 |
| `redis_tool` | 数据写入 | `/usr/local/bin/redis_tool -s 127.0.0.1 -P <key> -w -V <value> -t 1` 写值到 Redis |
| `modbusd` | Modbus 从站 | `/usr/local/bin/modbusd -c <modbusd.json>`，从 Redis MGET 读值，响应主站读请求 |
| `c4_modbus_client` | SUT | 主站，轮询 modbusd，写入共享内存 |

### 2.3 modbusd 授权（前置条件）

modbusd 启动前执行 `license_check`，需要有效授权文件。授权链路：

```bash
# 1. 生成 RSA-2048 密钥对（目标机器上，生成于当前目录）
/usr/local/bin/license_tool
#   → 生成 <机器码>.public（公钥）+ <机器码>.private（私钥），机器码 = base64(首网卡MAC)，'/'→'-'

# 2. 用公钥签发许可（生成 license.dat 于当前目录）
/usr/local/bin/license_gen -c <机器码>.public -n "test_user"
#   → 默认过期时间 2083 年

# 3. 放置授权文件到 $ACQUISITION/license/ 目录
mkdir -p $ACQUISITION/license
cp <机器码>.private $ACQUISITION/license/
cp license.dat        $ACQUISITION/license/

# 4. 启动 modbusd 时设置 ACQUISITION 环境变量
ACQUISITION=/var/acquisition modbusd -c modbusd.json
```

modbusd 运行时从 `$ACQUISITION/license/<机器码>.private` + `$ACQUISITION/license/license.dat`
读取并校验授权（RSA 解密 + MAC 匹配 + 过期校验）。

> **测试环境**：本机已有 `/var/acquisition/license/`（`AAwpOG7-.private`、`AAwpOG7-.public`、
> `license.dat`）。conftest.py 应检测该目录是否有有效授权；若缺失或过期，则执行上述
> 3 步生成流程（在临时目录生成后拷贝到位）。授权为**会话级 fixture**（一次生成，全用例复用）。

### 2.4 关键配置映射

c4_modbus_client 的 `fun`（Modbus 功能码 1/2/3/4）与 modbusd 的 `funcode`（数据区 0/1/2/3）对应关系：

| c4 `fun` | Modbus 功能码 | modbusd `funcode` | 数据区 |
|----------|--------------|-------------------|--------|
| 1 | 0x01 Read Coils | 0 | 线圈 |
| 2 | 0x02 Read Discrete Inputs | 1 | 离散输入 |
| 3 | 0x03 Read Holding Registers | 2 | 保持寄存器 |
| 4 | 0x04 Read Input Registers | 3 | 输入寄存器 |

即 **c4 `fun` = modbusd `funcode` + 1**。

### 2.5 前置条件准备流程

```
1. 启动 redis-server（127.0.0.1:6379）
2. 生成 modbusd.json（points 映射 Redis key，funcode/type 见 §3）
3. 启动 modbusd（设置 ACQUISITION 环境变量）→ 监听 127.0.0.1:<port>
4. redis_tool 写入各 point 的 Redis key 值
5. 生成 c4_modbus_client 配置（ip/port 指向 modbusd）
6. 启动 c4_shm_manager → create_shm → adjust_shm（回填 shm_id）→ 关闭
7. 启动 c4_modbus_client → MCP initialize
8. 调用 start 工具
```

### 2.6 连接验证

T0 语义下 `start` 返回 `"success"` 只表示各实例已进入运行态；TCP 连接由各实例 goroutine
按 t0 周期后台建立/重拨（c4_modbus_client.md §3.2），连接成功不作为 `start` 返回条件。

连接成功的验证手段：实例的 shm block `write_seq` 持续递增（轮询写入成立即连接已建立）。
设备不可达场景（TC12）：c4 配置指向**无 modbusd 监听的端口**，该实例保持未连接态、
按 T0 周期后台重拨，`start` 仍返回 success。

### 2.7 conftest.py 导出 fixture 契约

以下 fixture 由 `c4_fun_00062/conftest.py` 定义并导出，供 `c4_fun_00012`、`c4_fun_00063`
通过 `importlib.util` 复用（复用方式见各自 README §5.1）：

| fixture | scope | 职责 |
|---------|-------|------|
| `license_env` | session | 确保 `$ACQUISITION/license/` 有有效授权（缺失时用 `license_tool`+`license_gen` 生成），返回 `ACQUISITION` 环境变量路径 |
| `start_modbusd` | function | 启动一个 modbusd 子进程（传 modbusd.json 路径 + `ACQUISITION` env），返回 `(process, port)`，teardown 时关闭 |
| `write_redis` | function | 调用 `redis_tool -s 127.0.0.1 -P <key> -w -V <value> -t 1` 写一个 Redis key 值 |
| `prepare_environment` | function | 生成配置 → 启动 c4_shm_manager → create_shm → adjust_shm（回填 shm_id）→ 关闭，返回 config_path |
| `start_modbus_client` | function | 启动 c4_modbus_client 子进程（MCP initialize），返回 MCP 客户端句柄 |

---

## 3. 测试配置模板

### 3.1 modbusd 配置（设备端）

```json
{
    "engine": {"pwd": "/tmp/c4_test/modbusd_tc1", "stop_check": 100},
    "log": {"dir": "log", "file": "log.log", "level": 1, "debug_time": 300, "size": 128},
    "modbus": {"ip": "127.0.0.1", "port": <port>, "hton_register": 1, "hton_total": 0, "swap": 0, "timer": 100},
    "redis": {"ip": "127.0.0.1", "port": 6379, "dbid": 0, "auth": "", "with_timestamp": 1, "precision": 6},
    "points": [
        {"key": "MB_PT_001", "modbusaddr": 1000, "funcode": 2, "type": 4}
    ]
}
```

> 单寄存器 UINT16（type=4）仅受 `hton_register` 影响：modbusd 与 c4 的
> `hton_register` 必须一致（此处均 1），`swap` 均 0。`modbus.timer=100`（100ms）
> 加速 DAM 从 Redis 刷新。

### 3.2 c4_modbus_client 配置（SUT）

```json
{
    "c4_shm_manager": {"writer": ["c4_modbus_client"], "reader": ["c4_asfp2_client"]},
    "c4_modbus_client": [{
        "name": "启动测试设备", "id": "test_device",
        "ip": "127.0.0.1", "port": <port>,
        "t0": 5, "t1": 5, "retries": 3,
        "coils_quantity_max": 2000, "registers_quantity_max": 125,
        "hton_register": 1, "hton_total": 0, "timer": 100,
        "points": [
            {"id": "pt_a", "uid": 1, "addr": 1000, "fun": 3, "type": 4, "swap": 0, "shm_id": 0}
        ]
    }],
    "c4_asfp2_client": []
}
```

> 测试用 `timer=100`（100ms）加速轮询；生产约束 1Hz。`t0/t1=5`、`retries=3` 缩短失败路径。

### 3.3 期望的 shm_id 分配

1 个 point → `adjust_shm` 计算 `writer_points=1`，`max_points=2`，分配 `pt_a` → shm_id=1。

---

## 4. 测试用例

### TC1: 基本启动 — 单实例单点连接成功

- **前置**：启动 redis + modbusd（1 point，§3.1），redis_tool 写 `MB_PT_001` 值，c4 配置（§3.2）
- **操作**：启动 SUT → MCP initialize → 调用 `start`（传入 config_path）
- **预期**：`start` 返回 `"success"`（`isError: false`）
- **说明**：`start` 使实例进入运行态即返回 `"success"`；modbusd 连接由实例按 T0 周期后台建立，
  通过 `write_seq` 递增验证连接与轮询正常

### TC2: 多实例启动 — 3 个实例各自连接

- **前置**：1 个 modbusd（3 point），c4 配置 3 个实例（`id` 分别为 `dev1`/`dev2`/`dev3`，
  均指向同一 modbusd，各 1 个 point，addr 不同）
- **操作**：调用 `start`
- **预期**：返回 `"success"`
- **说明**：3 个 goroutine 各自 `net.Dial` 连接同一 modbusd（modbusd 支持多客户端并发连接）

### TC3: 空实例列表 — 0 个实例

- **前置**：配置 `"c4_modbus_client": []`（空数组）
- **操作**：调用 `start`
- **预期**：返回 `"success"`（无实例需连接，但仍需 shm_open + mmap + magic 校验）

### TC4: 重复调用 start → ALREADY_RUNNING（幂等成功）

- **前置**：TC1 已成功启动
- **操作**：再次调用 `start`（同一 SUT 进程，无间隔 `stop`）
- **预期**：`isError: false`，`content[0].text` 含 `ALREADY_RUNNING`——重复 start 属成功
  路径的幂等结果，**不是错误**（c4_architecture.md §3.1.2）；全部实例连接保持，轮询不中断

### TC5: start 未调用前调用 stop → 幂等 success

- **前置**：启动 SUT，MCP initialize 完成，但未调用 `start`
- **操作**：调用 `stop`
- **预期**：返回 `"success"`（`isError: false`）——`stop` 幂等

### TC6: config_path 缺失 → CONFIG_PATH_MISSING

- **前置**：启动 SUT，MCP initialize，共享内存正常
- **操作**：调用 `start`，提供 `instance_id` 但不提供 `config_path` 参数（`arguments: {"instance_id": "c4_fun62tc6"}`）
- **预期**：`isError: true`，错误码 `CONFIG_PATH_MISSING`

### TC7: 配置文件格式错误

- **前置**：共享内存正常。`pytest.mark.parametrize` 子场景：
  - (a) JSON 语法错误：`{invalid json`
  - (b) 合法 JSON 但缺 key：`{"c4_shm_manager": {...}}`（无 `c4_modbus_client` 段）
- **操作**：调用 `start`（传入对应 config_path）
- **预期**：
  - (a) `isError: true`，错误码 `CONFIG_PARSE_ERROR`——`CONFIG_PARSE_ERROR` 仅针对文件不可读
    或 JSON 非法（c4_architecture.md §3.3.1）
  - (b) 空/缺失配置段 = 期望状态零实例（合法），`start` 幂等返回 `"success"`（`isError: false`），
    不启动任何实例（c4_architecture.md §3.1.2/§3.3.1 空配置段语义）

### TC8: 共享内存不存在 → SHM_OPEN_FAILED

- **前置**：不创建共享内存（跳过 create_shm + adjust_shm），但**手工将配置中各 point 的
  `shm_id` 置为非 0 值**（如 1，模拟 adjust_shm 已回填），modbusd 已启动
- **操作**：调用 `start`
- **预期**：`isError: true`，错误码 `SHM_OPEN_FAILED`
- **说明**：跳过 adjust_shm 会让 shm_id 停留为 0，而 start 的校验顺序是「先校验配置
  （含 shm_id）再 shm_open」——shm_id=0 会先触发 `SHM_ID_NOT_ASSIGNED`。故须手工置非 0
  shm_id，才能让配置校验通过、进入 `shm_open` 失败分支

### TC9: 共享内存 magic 损坏 → SHM_CORRUPTED

- **前置**：c4_shm_manager 已创建 shm，Python 将 Header magic 改为 `0xDEADBEEF`
- **操作**：调用 `start`
- **预期**：`isError: true`，错误码 `SHM_CORRUPTED`

### TC10: shm_id 未分配（=0）→ SHM_ID_NOT_ASSIGNED

- **前置**：c4_shm_manager 完成 `create_shm`，但**跳过 `adjust_shm`**（配置中 shm_id 仍为 0），modbusd 已启动
- **操作**：调用 `start`
- **预期**：`isError: true`，错误码 `SHM_ID_NOT_ASSIGNED`

### TC11: point 字段非法 → INVALID_POINT

- **前置**：共享内存正常，modbusd 已启动。`pytest.mark.parametrize` 子场景：

  | 子场景 | 非法字段 | 配置 |
  |--------|---------|------|
  | (a) fun 非法 | `fun: 6` | 功能码超出 1~4 |
  | (b) type 非法 | `fun:3, type:1` | INT8 不适用于寄存器 |
  | (c) swap 非法 | `fun:3, type:10(FLOAT32), swap:3` | swap=3 不整除 count=4 |
  | (d) 重复 (uid,fun,addr) | 两个 point 均 `uid:1, fun:3, addr:1000` | 三元组重复 |
  | (e) point 区间重叠 | type:10(span=2) addr=1000 与 addr=1001 | 区间重叠 |

- **操作**：调用 `start`
- **预期**：`isError: true`，错误码 `INVALID_POINT`，消息指明具体字段与取值

### TC12: 设备不可达 — start 成功，实例按 T0 周期后台重拨

- **前置**：共享内存正常。配置 2 个实例：实例 1 指向正常 modbusd，实例 2 指向无监听的端口
- **操作**：调用 `start`
- **预期**：
  - `start` 返回 `"success"`（`isError: false`）——连接成功不作为返回条件；不可达的实例 2 保持
    未连接态，按 T0 周期后台重拨（c4_modbus_client.md §3.2/§6.1）
  - 实例 1 不受影响，正常轮询写入
  - 随后调用 `stop` 幂等返回 `"success"`
- **验证**：
  1. `start` 返回后记录实例 1 对应 shm block（shm_id=1）的 `write_seq` 为 `seq_before`
  2. 等待 3 个轮询周期（300ms）后重读，断言 `write_seq > seq_before`
     （实例 1 正常轮询写入；业务层网络错误不影响其他实例）
- **说明**：验证 T0 语义（c4_modbus_client.md §3.2/§6.1、c4_architecture.md §3.3.1 错误分类）：
  `start` 使实例进入运行态即返回 success；TCP 连接失败属运行态业务错误，仅记录日志并按
  t0 周期重连，禁止作为 tool 错误返回，也禁止 tear down 已连接实例。注：若实例 1 在
  观察窗口内尚未建立连接（`write_seq` 未递增），属弱信号，可延长等待窗口重试。

---

## 5. 实现注意

### 5.1 授权 fixture（会话级）

- conftest.py 提供 `license_env` fixture（`scope="session"`）：
  1. 检测 `/var/acquisition/license/` 是否有 `<机器码>.private` + `license.dat`
  2. 缺失时：临时目录运行 `license_tool` → `license_gen -c <code>.public -n test_user`
     → 拷贝 `<code>.private` + `license.dat` 到授权目录
  3. 返回 `ACQUISITION` 环境变量值（如 `/var/acquisition`）
- modbusd 启动时以 `env={"ACQUISITION": ...}` 传入
- 生成授权文件需 root 权限（授权目录通常归 root）；测试以 root 或 sudo 运行

### 5.2 modbusd 启动

- `subprocess.Popen(["modbusd", "-c", modbusd_json], env={..., "ACQUISITION": license_dir})`
- modbusd 前台运行；SIGINT/SIGTERM 优雅退出
- 启动后轮询等待端口监听（`socket.create_connection` 重试）
- 每个测试用例独立 modbusd 进程（function scope），独立动态端口 + 独立 `engine.pwd` 工作目录

### 5.3 redis_tool 写值

- 写值命令：`redis_tool -s 127.0.0.1 -P <key> -w -V <value> -t 1`
- **不带 `-n`**：写二进制结构体 `{double value; uint64_t timestamp}`，与 modbusd
  `with_timestamp=1` 匹配
- redis-server 可用 systemd 服务或测试进程；测试需确认 6379 端口可达

### 5.4 隔离性

- 每个 TC 独立 `instance_id`（共享内存路径不冲突）、独立 modbusd 端口、独立 Redis key 前缀
- fixture teardown：关闭 SUT、关闭 modbusd、shm_unlink、删除临时配置、清理 Redis key

### 5.5 禁止事项

- **不得调用 `status` 工具**：该接口后续有调整，测试用例中不得使用
- 验证手段仅限：`start` 返回值、`stop` 返回值、共享内存 mmap 读取

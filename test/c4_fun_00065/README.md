# C4_FUN_00065 测试方案

> **对应功能**：`c4/docs/specification/c4_function.md` C4_FUN_00065
> **对应需求**：C4_RS_00091
> **设计参考**：`c4/docs/design/c4_iec104_client.md` §2, §3, §6

C4_FUN_00065：Agent 生成 IEC104 采集 MCP 服务的配置文件后，启动 MCP 服务，MCP 服务根据配置文件启动多个 IEC104 Client（主站）。

---

## 1. 测试目标

验证 `c4_iec104_client` 在收到 Agent 的 `start` 工具调用后：

1. 通过 `config_path` 参数获取配置文件路径并读取 `c4_iec104_client` 配置段
2. 校验配置有效性（shm_id 合法性、addr 合法性、addr 实例内唯一、t2<t1、modules=32768、ioa_size ∈ {1,2,3}）
3. 以 `O_RDWR` 附加已有共享内存并校验 magic
4. 构建 `(instance, ioa) → shm_id` 映射索引
5. 为每个配置实例启动一个 goroutine，**异步**发起连接和 STARTDT 激活
6. **`start` 不等待连接/握手**——所有实例均已启动即返回 `"success"`，连接失败/STARTDT 失败属运行时事件（记日志 + 重连），不导致 `start` 返回错误
7. 各错误码正确返回

> **与 `c4_modbus_client` 的关键差异**：modbus 的 `start` 要求「全部连接成功才 success、任一失败 tear down 并返回 `CONNECT_FAILED`」；IEC104 的 `start` **不等待连接**（见 [c4_iec104_client.md §6.1](c4_iec104_client.md) 与 [c4_architecture.md §3.3.1](c4_architecture.md) 返回时机语义），故**无 `CONNECT_FAILED` 错误码**，取而代之的是「设备不可达时 `start` 仍返回 `success`」（TC13）。

---

## 2. 测试架构

```
c4/test/c4_fun_00065/
├── README.md              # 本文件
├── conftest.py            # 公共 fixture（复用 c4_fun_00062 的 redis+授权 fixture，新增 iec104d 从站 + SUT）
└── test_start.py          # TC1~TC13
```

### 2.1 被测对象 (SUT)

Go 编译的 `c4_iec104_client` 二进制，通过 Python `subprocess.Popen` 启动，
走 **MCP stdio JSON-RPC** 协议（同 c4_fun_00062）。

### 2.2 测试基础设施：redis + iec104d + redis_tool

`c4_iec104_client` 是**主动连接的主站**（`net.Dial`），测试用真实的生产组件
`iec104d`（IEC104 从站，`/usr/local/bin/iec104d`）+ Redis + `redis_tool` 构成设备端数据源：

```
redis_tool ──写值──> Redis ──MGET──> iec104d ──IEC104响应──> c4_iec104_client ──seqlock──> 共享内存
```

| 组件 | 角色 | 命令/说明 |
|------|------|----------|
| `redis-server` | 数据缓存 | Redis 服务，127.0.0.1:6379 |
| `redis_tool` | 数据写入 | `redis_tool -s 127.0.0.1 -P <key> -w -V <value> -t 1` 写值到 Redis（`-t 1`=写 1 次即退出；不带 `-n` 时写二进制结构体 `{double value; uint64_t timestamp}`） |
| `iec104d` | IEC104 从站 | `/usr/local/bin/iec104d -c <iec104d.json>`，从 Redis MGET 读值，作为从站（被控站）响应主站 |
| `c4_iec104_client` | SUT | 主站（控制站），连接 iec104d，总召采集，写入共享内存 |

### 2.3 iec104d 授权（前置条件）

iec104d 启动前执行 `license_check`，需要有效授权文件，机制与 modbusd 完全一致
（`$ACQUISITION/license/<机器码>.private` + `license.dat`）。复用 `c4_fun_00062` 的
`license_env` fixture（会话级，缺失时用 `license_tool`+`license_gen` 生成）。

### 2.4 104addr 与类型标识映射（关键）

iec104d 按 point 的 `104addr`（即 IOA）范围划分数据类型，上送对应类型标识：

| 104addr 范围 | 数据类型 | 上送类型标识（`with_cp56time2a=0`） | 上送类型标识（`with_cp56time2a=1`） | c4 映射 block.type |
|-------------|---------|------------------------------------|------------------------------------|-------------------|
| 1 ~ 16384 | 遥信 YX（单点） | M_SP_NA_1 (1) | M_SP_TB_1 (30) | BOOLEAN (0) |
| 16385 ~ 25600 | 遥测 YC（短浮点） | M_ME_NC_1 (13) | M_ME_TF_1 (36) | FLOAT32 (10) |
| 25601 ~ 28672 | 遥脉 YM（累计量） | M_IT_NA_1 (15) | M_IT_TB_1 (37) | INT32 (5) |

> 上表依据 [c4_iec104_client.md §4.3](c4_iec104_client.md) 类型映射与 iec104d 的
> `station.c`（`with_cp56time2a ? M_*_TB_1/TF_1 : M_*_NA_1/NC_1`）。iec104d 只上送这三类数据，
> 故测试点表 `104addr` 只能落在上述三个区间。

### 2.5 前置条件准备流程

```
1. 启动 redis-server（127.0.0.1:6379）
2. 生成 iec104d.json（points 映射 Redis key → 104addr，见 §3.1）
3. 启动 iec104d（设置 ACQUISITION 环境变量）→ 监听 127.0.0.1:<port>
4. 生成 c4_iec104_client 配置（ip/port 指向 iec104d，见 §3.2）
5. 启动 c4_shm_manager → create_shm → adjust_shm（回填 shm_id）→ 关闭
6. 启动 c4_iec104_client → MCP initialize
7. 调用 start 工具
```

### 2.6 连接验证

`start` 返回 `"success"` 仅证明「所有实例的 goroutine 已启动」，**不证明连接已建立**（设计文档 §3.1：
start 不等待连接）。连接建立与否无法通过 `start` 返回值直接观察——TC13 验证「设备不可达时 start 仍返回 success」。

### 2.7 conftest.py 导出 fixture 契约

以下 fixture 由 `c4_fun_00065/conftest.py` 定义并导出，供 `c4_fun_00066`、`c4_fun_00013`
通过 `importlib.util` 复用（复用方式见各自 README §5.1）：

| fixture | scope | 职责 |
|---------|-------|------|
| `license_env` | session | 复用 c4_fun_00062——确保 `$ACQUISITION/license/` 有有效授权 |
| `redis_server` | session | 复用 c4_fun_00062——确保 redis-server 在 127.0.0.1:6379 可达 |
| `write_redis` | function | 复用 c4_fun_00062——`redis_tool` 写一个 Redis key 值 |
| `start_iec104d` | function | 启动一个 iec104d 从站子进程（传 iec104d.json 路径 + `ACQUISITION` env），返回 `(process, port)`，teardown 时关闭 |
| `prepare_environment` | function | 复用 c4_fun_00062——生成配置 → create_shm → adjust_shm（回填 shm_id）→ 关闭，返回 config_path |
| `start_iec104_client` | function | 启动 c4_iec104_client 子进程（MCP initialize），返回 MCP 客户端句柄 |

---

## 3. 测试配置模板

### 3.1 iec104d 配置（设备端，单点遥测 FLOAT32）

```json
{
    "engine": {"pwd": "/tmp/c4_test/iec104d_tc1", "stop_check": 100},
    "log": {"dir": "log", "file": "log.log", "level": 1, "debug_time": 300, "size": 128},
    "iec104": {
        "ip": "127.0.0.1", "port": <port>,
        "k": 12, "w": 8,
        "t0": 30, "t1": 15, "t2": 10, "t3": 20,
        "modules": 32768,
        "common_address": 1,
        "with_cp56time2a": 0,
        "acquisition_of_events_timer": 100,
        "cyclic_data_transmission_timer": 0,
        "timer": 100
    },
    "redis": {
        "ip": "127.0.0.1", "port": 6379, "dbid": 0, "auth": "",
        "with_timestamp": 1, "precision": 6
    },
    "points": [
        {"key": "TF_TEST_AI001", "104addr": 16385}
    ]
}
```

> `timer=100`（100ms）加速 Redis 读取；`acquisition_of_events_timer=100` 加速事件上送；
> `cyclic_data_transmission_timer=0` 禁用循环上送。`with_cp56time2a=0` 上送无时标类型。

### 3.2 c4_iec104_client 配置（SUT）

```json
{
    "c4_shm_manager": {"writer": ["c4_iec104_client"], "reader": ["c4_asfp2_client"]},
    "c4_iec104_client": [{
        "name": "启动测试主变", "id": "test_transformer",
        "ip": "127.0.0.1", "port": <port>,
        "k": 12, "w": 8,
        "t0": 5, "t1": 5, "t2": 3, "t3": 5,
        "modules": 32768,
        "common_address": 1,
        "ioa_size": 3,
        "discard_cp56time2a": 0,
        "ignore_qds": 0,
        "it_timer": 0,
        "gi_timer": 100,
        "points": [
            {"id": "pt_a", "addr": 16385, "shm_id": 0}
        ]
    }],
    "c4_asfp2_client": []
}
```

> 测试用 `gi_timer=100`（100ms）加速总召；`t0/t1=5、t2=3、t3=5` 缩短失败路径。生产约束 `gi_timer=1000`（1Hz）。
> 注意 `t2 < t1`（3 < 5）。

### 3.3 期望的 shm_id 分配

1 个 point → `adjust_shm` 计算 `writer_points=1`，分配 `pt_a` → shm_id=1。

---

## 4. 测试用例

### TC1: 基本启动 — 单实例单点

- **前置**：启动 redis + iec104d（1 point，§3.1），c4 配置（§3.2）
- **操作**：启动 SUT → MCP initialize → 调用 `start`（传入 config_path）
- **预期**：`start` 返回 `"success"`（`isError: false`）
- **说明**：`start` 仅要求 goroutine 启动即返回 success（不等待连接）

### TC2: 多实例启动 — 3 个实例

- **前置**：1 个 iec104d（3 point，3 个不同 104addr），c4 配置 3 个实例
  （`id` 分别为 `dev1`/`dev2`/`dev3`，均指向同一 iec104d，各 1 个 point）
- **操作**：调用 `start`
- **预期**：返回 `"success"`
- **说明**：3 个 goroutine 各自 `net.Dial` 连接同一 iec104d（iec104d 支持多客户端并发连接）

### TC3: 空实例列表 — 0 个实例

- **前置**：配置 `"c4_iec104_client": []`（空数组）
- **操作**：调用 `start`
- **预期**：返回 `"success"`（无实例需启动，但仍需 shm_open + mmap + magic 校验）

### TC4: 重复调用 start → ALREADY_RUNNING

- **前置**：TC1 已成功启动
- **操作**：再次调用 `start`（同一 SUT 进程，无间隔 `stop`）
- **预期**：`isError: true`，`content[0].text` 以 `ALREADY_RUNNING` 开头

### TC5: start 未调用前调用 stop → 幂等 success

- **前置**：启动 SUT，MCP initialize 完成，但未调用 `start`
- **操作**：调用 `stop`
- **预期**：返回 `"success"`（`isError: false`）——`stop` 幂等

### TC6: config_path 缺失 → CONFIG_PATH_MISSING

- **前置**：启动 SUT，MCP initialize，共享内存正常
- **操作**：调用 `start`，提供 `instance_id` 但不提供 `config_path` 参数
- **预期**：`isError: true`，错误码 `CONFIG_PATH_MISSING`

### TC7: 配置文件格式错误 → CONFIG_PARSE_ERROR

- **前置**：共享内存正常。`pytest.mark.parametrize` 子场景：
  - (a) JSON 语法错误：`{invalid json`
  - (b) 合法 JSON 但缺 key：`{"c4_shm_manager": {...}}`（无 `c4_iec104_client` 段）
- **操作**：调用 `start`（传入对应 config_path）
- **预期**：`isError: true`，错误码 `CONFIG_PARSE_ERROR`

### TC8: 共享内存不存在 → SHM_OPEN_FAILED

- **前置**：不创建共享内存（跳过 create_shm + adjust_shm），但**手工将配置中各 point 的
  `shm_id` 置为非 0 值**（如 1，模拟 adjust_shm 已回填），iec104d 已启动
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

- **前置**：c4_shm_manager 完成 `create_shm`，但**跳过 `adjust_shm`**（配置中 shm_id 仍为 0），iec104d 已启动
- **操作**：调用 `start`
- **预期**：`isError: true`，错误码 `SHM_ID_NOT_ASSIGNED`

### TC11: 实例级字段非法 → INVALID_CONFIG

- **前置**：共享内存正常，iec104d 已启动。`pytest.mark.parametrize` 子场景：

  | 子场景 | 非法字段 | 配置 |
  |--------|---------|------|
  | (a) t2 ≥ t1 | `t1: 5, t2: 5` | 违反 t2 < t1 |
  | (b) ioa_size 非法 | `ioa_size: 4` | 超出 1/2/3 |
  | (c) modules 非法 | `modules: 1000` | 非 32768 |

- **操作**：调用 `start`
- **预期**：`isError: true`，错误码 `INVALID_CONFIG`，消息指明具体字段与取值

### TC12: point 字段非法 → INVALID_POINT

- **前置**：共享内存正常，iec104d 已启动。`pytest.mark.parametrize` 子场景：

  | 子场景 | 非法字段 | 配置 |
  |--------|---------|------|
  | (a) addr 越界 | `ioa_size:3, addr: 16777216` | 超出 3 字节范围 0~16777215 |
  | (b) addr 实例内重复 | 两个 point 均 `addr: 16385` | 同实例 IOA 重复 |

- **操作**：调用 `start`
- **预期**：`isError: true`，错误码 `INVALID_POINT`，消息指明具体字段与取值

### TC13: 设备不可达 — start 仍返回 success（不等待连接）

- **前置**：共享内存正常（create_shm + adjust_shm 完成）。配置指向**无 iec104d 监听的端口**
- **操作**：调用 `start`
- **预期**：`start` 返回 `"success"`（`isError: false`）
- **说明**：验证设计文档 §3.1「start 不等待连接」。连接失败是运行时事件（记日志 + t0 周期重连），
  **不导致 `start` 返回错误**——这是与 modbus 的 `CONNECT_FAILED` 语义的根本差异。

---

## 5. 实现注意

### 5.1 复用 c4_fun_00062 的基础 fixture

`conftest.py` 通过 `importlib.util` 复用 `c4_fun_00062/conftest.py` 的 fixture
（`license_env`、`redis_server`、`write_redis`、`prepare_environment`、`isolated_shm`、
`McpClient` 及各类 helper），复用方式同 c4_fun_00012 复用 c4_fun_00062。新增：

- `start_iec104d`：启动 iec104d 子进程（`iec104d -c <config>` + `ACQUISITION` env），
  轮询等待端口监听，teardown 关闭进程 + 清理工作目录
- `start_iec104_client`：启动 c4_iec104_client（`_find_iec104_client_binary` 查找/编译，
  编译失败 `pytest.skip`）
- 配置工厂：`_make_iec104d_config` / `_make_iec104d_point` / `_make_c4_config` /
  `_make_c4_instance` / `_make_c4_point`

### 5.2 iec104d 启动

- `subprocess.Popen(["iec104d", "-c", iec104d_json], env={..., "ACQUISITION": license_dir})`
- iec104d 前台运行；SIGINT 优雅退出
- 启动后轮询等待端口监听（`socket.create_connection` 重试）
- 每个测试用例独立 iec104d 进程（function scope），独立动态端口 + 独立 `engine.pwd` 工作目录

### 5.3 redis_tool 写值

- 写值命令：`redis_tool -s 127.0.0.1 -P <key> -w -V <value> -t 1`
- `-t 1` 表示写 1 次后退出（默认 `-t 0` 为无限循环写，必须指定）；`-T` 才是时间戳参数（未指定时自动取当前时间）
- **不带 `-n`**：写二进制结构体 `{double value; uint64_t timestamp}`（16 字节，double 本机序、
  timestamp 经 `hton64` 网络序），与 iec104d `with_timestamp=1` 匹配（同 modbusd）
- Redis key 须与 iec104d points 的 `key` 字段一致

### 5.4 隔离性

- 每个 TC 独立 `instance_id`（共享内存路径不冲突）、独立 iec104d 端口、独立 Redis key 前缀
- fixture teardown：关闭 SUT、关闭 iec104d、shm_unlink、删除临时配置、清理 Redis key

### 5.5 禁止事项

- **不得调用 `status` 工具**：该接口后续有调整，测试用例中不得使用
- 验证手段仅限：`start` 返回值、`stop` 返回值、共享内存 mmap 读取

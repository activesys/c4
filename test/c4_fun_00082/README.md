# C4_FUN_00082 测试方案

> **对应功能**：`docs/specification/c4_function.md` C4_FUN_00082（Agent 支持按点位查询实时数据快照）
> **对应需求**：C4_RS_00054
> **设计参考**：`docs/design/agent.md` §3.6（PointDisplayService）、`docs/design/c4_shm_manager.md` §3.3（`read_points` 工具）、`docs/design/c4_architecture.md` §2.2/§2.2.3

数据读取通道两层：**`read_points` MCP 工具契约**（§2，✅ 确定性）与 **快照状态标注**
（§3 经 Agent REST，✅ 确定性）。自然语言话术（"3 秒前刷新"等）与单次快照的
2s 突发采样（由 LLM 查询触发）为 LLM 层（❌ LLM 推理），不在本方案正测范围。

---

## 1. 测试栈（自建，不使用 systemd c4-agent）

> **硬约束**：Agent 读取的配置在固定路径 `~/.local/c4/config.json`（生产），且 shm 文件
> 权限为创建者 0600——测试**不得**触碰生产 Agent/配置/shm。conftest 以 function 级
> fixture 自建完整测试栈（参照 `c4/test/agent/README.md` §2.2 约定）：

1. `tmp_path` 写入测试 `agent.json`：`instance_id=c4_ft82`、`server.port` 取空闲端口、
   `shm_manager.config_path` 指向 tmp 测试 config.json；二进制路径来自环境变量
   `C4_AGENT_PATH` / `C4_SHM_MANAGER_PATH`（缺省 `/usr/local/bin/…`）
2. spawn `c4_shm_manager`（§2 直连栈，独立实例 `c4_ft82s`）与
   `c4_agent --config-dir <tmp>`（§3 REST 栈）；轮询 `GET /api/state` 直至就绪
3. teardown：SIGTERM→SIGKILL **整组进程**（Agent、Agent 自启的 c4_shm_manager、§2 直连的
   独立 c4_shm_manager——按进程组清理，防 SIGKILL 孤儿）+ `shm_unlink(/dev/shm/{instance_id})`
4. 测试以**普通用户**运行（无 sudo）；REST base URL 从 AgentHandle 读取（不硬编码 9988）；
   shm 路径为 `/dev/shm/c4_ft82`（非生产 `c4_main`）

## 2. read_points 工具契约（test_read_points.py，TC1~TC12 + TC11a~c）

stdio JSON-RPC 直连自建的 `c4_shm_manager`（握手流程参照 `c4_fun_00053/conftest.py`
McpClient）；shm 数据由测试按 seqlock 直写播种（`write_point` helper，§4）。

| TC | 场景 | 输入 | 预期 |
|----|------|------|------|
| TC1 | 单点正常读取 | 预写块（state=1, type=UINT32, value=7256, ts=now） | `status=ok`，value=7256，timestamp_ms=预写值，seq=写入 seq |
| TC2 | no_data | 块从未写入（state=0） | `status=no_data`，无 value 字段 |
| TC3 | 批量混合 | shm_ids=[已激活, 未激活] | reads 含 ok 与 no_data 各一条，互不影响 |
| TC4 | BOOLEAN/BIT 解码 | 预写 value=1（位 0 置位） | value=1；`value_raw == "1"`（十进制串） |
| TC5 | 有符号整型解码 | 预写 INT16 = -5 | value=-5（符号扩展） |
| TC6 | 无符号零扩展 | 预写 UINT16 = 0xFFFB | value=65531（零扩展，不为负；0xFFFB = 65531） |
| TC7 | FLOAT32 解码 | 预写 1.5 的 IEEE 位型（本机序低 4 字节） | value=1.5 |
| TC8 | **FLOAT16 特例** | 预写 float32 位型 0x3FC00000（低 4 字节） | value=1.5（按 f32 位模式解释低 4 字节）；回归锚点另见 `func_test_case.md` 用例 15（server 写入路径） |
| TC9 | FLOAT64 解码 | 预写 3.14（8 字节） | value≈3.14 |
| TC10 | value_raw 权威位型 | 预写 UINT64 = 2^63 | `value_raw == "9223372036854775808"` |
| TC11 | 越界 | shm_ids=[max_points+1] | `SHM_ID_OUT_OF_RANGE`（isError=true） |
| TC11a | 空列表 | shm_ids=[] | 校验拒绝（`SHM_ID_OUT_OF_RANGE`，isError=true——空数组通过 schema 校验，落入业务层检查） |
| TC11b | 数量超限 | shm_ids 共 1001 个（合法 id） | 校验拒绝，同上（schema 无 maxItems，落入业务层检查） |
| TC11c | header 块 | shm_ids=[0]（schema minimum 1） | 请求被拒绝——**JSON-RPC `-32602` schema 校验错误**（shm_manager.md §3 约定）或业务层 `isError=true`，二者皆视为通过 |
| TC12 | seqlock 稳定性 | 并发写线程持续写入时读 100 次 | 每次返回 ok 或 errors[contention]，**永不返回撕裂数据**（value ∈ 已写值集合） |

## 3. 快照状态标注（test_snapshot_status.py，TC13~TC17）

经 `POST /api/display` 建立单点会话后按 `intervalMs` **轮询+截止时间**断言
（REST 契约见 agent.md §3.6.5）：

| TC | 场景 | 预置 | 预期 |
|----|------|------|------|
| TC13 | 正常 | 每 500ms 写一次的写线程 | `state=ok`，timestampMs 随写推进 |
| TC14 | 暂无数据 | 点不写（块 state=0） | `state=no_data`；value 为 null 或缺省，**不得为数值** |
| TC15 | 已停止刷新 | 预写 ts=now−12min 后不再写 | `state=stale` + `staleForMs ∈ [700000, 740000]`（±tick 粒度） |
| TC16 | 阈值自适应（slow，~150s） | 写线程周期 25s 运行 ≥55s（建立 ≥2 变位，观测平均 ≈25s）→ 停写后**轮询至 `stale`（截止静默 90s）** | 静默 65s 时**仍为 `ok`**（有效阈值保留 75s——窗口剪空后保留最近计算值，agent.md §3.6.2）；静默 ≥80s 后转 `stale`（量化边界余量 ~2s，故以轮询截止兜底） |
| TC17 | 降级态 | kill Agent 栈内 c4_shm_manager 进程，轮询（截止 10s） | `degraded=true`，各点保持上次值与状态（不杜撰）；恢复断言 ❌ 待 C4_FUN_00021 |

## 4. 测试 fixture（conftest.py）

```json
{"c4_shm_manager": {"writer": ["c4_asfp2_server"], "reader": ["c4_asfp2_client"]},
 "c4_asfp2_server": [{"id": "wt1", "port": 19001,
    "points": [{"id": "windspeed", "addr": 3000}, {"id": "power", "addr": 3001},
               {"id": "oiltemp", "addr": 3002}]}],
 "c4_asfp2_client": [{"id": "center", "ip": "127.0.0.1", "port": 19900,
    "points": [{"key": "wt1.windspeed", "addr": 5000}]}]}
```

- schema 依据 `c4_architecture.md` §3.2.1：`c4_shm_manager.writer/reader` 为服务类型**字符串**；
  writer 点字段为 `id`；reader 点以 `key` 引用（服务端口取 19xxx 段避让生产 9001/9900；
  服务进程由测试栈 Agent 启动，数据播种走直写、不依赖服务真实运行）
- shm_id 从 `create_shm` **回填后的 tmp config.json** 读取
- `write_point(shm, shm_id, data_type, value, ts)` helper：seqlock 写（seq→data→seq+2），
  **本机序** `struct` 前缀 `=`（见 `c4_fun_00053/README.md` §4.3 历史勘误——勿按其旧版
  "大端"表述实现）；helper 以 `sys.path` 方式跨目录复用 `c4_fun_00053/shm_helpers.py`
- 每用例独立 instance，teardown `shm_unlink`

## 5. 断言面

- **所有状态转换断言使用轮询+截止时间**（poll-until-deadline），禁止固定 sleep 后单次断言；
- slow 标注：TC16（~150s）`pytest.mark.slow`；TC15 无需等待（固定 ts 预写）；
- LLM 层（❌ 不正测）：NL 话术、单次快照 2s 突发采样触发、`list_points`/`display_points`/
  `stop_display` 的自然语言触发路径（REST 为同一服务入口的确定性等价面）。

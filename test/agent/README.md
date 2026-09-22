# C4 Agent 功能测试方案

> **版本**：v2.0.0 | **最后更新**：2026-09-22
>
> **设计依据**：[agent.md](../../docs/design/agent.md) — C4 Agent 系统架构设计（缺口驱动
> 九阶段接入流水线）。本方案随 agent.md 架构重构整体升级：被测对象从 SuperWorker/子代理
> ReAct 循环变更为 **Workflow 编排器 + 阶段提取器 + 方案层（纯代码）+ 执行层（确定性拆解器）**。
>
> **测试范围**：agent.md 覆盖的全部实现功能，包括确定性代码逻辑和 LLM 驱动的提取行为。

---

## 0. 架构变更对测试面的影响（v1.2 → v2.0）

| 旧架构（v1.2 方案） | 新架构（本方案） | 对测试的影响 |
|---------------------|-----------------|-------------|
| SuperWorker + 子代理（info-gatherer / plan-generator / step-decomposer） | Workflow 编排器 + 阶段提取器（1-7，提示词驱动）+ 方案层（阶段 8，纯代码）+ 执行层（阶段 9，确定性拆解器） | 被测对象重命名；「子代理调度」类断言全部移除 |
| 方案由 plan-generator（LLM）在用户指令后生成 | 缺口闭合后**方案层自动装配**（纯代码：合并原料 + L1 + L2） | 「生成接入方案」指令不再是触发条件；断言「缺口闭合 → button_arm」 |
| interrupt 检查点 + 确认消息 | **按钮唯一确认通道**：`button_arm`/`button_disarm{reason}` 语义事件 + `[C4_BUTTON_CONFIRM]` 前缀消息 | `find_interrupt_id` → `wait_button_arm`；自由文本确认不构成确认 |
| step-decomposer 为 LLM 工具（output_plan_steps），校验失败 LLM 重试 | `generatePlanSteps` 确定性函数，校验失败 = 拆解中止、事务未启动、无重试 | 拆解失败断言改「无变更产生」；错误恢复章节同步 |
| 工具闸门（工具层强制阶段顺序） | **出口判据**（确定性代码）+ 逐侧协议锁 + 任意阶段取消 | 新增取消语义、协议锁、锁侧撤回、失效传播测试组 |
| 点校验分散在各工具 | **校验三层**：L0 提示词知识 / L1 通用结构（point_rules 共享库）/ L2 MCP `validate_points`（与启动校验同源） | 新增 validate_points 拒绝路径测试；黑名单更新 |

> 被测的**用户可见行为**大量保持不变（接入成功产物、双侧成对、非技术语言）——变化集中在
> **机制与状态语义**（按钮事件、缺口驱动、方案生命周期）。本方案 §4 各表格已按新机制重写。

---

## 1. 总则

### 1.1 测试目标

验证 C4 Agent 可执行文件 (`c4_agent`) 的功能正确性，覆盖从用户输入到 MCP 服务配置下发
的完整数据接入流程（九阶段缺口驱动流水线：提取层 1-7 → 方案层 8 → 执行层 9）。

### 1.2 测试原则

| 原则 | 说明 |
|------|------|
| **黑盒功能测试** | 仅通过 c4_agent 的 HTTP API 和启动行为进行测试，不侵入 Agent 内部代码 |
| **零额外接口** | 不为测试新增 MCP 工具、测试端点或调试开关。被测接口即生产接口 |
| **不 mock LLM** | 所有测试使用真实 c4_agent + 真实 LLM。LLM 驱动的行为通过结构验证、副作用验证和约束验证来断言，而非精确值比对 |
| **可观测面即断言面** | HTTP 响应内容 / SSE 事件流（含 `button_arm`/`button_disarm` 语义事件）/ 文件系统 / 进程状态 — 只断言这些可观察的东西 |

### 1.3 被测接口

| 接口 | 来源 | 说明 |
|------|------|------|
| Agent 启动流程 | agent.md §3.2.3（c4_architecture.md §3.1.2） | 启动时的配置加载、四级瀑布收敛（L0 config 健康 → L1 连接 → L2 收敛 → L3 监控接续） |
| `GET /api/services` | agent.md §3.3, §3.5 | Registry L1 服务摘要查询 |
| `GET /api/state` | agent.md §3.2.1.7 | Agent 运行时状态查询：`phase`、`hasAccessPlan`、`lastError` |
| `POST /api/chat` (SSE) | agent.md §3.1, §3.5 | Workflow 编排器回合（取消检测 → 阶段提取 → 缺口计算 → 方案层/执行层分叉）；SSE 含 `button_arm`/`button_disarm{reason}` 语义事件 |
| `POST /api/upload` | agent.md §3.5 | 文件上传 → 阶段 3/6 解析（`<file_data>` 注入点表提取器） |

### 1.4 测试层次

```
┌──────────────────────────────────────────────────────────────┐
│ L1: 确定性功能测试                                              │
│ 不依赖 LLM 推理 — 精确值/精确状态断言                            │
│                                                               │
│ · Registry 加载 → GET /api/services 响应结构（L1 摘要 + 阶段参数）│
│ · Agent 启动恢复 → 各崩溃时刻的 config / shm / 实例状态一致性    │
│ · 执行模块产物 → 完整数据流完成后 config.json / MCP 服务状态      │
└──────────────────────────────────────────────────────────────┘
                               │
                               ▼
┌──────────────────────────────────────────────────────────────┐
│ L2: LLM 驱动功能测试                                           │
│ 依赖 LLM 提取 — 结构/副作用/约束断言，不校验具体措辞              │
│                                                               │
│ · 阶段提取 → 解析结果进对话文本、缺口追问（聚合提问停等）          │
│ · 方案层 → 缺口闭合自动装配 → button_arm（L1+L2 校验后）          │
│ · 执行验证 → 按钮确认后的 config.json 产物 + 服务启动              │
│ · 取消/失效传播/协议锁 → 状态机语义验证                           │
│ · 非技术语言 → 响应文本黑名单检查                                │
└──────────────────────────────────────────────────────────────┘
```

> **注**：此 L1/L2 不同于 agent.md §1.2.1 中的 ✅/❌ 分类。agent.md 的分类是**实现方式**
> （是否依赖 LLM 推理），此处按**断言策略**分层：
> - L1 = 不用 LLM 就能验证的行为 → 精确断言
> - L2 = 必须经过 LLM 才能触发的行为 → 宽松断言
>
> 方案层（agent.md 中的纯代码）虽自身无 LLM，但其触发路径经过 LLM 提取（缺口闭合），
> 端到端验证划入 L2；其校验规则本体（point_rules 共享库）的精确断言归 TypeScript 单元测试
> （`agent/test/executor/point_rules.test.ts`，见 agent.md §2.7.2 契约），不在本黑盒方案覆盖范围内。

---

## 2. 测试环境

### 2.1 依赖

| 依赖 | 版本 / 说明 |
|------|-----------|
| Python | ≥ 3.8 |
| pytest | ≥ 7.0 |
| requests | HTTP client |
| sseclient-py | SSE 事件流解析 |
| c4_agent | 被测可执行文件，路径通过 `C4_AGENT_PATH` 环境变量或自动查找 |
| c4_shm_manager | 共享内存管理，路径通过 `C4_SHM_MANAGER_PATH` 或自动查找 |
| LLM API Key | `ZHIPU_API_KEY` 环境变量（L2 测试必需） |
| 各 MCP 服务二进制 | 按 agent.md §5.2 路径查找（`/usr/local/bin/c4_*` 或自动编译） |
| sudo | `/dev/shm` 满模拟等系统级测试需要，密码通过环境变量 `SUDO_PASSWORD` 传入 |

### 2.2 conftest.py 设计

文件：`c4/test/agent/python/conftest.py`

```python
# ── 核心 fixtures ──

@pytest.fixture(scope="session")
def agent_binary() -> str:
    """返回 c4_agent 可执行文件路径。从 C4_AGENT_PATH 或自动查找/编译。"""

@pytest.fixture(scope="session")
def shm_manager_binary() -> str:
    """返回 c4_shm_manager 二进制路径。"""

@pytest.fixture(scope="session")
def registry_dir(tmp_path_factory) -> Path:
    """制备 mcp-registry/ 目录，复制 config/mcp-registry/*.json 到临时路径。"""

@pytest.fixture(scope="function")
def agent(agent_binary, shm_manager_binary, registry_dir, tmp_path):
    """
    Function 级隔离的 Agent 实例。

    生命周期:
      1. 制备 tmp_path 作为配置目录（~/.local/c4/ 等效）替代（agent.json + config.json + mcp-registry/）
      2. 启动 c4_shm_manager（监听 tmp 目录 Unix socket——测试栈无 systemd，MCP 服务进程由
         测试自启；Agent 是 MCP 客户端，仅经 socket 连接、从不拉起 MCP 进程）
      3. 启动 c4_agent --config-dir <tmp_path>（经连接配置指向上述 socket 接入）
      4. 轮询 GET /api/services 直到返回 200（Agent 就绪）
      5. yield AgentHandle(base_url, process, config_dir)
      6. teardown: SIGTERM → wait(10s) → SIGKILL → 清理 shm（含 ipcrm 强制清理 + session 级 atexit 兜底）
    """

@pytest.fixture(scope="function")
def chat(agent):
    """
    返回 ChatHelper:
      - send(message: str) → SSEEventStream
      - send_with_file(message: str, file_path: str) → SSEEventStream
      - confirm() → SSEEventStream   # 点击确认按钮：POST [C4_BUTTON_CONFIRM] 前缀消息
      - cancel()  → SSEEventStream   # 点击取消按钮：POST [C4_BUTTON_CANCEL] 前缀消息

    SSEEventStream 提供:
      - events: list[SSEEvent]  — metadata, messages, button_arm, button_disarm, end
      - wait_for_event(type: str, timeout: float) → SSEEvent | None
      - wait_button_arm(timeout: float) → bool   # 等待按钮武装语义事件
      - text_content() → str    — 拼接所有 assistant 消息文本
    """

# ── 环境制备 helpers (conftest.py 内) ──

def write_agent_json(config_dir: Path) -> None:
    """写入最小 agent.json。LLM 配置中 temperature=0 确定性输出；server 端口；
    registry 路径指向 mcp-registry/。"""

def write_config_json(config_dir: Path, content: dict | None) -> None:
    """写入 config.json。content=None 表示不创建（模拟首次启动）。"""

def corrupt_config_json(config_dir: Path) -> None:
    """将 config.json 截断为损坏的 JSON。"""

def write_config_prev(config_dir: Path, content: dict) -> None:
    """写入 config.json.prev.1（回滚源，滚动保留 .prev.1~.3；用于 L0 恢复/事务回滚测试）。"""

def write_pending_marker(config_dir: Path, content: dict) -> None:
    """写入 pending_change.json 事务标记（模拟崩溃于变更事务中）。"""

# ── L2 测试批处理建议（conftest.py 文档注释） ──

"""
L2 测试执行时间较长（每个用例 10–60s LLM 响应），建议按以下策略优化：

1. 同一对话流的测试合并为一个测试函数内的多步骤验证
   （如 §4.3+§4.4+§4.5+§4.6 完整接入流），减少 Agent 重复启动

2. L2 测试按 batch 分组（参见 pytest.mark.llm 注册时的 batch 参数），
   批次间添加 cooling_off 间隔（如 10s）避免 API 限速

3. 错误恢复测试（§4.8）使用预构造的 JSON 文件绕过 LLM，
   直接测试执行模块
"""
```

### 2.3 AgentHandle 设计

```python
class AgentHandle:
    """封装 Agent 进程 + HTTP 访问。"""

    base_url: str          # http://127.0.0.1:{port}
    process: Popen         # Agent 子进程句柄
    config_dir: Path       # 配置目录（~/.local/c4/ 等效路径）

    def get_services(self) -> dict:
        """GET /api/services → JSON"""

    def get_state(self) -> dict:
        """GET /api/state → {phase, hasAccessPlan, lastError}
        hasAccessPlan 语义（agent.md §3.2.1.7）：等价 accessPlan !== null——
        回滚后为 true（方案保留），执行成功后为 false（方案被消耗）。"""

    def chat(self, message: str) -> SSEEventStream:
        """POST /api/chat → SSE 流"""

    def upload(self, file_path: str, message: str) -> SSEEventStream:
        """POST /api/upload (multipart) + 关联 chat 消息 → SSE 流"""

    def kill(self) -> None:
        """SIGKILL Agent 进程（模拟崩溃）"""

    def restart(self) -> None:
        """重新启动 Agent 进程并等待就绪"""
```

---

## 3. L1 测试 — 确定性功能

### 3.1 Registry 加载

**被测对象**：`McpServiceRegistry.loadFromDirectory()` → L1 服务摘要生成
（agent.md §3.3.0）

**被测接口**：`GET /api/services`

| # | 用例 | 输入条件 | 断言 |
|---|------|---------|------|
| 1.1 | 返回所有已注册服务 | `mcp-registry/` 含 c4_modbus_client, c4_iec104_client, c4_asfp2_server, c4_asfp2_client, c4_influxdb_client 共 5 个 JSON | `GET /api/services` 返回数组长度 = 5 |
| 1.2 | 每项含 L1 必须字段 | 同上 | 每项含 `service_type`, `display_name`, `role`, `protocols[]`, `point_fields`, `identity_fields` |
| 1.3 | protocols 含 description 和 selection_rules | 同上 | `protocols[0]` 含 `protocol`, `description`, `selection_rules[]` |
| 1.4 | L1 不含 L2 全量字段 | 同上 | 每项**不**含 `config_schema` 全量字段、`binary_path`、`error_mappings`、`prompt_hints` 全量（L1 仅含 config_schema 的必填/默认标记摘要——「无 default 键=必填」） |
| 1.5 | Registry 目录为空 | `mcp-registry/` 为空目录 | 返回空数组 `[]`，Agent 正常就绪（不崩溃） |
| 1.6 | Registry 目录缺失 | `mcp-registry/` 不存在 | Agent 不崩溃；`GET /api/services` 返回 200（空数组）或 5xx（启动失败），两种行为均视为合理防御 |
| 1.7 | 单个 JSON 文件损坏 | `mcp-registry/` 中 1 个文件为非 JSON | Agent 不崩溃；`GET /api/services` 正常返回其余有效服务（损坏文件不导致全局加载失败） |
| 1.8 | identity_fields 声明（L2 查重键） | 同 1.1 | modbus_client=[uid,fun,addr]；iec104_client=[addr]；asfp2_client/server=[addr]；influxdb_client=[measurement,field]——与 agent.md §2.7/§3.3 声明一致 |

### 3.2 Agent 启动恢复

**被测对象**：Agent 启动/恢复四级瀑布（agent.md §3.2.3；c4_architecture.md §3.1.2：
L0 config 健康 → L1 连接 → L2 收敛 → L3 监控接续）。无在途事务标记时收敛仅 start
（ALREADY_RUNNING 无动作），完整 Stop-Start（stop → adjust_shm → start）仅在恢复已回滚
事务的配置时执行。

**被测接口**：Agent 进程启动行为 + 文件系统副作用

**通用断言维度**：

| 维度 | 验证方法 |
|------|---------|
| Agent 就绪 | `GET /api/services` 返回 200 |
| config.json 状态 | 读取 config.json + config.json.prev.1，比较内容 |
| 实例运行状态 | 端口监听探测 / shm `write_seq` 推进——MCP 进程为常驻系统服务，进程存在与否**不**作为实例运行依据（生命周期双层模型，c4_architecture.md §3.1.1） |
| 共享内存状态 | 通过 c4_shm_manager MCP 调用检查 shm 块分配（可选深验证） |

#### 3.2.1 首次启动

| # | 用例 | 初始态 | 断言 |
|---|------|--------|------|
| 3.2.1.1 | 无 config.json — 首次启动 | `config.json` 不存在 | Agent 就绪，不创建 config.json，数据服务**零实例**（期望状态零实例合法）；瀑布对账 c4_shm_manager：段不存在则 `create_shm`（幂等 create-or-attach，无配置 → 默认 100k 点），shm 存在即视为正确 |
| 3.2.1.2 | 无 config.json — 仅 c4_shm_manager 可连 | 同上 | c4_shm_manager socket 可连，其余数据服务零实例（进程由 systemd/测试栈管理，不作为断言面）；shm 段存在 |

#### 3.2.2 正常重启

| # | 用例 | 初始态 | 断言 |
|---|------|--------|------|
| 3.2.2.1 | 有效 config.json，实例未运行 | config.json 含 1 个 c4_modbus_client + 1 个 c4_asfp2_client | Agent 就绪；L2 收敛对全部服务 start（无 pending_change.json 标记 → **不执行 Stop-Start**）；两服务实例按配置拉起（start 返回 success）；MCP 进程常驻不退出 |
| 3.2.2.2 | 有效 config.json，实例已在运行 | 先启动 Agent（同 3.2.2.1），再重启 Agent | Agent 就绪；对已在运行的服务 start 返回 ALREADY_RUNNING（一等成功路径结果，无动作）；数据路径**零中断**（c4_architecture.md §3.1.1 故障矩阵：Agent 崩溃/重启不影响 MCP 实例，C4_RS_00030/00031） |

#### 3.2.3 配置损坏恢复

| # | 用例 | 初始态 | 断言 |
|---|------|--------|------|
| 3.2.3.1 | config 损坏，.prev.1 有效 | config.json = 截断 JSON；config.json.prev.1 = 完好配置（含 1 个 modbus 实例） | L0：校验 .prev.1（parse + schema）通过 → 恢复为 config.json（获得权威地位）→ 正常启动并按恢复后的配置收敛；向用户显式报告恢复情况（不得静默） |
| 3.2.3.2 | config 损坏，.prev.1 不存在 | config.json = 截断 JSON；无 .prev.1 | L0：.prev 缺失 → **不得覆盖**、保留当前 config.json、删除 pending_change.json（防重入）、报告异常等待人工介入（文件存在但损坏 ≠ 首次启动的合法空态） |
| 3.2.3.3 | config 损坏，.prev.1 也损坏 | 两者都损坏 | L0：.prev 不可用 → 同 3.2.3.2：保留 config.json、报告异常等待人工介入 |
| 3.2.3.4 | pending_change.json 存在（崩溃于变更事务中） | pending_change.json 完好 + config.json.prev.1 有效 | L0：发现标记 → 恢复 .prev.1 → 以恢复后的配置执行完整 Stop-Start（含 adjust_shm）→ 向用户报告「上次接入变更未完成，已回滚，接入不成功」→ 继续瀑布（变更作废不续做，C4_RS_00066） |

#### 3.2.4 崩溃恢复

**事务边界原则**（agent.md §3.2.3、c4_architecture.md §3.1.2）：崩溃是否落在变更事务内由
`pending_change.json` 标记显式界定——无标记 → 收敛仅 start（ALREADY_RUNNING 无动作）；
有标记 → 变更作废：恢复 .prev.1 → 完整 Stop-Start（含 adjust_shm，禁止只 restart 不调
adjust_shm）→ 报告「接入不成功」。半截变更不得静默续做。

**测试策略**：事务窗口通过预构造 `pending_change.json` / `config.json.prev.1` 文件显式
模拟崩溃时刻，无需精确 kill。改为验证**恢复结果的一致性**：构造不同崩溃场景的初始态 →
kill Agent → restart → 断言 config.json ↔ shm ↔ 实例状态三者一致。

模拟方法：先让 Agent 正常运行 → `agent.kill()` 杀掉进程 → 重新 `agent.restart()` → 验证。

| # | 用例 | 初始态构造方法 | 断言（重启后三者一致） |
|---|------|--------------|----------------------|
| 3.2.4.1 | 正常运行中崩溃（无在途事务） | 启动 Agent 含 config.json + 运行中的实例 → kill | config.json 内容不变；实例保持运行（Agent 崩溃期间数据路径不中断——MCP 常驻，C4_RS_00030/00031）；重启后 start 返回 ALREADY_RUNNING 无动作；shm 块分配与 config 一致 |
| 3.2.4.2 | rename 后、删除标记前崩溃 | 构造变更中途态：pending_change.json 存在 + config.json = 新版本 + .prev.1 = 旧版本 → kill | 重启后 config.json = .prev.1（旧版生效，半截变更作废）；报告「接入不成功」；以恢复后的配置执行完整 Stop-Start（含 adjust_shm）；shm 分配与恢复后的 config 一致 |
| 3.2.4.3 | 写标记后、rename 前崩溃 | 构造变更中途态：pending_change.json 存在 + config.json 仍为旧版 + .prev.1 = 旧版 → kill | 重启后恢复 .prev.1（与当前一致）→ 完整 Stop-Start（含 adjust_shm）→ 报告「接入不成功」→ 收敛完成 |
| 3.2.4.4 | 无标记、收敛中途崩溃 | 正常收敛进行中 kill（无 pending_change.json） | 同 3.2.4.1——无在途事务标记即保证 config 与运行状态一致，start 幂等（ALREADY_RUNNING 无动作） |

> **一致性验证方法**：
> - `config.json`：文件内容与写入前快照一致（含所有服务实例、point 定义）
> - `shm`：通过 c4_shm_manager MCP 调用确认 config 中的每个 point 有对应已分配的 shm 块（shm_id ≠ 0）
> - `实例`：config 中声明的每种服务存在运行中的数据路径实例（端口监听 / write_seq 推进；
>   MCP 进程常驻，进程存在 ≠ 实例运行）

---

## 4. L2 测试 — LLM 驱动功能

### 4.1 通用注意事项

#### LLM 非确定性处理

| 策略 | 适用场景 | 示例 |
|------|---------|------|
| **结构断言** | JSON 产物、对话文本模式 | 方案展示文本含设备/协议/端口要素、`button_arm` 事件到达 |
| **副作用断言** | 文件系统、进程状态 | config.json 写入后 `shm_id` 必须 ≠ 0 |
| **黑名单断言** | 非技术语言约束 | 响应文本不出现 `shm_id`, `MCP`, `CONFIG_MISSING_SECTION` |
| **存在性断言** | 关键信息传达 | 方案描述含设备名、转发目标描述、逐条「地址 ↔ 点名」映射 |
| **容忍重试** | 偶发 LLM 输出格式偏差 | 失败时重试最多 2 次（共 3 次尝试） |

#### L2 测试超时

每个 LLM 驱动的对话可能耗时 10–60 秒。单个 SSE 流最长等待 120 秒，超时视为失败。

#### L2 测试的可跳过性

若 `ZHIPU_API_KEY` 未设置或 LLM 不可达，L2 测试应 `pytest.skip` 而非失败。
通过 `pytest.mark.llm` 标记区分：

```bash
pytest -m "not llm"  # 仅跑 L1
pytest -m llm         # 仅跑 L2
pytest                # 全跑（L2 在无 API key 时自动 skip）
```

---

### 4.2 编排器回合（取消检测 / 问候 / 查询）

**被测对象**：Workflow 编排器回合循环（agent.md §2.3、§2.4.2、§3.1）——顶层取消检测、
提取遍历、缺口计算、方案层/执行层分叉。

**被测接口**：`POST /api/chat`

| # | 用例 | 输入 | 预期（可观察副作用） |
|---|------|------|---------------------|
| 4.2.1 | 上传点表触发阶段提取 | `POST /api/upload`（上传 xlsx 点表）+ 消息含协议/端口 | 对话文本中出现从点表解析出的设备名/数据点信息（非空，非"无法解析"） |
| 4.2.2 | 查询类消息不触发接入 | `POST /api/chat` "现在有哪些设备在运行" | 正常回答；不产生方案、不弹按钮（无 button_arm） |
| 4.2.3 | 问候类消息直接回答 | `POST /api/chat` "你好" | SSE 流正常关闭，无 error，有 assistant 文本回复 |
| 4.2.4 | 空消息处理 | `POST /api/chat` "" | Agent 不崩溃，返回合理的引导性回复或提示 |
| 4.2.5 | **取消词全等匹配 → 整体取消** | 接入进行中发送「取消」/「算了」/「不接了」/「放弃」/「停止接入」（逐一） | 顶层确定性拦截：在途态清除（协议声明/点表/连接/方案/确认/两把锁/按钮武装），场站绑定与 abbr 记忆库**保留**，回复「已取消」并终局；后续可重新接入 |
| 4.2.6 | **包含"取消"的非全等消息走字段语义** | 接入进行中发送「取消转发目标」（恰含取消词但与词表不全等） | **不得**触发整体清除——按字段语义处理（转发链锁侧撤回，§4.4.6） |
| 4.2.7 | **executing 窗口拒绝取消** | 按钮确认消费后、执行/回滚完成前发送「取消」 | 取消被拒绝（确认是不可撤销的硬边界）；执行失败回滚完成后可再次取消 |
| 4.2.8 | 取消后记忆库保留 | 整体取消后 → 重新发起同设备接入 | abbr 记忆库与场站绑定保留（历史含合成取消记录），不产生重复 id |

### 4.3 阶段提取器（阶段 1-7，提示词驱动）

**被测对象**：阶段提取器（agent.md §3.2.0——非工具，提示词参数注入 + 出口判据）。
九阶段：1 场站 / 2 接入协议 / 3 接入点表 / 4 接入信息 / 5 转发协议 / 6 转发点表 / 7 转发信息 /
8 方案层（§4.4）/ 9 执行层（§4.6）。阶段 5-7 条件激活（无转发意图 → not_applicable）。

**被测接口**：`POST /api/upload` + `POST /api/chat`

| # | 用例 | 输入 | 预期 |
|---|------|------|------|
| 4.3.1 | 解析合法 xlsx 点表 | 上传含 "windspeed, addr=1000" 等字段的点表 | SSE 流中提取结果含设备名、协议、数据点列表；对话含逐条「地址 ↔ 点名」映射（方案展示前供核对） |
| 4.3.2 | 解析合法 csv 点表 | 上传 CSV 格式点表 | 同上 |
| 4.3.3 | 上传不支持的文件格式 | 上传 .bin 二进制文件 | Agent 给出友好提示（xlsx/csv/txt 之外不支持——§3.2.2 口径），不崩溃 |
| 4.3.4 | 上传损坏的 xlsx | 上传截断/损坏的 Excel | 同上 |
| 4.3.5 | 点表缺少必填字段 | 缺少实例必填参数（无 default 键，如 IP） | Agent 列出已有信息 + 明确指出缺失字段，聚合提问逐项补齐（缺口驱动，C4_FUN_00005 缺失引导）；**缺口闭合前不弹确认按钮** |
| 4.3.6 | 协议：用户必供——点表字段不参与推断 | 点表含 `uid/fun/type/swap` 列（形态仅 Modbus 匹配）+ 用户未提协议 | Agent 必须询问协议（禁止推断——func_test_case 用例 13 用户裁定）；未确定协议前不进入方案确认 |
| 4.3.7 | 协议：用户描述即视为提供 | 用户说"采集 Modbus 设备" | 协议缺口闭合，不再询问 |
| 4.3.8 | 转发协议：逐侧必供 | 用户声明接收协议但转发目标未提转发协议 | 转发协议缺口独立追问（禁止沿用接收侧协议或按目标描述推断） |
| 4.3.9 | 聚合提问停等 | 一条消息同时缺多个缺口（点名+转发地址） | 聚合为**一次**提问列出全部缺口（提问即终局，回合停等）；不得自问自答、不得边问边出方案 |
| 4.3.10 | L0 枚举映射 | 用户说「32位浮点」「保持寄存器」 | 提取产物 type=10、fun=3（point_field_hints 枚举映射；映射歧义时字段省略并追问，禁止猜测编码） |
| 4.3.11 | 点名缺失 → 追问（禁止编造） | 点表仅地址无点名（func_test_case 用例 8） | 逐点询问点名（或经确认采用确定性生成名），禁止「点1000」式编造 |
| 4.3.12 | 同回合拓扑序 | 一条消息同时提供协议+点表 | 阶段 3 消费同回合阶段 2 产物（1→2→3→4、5→6→7 拓扑序合并）——缺口不重复追问 |

### 4.4 方案层（阶段 8，纯代码）+ 协议锁 + 失效传播

**被测对象**：方案层装配（agent.md §3.2.0.1——纯代码：合并原料 + 缺省点名/abbr + L1 结构
校验 + L2 validate_points → AccessPlan + button_arm）与协议逐侧锁（§2.4.1）。

**关键机制**：方案层无 LLM——缺口闭合（gaps 为空）后**自动装配**，「生成方案」不再是必要
指令；L2 validate_points 拒绝时缺口重新打开（追问/修正循环）。

| # | 用例 | 输入 | 预期 |
|---|------|------|------|
| 4.4.1 | 缺口闭合 → 自动装配 + button_arm | 单消息提供全部信息（func_test_case 用例 1 形态） | 对话含方案要素（设备/协议/端口/「地址 ↔ 点名」映射）；SSE 收到 `button_arm`；确认按钮渲染 |
| 4.4.2 | 无转发意图 → 仅采集方案 | 用户明确「不需要转发」 | 方案仅含采集侧；阶段 5-7 = not_applicable；button_arm 照常 |
| 4.4.3 | 协议无可用服务 | 设备使用不支持的协议（如 DNP3） | 可读错误 + 已部署协议列表（registry 动态生成），不生成方案 |
| 4.4.4 | **L2 validate_points 拒绝 → 拦截** | 点表含同 uid+fun 下重叠 addr 区间（如 3000-3001 两个 float32） | button_arm **不出现**；Agent 以非技术语言报告重叠点并追问修正；config.json 无写入 |
| 4.4.5 | **协议逐侧锁**：下游产出后锁 | 接入点表已产出（receive 锁）→ 用户改口换协议 | 拒绝 + 引导「回复取消后重新开始」；提取值==锁定值时静默放行 |
| 4.4.6 | **锁侧撤回**（转发链） | 转发侧已锁定后发送「取消转发目标」 | 字段级撤回：forward 锁/阶段 5-7 数据清除、转发意图回 not_applicable、方案失效重装配；接入侧不受影响 |
| 4.4.7 | **失效传播**：点表变更 → 方案失效 | 方案展示态（button 已武装）→ 用户修改点表 | button_disarm{方案过期}；缺口重算；变更后重新装配并重新 button_arm |
| 4.4.8 | 方案展示含协议 + abbr 绑定 | 首次接入的方案展示 | 展示文本含协议名（逐侧）、「将新建设备 `hnals_wt1`」（abbr 绑定可读呈现）——单次确认覆盖协议+绑定+动作 |

### 4.5 按钮唯一确认通道

**被测对象**：按钮状态语义化（agent.md §2.8；web.md §3.1.3）——`button_arm`/`button_disarm{reason}`
后端语义事件 + `[C4_BUTTON_CONFIRM]` 前缀唯一确认。

| # | 用例 | 输入 | 预期 |
|---|------|------|------|
| 4.5.1 | 按钮确认 → 执行 | button_arm 后 POST `[C4_BUTTON_CONFIRM] 确认` | 流程进入执行层（拆解 → 事务） |
| 4.5.2 | 按钮取消 → 终止 | button_arm 后 POST `[C4_BUTTON_CANCEL] 取消，不执行` | 流程终止，不生成 config.json，不执行 Stop-Start |
| 4.5.3 | **自由文本确认不构成确认** | button_arm 后发送自由文本「确认」/「好的」/「执行」 | **不进入执行**——唯一确认通道是按钮；自由文本按普通消息处理（引导点击按钮） |
| 4.5.4 | 参数回答不误触执行 | 追问轮回答「转发地址从一万开始」 | 不命中确认（提问即终局 + 按钮唯一通道双保险）；缺口闭合后才 button_arm |
| 4.5.5 | 拒绝文案引导 → 按钮重现 | 闸门拒绝后 | 拒绝文本含确认引导，`button_arm` 重现（不得自相矛盾撤掉按钮） |
| 4.5.6 | abbr 绑定（命中） | 已有 `hnals_wt1` → modify/delete/加点方案 | 展示列「将在 `hnals_wt1`（1#风机）上修改」，复用已存 id |

### 4.6 执行验证（副作用检查）

**被测对象**：执行层确定性拆解器 `generatePlanSteps`（agent.md §3.2.0.1/§3.2.1）+ 执行模块
（merge → adjust_shm → stop → start → persist 事务五步）。

**触发路径**：完整 L2 流程：提取 → 缺口闭合 → 方案层装配 → button_arm → 按钮确认 → 执行。

#### 4.6.1 add 操作（首次接入 + 追加）

| # | 用例 | 预期 — config.json | 预期 — MCP 服务 |
|---|------|-------------------|-----------------|
| 4.6.1.1 | 首次接入（Modbus + ASFP2 转发） | config.json 含 `c4_shm_manager` + `c4_modbus_client[]` + `c4_asfp2_client[]`；所有 `shm_id != 0`；default 字段已填充（方案层装配） | 两服务实例运行中（端口监听 / write_seq 推进） |
| 4.6.1.2 | 首次接入（仅采集，无转发） | config.json 含 `c4_modbus_client[]`，**不**含 `c4_asfp2_client[]`；reader 为空或不存在 | — |
| 4.6.1.3 | 原子写入 | 无残留 .tmp 文件；非首次接入时 config.json.prev.1 存在（滚动保留 .prev.1~.3）；事务完成后 pending_change.json 已删除 | — |
| 4.6.1.4 | writer/reader 分类 | `c4_shm_manager.writer[]`/`reader[]` 只列实际使用（实例化）的服务类型，与 Registry role 声明一致（动态读取验证） | — |
| 4.6.1.5 | 追加设备（第二次接入） | 新实例追加到 `c4_modbus_client[]`，旧实例完整保留；`writer[]` 不重复添加相同 service_type | 新服务启动，旧服务不受影响 |

#### 4.6.2 modify 操作（修改已有实例）

| # | 用例 | 触发方式 | 预期 |
|---|------|---------|------|
| 4.6.2.1 | 修改实例参数（IP） | "将 1#风机的 IP 改为 192.168.110.5" | `hnals_wt1.ip` 更新，其余字段不变；服务重启后使用新 IP |
| 4.6.2.2 | 修改采集点参数 | "将 windspeed 的寄存器地址从 1000 改为 1012"（目标地址须空闲） | `windspeed.addr` = 1012；其他字段不变；shm_id 不变 |
| 4.6.2.3 | 新增采集点 | "给 1#风机增加风向采集点"（含转发地址——双侧成对） | 双侧成对新增：writer 侧新 point + reader 侧对应转发点；旧 point 保留且 shm_id 不变 |
| 4.6.2.4 | 删除采集点 | "不再采集 1#风机的温度数据" | 双侧成对删除；adjust_shm 回收对应 shm 块 |
| 4.6.2.5 | 修改不存在的实例 | 请求修改一个不存在的设备 ID | Agent 返回友好错误提示（非技术语言），不修改 config.json |
| 4.6.2.6 | **modify 端口语义（冻结范围）** | 对已接入实例请求变更**客户端连接端口**（如 modbus 设备端口） vs **Writer 监听端口**（c4_asfp2_server） | 连接型端口可随 modify 变更；监听端口一经确定永不变更（拒绝）——§3.3/§3.2.1.6 口径 |

#### 4.6.3 delete 操作（删除实例）

| # | 用例 | 触发方式 | 预期 |
|---|------|---------|------|
| 4.6.3.1 | 删除单个实例（仍有同类型其他实例） | 请求"停用 2#风机" | 目标实例移除；`writer[]` 仍含 `c4_modbus_client`；旧 shm 块回收 |
| 4.6.3.2 | 删除最后一个实例 | 删至 0 台 | 实例移除 → `writer[]` 移除该服务类型（合法空态：空数组 = 零实例，start 幂等 success） |
| 4.6.3.3 | 删除被 Reader 引用的设备（相关性检查） | modbus(hnals_wt1) + asfp2_client 引用其点位 → 停用 1#风机 | 目标设备移除，同时 asfp2_client 的 points[] 不再有指向已删除设备的 key 引用（双侧成对） |
| 4.6.3.4 | 删除不存在的实例 | 请求删除不存在的设备 ID | 友好错误提示，不修改 config.json |

#### 4.6.4 id 稳定性（abbr 记忆库 + pointMap）

**被测对象**：abbr 记忆库 + id 确定流程（agent.md §3.2.1.3a）。

**核心不变式**：同一采集/转发目标的 `instance.id` 跨会话稳定——首次接入固化，后续
modify/delete/加点复用已存 id；**点级映射（pointMap）为记忆库正式字段**——modify/delete
按记忆库映射匹配旧点，禁止仅凭重新翻译的点名匹配。

**记忆库**：`~/.local/c4/abbr_registry.json`（agent 内部状态），持久化 `<id, name, abbr,
description, pointMap>`；**site 存于 `agent.json`（权威配置）**。

| # | 用例 | 触发方式 | 预期 |
|---|------|---------|------|
| 4.6.4.1 | 首次接入固化 id + 写入记忆库 | 首次 add → 确认执行 | config.json 生成 `hnals_wt1`；`abbr_registry.json` 出现对应记录（含 pointMap） |
| 4.6.4.2 | modify 复用同一 id（跨会话稳定） | 重启 Agent → 修改 IP | 仍是 `hnals_wt1`（不重新提取 abbr） |
| 4.6.4.3 | 同一设备加点 → 合并，不新建实例 | 再次 add 同设备（abbr 复提且描述匹配） | 判为同一设备：合并加点；实例数不变 |
| 4.6.4.4 | 不同设备撞 abbr → 重新生成 | 新 add 2#风机 | 生成不同 id（如 `hnals_wt1_2`）；`hnals_wt1` 不受影响 |
| 4.6.4.5 | delete 物理删除记忆 | delete 停用设备 | config.json 移除；记忆库记录物理删除 |
| 4.6.4.6 | 删除后 abbr 释放可复用 | 重新 add | 可用原 abbr 生成新 id（无冲突） |
| 4.6.4.7 | modify/delete 目标不在记忆库 | 操作从未接入的设备 | 检索阶段即报「目标不存在」，不进入方案，不修改 config.json |
| 4.6.4.8 | entries 丢失 → 从 config.json 重建 | 仅删 entries → 重启 → modify | id/name 恢复、abbr 反推、description 退化为 name；pointMap 从 config.json 点表 `name → point.id` 重建 |
| 4.6.4.9 | site 缺失 → 重新询问 | 仅删 agent.json 的 site → 触发接入 | 重新询问场站；entries 保留 |
| 4.6.4.10 | site 首次询问固化 | 全空启动 → 发起接入 | 询问场站名称+缩写 → 写入 agent.json → 固化 |
| 4.6.4.11 | 场站归属校验（三态） | ① 无场站信息 ② 归属不明 ③ 其他场站 | ① 默认当前场站 ② `site_ambiguous` 提醒确认 ③ `site_mismatch` 回复「该资料不属于当前场站」；记忆库确定性标签优先于 LLM 语义判断（取更保守方） |
| 4.6.4.12 | 记忆库损坏 JSON 恢复 | 截断 abbr_registry.json → 重启 | entries 重建（pointMap 同步重建）；site 不受影响 |
| 4.6.4.13 | **点名跨会话映射（pointMap 匹配）** | 已接入含「温度→temperature」→ 跨会话 modify「删除温度点」 | 按记忆库 pointMap 匹配 `temperature` 删除；禁止因重新翻译漂移误建新点/误删错点 |

> **断言面**：`abbr_registry.json` 文件内容（确定性副作用）+ config.json 的 `instance.id`
> 稳定性。检索由 `query_abbr_registry`（agent 内部函数）确定性执行；固化与重建可预构造
> 文件绕过 LLM 做 L1 级精确断言。

### 4.7 非技术语言约束

**被测对象**：阶段提示词 + 总结轮的非技术语言规则（agent.md §1.4 原则 5）

**黑名单**（硬约束 — 响应文本中**不得**出现，除非标注了例外）：

| 类别 | 禁止词 | 例外场景 | 来源 |
|------|--------|---------|------|
| 共享内存/内部术语 | shm_id, shm, 共享内存, adjust_shm, point_count, max_points | **无例外** — 任何场景均禁止 | §1.2.1 C4_FUN_00005 |
| 内部标识/错误码 | MCP, generatePlanSteps, validate_points, point_rules, button_arm, button_disarm, config_schema, CONFIG_MISSING_SECTION, DUPLICATE_KEY, SHM_CORRUPTED, SHM_NOT_CREATED, SHM_SYSCALL_FAILED | **无例外** | §1.2.1 C4_FUN_00005 |
| 协议术语 | Modbus TCP, IEC104, ASFP2 | 方案展示等待确认时可用协议名 + 通俗解释；能力介绍时可用协议名 | §1.2.1 C4_FUN_00005 |
| 端口号 | 数字形式的端口 | 方案展示时配合通俗解释可用 | §1.2.1 C4_FUN_00005 |
| JSON 原文 | 连续 `"key":` 模式或多层 `{}` 嵌套 | **无例外** — 禁止向用户直接展示 JSON 结构 | §1.2.1 C4_FUN_00005 |

| # | 用例 | 触发方式 | 断言 |
|---|------|---------|------|
| 4.7.1 | 正常对话不含技术术语 | "你好，介绍一下你能做什么" | 不匹配无例外类；协议名在能力介绍场景豁免 |
| 4.7.2 | 方案展示含通俗解释 | button_arm 前的方案展示文本 | 含设备名 + 操作描述 + 「地址 ↔ 点名」映射；无例外类不得出现；协议名 + 端口号豁免 |
| 4.7.3 | 错误场景不暴露内部信息 | 制造错误（损坏文件/L2 拒绝）后对话 | 错误描述不匹配任何黑名单（错误场景无豁免） |
| 4.7.4 | 全程不展示 JSON 结构 | 整个对话中 | 无 JSON 泄漏模式 |

### 4.8 错误恢复路径（回滚策略）

**被测对象**：变更事务错误处理（agent.md §3.2.2/§2.10 回滚策略——stop_start 失败一律回滚：
恢复 .prev.1 → 完整 Stop-Start 含 adjust_shm → 报告失败 → 删除事务标记）

| # | 用例 | 错误条件构造 | 预期恢复行为 |
|---|------|------------|-------------|
| 4.8.1 | adjust_shm 失败 — DUPLICATE_KEY | config 中重复全局 key | 恢复 .prev.1 → 完整 Stop-Start → 删除标记；非技术语言失败描述（变更作废） |
| 4.8.2 | adjust_shm 失败 — CONFIG_MISSING_SECTION | reader 有 writer 无 | 同上 |
| 4.8.3 | adjust_shm 失败 — UNKNOWN_READER_KEY | asfp2_client key 指向不存在 writer | 同上 |
| 4.8.4 | adjust_shm 失败 — shm/系统类 | tmpfs 限容触发 SHM_SYSCALL_FAILED | 同上回滚；文案区别（系统问题，请稍后重试） |
| 4.8.5 | start 部分失败 | 某服务 start 返回 error | 同上回滚（变更作废，不残留半接入状态） |
| 4.8.6 | **拆解器失败 — 事务未启动** | 内容混乱的点表致 generatePlanSteps 校验失败 | **拆解中止、事务未启动（无回滚对象）**——无变更产生；用户收到非技术语言提示（配置生成遇到问题，本次接入已安全回退）；不残留 .tmp |
| 4.8.7 | **回滚后方案保留（重武装）** | 执行失败经回滚 | 回到方案展示态：accessPlan 保留、userConfirmed 复位、`button_arm` 重现——用户可重新确认执行或整体取消（§2.8）；总结轮如实报告失败与回滚（§2.9） |

> **4.8.1–4.8.5 实现依赖**：通过构造 AccessPlan/AccessPlanSteps JSON 或预写 config 绕过
> LLM 环节直接测执行模块。**4.8.6 注记**：拆解器为确定性代码、无重试语义；LLM 重试类
> 行为已不存在，旧「重试一次」断言废除。

### 4.9 AgentState 持久化与生命周期（agent.md §3.2.1.7）

| # | 用例 | 触发方式 | 断言 |
|---|------|---------|------|
| 4.9.1 | 接入流程中途重启 → 状态恢复 | 缺口闭合、button_arm 后 → `get_state()` 确认 `hasAccessPlan = true` → kill → restart | 重启后 `hasAccessPlan = true`（无需重新上传点表） |
| 4.9.2 | 用户确认后中断 → 状态保持 | 确认按钮点击 → kill（执行前）→ restart | `phase` 反映已确认状态，可继续执行 |
| 4.9.3 | **执行成功后状态重置** | 完整接入成功 | `phase = "idle"`, `hasAccessPlan = false`（**方案被消耗后置 null**——accessPlan 唯一置 null 时机） |
| 4.9.4 | **回滚后状态保持** | 执行失败回滚完成 | `hasAccessPlan = true`（方案保留）；`button_arm` 重现 |
| 4.9.5 | 状态重置后可处理新接入 | 4.9.3 后发起新接入 | 正常新流程，不混淆上次接入 |

> **`GET /api/state` 响应格式**：
> ```json
> { "phase": "idle", "hasAccessPlan": false, "lastError": null }
> ```
> `hasAccessPlan` 等价 `accessPlan !== null`（agent.md §3.2.1.7）——不是「本会话曾生成过方案」。

### 4.10 validate_points（L2 协议语义校验，C4_FUN_00086~00090）

**被测对象**：五个数据路径 MCP 服务各自注册的 `validate_points` 工具（agent.md §2.7.1
契约 + 各服务文档规则表）。**接口统一、语义各协议自治**：入参 `{points:[...]}`，返回
`{valid, errors[{code,message,points[],field}], warnings[]}`。

**测试归属**：双层——
1. **本方案（L2 对话流）**：通过对话注入违规点表，断言方案层拦截（button_arm 不出现 +
   非技术语言报告 + 缺口重开），见 §4.4.4；
2. **c4_fun_00086~00090（待 MCP 实现落地后补建）**：直接调用各服务 validate_points 工具的
   黑盒测试，规格来源 = agent.md §2.7.1 + 各服务文档「MCP 工具接口」的 validate_points
   小节（错误码表 + requireShmID 参数化子集口径）。按 AGENTS.md 规则 3，届时以各目录
   README.md 为唯一权威规格。

| # | 用例 | 触发方式 | 预期 |
|---|------|---------|------|
| 4.10.1 | modbus 重叠点拦截 | 点表含同 uid+fun 下 3000(int16) 与 3001(float32)（跨度重叠） | 方案层拦截，报告重叠点对，追问修正 |
| 4.10.2 | modbus fun-type 不兼容拦截 | 线圈点（fun=1）配 type=10（float32） | 拦截 + 非技术语言说明（线圈的合法类型） |
| 4.10.3 | modbus swap 非法拦截 | 单数据单元点 swap=2 | 拦截（单数据单元点 swap 必须为 0） |
| 4.10.4 | asfp2 addr 越界拦截 | addr > 16777215 | 拦截 |
| 4.10.5 | influxdb 重复组合拦截 | 转发点两行 (measurement, field) 相同 | 拦截（POINT_DUP） |
| 4.10.6 | 合法点表放行 | 全部合法 → 缺口闭合 | validate_points 通过（valid=true）→ button_arm 正常 |

---

## 5. 端到端场景

| # | 场景 | 流程 | 关键断言点 |
|---|------|------|-----------|
| 5.1 | 单设备 Modbus 接入 + ASFP2 转发 | 上传点表 → 提取/追问 → 缺口闭合自动装配 → button_arm → 确认按钮 → 执行 | §4.6.1.1 + §4.7 非技术语言 + §4.9.3 状态重置 |
| 5.2 | 单设备 Modbus 接入（仅采集） | 同上，用户明确不转发 | §4.6.1.2 无 reader 断言 |
| 5.3 | 首次接入后重启 | 完成 5.1 → kill → restart | §3.2.4.1 崩溃恢复断言 |
| 5.4 | 修改 + 追加完整生命周期 | 追加第二台风机 → 修改采集点参数 → 增加新采集点（双侧成对） | §4.6.1.5 + §4.6.2 断言 |
| 5.5 | Add → Modify → Delete 完整生命周期 | 含 Reader 引用相关性检查的删除 | §4.6.3 断言；最终 config.json 恢复空 |
| 5.6 | **任意阶段取消流（新）** | 提取中途「取消」→ 清理在途态 → 重新接入同设备 | §4.2.5/4.2.8 断言：场站与记忆库保留、无重复 id、重新接入成功 |
| 5.7 | **执行失败回滚 → 重新确认（新）** | 确认后执行失败（如端口占用 PORT_BIND_FAILED）→ 回滚 | §4.8.7 断言：config 未变更、方案保留、button 重现、重新确认可再次执行 |

---

## 6. 断言工具库

`c4/test/agent/python/assertions.py` — 共享断言函数：

```python
# ── 副作用断言 ──

def assert_config_json_valid(config_path: Path) -> dict: ...
def assert_shm_ids_assigned(config: dict) -> None: ...
def assert_writer_reader_from_registry(config: dict, registry_dir: Path) -> None: ...
def assert_no_tmp_file(config_dir: Path) -> None: ...
def assert_config_shm_process_consistent(config: dict, shm_mgr_client) -> None: ...

# ── 按钮/状态断言（新架构） ──

def wait_button_arm(stream) -> bool:
    """等待 SSE 流中的 button_arm 语义事件（超时返回 False）。"""

def wait_button_disarm(stream, reason: Optional[str] = None) -> bool:
    """等待 button_disarm{reason} 事件；reason 可选断言（方案过期/缺口置位/整体取消/锁侧撤回）。"""

def assert_button_not_armed(stream) -> None:
    """断言整个 SSE 流中未出现 button_arm（L2 拦截类用例）。"""

def assert_rollback_keeps_plan(agent, stream) -> None:
    """执行失败回滚后：hasAccessPlan == True 且后续 button_arm 重现（§2.8/§4.8.7）。"""

# ── 语言约束断言 ──

# 无例外黑名单（任何场景均禁止）——含新架构内部名
STRICT_BLACKLIST = [
    r'\bshm_id\b', r'\bshm\b(?!\w*mgr)',
    r'\bMCP\b',
    r'\bCONFIG_MISSING_SECTION\b', r'\bDUPLICATE_KEY\b',
    r'\bSHM_NOT_CREATED\b', r'\bSHM_SYSCALL_FAILED\b',
    r'\bSHM_CORRUPTED\b',
    r'\bgeneratePlanSteps\b', r'\bvalidate_points\b', r'\bpoint_rules\b',
    r'\bbutton_arm\b', r'\bbutton_disarm\b',
    r'\bconfig_schema\b', r'\badjust_shm\b', r'\bpoint_count\b', r'\bmax_points\b',
]

# 场景豁免黑名单（方案展示 / 能力介绍时放行）
CONTEXTUAL_BLACKLIST = [
    r'(?<!\w)Modbus(?!\s*TCP)', r'Modbus TCP',
    r'IEC\s*104', r'IEC104',
    r'ASFP2',
    r':\d{2,5}',
]

JSON_LEAK_PATTERNS = [
    r'"[a-zA-Z_]+"\s*:',
    r'\{\s*"[^"]+"\s*:',
]

def assert_no_technical_terms(text, allow_protocols=False, allow_ports=False): ...
def assert_no_json_leak(text: str) -> None: ...
```

> **历史名迁移**：`output_plan_steps` / `output_access_plan` / `interrupt` 已从断言库移除——
> 它们不再是生产接口；若响应文本中出现（实现残留），由 STRICT_BLACKLIST 的内部标识类
> 捕获（实现完成后按需增补具体词项）。

---

## 7. 运行方式

```bash
# 全部测试（L1 + L2）
cd c4/test/agent
ZHIPU_API_KEY=sk-xxx C4_AGENT_PATH=/path/to/c4_agent pytest python/ -v

# 仅 L1（不需要 LLM API key）
pytest python/ -v -m "not llm"

# 仅 L2
ZHIPU_API_KEY=sk-xxx pytest python/ -v -m llm

# 指定单文件
pytest python/test_registry.py -v
```

> **L2 测试执行时间警告**：全量 L2 可能超过 30 分钟。建议日常开发仅跑 L1，L2 按 batch
> 分组并在 CI 中设为可选。

---

## 8. 与其他测试的关系

| 测试目录 | 范围 | 被测对象 |
|---------|------|---------|
| `c4/test/c4_fun_00012/` — `c4_fun_00085/` | MCP 服务独立功能测试 | 单个 Go MCP 服务的工具签名、错误码、数据流 |
| `c4/test/c4_fun_00086~00090/`（待建） | validate_points 黑盒测试 | 各 MCP 服务的点表语义校验工具（规格 = agent.md §2.7.1 + 各服务文档；MCP 实现落地后按 AGENTS.md 规则 3 补建） |
| `c4/test/agent/` | Agent 整体功能测试 | c4_agent 的启动、HTTP API、编排器回合、LLM 提取端到端行为 |
| `c4/test/func_case_e2e/` | func_test_case.md 用例的端到端 runner | 隔离 agent 实例驱动用例 1/10/16~29（确认机制已按按钮通道适配） |
| point_rules 单测（TS） | 共享校验库单元测试 | 17 型跨度/身份查重/重叠检测（`agent/test/executor/point_rules.test.ts`，不在本黑盒方案） |

---

## 9. 参考

| 文档 | 路径 | 相关内容 |
|------|------|---------|
| Agent 架构设计 | `c4/docs/design/agent.md` | 被测系统的完整设计（九阶段流水线、§2.4 锁与取消、§2.7 校验分层、§2.8 按钮） |
| C4 整体架构 | `c4/docs/design/c4_architecture.md` | config.json 格式、MCP 服务配置 |
| 共享内存管理 | `c4/docs/design/c4_shm_manager.md` | adjust_shm 行为、错误码 |
| 功能用例记录 | `c4/test/func_test_case.md` | 缺陷回归用例（历史记录，不改） |
| 测试行为规则 | `c4/AGENTS.md` §行为规则 | 规则3（按 README 规格不参考源码）、规则4（验证流程） |

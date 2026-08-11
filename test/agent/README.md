# C4 Agent 功能测试方案

> **版本**：v1.2.0 | **最后更新**：2026-08-08
>
> **设计依据**：[agent.md](../../docs/design/agent.md) — C4 Agent 系统架构设计
>
> **测试范围**：agent.md 覆盖的全部实现功能，包括确定性代码逻辑和 LLM 驱动行为。

---

## 1. 总则

### 1.1 测试目标

验证 C4 Agent 可执行文件 (`c4_agent`) 的功能正确性，覆盖从用户输入到 MCP 服务配置下发
的完整数据接入流程。

### 1.2 测试原则

| 原则 | 说明 |
|------|------|
| **黑盒功能测试** | 仅通过 c4_agent 的 HTTP API 和启动行为进行测试，不侵入 Agent 内部代码 |
| **零额外接口** | 不为测试新增 MCP 工具、测试端点或调试开关。被测接口即生产接口 |
| **不 mock LLM** | 所有测试使用真实 c4_agent + 真实 LLM。LLM 驱动的行为通过结构验证、副作用验证和约束验证来断言，而非精确值比对 |
| **可观测面即断言面** | HTTP 响应内容 / SSE 事件流 / 文件系统 / 进程状态 — 只断言这些可观察的东西 |

### 1.3 被测接口

| 接口 | 来源 | 说明 |
|------|------|------|
| Agent 启动流程 | agent.md §3.2.3 | 启动时的配置加载、MCP 连接、无条件 Stop-Start |
| `GET /api/services` | agent.md §3.3, §3.5 | Registry L1 服务摘要查询 |
| `GET /api/state` | agent.md §3.1 | Agent 运行时状态查询：`phase`、`hasAccessPlan`、`lastError` |
| `POST /api/chat` (SSE) | agent.md §3.1, §3.5 | SuperWorker 对话、子代理调度、方案生成、执行触发 |
| `POST /api/upload` | agent.md §3.5 | 文件上传 → doc-parser 解析 |

### 1.4 测试层次

```
┌──────────────────────────────────────────────────────────────┐
│ L1: 确定性功能测试                                              │
│ 不依赖 LLM 推理 — 精确值/精确状态断言                            │
│                                                               │
│ · Registry 加载 → GET /api/services 响应结构                    │
│ · Agent 启动恢复 → 各崩溃时刻的 config / shm / 进程状态一致性     │
│ · 执行模块产物 → 完整数据流完成后 config.json / MCP 服务状态      │
└──────────────────────────────────────────────────────────────┘
                               │
                               ▼
┌──────────────────────────────────────────────────────────────┐
│ L2: LLM 驱动功能测试                                           │
│ 依赖 LLM 推理 — 结构/副作用/约束断言，不校验具体措辞              │
│                                                               │
│ · 对话路由 → 文档解析结果是否出现在对话文本中                  │
│ · 方案生成 → AccessPlan 结构 + 确认信号                         │
│ · 执行验证 → 用户确认后的 config.json 产物 + 服务启动            │
│ · 非技术语言 → 响应文本黑名单检查                                │
└──────────────────────────────────────────────────────────────┘
```

> **注**：此 L1/L2 不同于 agent.md §1.2.1 中的 ✅/❌ 分类。agent.md 的分类是**实现方式**
> （是否依赖 LLM 推理），此处按**断言策略**分层：
> - L1 = 不用 LLM 就能验证的行为 → 精确断言
> - L2 = 必须经过 LLM 才能触发的行为 → 宽松断言
>
> 执行模块（agent.md 中的 ✅）的部分测试（如 config.json 产物验证）因触发路径经过 LLM
> 而划入 L2，但其副作用验证仍为精确断言。

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
| LLM API Key | `DEEPSEEK_API_KEY` 环境变量（L2 测试必需） |
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
      1. 制备 tmp_path 作为 /etc/c4/ 替代（agent.json + config.json + mcp-registry/）
      2. 启动 c4_shm_manager（MCP 进程）
      3. 启动 c4_agent --config-dir <tmp_path>
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
      - confirm(interrupt_id: str) → SSEEventStream

    SSEEventStream 提供:
      - events: list[SSEEvent]  — metadata, messages, interrupt, end
      - wait_for_event(type: str, timeout: float) → SSEEvent | None
      - text_content() → str    — 拼接所有 assistant 消息文本
    """

# ── 环境制备 helpers (conftest.py 内) ──

def write_agent_json(config_dir: Path) -> None:
    """写入最小 agent.json。LLM 配置中 temperature=0 确保确定性输出；server 端口；
    registry 路径指向 mcp-registry/。"""

def write_config_json(config_dir: Path, content: dict | None) -> None:
    """写入 config.json。content=None 表示不创建（模拟首次启动）。"""

def corrupt_config_json(config_dir: Path) -> None:
    """将 config.json 截断为损坏的 JSON。"""

def write_config_bak(config_dir: Path, content: dict) -> None:
    """写入 config.json.bak（用于损坏恢复测试）。"""

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
    config_dir: Path       # 配置目录（/etc/c4/ 等效路径）

    def get_services(self) -> dict:
        """GET /api/services → JSON"""

    def get_state(self) -> dict:
        """GET /api/state → {phase, hasAccessPlan, lastError}"""

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

**前置**：Agent 正常启动，`mcp-registry/` 含完整 JSON 文件

| # | 用例 | 输入条件 | 断言 |
|---|------|---------|------|
| 1.1 | 返回所有已注册服务 | `mcp-registry/` 含 c4_modbus_client, c4_iec104_client, c4_asfp2_server, c4_asfp2_client, c4_influxdb_client 共 5 个 JSON | `GET /api/services` 返回数组长度 = 5 |
| 1.2 | 每项含 L1 必须字段 | 同上 | 每项含 `service_type`, `display_name`, `role`, `protocols[]` |
| 1.3 | protocols 含 description 和 selection_rules | 同上 | `protocols[0]` 含 `protocol`, `description`, `selection_rules[]` |
| 1.4 | L1 不含 L2 字段 | 同上 | 每项**不**含 `config_schema`, `binary_path`, `error_mappings` |
| 1.5 | Registry 目录为空 | `mcp-registry/` 为空目录 | 返回空数组 `[]`，Agent 正常就绪（不崩溃） |
| 1.6 | Registry 目录缺失 | `mcp-registry/` 不存在 | Agent 不崩溃；`GET /api/services` 返回 200（空数组）或 5xx（启动失败），两种行为均视为合理防御 |
| 1.7 | 单个 JSON 文件损坏 | `mcp-registry/` 中 1 个文件为非 JSON | Agent 不崩溃；`GET /api/services` 正常返回其余有效服务（损坏文件不导致全局加载失败） |

### 3.2 Agent 启动恢复

**被测对象**：Agent 启动时的无条件 Stop-Start 协议（agent.md §3.2.3）

**被测接口**：Agent 进程启动行为 + 文件系统副作用

**通用断言维度**：

| 维度 | 验证方法 |
|------|---------|
| Agent 就绪 | `GET /api/services` 返回 200 |
| config.json 状态 | 读取 config.json + config.json.bak，比较内容 |
| MCP 服务进程状态 | `ps aux | grep` 检查数据路径 MCP 进程是否存在 |
| 共享内存状态 | 通过 c4_shm_manager MCP 调用检查 shm 块分配（可选深验证） |

#### 3.2.1 首次启动

| # | 用例 | 初始态 | 断言 |
|---|------|--------|------|
| 3.2.1.1 | 无 config.json — 首次启动 | `config.json` 不存在 | Agent 就绪，不创建 config.json，无数据路径 MCP 进程；adjust_shm 在此场景下会由 c4_shm_manager 自动创建默认 shm（100k 点），shm 存在即视为正确 |
| 3.2.1.2 | 无 config.json — 仅 c4_shm_manager 在运行 | 同上 | c4_shm_manager 进程存活，其余 MCP 无进程；shm 段存在 |

#### 3.2.2 正常重启

| # | 用例 | 初始态 | 断言 |
|---|------|--------|------|
| 3.2.2.1 | 有效 config.json，服务未运行 | config.json 含 1 个 c4_modbus_client + 1 个 c4_asfp2_client | Agent 就绪；stop(幂等) → adjust_shm → start；两服务进程均启动 |
| 3.2.2.2 | 有效 config.json，服务已在运行 | 先启动 Agent（同 3.2.2.1），再重启 Agent | 同 3.2.2.1；重启过程中数据中断应尽量短 |

#### 3.2.3 配置损坏恢复

| # | 用例 | 初始态 | 断言 |
|---|------|--------|------|
| 3.2.3.1 | config 损坏，.bak 有效 | config.json = 截断 JSON；config.json.bak = 完好的配置（含 1 个 modbus） | Agent 从 .bak 恢复 config.json 后正常启动，服务运行 |
| 3.2.3.2 | config 损坏，.bak 不存在 | config.json = 截断 JSON；无 .bak | 等同于首次启动（3.2.1.1） |
| 3.2.3.3 | config 损坏，.bak 也损坏 | 两者都损坏 | 等同于首次启动（3.2.1.1） |

#### 3.2.4 崩溃恢复

**统一路径原则**（agent.md §3.2.3）：无论崩溃在 stop/adjust_shm/start 哪一步，
重启后全路径重走，保证 config.json ↔ shm ↔ 进程状态三者一致。

**测试策略**：不尝试在精确时刻 kill 进程（Stop-Start 全流程 < 5ms，外部 SIGKILL
无法精确定位代码行）。改为验证**恢复结果的一致性**：构造不同崩溃场景的初始态 →
kill Agent → restart → 断言三者一致。

模拟方法：先让 Agent 正常运行 → `agent.kill()` 杀掉进程 → 重新 `agent.restart()` → 验证。

| # | 用例 | 初始态构造方法 | 断言（重启后三者一致） |
|---|------|--------------|----------------------|
| 3.2.4.1 | 正常运行中崩溃 | 启动 Agent 含 config.json + 运行中的服务 → kill | config.json 内容不变；所有 config 中声明的 MCP 服务进程恢复运行；shm 块分配与 config 一致 |
| 3.2.4.2 | config 更新后未 stop 时崩溃 | 手动写入新 config.json（比当前多 1 个服务）→ kill（不经过 Agent 重启） | 重启后 config.json = 新版本；shm 分配反映新 config；新服务进程运行 |
| 3.2.4.3 | stop 完成后崩溃 | 重启 Agent → 等待就绪 → kill（此时 stop 已执行，start 可能未完成） | 同 3.2.4.1 |
| 3.2.4.4 | start 中途崩溃 | 重启 Agent → 等待就绪 → kill（同上） | 同 3.2.4.1 |

> **一致性验证方法**：
> - `config.json`：文件内容与写入前快照一致（含所有服务实例、point 定义）
> - `shm`：通过 c4_shm_manager MCP 调用确认 config 中的每个 point 有对应已分配的 shm 块（shm_id ≠ 0）
> - `进程`：config 中声明的每种 MCP service_type 对应至少一个运行中的进程

> **注**：3.2.4.2 需预先准备两份 config.json（旧版 1 服务、新版 2 服务），不依赖 LLM。
> 标记为 L1 可直接运行。其余三个场景均基于同一套初始 config.json + 正常启动后 kill。

---

## 4. L2 测试 — LLM 驱动功能

### 4.1 通用注意事项

#### LLM 非确定性处理

| 策略 | 适用场景 | 示例 |
|------|---------|------|
| **结构断言** | JSON 产物、对话文本模式 | AccessPlan 含 `devices[]`、方案确认文本含"确认"关键词 |
| **副作用断言** | 文件系统、进程状态 | config.json 写入后 `shm_id` 必须 ≠ 0 |
| **黑名单断言** | 非技术语言约束 | 响应文本不出现 `shm_id`, `MCP`, `CONFIG_MISSING_SECTION` |
| **存在性断言** | 关键信息传达 | 方案描述含设备名、转发目标描述 |
| **容忍重试** | 偶发 LLM 输出格式偏差 | 失败时重试最多 2 次（共 3 次尝试） |

#### L2 测试超时

每个 LLM 驱动的对话可能耗时 10–60 秒。单个 SSE 流最长等待 120 秒，超时视为失败。

#### L2 测试的可跳过性

若 `DEEPSEEK_API_KEY` 未设置或 LLM 不可达，L2 测试应 `pytest.skip` 而非失败。
通过 `pytest.mark.llm` 标记区分：

```bash
pytest -m "not llm"  # 仅跑 L1
pytest -m llm         # 仅跑 L2
pytest                # 全跑（L2 在无 API key 时自动 skip）
```

---

### 4.2 对话路由

**被测对象**：SuperWorker 意图识别与子代理调度（agent.md §3.1）

**被测接口**：`POST /api/chat`

| # | 用例 | 输入 | 预期（可观察副作用） |
|---|------|------|---------------------|
| 4.2.1 | 上传文档触发 doc-parser | `POST /api/upload`（上传 xlsx 点表）+ `POST /api/chat` "接入华能阿拉善1#风机" | 对话文本中出现从点表解析出的设备名/协议名/数据点信息（非空，非"无法解析"） |
| 4.2.2 | 查询类消息不触发子代理 | `POST /api/chat` "现在有哪些设备在运行" | 对话文本中**不**出现设备名+协议+数据点的结构化枚举（即不是 doc-parser 的输出格式） |
| 4.2.3 | 问候类消息直接回答 | `POST /api/chat` "你好" | SSE 流正常关闭，无 error，有 assistant 文本回复 |
| 4.2.4 | 空消息处理 | `POST /api/chat` "" | Agent 不崩溃，返回合理的引导性回复或提示 |

> **注**：不直接断言 SSE 事件名（如 `subagent_start`），因为事件格式依赖 deepagents/LangGraph
> 框架实现细节，不属于 agent.md 定义的接口。改为验证 doc-parser 的可观察副作用——
> 解析结果是否出现在对话文本中。

### 4.3 文档解析 (doc-parser)

**被测对象**：doc-parser 子代理（agent.md §3.2 — C4_FUN_00002）

**被测接口**：`POST /api/upload` + `POST /api/chat`

| # | 用例 | 输入 | 预期 |
|---|------|------|------|
| 4.3.1 | 解析合法 xlsx 点表 | 上传含 "windspeed, addr=1000" 等字段的点表 | SSE 流中文档解析结果含设备名、协议、数据点列表（由 doc-parser 子代理产出） |
| 4.3.2 | 解析合法 csv 点表 | 上传 CSV 格式点表 | 同上 |
| 4.3.3 | 上传不支持的文件格式 | 上传 .txt 或二进制文件 | Agent 给出友好提示（非技术语言的错误描述），不崩溃 |
| 4.3.4 | 上传损坏的 xlsx | 上传截断/损坏的 Excel | 同上 |
| 4.3.5 | 点表缺少关键字段 | 点表无 IP 地址列 | Agent 列出已有信息 + 明确指出缺失字段（如 IP） |

### 4.4 方案生成 (plan-generator)

**被测对象**：plan-generator 子代理 → AccessPlan 生成（agent.md §3.2 — C4_FUN_00004）

**被测接口**：`POST /api/chat`（doc-parser 完成后继续对话）

| # | 用例 | 输入 | 预期 — 结构 | 预期 — 交互 |
|---|------|------|------------|------------|
| 4.4.1 | Modbus → ASFP2 转发方案 | doc-parser 结果含 Modbus 设备 + "转发到中心侧" | 判断条件略宽松：只要对话文本包含接入方案的关键要素即可进入确认流程 | 对话文本含"确认"或等价关键词（表示等待用户确认） |
| 4.4.2 | 无转发目标时仅采集 | doc-parser 结果含 Modbus 设备，不提转发 | 对话文本含等待确认的信号（仍需确认方案） | 方案描述仅含采集，不含转发 |
| 4.4.3 | 无法推断协议 | doc-parser 结果缺少协议信息 | — | Agent 主动询问澄清（非技术语言），不进入 confirm 状态 |
| 4.4.4 | 协议无可用服务 | 设备使用 Agent 不支持的协议（如 DNP3） | — | Agent 告知无可用服务，不生成错误方案 |

### 4.5 用户确认与拒绝

| # | 用例 | 输入 | 预期 |
|---|------|------|------|
| 4.5.1 | 用户确认方案 | interrupt 后发送确认消息（如 "确认" / "好的"） | 流程继续进入 step-decomposer → 执行 |
| 4.5.2 | 用户拒绝方案 | interrupt 后发送拒绝消息（如 "取消" / "不对"） | 流程停止，不生成 config.json，不执行 Stop-Start |

### 4.6 执行验证（副作用检查）

**被测对象**：step-decomposer + 执行模块（agent.md §3.2 — C4_FUN_00044 + C4_FUN_00006/00007）

**触发路径**：完整 L2 流程：上传点表 → doc-parser → plan-generator → 用户确认 → step-decomposer + 执行

#### 4.6.1 add 操作（首次接入 + 追加）

| # | 用例 | 预期 — config.json | 预期 — MCP 服务 |
|---|------|-------------------|-----------------|
| 4.6.1.1 | 首次接入（Modbus + ASFP2 转发） | config.json 含 `c4_shm_manager` + `c4_modbus_client[]` + `c4_asfp2_client[]`；所有 `shm_id != 0`；default 字段已填充 | c4_modbus_client 和 c4_asfp2_client 进程运行中 (ps) |
| 4.6.1.2 | 首次接入（仅采集，无转发） | config.json 含 `c4_modbus_client[]`，**不**含 `c4_asfp2_client[]`；reader 为空或不存在 | — |
| 4.6.1.3 | 原子写入 | 无残留 .tmp 文件；config.json.bak 存在（首次接入时为 config.json 的副本；非首次时为写入前版本） | — |
| 4.6.1.4 | writer/reader 分类 | `c4_shm_manager.writer[]` 含所有 Registry 中 role=writer 的 service_type（动态读取 Registry 验证），`reader[]` 同理 | — |
| 4.6.1.5 | 追加设备（第二次接入） | 新实例追加到 `c4_modbus_client[]`，旧实例完整保留；`c4_shm_manager.writer[]` 不重复添加相同 service_type | 新服务启动，旧服务不受影响 |

#### 4.6.2 modify 操作（修改已有实例）

**场景**：已有 config.json 含 `hnals_1_scada`（IP: 192.168.110.1）。用户请求修改该设备的 IP 和/或添加新采集点。

| # | 用例 | 触发方式 | 预期 |
|---|------|---------|------|
| 4.6.2.1 | 修改实例参数（IP/端口） | 在已有设备的基础上，请求"将 1#风机的 IP 改为 192.168.110.5" | config.json 中 `hnals_1_scada.ip` = 192.168.110.5，其余字段不变；服务重启后使用新 IP |
| 4.6.2.2 | 修改采集点参数 | 请求"将 windspeed 的寄存器地址从 1000 改为 1002" | `hnals_1_scada.points[]` 中 `windspeed.addr` = 1002；其他字段不变；shm_id 不变 |
| 4.6.2.3 | 新增采集点 | 请求"给 1#风机增加风向采集点" | `hnals_1_scada.points[]` 末尾追加新 point（含新 `id`）；旧 point 保留且 shm_id 不变；adjust_shm 为新 point 分配 shm_id |
| 4.6.2.4 | 删除采集点 | 请求"不再采集 1#风机的温度数据" | `hnals_1_scada.points[]` 中移除 temperature；adjust_shm 回收对应 shm 块 |
| 4.6.2.5 | 修改不存在的实例 | 请求修改一个不存在的设备 ID | Agent 返回友好错误提示（非技术语言），不修改 config.json |

#### 4.6.3 delete 操作（删除实例）

**场景**：已有 config.json 含 2 个 `c4_modbus_client` 实例。

| # | 用例 | 触发方式 | 预期 |
|---|------|---------|------|
| 4.6.3.1 | 删除单个实例（仍有同类型其他实例） | 请求"停用 2#风机" | 目标实例从 `c4_modbus_client[]` 中移除；`c4_shm_manager.writer[]` 仍含 `c4_modbus_client`；旧 shm 块被 adjust_shm 回收 |
| 4.6.3.2 | 删除最后一个实例 | 先删到只剩 1 个 modbus → 再请求删除最后一个 | 目标实例移除 → `c4_modbus_client[]` 为空 → `c4_shm_manager.writer[]` 移除 `c4_modbus_client` |
| 4.6.3.3 | 删除被 Reader 引用的设备（相关性检查） | config 中有 modbus(hnals_1_scada) + asfp2_client(key 引用 hnals_1_scada.windspeed) → 请求停用 1#风机 | 执行后 config.json 中目标设备被移除，同时 asfp2_client 的 points[] 中不再有指向已删除设备的 key 引用 |
| 4.6.3.4 | 删除不存在的实例 | 请求删除不存在的设备 ID | Agent 返回友好错误提示，不修改 config.json |

### 4.7 非技术语言约束

**被测对象**：SuperWorker 系统提示中的非技术语言规则（agent.md §3.1 硬约束）

**被测接口**：所有 `POST /api/chat` 的文本回复

**黑名单**（硬约束 — 响应文本中**不得**出现，除非标注了例外）：

| 类别 | 禁止词 | 例外场景 | 来源 |
|------|--------|---------|------|
| 共享内存/内部术语 | shm_id, shm, 共享内存, adjust_shm, point_count, max_points | **无例外** — 任何场景均禁止 | §3.1 规则2 |
| 内部标识/错误码 | MCP, output_plan_steps, config_schema, CONFIG_MISSING_SECTION, DUPLICATE_KEY, SHM_CORRUPTED, SHM_NOT_CREATED, SHM_SYSCALL_FAILED | **无例外** | §3.1 规则3,4 |
| 协议术语 | Modbus TCP, IEC104, ASFP2 | 方案展示等待确认时可用协议名 + 通俗解释（如"通过 Modbus 通信采集风机数据"）；能力介绍时可用协议名（如"我可以采集 Modbus 设备的数据"） | §3.1 规则1 |
| 端口号 | 数字形式的端口（如 `:502`） | 方案展示时配合通俗解释可用（如"通过标准端口连接设备"） | §3.1 规则4 |
| JSON 原文 | 连续出现 `"key":` 模式或多层 `{}` 嵌套 | **无例外** — 禁止向用户直接展示 JSON 结构 | §3.1 规则4 |

| # | 用例 | 触发方式 | 断言 |
|---|------|---------|------|
| 4.7.1 | 正常对话不含技术术语 | `POST /api/chat` "你好，介绍一下你能做什么" | 响应文本不匹配黑名单的**无例外**类（共享内存/内部标识/错误码/JSON）；协议名在能力介绍场景豁免 |
| 4.7.2 | 方案展示含通俗解释 | 方案确认（interrupt 前）的文本 | 含设备名 + 操作描述；不匹配黑名单的**无例外**类；协议名 + 端口号在此场景豁免 |
| 4.7.3 | 错误场景不暴露内部信息 | 制造错误（如上传损坏文件）后对话 | 错误描述不匹配任何黑名单（包括协议名 — 错误场景无豁免） |
| 4.7.4 | 全程不展示 JSON 结构 | 整个对话中 | 无连续 `"key":` 模式、无 `{"...": {...}}` 多层嵌套块 |

**测试方法**：从所有 L2 用例的 SSE 流中提取 `assistant` 文本，按场景分类运行对应黑名单检查。

---

### 4.8 错误恢复路径

**被测对象**：Stop-Start 安全协议中的错误处理分支（agent.md §3.2, §3.2.2）

**测试策略**：通过构造错误条件（无效 config、冲突 key 等）触发 adjust_shm 的不同失败路径，
验证恢复行为符合设计。

| # | 用例 | 错误条件构造 | 预期恢复行为 |
|---|------|------------|-------------|
| 4.8.1 | adjust_shm 失败 — config 类错误（回退 .bak） | 在 config.json 中写入重复的 `{service_id}.{point_id}` 全局 key → 触发 `DUPLICATE_KEY` | config.json 恢复为 .bak 内容；已 stop 的服务被 restart；操作 abort；用户收到底层性的错误描述（但非技术语言） |
| 4.8.2 | adjust_shm 失败 — config 类错误（缺失 section） | config.json 含 reader 但 writer 为空 → 触发 `CONFIG_MISSING_SECTION` | 同上（回退 config.json.bak → restart 服务） |
| 4.8.3 | adjust_shm 失败 — config 类错误（未知 reader key） | asfp2_client points[0].key 指向不存在的 writer → 触发 `UNKNOWN_READER_KEY` | 同上 |
| 4.8.4 | adjust_shm 失败 — 非 config 类错误（不回退 config） | 挂载一个小容量 tmpfs 到 `/dev/shm`（`sudo mount -o remount,size=1M /dev/shm`）→ 触发 `SHM_SYSCALL_FAILED` | config.json **不**回退；已 stop 的服务被 restart；操作 abort；用户被告知"系统问题，请稍后重试" |
| 4.8.5 | start 部分失败 | adjust_shm 成功，但某个 MCP 服务的 start 返回 error | 成功的服务保持运行；失败的服务被报告；用户收到失败列表 + "其余正常运行" |

> **4.8.1–4.8.3 的实现依赖**：需要 step-decomposer 生成含冲突的 AccessPlanSteps。
> 可通过构造特定的 AccessPlan JSON 文件作为 plan-generator 的 mock 输出来绕过 LLM
> 生成环节，直接测试执行模块的错误处理。

> **4.8.4 的实现依赖**：测试前通过 `sudo mount -o remount,size=1M /dev/shm` 限制 tmpfs 大小
> （需要 `SUDO_PASSWORD` 环境变量），测试后恢复 `sudo mount -o remount,size=... /dev/shm`。
> 若环境不允许 remount（如容器），此用例标记 `pytest.mark.skip`。

> **4.8.5 的实现依赖**：需要能够注入一个会 start 失败的 MCP 服务（如 mock 二进制
> 或配置不存在的 binary_path）。

#### 4.8.6 step-decomposer 失败 — 用户消息验证

**被测对象**：step-decomposer 失败时 SuperWorker 的非技术语言错误消息（agent.md §3.2.2）

| # | 用例 | 触发方式 | 预期 |
|---|------|---------|------|
| 4.8.6 | step-decomposer 失败 → 用户收到非技术语言提示 | 上传一个内容混乱的点表（如字段名拼写错误、数值越界），使 doc-parser 解析后 plan-generator 生成的方案在 step-decomposer 分解时失败 | 用户收到的错误消息不匹配任何黑名单术语；消息含引导性内容（如"请重新描述需求"）；不残留 config.json.tmp |

> **4.8.6 的实现注记**：此用例仅验证用户在 step-decomposer 失败时看到的最终错误消息。
> agent.md §3.2.2 规定的 "output_plan_steps 校验失败 → 重试一次 → 仍失败" 机制属于
> 子代理内部逻辑，适合通过 TypeScript 单元测试（`GenericFakeChatModel`）验证，不在本
> 黑盒测试方案覆盖范围内。

### 4.9 AgentState 持久化（agent.md §3.1）

| # | 用例 | 触发方式 | 断言 |
|---|------|---------|------|
| 4.9.1 | 接入流程中途重启 → 状态恢复 | 完成 doc-parser + plan-generator → `agent.get_state()` 确认 `hasAccessPlan = true` → `agent.kill()` → `agent.restart()` | 重启后 `agent.get_state()` 返回 `hasAccessPlan = true`（用户无需重新上传点表即可继续） |
| 4.9.2 | 用户确认后中断 → 状态保持 | 4.9.1 后 → 发送确认消息 → `agent.kill()`（step-decomposer 执行前）→ `agent.restart()` | 重启后 `agent.get_state()` 返回 `phase` 反映已确认状态，Agent 可继续执行 |
| 4.9.3 | 执行完成后状态重置 | 完成一次完整接入 → `agent.get_state()` | `phase = "idle"`, `hasAccessPlan = false` |
| 4.9.4 | 状态重置后可处理新接入 | 在 4.9.3 之后发起新的接入请求 | Agent 正常启动新流程，不混淆上一次接入的设备和配置 |

> **`GET /api/state` 响应格式**：
> ```json
> { "phase": "idle", "hasAccessPlan": false, "lastError": null }
> ```
> 此端点是 agent.md §3.1 AgentState 的最小可观测出口，不暴露完整 accessPlan 内容。
>
> ⚠️ **前提**：§4.9.1、§4.9.2 依赖 LangGraph checkpoint 在 `kill()` → `restart()` 后
> 自动恢复 `phase` 和 `accessPlan` 字段。这要求 `createDeepAgent` 在初始化时能从
> `state.backend: "filesystem"` (§5.1) 加载 persistent checkpoint。若实现采用
> `StateBackend` 但不自动恢复，则这两个用例需降级为 TypeScript 单元测试（mock checkpoint）。

---

## 5. 端到端场景

完整数据接入流程的端到端验证（综合 L1 + L2 断言）：

| # | 场景 | 流程 | 关键断言点 |
|---|------|------|-----------|
| 5.1 | 单设备 Modbus 接入 + ASFP2 转发 | 上传点表 → 解析 → 生成方案 → 确认 → 执行 | §4.6.1.1 首次接入断言 + §4.7 非技术语言 + 启动恢复基本场景 |
| 5.2 | 单设备 Modbus 接入（仅采集） | 同上，无转发 | §4.6.1.2 无 reader 服务断言 |
| 5.3 | 首次接入后重启 | 完成 5.1 → kill Agent → restart | §3.2.4.1 崩溃恢复断言：config/shsm/进程三者一致 |
| 5.4 | 修改 + 追加完整生命周期 | 完成 5.1 → 追加第二个风机 → 修改第一个风机的采集点参数 → 给第一个风机增加新采集点 | §4.6.1.5 追加断言 + §4.6.2 修改断言；旧设备数据不受影响 |
| 5.5 | Add → Modify → Delete 完整生命周期 | 完成 5.4 → 删除第二个风机 → 再删除第一个风机（含 Reader 引用的相关性检查） | §4.6.3.1 单删断言 + §4.6.3.3 相关性检查断言；最终 config.json 恢复到空 |

---

## 6. 断言工具库

`c4/test/agent/python/assertions.py` — 共享断言函数：

```python
# ── 结构断言 ──

def assert_valid_access_plan(plan: dict) -> None:
    """验证 AccessPlan 含 site.devices.forward_targets 结构。"""

# ── 副作用断言 ──

def assert_config_json_valid(config_path: Path) -> dict:
    """读取 config.json，验证 JSON 有效，返回 parsed dict。"""

def assert_shm_ids_assigned(config: dict) -> None:
    """验证所有 services[].points[].shm_id != 0。"""

def assert_writer_reader_from_registry(config: dict, registry_dir: Path) -> None:
    """
    验证 c4_shm_manager.writer/reader 分类与 Registry JSON 的 role 字段一致。
    动态读取 mcp-registry/ 中的 role 声明，不硬编码 service_type 列表。
    """

def assert_no_tmp_file(config_dir: Path) -> None:
    """验证无残留 config.json.tmp。"""

# ── 进程断言 ──

def assert_process_running(process_name: str) -> None:
    """ps aux 验证进程存在。"""

# ── 一致性断言（崩溃恢复用） ──

def assert_config_shm_process_consistent(config: dict, shm_mgr_client) -> None:
    """验证 config.json ↔ shm ↔ 进程状态三者一致。"""

# ── 语言约束断言 ──

# 无例外黑名单（任何场景均禁止）
STRICT_BLACKLIST = [
    r'\bshm_id\b', r'\bshm\b(?!\w*mgr)',  # shm 但允许 shm_manager（文件名/进程名场景）
    r'\bMCP\b',
    r'\bCONFIG_MISSING_SECTION\b', r'\bDUPLICATE_KEY\b',
    r'\bSHM_NOT_CREATED\b', r'\bSHM_SYSCALL_FAILED\b',
    r'\bSHM_CORRUPTED\b',
    r'\boutput_plan_steps\b', r'\bconfig_schema\b',
    r'\badjust_shm\b', r'\bpoint_count\b', r'\bmax_points\b',
]

# 场景豁免黑名单（方案展示 / 能力介绍时放行）
CONTEXTUAL_BLACKLIST = [
    r'(?<!\w)Modbus(?!\s*TCP)', r'Modbus TCP',  # 协议名
    r'IEC\s*104', r'IEC104',
    r'ASFP2',
    r':\d{2,5}',  # 端口号模式
]

# JSON 泄漏检测模式
JSON_LEAK_PATTERNS = [
    r'"[a-zA-Z_]+"\s*:',     # "key": 模式
    r'\{\s*"[^"]+"\s*:',     # {"key": 模式
]

def assert_no_technical_terms(text: str,
    allow_protocols: bool = False,
    allow_ports: bool = False) -> None:
    """
    验证文本不含黑名单术语。
    allow_protocols=True: 放行协议名（方案展示 / 能力介绍场景）
    allow_ports=True: 放行端口号（方案展示场景）
    无例外黑名单始终检查。
    """

def assert_no_json_leak(text: str) -> None:
    """验证文本不含 JSON 结构泄漏。"
```

---

## 7. 运行方式

```bash
# 全部测试（L1 + L2）
cd c4/test/agent
DEEPSEEK_API_KEY=sk-xxx C4_AGENT_PATH=/path/to/c4_agent pytest python/ -v

# 仅 L1（不需要 LLM API key）
pytest python/ -v -m "not llm"

# 仅 L2
DEEPSEEK_API_KEY=sk-xxx pytest python/ -v -m llm

# 指定单文件
pytest python/test_registry.py -v
```

> **L2 测试执行时间警告**：全量 L2 测试（~20 个用例 × 10–60s LLM 响应）可能超过 30 分钟。
> 建议：
> - 日常开发仅跑 L1（`-m "not llm"`）
> - L2 测试按 batch 分组（如 `-m "llm and batch1"`），批次间设 cooling_off 避免 API 限速
> - CI 中可将 L2 设为可选/手动触发

---

## 8. 与 c4_fun_XXXXX/ 测试的关系

| 测试目录 | 范围 | 被测对象 |
|---------|------|---------|
| `c4/test/c4_fun_0053/` — `c4_fun_0060/` | MCP 服务独立功能测试 | 单个 Go MCP 服务的工具签名、错误码、数据流 |
| `c4/test/agent/` | Agent 整体功能测试 | c4_agent 可执行文件的启动、HTTP API、LLM 驱动的端到端行为 |

两者互补：
- `c4_fun_XXXXX/` 验证 MCP 服务本身的正确性（Agent 在实时数据路径之外）
- `c4/test/agent/` 验证 Agent 是否正确调用 MCP 服务、生成配置、处理对话

MCP 服务变更 → 跑对应的 `c4_fun_XXXXX/` 测试。
Agent 变更（agent.md 修改、TypeScript 代码变更）→ 跑 `c4/test/agent/` 测试。

---

## 9. 参考

| 文档 | 路径 | 相关内容 |
|------|------|---------|
| Agent 架构设计 | `c4/docs/design/agent.md` | 被测系统的完整设计 |
| C4 整体架构 | `c4/docs/design/c4_architecture.md` | config.json 格式、MCP 服务配置 |
| 共享内存管理 | `c4/docs/design/c4_shm_manager.md` | adjust_shm 行为、错误码 |
| 测试行为规则 | `c4/AGENTS.md` §行为规则 | 规则3（按 README 规格不参考源码）、规则4（验证流程） |

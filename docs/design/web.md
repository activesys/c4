# C4 Web 界面设计

> **版本**：v0.4.0 | **最后更新**：2026-10-06 | **父文档**：[agent.md](agent.md)
>
> **设计范围**：C4 Web 界面的页面、组件与交互设计，**仅覆盖后端已就绪的功能**——
> 对话式数据接入、文件上传、已接入 MCP 服务目录展示（含注册图标）、Agent 工作状态展示、
> 场站信息初始化与修改（§3.6）。
> 用户身份认证、告警通知、操作审计日志等后端未就绪的功能不在本次设计范围；
> MCP 服务的运行期热注册（免重启动态发现）不在本次设计范围（注册为部署期操作：root 安装单元，注册后重启 Agent 识别，见 §3.3）。
>
> **当前实现状态**：后端 HTTP API 已实现（`src/server/`，见 §1.3），前端已实现
> （`agent/frontend/`，React SPA）；视觉样式以 DeepSeek 浅色主题为基准（§4.4）。

---

## 1. 设计背景

### 1.1 定位

C4 Web 界面是 C4 实例的人机交互入口，运行于工业数据服务器上，面向不具备计算机专业知识的
场站工作人员。用户通过浏览器提交数据接入需求、上传配置文档、查看 MCP 服务目录与工作状态。

界面遵循 agent.md §3.5 的架构：**React SPA + Express Server**，通过 HTTP / SSE 与后端交互。
**Agent 不进入实时数据路径**——Web 界面只负责「人 ↔ Agent」的交互，不参与数据搬运。

### 1.2 设计原则

| 原则 | 说明 |
|------|------|
| 非技术语言 | 界面文案、状态提示、错误信息使用场站用户可理解的语言，避免协议级术语与内部错误码（C4_FUN_00005） |
| 流式反馈 | 对话与解析过程实时流式呈现，避免用户面对「黑盒等待」 |
| 进度透明 | 展示 Agent 当前工作阶段（收集 / 规划 / 确认 / 执行），让用户知道系统正在做什么 |
| 确认显性化 | 接入方案确认以**显眼的确认/取消按钮**呈现，不依赖用户手工输入「确认」二字 |
| 后端就绪优先 | 仅设计后端 API 已支撑的功能，不设计需要补后端的「空中楼阁」 |
| 以代码为准 | 文档契约以 `src/server/` 实际实现为准，对后端「声明但未实现」的能力如实标注、不依赖 |

### 1.3 后端能力盘点

Web 界面依赖的 HTTP API（当前 `src/server/app.ts` 已挂载）：

| 路由 | 方法 | 作用 | 就绪状态 |
|------|------|------|:--:|
| `/api/chat` | POST | 自然语言对话，SSE 流式 | ✅ |
| `/api/upload` | POST | 文件上传（multer），SSE 流式返回解析结果 | ✅（实际可解析 xlsx/csv/txt，见 §3.2） |
| `/api/services` | GET | 返回已接入 MCP 服务目录（L1 摘要，`icon` 为解析后的图标对外 URL）；`GET <servicesPath>/icons/<file>` 只读静态托管图标文件 | ✅（§3.3） |
| `/api/state` | GET | 返回 Agent 状态 `{ phase, hasAccessPlan, lastError, siteName }` | ✅（§3.4） |
| `/api/site` | GET / POST | 场站信息读取 / 绑定（首次启动引导与顶栏编辑共用；写入落盘 `agent.json` 并推送状态） | ✅（§3.6） |
| `/api/points` | GET | 点位发现：已接入点列表，`?filter=` 按设备/关键词筛选 | ❌（C4_FUN_00085，§3.5） |
| `/api/display` | GET | 活跃显示会话状态（前端按 `intervalMs` 轮询，见 §3.5） | ❌（C4_FUN_00082/00083/00084，§3.5） |
| `/api/display` | POST | 创建显示会话 `{ pointKeys[], mode?, intervalMs?, durationMinutes?, refreshCount? }`——重新订阅按钮使用（§3.5.3） | ❌（C4_FUN_00084，§3.5） |
| `/api/display/stop` | POST | 停止显示（整会话或指定点位，body `{ pointKeys?: string[] }`） | ❌（C4_FUN_00084，§3.5） |

> **确认机制说明**：后端 `AgentStreamEvent` 类型**声明**了 `interrupt` 事件，但当前编排器
> 实现**从不产出该事件**（无任何生产者，`interruptId` 也从未生成）。接入方案确认只能通过
> **确认按钮的结构化消息**触发（见 §3.1.3），前端设计以按钮驱动为准，**不依赖 interrupt 事件**。

> **未就绪、不在本设计范围**（后端缺失，对应 `c4_function.md` 的相关条目）：
> C4_FUN_00070 身份认证/角色授权、C4_FUN_00074 结构化审核 UI、C4_FUN_00075 实时运行指标、
> C4_FUN_00077 告警通知、C4_FUN_00078 审计日志、C4_FUN_00079 的「注册新服务」、C4_FUN_00080 配置向导。

---

## 2. 整体架构

### 2.1 前后端交互架构

```
┌────────────────────────────────────────────────────────────┐
│          React SPA（浏览器）                                │
│                                                            │
│  ChatView   FileUpload   ServiceDashboard   SiteSetupGate  │
│      │          │              │                │          │
│      │ SSE      │ HTTP+SSE     │ HTTP           │ HTTP     │
└──────┼──────────┼──────────────┼────────────────┼──────────┘
       │          │              │                │
       ▼          ▼              ▼                ▼
┌────────────────────────────────────────────────────────────┐
│                 Express Server（已实现）                     │
│  POST /api/chat      POST /api/upload                      │
│  GET  /api/services  GET  /api/state    GET/POST /api/site │
└────────────────────────┬───────────────────────────────────┘
                         │
                         ▼
                Workflow 编排器（orchestrator.ts，实现 C4Agent）
```

### 2.2 页面结构

采用**单页应用（SPA）+ 顶部状态栏**布局，双形态外壳——未交互时呈现落地页（居中大输入框），
首条消息/上传后切换为对话页（侧边导航 + 消息列），「开启新对话」回到落地页；对话接入与
服务目录两个主视图通过侧边导航切换：

```
┌──────────────────────────────────────────────────────────────┐
│ 顶栏三栏：[C4 品牌]    场站名（点击可改，§3.6）   [错误][徽标] │
├───────────────┬──────────────────────────────────────────────┤
│               │                                              │
│  导航          │   对话接入（主视图，默认）                     │
│   · 对话接入    │   ┌──────────────────────────────────────┐  │
│   · 服务目录    │   │  消息流（用户 / Agent 气泡）           │  │
│               │   │  · 流式 token 渲染                     │  │
│               │   │  · 工具调用进度卡片（折叠）              │  │
│               │   │  · 方案确认按钮（结构化消息）            │  │
│               │   ├──────────────────────────────────────┤  │
│               │   │  [📎 上传] 输入框              [发送]   │  │
│               │   └──────────────────────────────────────┘  │
│               │                                              │
│               │   服务目录（次视图）                          │
│               │   ┌──────────────────────────────────────┐  │
│               │   │  MCP 服务卡片列表（GET /api/services） │  │
│               │   └──────────────────────────────────────┘  │
└───────────────┴──────────────────────────────────────────────┘
```

**工作阶段徽标**（来自 `GET /api/state` 的 `phase`，见 §3.4）始终位于顶栏右侧，对话过程中随
Agent 阶段变化刷新。顶栏中央展示当前绑定场站名（纯文字，落地页与对话页均显示），点击弹出
编辑对话框（§3.6.3）；`siteName` 为空（首次部署未绑定场站）时，全屏引导层强制初始化
（§3.6.2），不可跳过。

---

## 3. 页面设计

### 3.1 对话接入页（ChatView，核心）

对话接入是 Web 界面的**主功能**，承载 C4_FUN_00071（提交接入需求）与 C4_FUN_00005（非技术语言交互）。

#### 3.1.1 接口契约

`POST /api/chat`，请求体：

```typescript
interface ChatRequest {
    message: string;                          // 用户消息文本（必填）
    conversationId?: string;                  // 会话 ID（后端按此持久化跨轮历史，见 §3.1.2）
    history?: Array<{ role: string; content: string }>;  // 历史消息（前端维护并回传）
    // 以下字段后端代码保留，但当前无实际作用（确认不依赖中断恢复，见 §3.1.3）
    resume?: boolean;                         // 无实际作用
    interruptId?: string;                     // 后端从不产生，无实际作用
}
```

SSE 响应事件（`Content-Type: text/event-stream`）：

| 事件类型 | SSE 形态 | 数据 | 前端处理 |
|---------|---------|------|---------|
| `text` | 默认 `data:` | `{ type:"text", content, conversationId }` | 追加到当前 Agent 气泡尾部（用于缓冲匹配，§3.1.3） |
| `tool_call` | 默认 `data:` | `{ type:"tool_call", name, args: {}, conversationId }` | 显示「执行中」工具卡片（`args` 恒为空对象，见下注） |
| `tool_result` | 默认 `data:` | `{ type:"tool_result", name, result, conversationId }` | 更新工具卡片为「完成」 |
| `done` | `event: done` | `{ conversationId }` | 结束本次流（可能缺失，见 §4.2） |
| `error` | `event: error` | `{ message, conversationId }` | 显示错误气泡，终止流 |
| `button_arm` | 默认 `data:` | `{ type:"button_arm", conversationId }` | 渲染「确认 / 取消」按钮（§3.1.3） |
| `button_disarm` | 默认 `data:` | `{ type:"button_disarm", reason, conversationId }` | 解除按钮武装并标注原因（§3.1.3） |

> **注意**：
> - `text`/`tool_call`/`tool_result` 是**默认消息**（无 `event:` 行，仅 `data:`），`done`/`error`
>   带 `event:` 行。前端 SSE 解析需同时处理两种形态。
> - 连接建立后后端先发送一行 `:ok` keepalive 注释，需忽略注释行；但**不要假设所有流必以
>   `:ok` 开头**（upload 的 multer 出错分支不发 keepalive）。
> - `tool_call` 的 `args` 当前恒为 `{}`，工具卡片**只展示 `name` 与进度，不展示 args**。
> - `interrupt` 事件虽在类型中声明，但当前后端不产生，前端**不监听、不依赖**它（见 §1.3）。

#### 3.1.2 消息流与流式渲染

- 用户发送消息后，前端将 `message` + 当前 `history` + 本次 `conversationId` 一并 POST。
- 后端 `text` 事件按 token 逐段返回，前端**追加渲染**到当前 Agent 气泡（非整体替换）。
- `tool_call` 触发时在消息流插入一张折叠卡片，展示工具名与进度；`tool_result` 到达后标记完成。
  工具内部细节默认折叠，避免协议级术语惊吓非技术用户。
- `done` 结束本次流（或流关闭兜底，见 §4.2）。

**conversationId 语义**：`conversationId` 是**会话主键**——后端按它持久化完整消息历史（含工具
调用/结果证据，见下「多轮上下文」），并把它写进 `X-Conversation-Id` 响应头与每个事件的
`conversationId` 字段。前端**必须跨轮复用同一 `conversationId`**（`useChatStream` 持久化该值并
自动附带），否则服务端历史无法恢复、跨轮工具证据丢失。

**多轮上下文**：跨轮状态由两部分互补：
- **后端按 `conversationId` 持久化完整历史**：编排器每轮结束后把 `run.output.messages`
  （含工具调用/结果）按 conversationId 存入内存 Map，后续轮优先恢复服务端历史（前端 history
  仅作服务端无记录时的兜底）；nudge 消息剔除，限长 100 条。修复跨轮工具证据丢失导致的
  推理死循环（原 func_test_case 用例 12，2026-10-04 随 ReAct 时代用例退役删除）。
- **后端内存闭包**：`C4Agent` 在内存中维护跨轮的设备信息与接入方案（agent.md §3.2.1.3a），
  且不持久化（Agent 重启即丢失——重启后服务端历史为空，退化为前端 history 兜底）。

> **history 回传需限长**：`express.json` 的 body 上限为 1 MB，长会话下 history 逐轮增长会
> 撞上限并推高 LLM 上下文成本。前端应**仅回传最近 N 轮**（建议 N=10），而非全量历史。

#### 3.1.3 方案确认（按钮驱动，结构化消息）

Agent 生成接入方案后需要用户确认。**确认的唯一有效通道是前端确认按钮**：按钮发送结构化
消息，后端据此置位确认状态（agent.md「执行闸门」）；自由文本一律不构成确认——参数回答与
确认词在文本上重叠（如「从一万**开始**」）时不会误触执行。

**前端确认按钮**（「确认显性化」落点）：
1. 按钮呈现由**后端显式事件驱动**（v0.2.0 修订，对应 agent.md §2.8）：流中出现 `button_arm`
   事件才渲染「确认 / 取消」按钮；出现 `button_disarm{reason}` 事件即解除武装并在按钮区标注
   原因。arm/disarm 判定全部在后端完成——arm 条件 = 本回合方案层装配成功产出 AccessPlan
   **且** 回合终结时 `gaps` 为空（**回合终结时判定一次**；单缺口提问停等即不判 arm，agent.md
   §2.8）；disarm 条件 = 方案被消耗（**执行完成**；回滚不销毁方案，回到方案展示态并重发
   `button_arm`）/ 被新方案覆盖（方案过期）/ 缺口置位（单缺口提问停等）/ 出口判据拒绝（存在
   未消耗有效方案时后端重发 `button_arm`）；**整体取消与下游目标撤回（agent.md §2.4.2
   两级：目标级撤回清该目标在途态，整链撤回清全部目标并回到必问下游缺口）均触发
   `button_disarm`**。信息收集阶段的普通询问（如「请确认转发地址映射」）因缺口停等而不会
   arm——**信息不齐或冲突未决时按钮不再弹出**。
2. 「确认」→ 发起普通 POST `{ message:"[C4_BUTTON_CONFIRM] 确认", history }`；
   「取消」→ POST `{ message:"[C4_BUTTON_CANCEL] 取消，不执行", history }`。
   两者都是**新的一轮对话**，不依赖任何 interrupt/resume 机制。

**后端识别**（编排器确认分支）：
- 确认：用户消息以 `[C4_BUTTON_CONFIRM]` 开头 → 置位「已确认」→ 注入上下文并执行方案。
- 拒绝正则：`/取消|拒绝|放弃|停止|算了|不执行|不要执行|不确认/` → 用于反向防误判。
- 消息前缀常量在前端 `useConfirmDetect.ts`（`CONFIRM_KEYWORD` / `CANCEL_KEYWORD`）与
  后端各自定义，**修改时必须两侧同步**。

> **匹配健壮性**（LLM token 非确定性）：
> - arm/disarm 判定由**后端**基于**会话状态**执行（方案层产物 AccessPlan + gaps，agent.md §2.8），
>   与 LLM 输出文本的拆分/措辞无关；前端只消费事件结果，不做本地缓冲匹配（原前端双条件推断废除）。
> - `button_arm` **不由句式直接触发**——arm 判定 = 回合终结时的方案层产物（AccessPlan + gaps
>   为空，agent.md §2.8）；句式仅用于 §2.6 提问即终局的确认句式排除（是否确认执行 / 确认执行）。
>   「执行」「好的」「开始」等词在普通语句中极易误触发，不参与任何判定。
> - 执行安全不依赖句式匹配：即使用户未点按钮，闸门也会拒绝执行并引导点击按钮。

---

### 3.2 文件上传（FileUpload）

承载 C4_FUN_00072（上传配置文档），作为对话输入区的附属能力（📎 按钮 + 拖拽区域）。

#### 3.2.1 接口契约

`POST /api/upload`，`multipart/form-data`，文件字段名 `file`，可选文本字段 `message`。

| 项 | 值 |
|----|----|
| 允许扩展名 | `.xlsx .csv .xls .pdf .docx .doc .png .jpg .jpeg .gif .bmp .txt` |
| 大小上限 | 50 MB |
| 响应 | SSE 流（`text`/`done`/`error` 事件；解析为确定性步骤，**不产出 `tool_call`/`tool_result`**——2026-09-23 起随阶段流水线生效；`done` 事件带 `conversationId`，响应头 `X-Conversation-Id` 回传会话 ID） |

#### 3.2.2 前端处理

- 选择文件后立即上传，后端将文件落盘到 `/tmp` 并把路径传给 Agent，由 Agent **确定性解析**文件内容
  （解析文本经 `<file_data>` 注入阶段提取流水线，不再产出解析工具的 `tool_call`/`tool_result`
  卡片事件），随后流式返回解析结果。
- **实际可解析格式提示**：仅 `.xlsx`/`.csv`/`.txt` 有对应解析工具；`.pdf`/`.docx`/图片会被
  后端接受（multer 放行）但**无解析器**，Agent 无法提取内容。前端在文件选择器中对此类格式
  标注「暂不支持解析」，避免用户误传后得到空结果。
- **会话关联**：upload 接口**接收可选 `conversationId` 表单字段并回传**（`X-Conversation-Id` 头 +
  `done` 事件）——上传解析轮与后续对话轮同属一个会话，服务端按 conversationId 持久化解析
  产生的工具证据，供后续轮恢复。前端 `streamUpload` 返回该 ID，`ChatView` 回写到 `useChatStream`
  的会话状态，后续 `POST /api/chat` 自动复用。上传是「一次性解析、结果纯文本回显」——回显气泡
  按 **agent 样式**渲染（解析结果是 Agent 的输出，非用户输入）。
- **文件生命周期**：上传文件落盘 `/tmp/c4_upload_*` 后，当前后端**不清理**。前端无需处理，
  但部署文档需注明「运维定期清理 `/tmp/c4_upload_*`」或后续由后端补充清理逻辑。

### 3.3 服务目录页（ServiceDashboard）

承载 C4_FUN_00079 的「展示已接入 MCP 服务」（注册新 MCP 服务为部署期操作（root 账户安装单元，注册后重启 Agent 识别），Web 界面提供注册发起入口与已接入服务展示）。

#### 3.3.1 接口契约

`GET /api/services`：

```typescript
// 200
interface ServicesResponse {
    success: true;
    services: ServiceCatalogEntry[];   // L1 摘要
    count: number;
}
// 503（registry 尚未加载）
interface ServicesError { success: false; error: string; }

interface ServiceCatalogEntry {
    service_type: string;              // 如 "c4_modbus_client"
    display_name: string;              // 如 "Modbus 数据采集"
    role: string;                      // 后端为 string；语义上仅 "writer"(采集) / "reader"(转发)
    icon?: string;                     // 图标对外 URL（注册 JSON 的相对路径已由后端解析；缺省=未提供图标）
    protocols: Array<{
        protocol: string;
        description: string;
        selection_rules: Array<{ condition: string; description: string }>;
    }>;
    point_fields: Array<{ name: string; type: string; description: string }>;
    plan_fields: Array<{ name: string; type: string; required: boolean; default: unknown; description: string }>;
}
```

> **role 取值**：Registry 加载期 Zod 已约束为 `"writer"`/`"reader"`（agent.md §3.3，非法值
> 加载即报错）；L1 摘要的 TS 类型保留 `string`，前端对未知值做兜底展示（不崩溃）。

> **图标（协议无关架构，2026-10-05）**：注册 JSON 的 `icon` 字段为相对注册目录 `icons/`
> 子目录的文件路径（agent.md §3.3），后端解析为对外 URL——`GET /api/services/icons/<basename>`
> 只读静态托管，`Cache-Control: immutable` 长缓存（**换图标须换文件名**，文件名即缓存键）。
> 前端只引用 URL、不解释内容；字段缺省或文件加载失败（404/网络错误）时由前端动态生成
> 默认徽标（§3.3.2），目录永不出现空白图标。

#### 3.3.2 前端处理

- 按 `role` 分组的列表（ZCode 子智能体设置页样式）：`writer` → 「采集」组、`reader` → 「转发」组、
  其他 role 值 → 以原值为标题的独立分组（纯前端防御：role 在 Registry 加载期已约束为
  writer/reader，合法数据不会出现其他值）；组标题带「N 项」计数。每组内每个 MCP 一行——
  **行图标 + `display_name` + `service_type` 副标题 + 一句话描述**（取各协议
  `protocol — description` 拼接；无协议时按 role 给通用说明）。
- 点击任意行弹出**详情弹窗**：通信协议（协议名 + 描述 + 选择规则）、点表字段表（名称/类型/
  说明）、接入配置表（名称/类型/必填/默认值/说明）；× / 遮罩 / Esc 关闭，打开期间锁定页面滚动。
- **行图标**：优先以 `<img>` 引用 `icon` URL（§3.3.1）；缺省或加载失败时动态生成默认徽标——
  深色圆角矩形 + 浅色英文缩写（`service_type` 去 `c4_` 前缀取前两个字母，如 asfp2→AS）。
  底色按 `service_type` 的 FNV-1a 哈希从深色调色板取色——同一服务稳定不变、不同服务呈现
  差异；前端不维护任何协议专属图标知识。
- 加载中显示骨架屏；`503` 时提示「Agent 启动中，请稍候」并支持手动重试。

### 3.4 工作状态展示（顶栏徽标）

承载 Agent 工作阶段的实时可视化。

#### 3.4.1 接口契约

`GET /api/state`：

```typescript
// 200
interface StateResponse {
    success: true;
    state: {
        phase: "idle" | "collecting" | "planning" | "confirmed" | "executing";
        hasAccessPlan: boolean;        // 是否存在待执行的 AccessPlan（等价 accessPlan !== null；回滚后为 true，执行成功后为 false）
        lastError: string | null;      // 最近一次错误（非技术语言，已翻译）
        siteName: string | null;       // 当前绑定场站名（agent.json 权威配置；未绑定为 null，§3.6）
    };
}
```

#### 3.4.2 前端处理

| phase | 徽标文案 | 视觉 |
|-------|---------|------|
| `idle` | 空闲 | 灰 |
| `collecting` | 收集信息中 | 蓝 |
| `planning` | 生成方案中 | 蓝 |
| `confirmed` | 已确认 | 绿 |
| `executing` | 执行中 | 橙 |

- 顶栏徽标通过**短间隔轮询**（如 1s）+ 对话流开始/结束时强制刷新来更新。
- `lastError` 非空时，在顶栏显示可关闭的错误条（文案已由后端错误翻译层转为非技术语言）。
- `siteName` 随同一轮询更新：顶栏中央纯文字展示当前场站名（落地页与对话页均显示）。
  从 null 变为有值（首次启动引导完成，或对话内场站绑定固化）即顶栏即时出现场站名；
  点击可进入编辑对话框（§3.6.3）。`siteName` 为 null 时触发全屏引导层（§3.6.2）。

> **轮询滞后与 phase 残留（如实告知，避免实现误判）**：
> - `phase` 在**流进行中**被后端写入，1s 轮询必然滞后；`confirmed → executing → idle` 可能在
>   一轮对话内快速跳变，前端**可能跳过中间态**，徽标只反映「最近一次读到的 phase」。
> - v0.2.0 起（对应 agent.md §2.4.1/§2.4.5）后端补齐 `executing` 置位，且回合终结必回
>   `idle`；1s 轮询下徽标的短暂「滞留」属轮询滞后，前端不应据此误报「卡死」。

---

### 3.5 点位显示（PointDisplayPanel，C4_FUN_00082 ~ 00085）

对点核验的展示载体：用户在对话中订阅点位（"显示 1#风机的风速点，刷新 20 次"）后，
LLM 调用 `display_points` 建立显示会话，ChatView 消息流**顶部**插入一张粘性显示卡片
（PointDisplayPanel），随会话存在、随会话结束移除。设计规格见 agent.md §3.6（会话模型、
数据读取通道、终止语义），本节只约定前端行为。

#### 3.5.1 卡片结构

```
┌─ PointDisplayPanel ────────────────────────────────┐
│ ● 显示中 · 实时值模式 · 剩余 3分12秒 / 已刷 47/∞   ✕ │
│ ┌────────────────────────────────────────────────┐ │
│ │ 风速  channel1.wt1_windspeed      [正常]          │ │
│ │   7.256        数据时间 14:23:05（1 秒前）      │ │
│ │   近 60s 刷新 58 次 · 平均 1.03s               │ │
│ ├────────────────────────────────────────────────┤ │
│ │ 功率  channel1.wt1_power          [已停止刷新 12 分钟] │ │
│ │   231.7        数据时间 14:11:03（12 分钟前）   │ │
│ │   近 60s 无刷新                                │ │
│ └────────────────────────────────────────────────┘ │
└────────────────────────────────────────────────────┘
```

- 每个点位一个区块：**点名行**（用户输入的中文点名为主、key 灰色小字为辅——中文名由
  LLM 经 `display_points.displayNames` 传入，未提供时回退仅显 key；key 含实例句柄
  `channel{N}`，接入对话与方案文本不展示该句柄，监控面板展示属豁免场景——agent.md
  §3.2.1.3，2026-10-01 裁定）、**状态徽标**
  （`正常` 绿 / `暂无数据` 灰 / `已停止刷新 x 分钟` 黄）、当前值（原始值，无单位、不修饰）、
  **数据时间行**（采集时间戳绝对时刻 + 相对"x 秒前"）、频率行
  （近 60s 刷新次数 · 平均间隔——来自会话 tick 统计）；
- 会话头：模式、终止进度（剩余时长或已刷次数/预算）、✕ 按钮终止整个会话；
  多点位时每个区块右上角提供单点 ✕（仅移除该点）。

#### 3.5.2 两种展示模式

| 模式 | 渲染 | 说明 |
|------|------|------|
| **实时值模式**（缺省） | 每点固定一行，值与时间戳**原地更新** | 盯住实时读数与厂家系统并排比对 |
| **累积模式** | 每点一张短期序列表（时间戳 + 值，新记录在顶部追加） | "刷新 10 次"即展示 10 条，用于回看刷新过程、核对频率均匀性；仅保留会话内记录 |

模式由订阅时的 LLM 工具参数决定（用户口头指定或缺省），卡片头部展示当前模式；
模式不支持会话中途切换——切换模式即重新订阅（旧会话隐式取消，与"切换即取消"一致）。

#### 3.5.3 轮询与生命周期

- 订阅成立后前端以 1s 周期轮询 `GET /api/display` 探测，取到活跃会话后改按其
  `intervalMs` 轮询（会话建立无需对话消息携带参数——轮询自然发现）；
- 累积模式增量拉取 `GET /api/display?since=<tick>`：游标为**会话内单调 tick 序号**
  （每条累积记录自带 tick），返回该 tick 之后追加的记录；响应携带 `sessionId`——
  **id 变化（被切换/新会话）即弃游标全量拉取**；断线重连同理全量补齐；
- 服务端 timer 独立 ticking：**页面关闭/断线期间会话照常运行与到期**，重新打开页面即恢复
  展示（累积模式凭游标补齐缺口）；
- 会话自然结束/被替换后，`lastSession` 摘要（`endedReason` / `finalTick`）保留至下一
  会话建立：自然结束（`completed_count` / `completed_duration`）展示"已完成（共刷新
  N 次）"；无法区分原因时统一显示"显示已中止，数据管道未受影响"，并提供**重新订阅**
  按钮（`POST /api/display` 按本卡片持有的上次参数重建会话，与 LLM 订阅同一服务入口）。

#### 3.5.4 呈现边界（与后端约定一致）

- 只显示数值，**不含单位**；原始值不做精度修饰或舍入；
- 状态徽标为强制元素：`暂无数据` / `已停止刷新` 必须醒目——对点场景下把过时值当
  当前值比对待会导致错误的"不一致"结论。

---

### 3.6 场站信息（SiteSetupGate / SiteEditDialog，2026-10-05 新增）

场站（site）是归属判定的根基——一个 C4 实例属于一个场站，接入资料按场站归属校验
（agent.md §3.2.1.3a）。2026-10-05 起：**首次部署（`agent.json` 无 `site`）必须先由用户
提供场站信息方可使用**（Web 引导层，不可跳过）；绑定后允许经顶栏修改场站名称。
对话内的场站语义不变：不询问、归属校验照旧（§3.6.4）。

#### 3.6.1 接口契约

`GET /api/site` → 当前绑定；`POST /api/site` → 校验 + 落盘 `agent.json` + 状态推送。
读/写/校验收敛在后端共享模块 `site_config.ts`，与对话内绑定 `persist_site` 共用同一实现：

```typescript
// GET 200
interface SiteGetResponse {
    success: true;
    site: { name: string; abbr: string } | null;   // 未绑定为 null
}
// POST 请求体
interface SiteBindRequest { name: string; abbr?: string }   // abbr 可选（缺省自动生成）
// POST 200
interface SiteBindResponse { success: true; site: { name: string; abbr: string } }
// 400：名称/缩写校验失败；422：缩写自动生成失败；500：agent.json 不可写
interface SiteError { success: false; error: string }       // error 为用户可读中文
```

**校验口径**（与对话内绑定一致）：名称 2~20 个非空白/非分隔字符（不含空格、逗号、句号）；
缩写 2~12 位字母或数字（缩写用作点 key / 写入标识，如 InfluxDB measurement）。

**缩写缺省时的生成链**（初始化不被 LLM 可用性卡死）：
1. LLM 生成（拼音首字母组合或名称中已有英文词，单次调用 15s 超时，失败返回空）；
2. 失败/超时 → 名称 ASCII 派生（取首个 2~12 位字母数字序列并小写，如「HN-阿拉善」→ hn；
   纯中文名称无 ASCII 序列 → 派生不出）；
3. 仍为空 → **422** 要求用户手填（前端渐进露出缩写输入框）。

#### 3.6.2 SiteSetupGate（首次启动引导层，不可跳过）

- **触发**：`GET /api/state` 轮询返回 `siteName === null`（首轮 `phase === "unknown"` 时不渲染，
  防接口未就绪闪现）。渲染于所有视图之上——落地页云雾背景先渲染，引导卡片浮于其上
  （透明浮层拦截点击，背景不可交互）；
- **表单**：只收集**场站名称**——主文案「初次见面，告诉我你在哪里」，占位「例如：华能阿拉善一区」；
  **不展示缩写输入**（缩写由后端自动生成）；422 时渐进露出手填输入（占位「2~12 位字母/数字，
  留空自动生成」）；
- **无取消/跳过入口**：场站是归属判定基准，初始化只需一次——POST 成功落盘 `agent.json` 后，
  `/api/state` 轮询（1s）内 `siteName` 到位，引导层卸载并触发状态立即刷新；
- 表单 `autoComplete` 关闭（含非标准 `name` 属性，防 Chrome 字段启发式自动填充）。

#### 3.6.3 SiteEditDialog（顶栏编辑对话框）

- **触发**：点击顶栏中央场站名（§3.4.2）。与引导层同风格同尺寸，主文案「场站有变？数据不搬家」；
- **关闭**：Esc / 点击遮罩 / 取消按钮（与引导层的关键差异——编辑可放弃）；
- **不展示缩写输入**：表单内部持有原缩写，保存时**原值随请求回传**——改名不触发缩写重新生成
  （既有设备的点 key / measurement 前缀不受影响）；前端不提供缩写修改入口；
- **影响面传达**：框内仅主标题「场站有变？数据不搬家」一句传达不迁移语义（无详细说明
  文案）——改名仅影响归属判定基准与新设备缩写生成，已接入设备不迁移；
- **实现约束**（防「删字被填回」）：App 每秒状态轮询会重渲染并生成新的回调引用——对话框的
  数据拉取/键盘监听仅在挂载时执行一次（`onClose` 经 ref 转发），表单初始值仅在服务器值首次
  到达时同步一次；不得随父组件重渲染反复 fetch / 覆盖受控输入。

#### 3.6.4 与对话内绑定的关系（agent.md §3.2.1.3a 修订）

| 通道 | 时机 | 行为 |
|------|------|------|
| Web 引导层（§3.6.2） | 首次部署未绑定 | 强制补填名称，缩写自动生成；落盘 `agent.json` |
| 对话内绑定（persist_site / group_bind_site） | 首次提取到场站信息时即固化（先于方案确认，取消接入不回滚场站） | 单设备与组接入均经 `site_config.ts` 落盘 + 推送顶栏；对话内此后不询问、不可变更（agent.md §3.2.1.3a） |
| Web 顶栏编辑（§3.6.3） | 绑定后任意时刻 | 允许改名（原缩写回传，不触发重新生成）；已接入设备不迁移 |

三个写入口（对话内 `persist_site` / `group_bind_site` 与 `POST /api/site`）共用
`site_config.ts` 读改写实现（单处维护防漂移），绑定成功均经 `AgentStateWriter.setSiteName`
推送（`/api/state` 1s 轮询内生效，顶栏即时更新）。**场站修改（含首次填写）立即生效于后续
工作、无需重启**（2026-10-06 用户指令）：`POST /api/site` 成功后经 `rebindSite` 回灌运行中
编排器（绑定基准与全部活会话草稿），归属判定与新会话默认值即刻切换新场站；`agent.json`
为权威持久化，重启后同样生效。

---

## 4. 交互流程

### 4.1 端到端接入流程（多轮）

> 注意：接入是**多轮对话**，各 phase 分布在多轮中，**单次 SSE 流内不会走完整流程**。
>
> **前置（首次部署）**：`agent.json` 未绑定场站时，全屏引导层（§3.6.2）先行——完成场站
> 初始化（POST /api/site）后方可进入对话；后续部署/重启直接进入主界面（顶栏显示场站名）。

```
┌─ 轮 1：上传 + 描述 ─────────────────────────────────────────────┐
│ 前端 POST /api/upload（风机点表.xlsx）                          │
│  → 工具卡片「解析点表」→ 流式「解析完成：1#风机，Modbus TCP」      │
│ 前端 POST /api/chat「接入华能阿拉善 1#风机，转发到中心侧」         │
│  → Agent 收集信息 / 询问缺失字段（如缺 IP）                       │
│  → 提取层产出设备信息/接入点表后 phase: collecting（§2.8：状态派生自语义状态）  │
│    （可能发生在上述 upload 或 chat 任一 invoke 中）               │
└───────────────────────────────────────────────────────────────┘
┌─ 轮 2：生成方案 ────────────────────────────────────────────────┐
│ 前端 POST /api/chat「生成接入方案」                              │
│  → Agent 流式输出方案文本「方案：Modbus TCP 采集 → ASFP2 转发…」   │
│  → phase: planning                                             │
└───────────────────────────────────────────────────────────────┘
┌─ 轮 3：确认 + 执行 ─────────────────────────────────────────────┐
│ 后端回合终结判定：AccessPlan 产出且 gaps 为空 → button_arm        │
│ 用户点击「确认」→ POST /api/chat「确认」                          │
│  → phase: confirmed → executing                                 │
│  → Agent 写配置 + Stop-Start → 流式「接入完成，服务已重启」        │
│  → phase: idle                                                  │
└───────────────────────────────────────────────────────────────┘
```

### 4.2 SSE 事件处理状态机

单次流的处理状态机（确认按钮的渲染由后端 button_arm / button_disarm 事件驱动，点击发起新 POST，
不改变本状态机）：

```
                 ┌──────────────┐
   POST 发起 ──→ │  connecting  │
                 └──────┬───────┘
         ┌──────────────┼──────────────────┐
         │ text         │ tool_call        │
         ▼              ▼                  │
    ┌──────────┐  ┌────────────┐           │
    │ 追加到气泡 │  │ tool 卡片   │           │
    │ (缓冲匹配) │  │ (进行中)    │           │
    └──────────┘  └─────┬──────┘           │
                        │ tool_result      │
                        ▼                  │
                   ┌─────────┐            │
                   │ 卡片完成 │            │
                   └─────────┘            │
         ┌──────────────┴──────────────────┘
         │ done 事件 / error 事件 / 流关闭(兜底) │
         ▼
    ┌─────────┐
    │ 结束     │
    └─────────┘
```

> **流关闭兜底**：当前后端所有回合——含缺口单缺口提问（如「请提供场站名称与
> 缩写…」，2026-09-23 起取代旧的「请提供场站名称…」早退路径）——均以 `done` 事件收尾；
> 前端仍须以 **ReadableStream 关闭**（`fetch` 响应体读尽）作为流终止的兜底信号（防御旧版
> 后端或异常断流），不能只认 `done`/`error`。

### 4.3 前端技术选型

| 组件 | 选型 | 理由 |
|------|------|------|
| 框架 | React + TypeScript | 与 agent.md §5 既定选型一致，类型复用后端契约 |
| 构建 | Vite | 快速 dev server + proxy |
| SSE 客户端 | `fetch` + `ReadableStream`（或 `@microsoft/fetch-event-source`） | POST 请求不能用 `EventSource`（仅支持 GET），需手写 SSE 解析 |
| 状态 | 轻量 React hooks（`useState`/`useReducer`） | 页面简单，无需引入重型状态库 |
| 样式 | 简洁纯 CSS + 设计令牌（`styles.css` `:root`） | 工业现场界面以清晰可读为先，避免花哨；视觉基准对齐 DeepSeek 浅色主题（§4.4），全部色彩经 CSS 自定义属性收敛 |

> **静态托管**：当前 Express 未挂载静态文件服务。开发期用 Vite dev server + `proxy` 转发
> `/api/*` 到后端；生产期构建产物可交由 Express 托管（`express.static`，需后端补充）或 nginx
> 反代。二者对前端代码无影响，属部署决策。

> **CORS 注意**：后端当前默认 `cors_origin = "*"`，且同时设置 `Access-Control-Allow-Credentials: true`。
> 按规范 `*` 与 credentials 组合会被浏览器拒绝；当前 SPA 不带凭据（无 cookie）故 `fetch` 可通，
> 但属潜在隐患。部署时应**收紧 `cors_origin` 到实际域名**或**去掉 credentials 头**。

### 4.4 视觉风格（DeepSeek 浅色基准，v0.3.0）

> 令牌取值实测自 chat.deepseek.com 生产样式表（2026-09-25）。仅浅色主题；DOM 结构与
> 类名不受视觉换肤影响（单元/e2e 测试的 `data-testid` 契约不变）。

| 令牌组 | 取值 | 说明 |
|--------|------|------|
| 主色 | `--primary: #3964fe`（hover `#5686fe`） | DeepSeek brand-500/450，浅色主题 hover 变亮 |
| 背景 | 主区 `#ffffff`；侧栏 `#f9fafb`；hover `#f1f3f5`；active `#ebeef2` | neutral-bluish 浅色映射 |
| 文本 | `#0f1115` / 弱化 `#61666b` / 图标 `#81858c` | label-primary/secondary/tertiary |
| 边框 | `rgba(0,0,0,.1)`，弱分隔 `.04`，强调 `.12` | border-l2/l1/l3 |
| 通用 hover 填充 | `rgba(38,49,72,.06)` | DeepSeek 全局一致 |
| 状态色 | 蓝 `#3964fe`、成功 `#22c55e`、执行 `#f59e0b`、空闲 `#81858c` | 配 rgba 底（10%~15%）做徽标 tint |
| 错误 | `#f25a5a` 家族：底 `#fdf0f0`、正文 `#cf4242`、描边 25% 透明 | state-error 系列 |
| 用户气泡 | `#f9fafb` 底深色字，圆角 22px（右下 6px 尾角），16px/24px | DeepSeek 浅色用户气泡 |
| AI 回复 | **无气泡**纯文本，占满栏宽，16px/28px（Markdown 正文） | DeepSeek 助手消息形态 |
| 输入框 | 24px 胶囊圆角，hover/focus 双档悬浮阴影（DeepSeek 实测值） | 发送按钮为蓝色胶囊 |
| 消息列 | 居中 `max-width: 840px`（<1024px 视口 712px） | DeepSeek 阅读列宽 |
| 圆角阶梯 | 6 / 10 / 12 / 16px，气泡 22px，输入框 24px | sm/md/lg/xl/bubble/input |
| 过渡 | 交互 `0.2s ease`，全局 | DeepSeek 全局时长 |
| 焦点环 | `#4d6bfe`（`:focus-visible` 2px） | DeepSeek 焦点色 |
| 字体 | Inter + 系统栈（不加载 webfont，离线环境零依赖） | 中文回退 PingFang/雅黑 |

---

## 5. 文件结构（前端）

```
c4/agent/frontend/                      # React SPA
├── package.json                        # react, react-dom, typescript, vite
├── vite.config.ts                      # proxy: /api → http://localhost:9988
├── index.html                          # Vite 入口（favicon：C4 星芒徽章）
└── src/
    ├── main.tsx                        # 入口
    ├── App.tsx                         # 双形态外壳（落地页↔对话页）+ 顶栏三栏 + 场站引导/编辑挂载
    ├── api/
    │   ├── chat.ts                     # POST /api/chat（SSE 解析）
    │   ├── upload.ts                   # POST /api/upload
    │   ├── services.ts                 # GET /api/services（含 icon URL）
    │   ├── state.ts                    # GET /api/state（含 siteName）
    │   ├── site.ts                     # GET/POST /api/site（§3.6，2026-10-05）
    │   └── sse.ts                      # SSE 解析公共层
    ├── hooks/
    │   ├── useChatStream.ts            # SSE 流状态机（§4.2）
    │   ├── useConfirmDetect.ts         # 确认/取消消息前缀常量（CONFIRM_KEYWORD / CANCEL_KEYWORD，§3.1.3）
    │   └── useAgentState.ts            # 顶栏 phase/siteName 轮询（§3.4）
    ├── assets/
    │   └── c4-icon.svg                 # C4 星芒徽章（顶栏品牌 + favicon，Vite 指纹化）
    └── components/
        ├── ChatView.tsx                # 对话消息流 + 输入区（落地形态渲染 hero）
        ├── LandingHero.tsx             # 落地页首屏（eyebrow + 超大标题 + 免责声明）
        ├── ConfirmButtons.tsx          # 方案确认按钮（结构化消息，§3.1.3）
        ├── ToolCallCard.tsx            # 工具调用进度卡片（折叠）
        ├── ThinkingBlock.tsx           # 思考过程折叠块
        ├── Markdown.tsx                # Markdown 渲染（方案文本等）
        ├── FileUpload.tsx              # 文件上传（拖拽 + 按钮）
        ├── ServiceDashboard.tsx        # MCP 目录：分组列表 + 行图标 + 详情弹窗（§3.3）
        ├── SiteSetupGate.tsx           # 首次启动引导层 SiteSetupGate + 编辑对话框 SiteEditDialog（§3.6，2026-10-05）
        ├── PointDisplayPanel.tsx       # 点位显示卡片（§3.5）
        └── PhaseBadge.tsx              # 工作阶段徽标
```

---

## 6. 设计决策记录

| 决策 | 选项 | 结论 | 理由 |
|------|------|------|------|
| SSE 客户端 | EventSource / fetch+ReadableStream | fetch+ReadableStream | `/api/chat` 是 POST，EventSource 仅支持 GET |
| 多轮上下文 | 后端 session / 前端 history 回传 | 前端 history 回传（限长） | 后端未实现 session，跨轮态存内存闭包，前端 history 与之互补 |
| 方案确认 | interrupt 卡片 / 按钮结构化消息 | 按钮结构化消息（唯一）| 后端声明 interrupt 但从不产出，按钮结构化消息是唯一真实机制 |
| 阶段展示 | SSE 驱动 / 轮询 | 轮询 + 事件触发刷新 | `/api/state` 已有，简单可靠；接受 1s 滞后 |
| 静态托管 | Express 托管 / Vite dev | dev 用 Vite proxy，生产待定 | 后端未挂静态服务，属部署决策 |
| 状态库 | 引入 Redux 等 / 轻量 hooks | 轻量 hooks | 页面简单，重型状态库不必要 |
| 解析格式提示 | 全量展示 / 标注不支持 | 标注不支持（pdf/docx/图片） | 后端缺解析器，避免误导用户 |
| 确认判定 | ~~单词/累积句式匹配~~ → **后端状态事件**（button_arm/disarm，agent.md §2.8） | 后端状态事件 | token 可能跨事件拆分；句式匹配废除（「执行/好的」等单词易误触发） |
| 视觉基准 | 自绘工业风 / 对齐 DeepSeek 浅色主题 | DeepSeek 浅色令牌（§4.4） | 主流 AI 界面心智，清爽易读；纯 CSS 令牌替换，DOM/测试零改动（2026-09-25） |
| MCP 目录图标（2026-10-05） | 前端按协议硬编码图标 / 注册 JSON 提供文件（Agent 解析为 URL）/ base64 内嵌 | **注册提供文件路径（唯一形式）**：后端解析为对外 URL 并静态托管（immutable 长缓存，换图须换文件名）；缺省/加载失败由前端按 service_type 哈希生成稳定默认徽标 | 协议无关架构——新增服务零前端代码；Agent 与前端均不解释图标内容；注册即有图标，未注册也不空白 |
| 场站初始化（2026-10-05） | 对话内询问 / Web 引导层强制补填 | **Web 引导层（不可跳过）**，只收集名称；缩写 LLM 生成（15s）→ 名称派生 → 422 手填三级兜底 | 首次部署即建立归属判定基准；初始化不被 LLM 可用性卡死；对话内绑定语义不变（agent.md §3.2.1.3a） |
| 场站修改（2026-10-05） | 绑定后一律不可变 / Web 顶栏编辑 | **对话内不可变 + Web 顶栏编辑**（原缩写回传，不触发重新生成）；改名经 `rebindSite` 回灌运行中编排器，即时生效、无需重启（2026-10-06 用户指令） | 站点更名属正常运维；数据不搬家——已接入设备/点 key 不迁移，仅归属判定基准与新设备缩写受影响 |

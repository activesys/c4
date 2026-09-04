# C4 Web 界面设计

> **版本**：v0.1.2 | **最后更新**：2026-08-26 | **父文档**：[agent.md](agent.md)
>
> **设计范围**：C4 Web 界面的页面、组件与交互设计，**仅覆盖后端已就绪的功能**——
> 对话式数据接入、文件上传、已注册 MCP 服务目录展示、Agent 工作状态展示。
> 用户身份认证、告警通知、操作审计日志、MCP 服务运行时注册等后端未就绪的功能不在本次设计范围。
>
> **当前实现状态**：后端 HTTP API 已实现（`src/server/`，见 §1.3），前端 `frontend/` 目录为空，
> 本设计为前端实现的依据。

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
| `/api/services` | GET | 返回已注册 MCP 服务目录（L1 摘要） | ✅ |
| `/api/state` | GET | 返回 Agent 状态 `{ phase, hasAccessPlan, lastError }` | ✅ |

> **确认机制说明**：后端 `AgentStreamEvent` 类型**声明**了 `interrupt` 事件，但当前 SuperWorker
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
│                    React SPA（浏览器）                       │
│                                                            │
│   ChatView       FileUpload       ServiceDashboard         │
│      │               │                  │                  │
│      │ SSE           │ HTTP+SSE        │ HTTP              │
└──────┼───────────────┼──────────────────┼──────────────────┘
       │               │                  │
       ▼               ▼                  ▼
┌────────────────────────────────────────────────────────────┐
│                 Express Server（已实现）                     │
│  POST /api/chat      POST /api/upload                      │
│  GET  /api/services  GET  /api/state                       │
└────────────────────────┬───────────────────────────────────┘
                         │
                         ▼
                SuperWorker (C4Agent wrapper)
```

### 2.2 页面结构

采用**单页应用（SPA）+ 顶部状态栏**布局，两个主视图通过侧边导航切换：

```
┌──────────────────────────────────────────────────────────────┐
│  顶栏：C4 · 场站名（可选）         [工作阶段徽标]  [错误提示]  │
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

**工作阶段徽标**（来自 `GET /api/state` 的 `phase`，见 §3.4）始终位于顶栏，对话过程中随
Agent 阶段变化刷新。

---

## 3. 页面设计

### 3.1 对话接入页（ChatView，核心）

对话接入是 Web 界面的**主功能**，承载 C4_FUN_00071（提交接入需求）与 C4_FUN_00005（非技术语言交互）。

#### 3.1.1 接口契约

`POST /api/chat`，请求体：

```typescript
interface ChatRequest {
    message: string;                          // 用户消息文本（必填）
    conversationId?: string;                  // 客户端关联标识（后端仅回显，见 §3.1.2）
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

**conversationId 语义**：`conversationId` 是**纯客户端关联/回显标识**——后端把它写进
`X-Conversation-Id` 响应头与每个事件的 `conversationId` 字段，但**不参与任何服务端状态管理**。
前端可将它作为日志/追踪标识，但**不得承诺「续传」**。

**多轮上下文**：后端未实现会话持久化，跨轮状态由两部分互补：
- **前端维护 `history`**：每轮把历史消息随请求回传，后端据此重建对话上下文。
- **后端内存闭包**：`C4Agent` 在内存中维护跨轮的设备信息与接入方案（agent.md §3.2.1.3a），
  **按 agent 实例、而非按 conversationId 隔离**，且不持久化（Agent 重启即丢失）。

> **history 回传需限长**：`express.json` 的 body 上限为 1 MB，长会话下 history 逐轮增长会
> 撞上限并推高 LLM 上下文成本。前端应**仅回传最近 N 轮**（建议 N=10），而非全量历史。

#### 3.1.3 方案确认（按钮驱动，结构化消息）

Agent 生成接入方案后需要用户确认。**确认的唯一有效通道是前端确认按钮**：按钮发送结构化
消息，后端据此置位确认状态（agent.md「执行闸门」）；自由文本一律不构成确认——参数回答与
确认词在文本上重叠（如「从一万**开始**」）时不会误触执行。

**前端确认按钮**（「确认显性化」落点）：
1. 按钮呈现为**双条件**：① 流中出现 `output_access_plan` 成功的 `tool_result`（结构化方案已
   产出，武装按钮）；② 累积缓冲命中方案展示句式（如「是否确认」「确认执行」）。两者同时
   满足才渲染「确认 / 取消」按钮——信息收集阶段的普通询问（如「请确认转发地址映射」）
   即使含确认句式也不得弹出按钮（此时还没有完整方案）；`output_device_info` 成功（信息
   更新、方案过期）即解除武装，须重新生成方案后重新武装。
2. 「确认」→ 发起普通 POST `{ message:"[C4_BUTTON_CONFIRM] 确认", history }`；
   「取消」→ POST `{ message:"[C4_BUTTON_CANCEL] 取消，不执行", history }`。
   两者都是**新的一轮对话**，不依赖任何 interrupt/resume 机制。

**后端识别**（`super_worker.ts` 确认分支）：
- 确认：用户消息以 `[C4_BUTTON_CONFIRM]` 开头 → 置位「已确认」→ 注入上下文并执行方案。
- 拒绝正则：`/取消|拒绝|放弃|停止|算了|不执行|不要执行|不确认/` → 用于反向防误判。
- 消息前缀常量在前端 `useConfirmDetect.ts`（`CONFIRM_KEYWORD` / `CANCEL_KEYWORD`）与
  后端各自定义，**修改时必须两侧同步**。

> **匹配健壮性**（LLM token 非确定性）：
> - 按钮可见性匹配对象是**完整累积文本**而非单个 token 事件——「是否」+「确认」可能被
>   拆成两个 text 事件。
> - 按钮可见性匹配**完整句式**而非单词——「执行」「好的」「开始」等词在普通语句中极易
>   误触发，仅在检测到「是否确认」「确认执行」等明确句式时才渲染按钮。
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
| 响应 | SSE 流（`text`/`tool_call`/`tool_result`/`done`/`error` 事件，**均不带 `conversationId`**） |

#### 3.2.2 前端处理

- 选择文件后立即上传，后端将文件落盘到 `/tmp` 并把路径传给 Agent，Agent 调用解析工具
  （`xlsx_parser` / `csv_parser` / `txt_parser`）提取内容，随后流式返回解析结果。
- **实际可解析格式提示**：仅 `.xlsx`/`.csv`/`.txt` 有对应解析工具；`.pdf`/`.docx`/图片会被
  后端接受（multer 放行）但**无解析器**，Agent 无法提取内容。前端在文件选择器中对此类格式
  标注「暂不支持解析」，避免用户误传后得到空结果。
- **会话关联**：upload 接口**不接收也不返回 `conversationId`**，其 SSE 事件亦不带该字段，
  **无法与进行中的对话做服务端关联**。上传是「一次性解析、结果纯文本回显」——回显气泡按
  **agent 样式**渲染（解析结果是 Agent 的输出，非用户输入）；如需让解析结果参与后续对话，
  由前端把回显文本自行存入本地 `history`（以 assistant 角色回传），在**下一轮** `POST /api/chat`
  时随 `history` 回传即可。
- **文件生命周期**：上传文件落盘 `/tmp/c4_upload_*` 后，当前后端**不清理**。前端无需处理，
  但部署文档需注明「运维定期清理 `/tmp/c4_upload_*`」或后续由后端补充清理逻辑。

### 3.3 服务目录页（ServiceDashboard）

承载 C4_FUN_00079 的「展示已注册 MCP 服务」（注册新服务不在范围）。

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
    protocols: Array<{
        protocol: string;
        description: string;
        selection_rules: Array<{ condition: string; description: string }>;
    }>;
    point_fields: Array<{ name: string; type: string; description: string }>;
    plan_fields: Array<{ name: string; type: string; required: boolean; default: unknown; description: string }>;
}
```

> 后端 `role` 字段类型为 `string`（未约束为联合类型），前端按 `"writer"`/`"reader"` 取值渲染，
> 但对未知值需做兜底展示（不崩溃）。

#### 3.3.2 前端处理

- 以卡片列表展示每个服务：`display_name` 为主标题，`service_type` 为副标题，
  `role` 以「采集 / 转发」徽标区分（未知值显示原值）。
- 每张卡片展示该服务支持的 `protocols`（协议名 + 描述）与 `point_fields`（点表字段）。
  `plan_fields` 以「必填 / 可选」标注，供用户了解接入前需准备的信息。
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
        hasAccessPlan: boolean;        // 是否已生成接入方案
        lastError: string | null;      // 最近一次错误（非技术语言，已翻译）
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

> **轮询滞后与 phase 残留（如实告知，避免实现误判）**：
> - `phase` 在**流进行中**被后端写入，1s 轮询必然滞后；`confirmed → executing → idle` 可能在
>   一轮对话内快速跳变，前端**可能跳过中间态**，徽标只反映「最近一次读到的 phase」。
> - `phase` 仅由后端在特定节点写入：merge 失败时 `setError` 但 phase 停在 `executing`；
>   用户闲聊不确认时 phase 停在 `collecting`/`planning`；确认后若 LLM 未产出执行步骤
>   （`planSteps` 为空），phase 停在 `confirmed`。后端均无「重置回 idle」逻辑，
>   徽标出现这类「滞留」属后端行为，前端不应据此误报「卡死」。

---

## 4. 交互流程

### 4.1 端到端接入流程（多轮）

> 注意：接入是**多轮对话**，各 phase 分布在多轮中，**单次 SSE 流内不会走完整流程**。

```
┌─ 轮 1：上传 + 描述 ─────────────────────────────────────────────┐
│ 前端 POST /api/upload（风机点表.xlsx）                          │
│  → 工具卡片「解析点表」→ 流式「解析完成：1#风机，Modbus TCP」      │
│ 前端 POST /api/chat「接入华能阿拉善 1#风机，转发到中心侧」         │
│  → Agent 收集信息 / 询问缺失字段（如缺 IP）                       │
│  → 当 output_device_info 工具产出设备信息时 phase: collecting    │
│    （可能发生在上述 upload 或 chat 任一 invoke 中）               │
└───────────────────────────────────────────────────────────────┘
┌─ 轮 2：生成方案 ────────────────────────────────────────────────┐
│ 前端 POST /api/chat「生成接入方案」                              │
│  → Agent 流式输出方案文本「方案：Modbus TCP 采集 → ASFP2 转发…」   │
│  → phase: planning                                             │
└───────────────────────────────────────────────────────────────┘
┌─ 轮 3：确认 + 执行 ─────────────────────────────────────────────┐
│ 前端在累积文本中匹配到「是否确认」→ 渲染「确认 / 取消」按钮        │
│ 用户点击「确认」→ POST /api/chat「确认」                          │
│  → phase: confirmed → executing                                 │
│  → Agent 写配置 + Stop-Start → 流式「接入完成，服务已重启」        │
│  → phase: idle                                                  │
└───────────────────────────────────────────────────────────────┘
```

### 4.2 SSE 事件处理状态机

单次流的处理状态机（确认按钮的渲染在「追加到气泡」过程中根据累积文本触发，点击发起新 POST，
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

> **流关闭兜底**：后端某些分支（如「已记录场站…」「请提供场站名称…」的早退路径）在返回文本后
> **不发送 `done` 事件**直接结束流。前端不能只认 `done`/`error`——必须以
> **ReadableStream 关闭**（`fetch` 响应体读尽）作为流终止的兜底信号。

### 4.3 前端技术选型

| 组件 | 选型 | 理由 |
|------|------|------|
| 框架 | React + TypeScript | 与 agent.md §5 既定选型一致，类型复用后端契约 |
| 构建 | Vite | 快速 dev server + proxy |
| SSE 客户端 | `fetch` + `ReadableStream`（或 `@microsoft/fetch-event-source`） | POST 请求不能用 `EventSource`（仅支持 GET），需手写 SSE 解析 |
| 状态 | 轻量 React hooks（`useState`/`useReducer`） | 页面简单，无需引入重型状态库 |
| 样式 | 优先简洁 CSS / 现有设计体系 | 工业现场界面以清晰可读为先，避免花哨 |

> **静态托管**：当前 Express 未挂载静态文件服务。开发期用 Vite dev server + `proxy` 转发
> `/api/*` 到后端；生产期构建产物可交由 Express 托管（`express.static`，需后端补充）或 nginx
> 反代。二者对前端代码无影响，属部署决策。

> **CORS 注意**：后端当前默认 `cors_origin = "*"`，且同时设置 `Access-Control-Allow-Credentials: true`。
> 按规范 `*` 与 credentials 组合会被浏览器拒绝；当前 SPA 不带凭据（无 cookie）故 `fetch` 可通，
> 但属潜在隐患。部署时应**收紧 `cors_origin` 到实际域名**或**去掉 credentials 头**。

---

## 5. 文件结构（前端）

```
c4/agent/frontend/                      # React SPA（待实现）
├── package.json                        # react, react-dom, typescript, vite
├── vite.config.ts                      # proxy: /api → http://localhost:9988
└── src/
    ├── main.tsx                        # 入口
    ├── App.tsx                         # SPA 布局 + 侧边导航 + 顶栏
    ├── api/
    │   ├── chat.ts                     # POST /api/chat（SSE 解析）
    │   ├── upload.ts                   # POST /api/upload
    │   ├── services.ts                 # GET /api/services
    │   └── state.ts                    # GET /api/state
    ├── hooks/
    │   ├── useChatStream.ts            # SSE 流状态机（§4.2）
    │   ├── useConfirmDetect.ts         # 累积文本匹配确认句式（§3.1.3）
    │   └── useAgentState.ts            # 顶栏 phase 轮询
    └── components/
        ├── ChatView.tsx                # 对话消息流 + 输入区
        ├── ConfirmButtons.tsx          # 方案确认按钮（结构化消息，§3.1.3）
        ├── ToolCallCard.tsx            # 工具调用进度卡片（折叠，仅展示 name）
        ├── FileUpload.tsx              # 文件上传（拖拽 + 按钮）
        ├── ServiceDashboard.tsx        # 服务目录卡片列表
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
| 确认句式匹配 | 单词匹配 / 累积句式匹配 | 累积句式匹配 | token 可能跨事件拆分，「执行/好的」等单词易误触发 |

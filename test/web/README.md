# C4 Web 界面测试方案

> **版本**：v0.1.2 | **最后更新**：2026-08-26
>
> **设计依据**：[web.md](../../docs/design/web.md) — C4 Web 界面设计（v0.1.2）
>
> **测试范围**：web.md 覆盖的 Web 前端（React SPA）与「前端 ↔ 后端 HTTP API」契约。
> 后端 API 自身的功能正确性由 `c4/test/agent/` 测试方案覆盖，本方案聚焦**前端行为**
> 与 **web.md 声明的后端契约**。

---

## 1. 总则

### 1.1 测试目标

验证 Web 前端实现与 web.md 设计一致，覆盖：

1. **前端纯逻辑**：SSE 解析、确认句式匹配、phase 映射、对话流渲染、history 限长等不依赖后端即可验证的行为。
2. **前端 ↔ 后端契约**：web.md §3 声明的 HTTP/SSE 契约与真实后端一致（「以代码为准」原则的落点）。
3. **端到端交互**：对话接入、文件上传、服务目录、状态展示的完整流程。

### 1.2 测试原则

| 原则 | 说明 |
|------|------|
| 以 web.md 为唯一规格 | 测试断言来自 web.md 的接口契约与交互设计，不参考前端实现细节 |
| 契约即断言面 | 只断言 HTTP 响应结构、SSE 事件流、DOM 渲染结果、请求负载——不侵入前端内部状态 |
| 分层隔离 | L1 纯逻辑用 mock，L2 集成用真实后端，E2E 用真实浏览器；互不依赖 |
| 如实标注「未实现」 | web.md 明确标注的「后端未就绪」能力（interrupt 事件、身份认证等）**不做正测**，仅做「前端不依赖」的负向防护 |

### 1.3 被测对象

| 对象 | 来源 | 说明 |
|------|------|------|
| SSE 解析器 | web.md §3.1.1, §4.2 | 默认 `data:` 消息 + `event:` 行、`:ok` 注释、流关闭兜底 |
| 对话流渲染 | web.md §3.1.2 | 追加渲染、工具卡片、done/error 处理 |
| 确认按钮 | web.md §3.1.3 | 关键词驱动、累积缓冲匹配、不依赖 interrupt |
| 文件上传组件 | web.md §3.2 | 格式提示、一次性解析、会话关联处理 |
| 服务目录页 | web.md §3.3 | 卡片渲染、role 兜底、503 处理 |
| 工作状态徽标 | web.md §3.4 | phase → 文案/颜色映射、轮询、lastError |
| 后端 HTTP API | web.md §1.3 | `/api/chat` `/api/upload` `/api/services` `/api/state` 契约 |

### 1.4 测试层次

```
┌──────────────────────────────────────────────────────────────┐
│ L1: 前端单元测试（Vitest + React Testing Library）              │
│ 不依赖真实后端/LLM — mock 流与 HTTP 响应，精确断言 DOM/逻辑        │
│ · SSE 解析器 · 确认句式匹配 · phase 映射 · 组件渲染 · 对话流      │
└──────────────────────────────────────────────────────────────┘
                               │
                               ▼
┌──────────────────────────────────────────────────────────────┐
│ L2: 前端集成测试（真实 c4_agent 后端）                          │
│ 真实 HTTP/SSE — 验证前端与真实后端契约一致                       │
│ · 对话流  · 文件上传  · 服务目录  · 状态轮询                     │
└──────────────────────────────────────────────────────────────┘
                               │
                               ▼
┌──────────────────────────────────────────────────────────────┐
│ L3: 端到端测试（Playwright，真实浏览器 + 真实后端）              │
│ 完整接入流程 + 多轮交互                                          │
└──────────────────────────────────────────────────────────────┘
```

> **L1/L2/L3 与 agent 测试方案的 L1/L2 含义不同**：agent 方案按「是否依赖 LLM 推理」分层；
> 本方案按「测试隔离级别」分层——L1=mock 隔离、L2=真实后端集成、L3=真实浏览器 E2E。
> LLM 依赖只影响 L2/L3 中需要真实对话的用例，用 `describe.skipIf(!process.env.DEEPSEEK_API_KEY)`
> 或自定义测试标签 + `--grep` 过滤（TS 侧无 pytest.mark），无 API key 时 skip。

---

## 2. 测试环境

### 2.1 依赖

| 依赖 | 说明 |
|------|------|
| Node.js | ≥ 20（前端构建） |
| Vitest | 单元/集成测试运行器 |
| @testing-library/react | 组件渲染断言 |
| msw | §3.5 服务目录 HTTP 拦截（§3.1/§3.2 纯函数、§3.3/§3.4 纯组件无需 mock） |
| Playwright | L3 端到端浏览器测试 |
| c4_agent | 真实后端（L2/L3/§6），路径通过 `C4_AGENT_PATH` 或自动查找 |
| c4_shm_manager | L2/L3/§6 共享内存依赖 |
| LLM API Key | `DEEPSEEK_API_KEY`（L2/L3 中 LLM 驱动用例必需） |

### 2.2 测试目录结构

```
c4/test/web/
├── README.md              # 本文件
├── unit/                  # L1 前端单元测试（Vitest）
│   ├── sse_parser.test.ts        # SSE 解析器（§3.1）
│   ├── confirm_detect.test.ts    # 确认句式匹配（§3.2）
│   ├── phase_badge.test.tsx      # phase 映射（§3.3）
│   ├── tool_card.test.tsx        # 工具卡片（§3.4）
│   ├── service_dashboard.test.tsx # 服务目录（§3.5）
│   └── chat_stream.test.tsx      # 对话流渲染 + history 限长（§3.6）
├── integration/           # L2 前端集成测试（Vitest，真实后端）
│   ├── chat_flow.test.ts         # 对话流（§4.1）
│   ├── upload_flow.test.ts       # 文件上传（§4.2）
│   ├── services_flow.test.ts     # 服务目录（§4.3）
│   └── state_poll.test.ts        # 状态轮询（§4.4）
├── python/                # §6 契约一致性测试（Python/pytest，真实 c4_agent）
│   ├── conftest.py               # 移植 agent 测试的启动 fixture（见 §2.4）
│   └── test_contract_web.py      # 契约断言（§6）
└── e2e/                   # L3 端到端（Playwright）
    └── access_flow.spec.ts       # 端到端接入（§5）
```

### 2.3 L1 测试隔离策略

L1 不依赖真实后端，按被测对象类型选择隔离方式：

| 被测对象 | 隔离方式 |
|---------|---------|
| §3.1 SSE 解析器、§3.2 确认句式匹配 | **纯函数**：直接构造输入（原始 SSE 字符串 / 累积文本），无需任何 mock |
| §3.3 phase 映射、§3.4 工具卡片 | **纯组件**：直接传 props，无需 mock |
| §3.5 服务目录 | **msw** 拦截 `GET /api/services`（模拟 200 / 503 / 加载中） |
| §3.6 对话流渲染、§3.1.7 流关闭兜底 | **可控 mock ReadableStream**：模拟多个 text token 逐个到达、以及「文本后直接关闭、无 done 事件」的早退流 |

> 注：SSE 逐字节分帧（§3.1.1–3.1.6、3.1.8）是对**解析函数**的直接字符串输入测试，**不需要** mock server；
> 只有「流消费」类行为（§3.1.7 流关闭兜底、§3.6 追加渲染）才需要一个可控的 ReadableStream 数据源。
>
> **前端源码引用**：测试经 monorepo/alias 引用 `c4/agent/frontend/src`（如 Vitest `resolve.alias`）；
> 且 SSE 解析逻辑需从 `api/chat.ts` 抽取为**可单测的独立纯函数模块**（如 `sseParser`），§3.1 直接对该函数做字符串输入测试。

### 2.4 python/ 契约测试的 fixture（§6）

§6 契约测试是黑盒后端测试（Python/pytest，真实 `c4_agent`）。pytest 的 conftest **按 test 文件
所在目录向上发现、不会跨测试根复用**，因此 `c4/test/web/python/` 自带 `conftest.py`，其启动 fixture
**移植**（而非直接 import）`c4/test/agent/python/conftest.py` 的等价逻辑：写 agent.json → 启动
shm_manager → 启动 agent → 轮询就绪 → teardown。

> §6 契约测试**不使用** agent 测试方案中的 `ChatHelper.confirm(interrupt_id)`——该方法按 interrupt
> 模型设计，与 web.md v0.1.2「后端从不产出 interrupt」结论冲突。契约测试直接经 `POST /api/chat`
> 发送「确认」「取消，不执行」关键词，与前端实际行为对齐。

---

## 3. L1 前端单元测试

### 3.1 SSE 解析器（web.md §3.1.1, §4.2）

**被测对象**：SSE 事件流解析逻辑。

| # | 用例 | 输入 | 断言 |
|---|------|------|------|
| 3.1.1 | 解析默认 `data:` 消息 | `data: {"type":"text","content":"你好","conversationId":"c1"}\n\n` | 解析出 `text` 事件，字段 `content`/`conversationId` 正确 |
| 3.1.2 | 解析 `event:` 行 | `event: done\ndata: {"conversationId":"c1"}\n\n` | 解析出 `done` 事件，`conversationId` 正确 |
| 3.1.3 | 解析 `event: error` | `event: error\ndata: {"message":"...","conversationId":"c1"}\n\n` | 解析出 `error` 事件 |
| 3.1.4 | 忽略 `:ok` 注释行 | 流首行 `:ok\n\n` | 注释行被忽略，不产生事件 |
| 3.1.5 | 忽略无 `:ok` 开头的流 | 首事件直接是 `data:` | 正常解析（不依赖 `:ok`，§3.1.1 注意项） |
| 3.1.6 | tool_call args 恒空 | `data: {"type":"tool_call","name":"xlsx_parser","args":{},"conversationId":"c1"}` | 解析出 `tool_call`，`args` 为空对象 |
| 3.1.7 | 流关闭兜底（无 done） | mock ReadableStream 在 `text` 事件后直接关闭 | 消费者以「流关闭」为终止信号，不等待 done（§4.2 兜底） |
| 3.1.8 | interrupt 负向防护 | 解析 `event: interrupt` | 解析器能安全解析/忽略该事件、不崩溃（确认流程仅关键词驱动，不依赖 interrupt，§1.3 确认机制说明） |

> **注意**：3.1.8 是「不依赖 interrupt」的负向防护——web.md 明确「后端从不产出 interrupt」，
> 前端若收到该事件不应出错，但确认流程绝不建立在它之上。
> 3.1.7 属「流消费」行为，用可控 mock ReadableStream 模拟（见 §2.3），非纯字符串解析。

### 3.2 确认句式匹配（web.md §3.1.3）

**被测对象**：`useConfirmDetect` — 累积缓冲上的确认句式匹配。

| # | 用例 | 输入（累积文本） | 断言 |
|---|------|-----------------|------|
| 3.2.1 | 完整句式命中 | 「…是否确认执行？」 | 渲染「确认 / 取消」按钮 |
| 3.2.2 | 跨事件拆分命中 | token 序列「是否」+「确认」（拆成两个 text 事件） | 累积后仍命中（§3.1.3 匹配健壮性） |
| 3.2.3 | 单词不误触发 | 「好的，我明白了」/「请执行下一步」 | **不**渲染按钮（完整句式匹配，非单词） |
| 3.2.4 | 确认按钮负载 | 用户点击「确认」 | 发起 POST `{ message:"确认", history }`（非 interrupt/resume） |
| 3.2.5 | 取消按钮负载 | 用户点击「取消」 | 发起 POST `{ message:"取消，不执行", history }` |
| 3.2.6 | 非方案文本不触发 | 「请问今天天气如何」 | 不渲染按钮 |

> **确认/取消负载是普通 POST**：验证请求体**不含** `resume:true`、`interruptId`（web.md §3.1.3「不依赖任何 interrupt/resume 机制」）。

### 3.3 phase 映射（web.md §3.4.2）

**被测对象**：`PhaseBadge` — phase → 文案/颜色映射。

| # | 用例 | phase 输入 | 断言 |
|---|------|-----------|------|
| 3.3.1 | idle | `idle` | 文案「空闲」，灰色 |
| 3.3.2 | collecting | `collecting` | 「收集信息中」，蓝色 |
| 3.3.3 | planning | `planning` | 「生成方案中」，蓝色 |
| 3.3.4 | confirmed | `confirmed` | 「已确认」，绿色 |
| 3.3.5 | executing | `executing` | 「执行中」，橙色 |
| 3.3.6 | 未知 phase 兜底 | `"unknown"` | 不崩溃，显示原值或「未知」（§3.4.2 表外兜底） |
| 3.3.7 | lastError 展示与关闭 | `lastError:"权限不足，请联系管理员"` | 顶栏显示可关闭错误条；**点击关闭后错误条消失** |

### 3.4 工具卡片（web.md §3.1.1, §3.1.2）

**被测对象**：`ToolCallCard`。

| # | 用例 | 输入 | 断言 |
|---|------|------|------|
| 3.4.1 | tool_call 渲染 | `tool_call(name="xlsx_parser", args={})` | 显示工具名 + 「执行中」状态；**不展示 args**（§3.1.1 注意项） |
| 3.4.2 | tool_result 完成 | 随后 `tool_result(name="xlsx_parser")` | 卡片标记「完成」 |
| 3.4.3 | 默认折叠 | 工具卡片 | 工具内部细节默认折叠，需点击展开 |

### 3.5 服务目录页（web.md §3.3）

**被测对象**：`ServiceDashboard`。

| # | 用例 | 输入（mock `/api/services`） | 断言 |
|---|------|------------------------------|------|
| 3.5.1 | 卡片渲染 | 5 个服务的 L1 摘要 | 渲染 5 张卡片，`display_name` 主标题、`service_type` 副标题 |
| 3.5.2 | role 徽标 | `role:"writer"` / `role:"reader"` | 分别显示「采集」/「转发」徽标 |
| 3.5.3 | role 未知值兜底 | `role:"unknown_value"` | 不崩溃，显示原值（§3.3.1 注） |
| 3.5.4 | plan_fields 标注 | `plan_fields` 含 `required:true/false` | 「必填」/「可选」正确标注 |
| 3.5.5 | 503 处理 | 响应 503 `{success:false,error:...}` | 显示「Agent 启动中，请稍候」+ 手动重试按钮 |
| 3.5.6 | 加载中骨架屏 | 响应延迟（pending） | 加载期间显示骨架屏，完成后替换为卡片列表（§3.3.2） |

### 3.6 对话流渲染与 history 管理（web.md §3.1.2）

**被测对象**：`ChatView` 的流式追加渲染 + history 截断逻辑。

| # | 用例 | 输入 | 断言 |
|---|------|------|------|
| 3.6.1 | 追加渲染（非整体替换） | mock ReadableStream 依次发射 3 个 `text` token（「方案」、「如下」、「…」） | 三个 token **追加到同一个** Agent 气泡（非整体替换），气泡文本为拼接结果 |
| 3.6.2 | history 限长 | 构造超过 N 轮（N=10）的 history | 发送时**仅回传最近 10 轮**，超出的历史被截断（§3.1.2 限长） |
| 3.6.3 | interrupt 事件不触发确认卡片 | mock ReadableStream 发射 `event: interrupt` 后继续流 | 收到 interrupt **不渲染**「确认/取消」卡片；确认按钮仅由累积文本句式匹配触发（§3.1.3） |

---

## 4. L2 前端集成测试（真实后端）

> L2 需真实 `c4_agent` + `c4_shm_manager` 启动。fixture 在 Node/TS 侧**重新实现**等价逻辑（写
> agent.json → 启动 shm_manager → 启动 agent → 轮询就绪 → teardown，对齐
> `c4/test/agent/python/conftest.py`），**不能直接复用 Python fixture**。LLM 驱动用例用
> `describe.skipIf(!process.env.DEEPSEEK_API_KEY)` 标记，无 `DEEPSEEK_API_KEY` 时 skip。

### 4.1 对话流（web.md §3.1.2）

| # | 用例 | 操作 | 断言 |
|---|------|------|------|
| 4.1.1 | 流式渲染真实对话 | `POST /api/chat`「你好」 | SSE 流被正确渲染为气泡；无 error 事件；流正常关闭（done 或流关闭兜底） |
| 4.1.2 | conversationId 回显 | 请求带 `conversationId:"c123"` | 响应头 `X-Conversation-Id` 与事件 `conversationId` 均回显 `c123`；前端不据此做服务端续传（§3.1.2） |
| 4.1.3 | history 回传 | 请求带 `history`（前一轮对话） | 后端正常响应（history 截断逻辑见 §3.6.2，属 L1 单测） |

### 4.2 文件上传（web.md §3.2.2）

| # | 用例 | 操作 | 断言 |
|---|------|------|------|
| 4.2.1 | 上传可解析文件 | `POST /api/upload` 上传 .xlsx | SSE 流含解析结果文本，流正常关闭 |
| 4.2.2 | 无 conversationId 关联 | 上传响应 | 事件**不含** `conversationId`；前端把回显文本自行存入本地 history（§3.2.2） |
| 4.2.3 | 格式提示（前端） | 文件选择器 | pdf/docx/图片标注「暂不支持解析」，xlsx/csv/txt 正常可选（§3.2.2） |

### 4.3 服务目录（web.md §3.3）

| # | 用例 | 操作 | 断言 |
|---|------|------|------|
| 4.3.1 | 真实目录加载 | `GET /api/services` | 返回 `{success:true, services:[...], count}`，前端渲染卡片列表 |

### 4.4 状态轮询（web.md §3.4.2）

| # | 用例 | 操作 | 断言 |
|---|------|------|------|
| 4.4.1 | phase 徽标刷新 | 轮询 `GET /api/state` | 徽标随 `phase` 变化更新（含 idle/collecting/... 等值） |
| 4.4.2 | 轮询滞后容忍 | phase 快速跳变（confirmed→executing→idle） | 前端不误报「卡死」，徽标只反映最近读到的 phase（§3.4.2 注） |

---

## 5. L3 端到端测试（Playwright）

### 5.1 端到端接入流程（web.md §4.1）

| # | 场景 | 流程 | 关键断言 |
|---|------|------|---------|
| 5.1.1 | 上传 + 对话接入 | 打开页面 → 上传点表 xlsx → 对话「接入 1#风机」→ 匹配到「是否确认」渲染确认按钮 → 点击「确认」→ 流式「接入完成」 | 按钮在累积文本命中后出现；确认后无 interrupt/resume 请求；最终展示「接入完成」 |
| 5.1.2 | 服务目录浏览 | 导航到服务目录 | 卡片列表展示，无 503 时正常渲染 |
| 5.1.3 | 状态徽标联动 | 对话过程中观察顶栏徽标 | 徽标随 phase 变化（允许 1s 滞后） |

> **E2E 依赖**：需前端已完成构建并可访问（Vite dev server 或已托管的构建产物），
> 后端 c4_agent 就绪。若前端尚未实现，本节标记为「待实现后启用」。

---

## 6. 契约一致性测试（web.md 契约 vs 真实后端）

> 本节锁定 web.md 声明的**后端契约**，防止后端变更破坏前端依赖。属黑盒契约断言，测试文件位于
> `c4/test/web/python/test_contract_web.py`（自带 `conftest.py`，见 §2.4），不复用 agent 测试方案的
> interrupt 模型 `ChatHelper.confirm()`。

| # | web.md 声明 | 契约断言 |
|---|------------|---------|
| 6.1 | `tool_call` 的 `args` 恒为 `{}`（§3.1.1） | 任意真实对话中，`tool_call` 事件的 `args` 为 `{}` |
| 6.2 | `upload` 事件不带 `conversationId`（§3.2.1） | 上传响应的事件对象无 `conversationId` 字段 |
| 6.3 | 后端不产出 `interrupt` 事件（§1.3） | 完整接入对话流中，SSE 事件**不**出现 `interrupt` |
| 6.4 | 流可能无 `done` 事件（§4.2） | 两处早退分支——「已记录场站…」与「请提供场站名称…」——返回文本后流直接关闭、无 `done` |
| 6.5 | `conversationId` 仅回显（§3.1.2） | 不同 `conversationId` 的请求不产生服务端会话隔离副作用（纯回显） |
| 6.6 | 确认/拒绝关键词正则（§3.1.3） | 确认正则 `/确认|好的|执行|按方案|开始/`、拒绝正则 `/取消|拒绝|放弃|停止|算了|不执行|不要执行|不确认/` 与实际行为一致 |

> **6.5 说明**：该断言是「否定式」（证明无隔离副作用），本身较弱，需与 §4.1.2 的正向「原样回显」互补。
> **6.1 触发条件**：需先通过上传文件触发一次工具调用（如 `xlsx_parser`）才能观察到 `tool_call` 事件。

---

## 7. 运行方式

```bash
# L1 单元测试（无需后端）
cd c4/test/web && npm test -- unit/

# L2 集成测试（需真实后端，LLM 用例自动 skip 若无 key）
C4_AGENT_PATH=/path/to/c4_agent DEEPSEEK_API_KEY=sk-xxx npm test -- integration/

# L3 端到端（Playwright，需前端构建 + 后端）
npm run test:e2e

# §6 契约一致性测试（Python/pytest，真实 c4_agent，自带 conftest）
cd c4/test/web && C4_AGENT_PATH=/path/to/c4_agent pytest python/test_contract_web.py -v
```

---

## 8. 与 c4/test/agent/ 的关系

| 测试目录 | 范围 | 被测对象 |
|---------|------|---------|
| `c4/test/agent/` | Agent 后端整体功能 | `c4_agent` 的启动、HTTP API、LLM 驱动端到端行为 |
| `c4/test/web/` | Web 前端 + 前端↔后端契约 | React SPA 的 SSE 解析、确认交互、渲染；web.md 声明的后端契约 |

互补关系：

- `c4/test/agent/` 验证后端 API **本身正确**（业务语义）。
- `c4/test/web/` 验证前端 **正确消费** 这些 API（契约 + 交互）。
- 第 6 节契约测试是两者的**桥梁**：锁定 web.md 声明的后端契约，任一侧变更时报警。

后端 API 变更 → 同时跑 `c4/test/agent/`（业务）+ `c4/test/web/` §6（契约）。
前端变更（web.md 修改、前端代码变更）→ 跑 `c4/test/web/` L1/L2/L3。

> **语言分工说明**：`c4/test/` 全局约定为 Python 3，但 Web 前端是 TypeScript/React——L1/L2 单元与
> 集成测试用 Vitest（TS）直接测试前端源码，L3 用 Playwright（TS），仅 §6 后端契约测试用 Python
> （复用黑盒思路，需在 `c4/test/web/python/` 自建 conftest，见 §2.4）。

---

## 9. 参考

| 文档 | 路径 | 相关内容 |
|------|------|---------|
| Web 界面设计 | `c4/docs/design/web.md` | 被测前端的设计规格（唯一权威） |
| Agent 架构设计 | `c4/docs/design/agent.md` | §3.5 Web 层、§3.2.1.3a 确认机制 |
| Agent 测试方案 | `c4/test/agent/README.md` | 后端测试基建（L2/L3 与 §6 契约测试的对齐参考） |
| 测试行为规则 | `c4/AGENTS.md` §行为规则 | 规则3（按 README 规格不参考源码）、规则4（验证流程） |

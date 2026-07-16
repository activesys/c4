# AGENTS.md — C4 项目规范

C4 是一个部署在工业数据服务器上的 AI 智能体。架构：Agent（TypeScript）+ MCP 服务集群（Go），
通过 POSIX 共享内存交换数据。

## 目录结构

```
c4/
├── mcp/            # MCP Server 源代码（Go），目录名加 c4_ 前缀
│   ├── c4_shm_manager/
│   ├── c4_modbus_client/
│   ├── c4_iec104_client/
│   ├── c4_asfp2_client/
│   ├── c4_asfp2_server/
│   └── c4_influxdb_client/
├── agent/          # Agent 源代码（Node.js / TypeScript）
├── test/           # 功能测试（Python 3）
│   ├── c4_fun_00053/
│   └── ...
├── docs/           # 项目文档
│   ├── design/         # 架构设计
│   └── specification/  # 需求/功能规格
├── AGENTS.md       # 本文件
├── README.md
└── LICENSE
```

## 语言约定

| 目录 | 语言 | 说明 |
|------|------|------|
| `c4/mcp/` | **Go** | MCP Server，目录名加 `c4_` 前缀，编译为静态二进制，部署于工业 Linux |
| `c4/agent/` | **Node.js / TypeScript** | AI 推理层，MCP SDK 原生支持、Web 界面同语言 |
| `c4/test/` | **Python 3** | 功能测试，测试目录命名对应功能编号（如 `c4_fun_00053`） |

## 行为规则（硬约束 —— 绝对不可违反）

1. **评审之后不能直接修改**：经 Oracle / Momus / 代码评审后，必须先向用户汇报评审发现的问题，由用户确认后再修改。不得在评审后静默修改代码或文档。
2. **修改代码之后不能直接提交**：任何代码修改完成后，必须经用户确认后方可 `git commit`。不得在用户未明确要求的情况下自动提交。
3. **实现功能测试代码时必须严格按照 README 规格，不参考源代码**：功能测试用例的 README.md 是测试的唯一权威来源。实现测试代码时不得阅读 Go 源码、不得根据当前二进制行为调整断言——即使测试全部失败，也说明源码尚未实现对应功能，测试代码本身是正确的。
4. **实现功能后必须验证本功能测试全部通过，再验证全部功能测试通过**：每次实现功能之后，首先确保本功能的测试用例全部通过，然后运行全部功能测试用例（`c4/test/` 下所有测试套件）确保没有引入回归。如果本功能导致了其他功能的测试用例失败，应立即停止、报告错误和原因，并报告可能的功能冲突或设计冲突。本功能未全部通过前不得进入下一步，全部测试未通过前不得提交。当所有的功能测试用例全部通过后，将 `c4_function.md` 对应的功能条目设置成已完成（标题后加 ✅）。

## 核心设计原则

- **Agent 不在实时数据路径中**：Agent 负责理解意图、规划方案、配置和监控 MCP 服务；
  MCP 服务负责确定性数据搬运。Agent 故障不影响已运行的 MCP 数据管道。
- **c4_shm_manager 是每个 C4 实例的首个 MCP 服务**：负责 POSIX 共享内存的创建、扩容、
  块分配回收和销毁。
- **MCP 服务之间通过 POSIX 共享内存交换数据**：零拷贝、纳秒级延迟。

## 关键文档

| 文档 | 路径 | 说明 |
|------|------|------|
| 架构设计 | `docs/design/c4_architecture.md` | 整体架构、共享内存布局、并发协议、配置格式 |
| 共享内存管理 | `docs/design/c4_shm_manager.md` | 创建/扩容/分配算法、MCP 工具定义、交互时序、错误码 |
| 功能规格 | `docs/specification/c4_function.md` | 功能点及其验收指导 |
| 需求规格 | `docs/specification/c4_requirement.md` | 83 条形式化需求 |
| 项目概述 | `docs/specification/c4_description.md` | 架构、应用场景 |
| ASFP2 协议 | `docs/specification/asfp2_specification.md` | 数据包格式、类型枚举 |

## 共享内存关键常量

| 常量 | 值 | 来源 |
|------|-----|------|
| `MAGIC` | `0xC4DA7A00` | §2.2.1 |
| `BLOCK_SIZE` | `32` 字节 | §2.2 |
| `HEADER_SHM_ID` | `0` | §2.2 |
| `DATA_START_SHM_ID` | `1` | §2.2 |
| `VERSION` | `1` | §2.2.1 |
| 默认点数（无配置） | `100000` | §3.3 |

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

## 核心设计原则

- **Agent 不在实时数据路径中**：Agent 负责理解意图、规划方案、配置和监控 MCP 服务；
  MCP 服务负责确定性数据搬运。Agent 故障不影响已运行的 MCP 数据管道。
- **c4_shm_manager 是每个 C4 实例的首个 MCP 服务**：负责 POSIX 共享内存的创建、扩容、
  块分配回收和销毁。
- **MCP 服务之间通过 POSIX 共享内存交换数据**：零拷贝、纳秒级延迟。

## 关键文档

| 文档 | 路径 | 说明 |
|------|------|------|
| 架构设计 | `docs/design/c4_architecture.md` | 共享内存布局、MCP 工具定义、配置格式 |
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

# C4 — 数据接入 AI 智能体

[![License](https://img.shields.io/badge/license-AGPL%20v3-blue.svg)](LICENSE)
[![Spec](https://img.shields.io/badge/spec-v0.3.0-green.svg)](docs/specification/c4_description.md)

让数据接入智能化、自动化。

C4 是一个部署在工业数据服务器上的 AI 智能体，将传统需要实施工程师手动完成的数据接入配置工作——解读点表、配置协议参数、设置转发规则、排查连接故障——转变为自然语言驱动的自动化流程。场站工作人员无需专业知识，通过 Web 界面提交需求和任意格式的文档，即可完成数据接入。

## 架构

```
用户（场站工作人员）
       │ 自然语言 + 文档（Excel/PDF/Word/图片）
       ▼
┌──────────────────────────────┐
│          C4 智能体            │
│  ┌──────────┐  MCP协议  ┌──────────────┐
│  │  Agent   │◄────────►│ MCP 服务集群   │
│  │ (LLM推理) │           │ Modbus·IEC104 │
│  │          │           │ IEC101·ASFP2  │
│  │ 理解意图  │           │ 数据库·转发    │
│  │ 规划任务  │           │ ...           │
│  │ 调度MCP  │           └──────┬───────┘
│  │ 监控诊断  │                  │
│  └──────────┘            设备/第三方系统
└──────────────────────────────┘
```

- **Agent**（AI 推理层）：理解意图、规划方案、调度 MCP、监控诊断、自主修复——AI 不进入实时数据路径
- **MCP 服务**（确定性执行层）：封装协议级数据采集/转换/转发，支持按需启动，Agent 故障时继续自主运行
- **统一网络**：跨 I/II/III 安全分区及中心侧的对等通信，端到端数据可追溯

## 设计原则

| 原则 | 说明 |
|------|------|
| AI 在管道外 | Agent 不在实时数据路径中运行，确定性数据搬运由 MCP 服务负责 |
| 按需启动 | MCP 服务按需启停，节约资源 |
| 渐进自主 | 常规操作自主执行，关键变更人工审核 |
| 故障隔离 | Agent 故障不影响已运行的 MCP 数据管道 |

## 文档

| 文档 | 说明 |
|------|------|
| [描述文档](docs/specification/c4_description.md) | 项目概述、架构、应用场景 |
| [需求文档](docs/specification/c4_requirement.md) | 82 条形式化需求 |
| [功能文档](docs/specification/c4_function.md) | 46 个功能点及其验收指导 |

## 许可

C4 采用 [GNU Affero General Public License v3](LICENSE) 开源许可。企业部署 Agent 实例需获取[商业授权](docs/specification/c4_description.md#11-商用许可)，按 Agent 实例数量计费。

© 2026 Bo Wang

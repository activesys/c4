// c4/agent/src/super_worker/subagents.ts — 子代理注册
// 注册 3 个子代理: doc-parser, plan-generator, step-decomposer
// 根据 agent.md §3.2 实现

import type { McpServiceRegistry } from "../registry/registry.js";
import { xlsxParserTool, csvParserTool } from "../subagents/tools/doc_parsers.js";
import { outputPlanStepsTool } from "../subagents/tools/output_plan_steps.js";
import { createQueryRegistryTool } from "../subagents/tools/query_registry.js";

// ── 子代理配置类型 ───────────────────────────────────────

/** deepagents createDeepAgent 期望的子代理格式 */
export interface SubagentConfig {
    name: string;
    description: string;
    systemPrompt: string;
    tools: unknown[];
}

// ── doc-parser ────────────────────────────────────────────

/**
 * doc-parser 子代理（C4_FUN_00002 / 00003）
 *
 * 解析 Excel/CSV/PDF/Word/图片文档，提取设备地址、寄存器映射、
 * 数据类型、通信参数。工具接收文件路径字符串，自行打开文件读取内容。
 *
 * 根据 agent.md §3.2 doc-parser 定义。
 */
export function createDocParserSubagent(): SubagentConfig {
    return {
        name: "doc-parser",
        description:
            "解析 Excel/CSV/PDF/Word/图片，提取设备地址、寄存器映射、数据类型、通信参数。" +
            "工具接收文件路径字符串，自行打开文件读取内容。",
        systemPrompt: [
            "从文档中提取接入所需关键信息。",
            "支持 .xlsx .csv .pdf .docx .png .jpg 格式。",
            "工具参数是文件路径，收到后调用对应解析工具。",
            "",
            "输出结构化数据，包含以下字段：",
            "- name: 设备名称",
            "- protocol: 通信协议（从设备连接参数推断）",
            "- connection: { ip, port }",
            "- points: [{ name, addr, uid?, fun?, type?, swap? }]",
            "",
            "信息不完整时列出已有信息和缺失字段。",
            "例如：\"找到了风速、温度共 2 个数据点，但缺少设备 IP 地址\"。",
        ].join("\n"),
        tools: [xlsxParserTool, csvParserTool],
    };
}

// ── plan-generator ────────────────────────────────────────

/**
 * plan-generator 子代理（C4_FUN_00004）
 *
 * 通过系统提示中的 L1 服务摘要（{{ service_catalog }}）获知可用服务
 * 及其支持协议，从设备信息生成结构化 AccessPlan。
 *
 * 根据 agent.md §3.2 plan-generator 定义。
 *
 * @param registry - McpServiceRegistry 单例（用于注入 L1 服务摘要）
 */
export function createPlanGeneratorSubagent(
    registry: McpServiceRegistry,
): SubagentConfig {
    const catalog = registry.isLoaded
        ? registry.getServiceCatalog()
        : "暂无可用服务。";

    return {
        name: "plan-generator",
        description:
            "从设备信息生成结构化接入方案 (AccessPlan)，" +
            "包含协议选择、设备清单、数据点映射、转发目标。",
        systemPrompt: [
            "根据设备连接参数推断协议，从以下可用服务中选择匹配的服务类型生成 AccessPlan。",
            "",
            "## 可用 MCP 服务",
            catalog,
            "",
            "输出 AccessPlan JSON 对象，格式如下：",
            "```",
            "{",
            '  "site": { "name": "场站名称", "abbr": "场站缩写" },',
            '  "devices": [{',
            '    "name": "设备名称",',
            '    "seq": 1,',
            '    "protocol": "modbus_tcp",',
            '    "connection": { "ip": "...", "port": 502 },',
            '    "points": [{ "name": "...", "addr": 1000, "uid": 1, "fun": 3, "type": 10, "swap": 2 }]',
            "  }],",
            '  "forward_targets": [{',
            '    "name": "目标名称",',
            '    "protocol": "asfp2",',
            '    "connection": { "ip": "...", "port": 9999 }',
            "  }]",
            "}",
            "```",
            "",
            "规则：",
            "- 场站缩写用于生成服务实例 id（如 hnals_1_scada）",
            "- 协议选择依据 Registry 中的 protocol 定义和 selection_rules",
            "- 若设备协议信息不足，在返回中注明需要用户补充",
        ].join("\n"),
        tools: [],
    };
}

// ── step-decomposer ───────────────────────────────────────

/**
 * step-decomposer 子代理（C4_FUN_00044）
 *
 * 将本次接入的 AccessPlan 分解为增量 MCP 服务配置。
 * 输出带 action（add/modify/delete）的 AccessPlanSteps，
 * 告知执行模块如何更新 config.json。
 *
 * 根据 agent.md §3.2 step-decomposer 定义。
 *
 * @param registry - McpServiceRegistry 单例（用于创建 queryRegistryTool）
 */
export function createStepDecomposerSubagent(
    registry: McpServiceRegistry,
): SubagentConfig {
    const catalog = registry.isLoaded
        ? registry.getServiceCatalog()
        : "暂无可用服务。";

    const queryRegistryTool = createQueryRegistryTool(registry);

    return {
        name: "step-decomposer",
        description:
            "将本次接入的 AccessPlan 分解为增量 MCP 服务配置。" +
            "输出带 action（add/modify/delete）的 AccessPlanSteps，" +
            "告知执行模块如何更新 config.json。",
        systemPrompt: [
            "你负责将本次接入方案转化为增量 MCP 服务配置。",
            "只需输出本次请求涉及的服务，不需要关心 config.json 中已有的其他服务。",
            "",
            "每条配置用 action 标注操作类型：",
            "  add    — 本次新增的 MCP 服务",
            "  modify — 修改已有服务的参数（如添加新转发目标）",
            "  delete — 本次要移除的 MCP 服务",
            "",
            "流程：",
            "1) 使用 query_registry 工具获取每类服务的 config_schema",
            "   - config_schema.required 中的字段必须全部有值",
            "   - config_schema.fields 中 source=\"plan\" 的字段从 AccessPlan 提取",
            "   - config_schema.fields 中 source=\"default\" 的字段填入默认值",
            "2) 从 AccessPlan 提取设备/转发信息填入 plan 来源字段",
            "3) 按以下规则生成 instance.id：",
            "   {site_abbr}_{device_seq}_{role_abbr}",
            "   角色缩写映射：c4_modbus_client→scada, c4_iec104_client→transformer,",
            "   c4_asfp2_client→asfp2, c4_influxdb_client→influx",
            "4) Writer points 从 AccessPlan devices[].points[] 映射",
            "   (name→id, addr, uid, fun, type, swap 直传)",
            "5) Reader points 的 key 格式为 {writer_instance_id}.{point_name}",
            "   addr 由你自动分配",
            "6) c4_shm_manager 已在运行，不在输出范围",
            "",
            "所有服务在同一 Stop-Start 周期统一处理。",
            "用 output_plan_steps 工具输出 AccessPlanSteps。",
            "",
            "## 可用 MCP 服务（按需用 query_registry 查询完整信息）",
            catalog,
        ].join("\n"),
        tools: [outputPlanStepsTool, queryRegistryTool],
    };
}

// ── 批量创建 ──────────────────────────────────────────────

/**
 * 创建全部 3 个子代理配置数组。
 *
 * 用于 createDeepAgent({ subagents }) 注入。
 *
 * @param registry - McpServiceRegistry 单例
 * @returns 子代理配置数组
 */
export function createSubagents(
    registry: McpServiceRegistry,
): SubagentConfig[] {
    return [
        createDocParserSubagent(),
        createPlanGeneratorSubagent(registry),
        createStepDecomposerSubagent(registry),
    ];
}

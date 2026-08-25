// c4/agent/src/super_worker/subagents.ts — 子代理注册（目标架构预留）
// 注册 3 个子代理: info-gatherer, plan-generator, step-decomposer
// 根据 agent.md §3.2 实现。当前实现为扁平工具（见 super_worker.ts），
// 本文件为 createDeepAgent 目标架构预留，保持与设计文档一致。

import type { McpServiceRegistry } from "../registry/registry.js";
import { xlsxParserTool, csvParserTool } from "../subagents/tools/doc_parsers.js";
import { createOutputPlanStepsTool } from "../subagents/tools/output_plan_steps.js";
import { createQueryRegistryTool } from "../subagents/tools/query_registry.js";

// ── 子代理配置类型 ───────────────────────────────────────

/** deepagents createDeepAgent 期望的子代理格式 */
export interface SubagentConfig {
    name: string;
    description: string;
    systemPrompt: string;
    tools: unknown[];
}

// ── info-gatherer ─────────────────────────────────────────

/**
 * info-gatherer 子代理（C4_FUN_00002 / 00003）
 *
 * 解析文档、推断协议、收集实例参数与点表字段，缺失时逐个询问用户补齐。
 * 工具接收文件路径字符串，自行打开文件读取内容。
 *
 * 根据 agent.md §3.2 info-gatherer 定义。
 */
export function createInfoGathererSubagent(): SubagentConfig {
    return {
        name: "info-gatherer",
        description:
            "解析 Excel/CSV/PDF/Word/图片，推断协议，收集实例参数与点表字段。" +
            "工具接收文件路径字符串，自行打开文件读取内容。",
        systemPrompt: [
            "负责收集接入所需的全部必要信息——解析文档、推断协议、收集实例参数与点表字段，缺失时逐个询问用户补齐。",
            "支持 .xlsx .csv .pdf .docx .png .jpg 格式。",
            "工具参数是文件路径，收到后调用对应解析工具。",
            "",
            "协议推断分三层（先匹配服务目录中各协议的 point_fields）：",
            "1) 从点表字段唯一匹配推断（如含 uid/fun/type/swap 列 → Modbus）",
            "2) 从用户描述推断（如「采集 Modbus 设备」）",
            "3) 前两步均无法确定时，询问用户协议，得知后用该协议的 point_fields 重新理解点表列",
            "",
            "输出结构化数据，包含以下字段：",
            "- name: 设备名称",
            "- abbr: 采集目标标识（从描述提取，如 1#风机 → wt1）",
            "- protocol: 通信协议",
            "- 实例 plan 字段直接平铺（ip/port/url/token 等，由 config_schema 声明）",
            "- points: [{ name, ...点业务字段 }]（字段名由 point_fields 声明）",
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
 * 不再推断协议、不再收集信息——只做「选型 + 组装方案 + 方案确认」。
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
            "基于 info-gatherer 产出的信息齐全的 deviceInfo，从以下可用服务中选择匹配的服务类型生成 AccessPlan。",
            "不再推断协议、不再收集信息——只做「选型 + 组装方案 + 方案确认」。",
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
            '    "abbr": "采集目标标识（如 wt1）",',
            '    "protocol": "modbus_tcp",',
            '    "ip": "...",',
            '    "port": 502,',
            '    "points": [{ "name": "...", "addr": 1000, "uid": 1, "fun": 3, "type": 10, "swap": 2 }]',
            "  }],",
            '  "forward_targets": [{',
            '    "name": "目标名称",',
            '    "abbr": "转发目标标识（如 center）",',
            '    "protocol": "asfp2",',
            '    "ip": "...",',
            '    "port": 9999',
            "  }]",
            "}",
            "```",
            "",
            "规则：",
            "- 实例 id 由 {site_abbr}_{target_abbr} 生成（如 hnals_wt1），不含协议/角色信息",
            "- 协议选择依据 Registry 中的 protocol 定义和 selection_rules",
            "- 展示方案时须一并展示协议与 abbr 绑定，让用户确认协议是否正确、abbr 是否绑定到正确设备",
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
    const outputPlanStepsTool = createOutputPlanStepsTool(registry);

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
            "   - config_schema.fields 中 source=\"plan\" 且 default=null 的字段必须由用户提供值",
            "   - config_schema.fields 中 source=\"plan\" 且有 default 的字段可选（不提供则用默认值）",
            "   - config_schema.fields 中 source=\"default\" 的字段填入默认值",
            "2) 从 AccessPlan 提取设备/转发信息填入 plan 来源字段（平铺，不做语义分类）",
            "3) 按以下规则生成 instance.id：{site_abbr}_{target_abbr}",
            "   target_abbr 即采集/转发目标标识（如 1#风机 → wt1），不含协议/角色信息",
            "4) Writer points 从 AccessPlan devices[].points[] 映射（name→id，点业务字段由 point_fields 声明）",
            "5) Reader points 的 key 格式为 {writer_instance_id}.{point_name}",
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
        createInfoGathererSubagent(),
        createPlanGeneratorSubagent(registry),
        createStepDecomposerSubagent(registry),
    ];
}

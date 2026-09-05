// c4/agent/src/subagents/tools/output_access_plan.ts — plan-generator 结构化输出
// LLM 在拿到 deviceInfo 后，调用此工具输出结构化 AccessPlan

import { tool } from "langchain";
import { z } from "zod";
import type { McpServiceRegistry } from "../../registry/registry.js";
import { find_service_type, list_supported_protocols, normalize_protocol } from "./output_plan_steps.js";

// ── Schema ─────────────────────────────────────────────────

const devicePointSchema = z.object({
    name: z.string().describe("数据点名称"),
    addr: z.number().describe("协议地址"),
    uid: z.number().optional().describe("Modbus: 单元标识符"),
    fun: z.number().optional().describe("Modbus: 功能码"),
    type: z.number().optional().describe("Modbus: 数据类型"),
    swap: z.number().optional().describe("Modbus: 字节交换"),
});

const deviceSpecSchema = z.object({
    name: z.string().describe("设备名称"),
    abbr: z.string().optional().describe("采集目标标识（候选，从 deviceInfo.abbr 原样复制，如 wt1；用于生成 instance.id 与记忆库一致）"),
    seq: z.number().describe("设备编号（从1开始）"),
    protocol: z.string().describe("通信协议，如 modbus, iec104"),
    connection: z.object({
        ip: z.string().describe("设备 IP"),
        port: z.number().describe("端口"),
    }),
    points: z.array(devicePointSchema).describe("数据点列表"),
});

const forwardTargetSchema = z.object({
    name: z.string().describe("转发目标名称，如 中心侧数据库"),
    abbr: z.string().optional().describe("转发目标标识（候选，从 deviceInfo.forward_targets 的 abbr 原样复制，如 center）"),
    protocol: z.string().describe("转发协议，必须由用户明确提供，禁止沿用接收侧协议或猜测"),
    connection: z.object({
        ip: z.string().optional().describe("目标 IP（asfp2 等 TCP 协议用）"),
        port: z.number().optional().describe("目标端口（asfp2 等 TCP 协议用）"),
        url: z.string().optional().describe("InfluxDB 写入端点 URL（influxdb 用，必须由用户提供）"),
        token: z.string().optional().describe("InfluxDB 认证 token（influxdb 用）"),
        org: z.string().optional().describe("InfluxDB 组织名（influxdb 用）"),
        bucket: z.string().optional().describe("InfluxDB bucket 名（influxdb 用）"),
    }),
    measurement: z.string().optional().describe("InfluxDB measurement 名（influxdb 用，所有数据点共用；缺省时用场站缩写）"),
});

const accessPlanArgSchema = z.object({
    site: z.object({
        name: z.string().describe("场站名称，如 华能阿拉善"),
        abbr: z.string().describe("场站缩写，用于生成 instance.id，如 hnals"),
    }),
    devices: z.array(deviceSpecSchema).describe("采集设备列表"),
    forward_targets: z.array(forwardTargetSchema).optional().describe("转发目标列表"),
});

// ── 业务校验 ───────────────────────────────────────────────

function validate_access_plan(plan: Record<string, unknown>): { valid: boolean; errors: string[] } {
    const errors: string[] = [];

    if (!plan.site || typeof plan.site !== "object") {
        errors.push("site: 必须提供场站信息（name 和 abbr）");
    } else {
        const site = plan.site as Record<string, unknown>;
        if (typeof site.name !== "string" || site.name.trim().length === 0)
            errors.push("site.name: 必须是非空字符串");
        if (typeof site.abbr !== "string" || site.abbr.trim().length === 0)
            errors.push("site.abbr: 必须是非空字符串");
        else if (!/^[a-zA-Z_]+$/.test(site.abbr))
            errors.push(`site.abbr: "${site.abbr}" 仅允许 [a-zA-Z_]+`);
    }

    if (!Array.isArray(plan.devices) || plan.devices.length === 0) {
        errors.push("devices: 必须是非空数组");
    } else {
        for (let i = 0; i < (plan.devices as unknown[]).length; i++) {
            const dev = (plan.devices as unknown[])[i] as Record<string, unknown>;
            const prefix = `devices[${i}]`;
            if (typeof dev.name !== "string" || dev.name.trim().length === 0)
                errors.push(`${prefix}.name: 必须是非空字符串`);
            if (typeof dev.seq !== "number" || dev.seq < 1)
                errors.push(`${prefix}.seq: 必须是正整数`);
            if (typeof dev.protocol !== "string" || dev.protocol.trim().length === 0)
                errors.push(`${prefix}.protocol: 必须是非空字符串`);
            if (!dev.connection || typeof dev.connection !== "object")
                errors.push(`${prefix}.connection: 必须提供连接信息`);
            if (!Array.isArray(dev.points) || dev.points.length === 0)
                errors.push(`${prefix}.points: 必须是非空数组`);
        }
    }

    return { valid: errors.length === 0, errors };
}

// ── 工具 ──────────────────────────────────────────────────

// registry 必填项前置校验（agent.md「必填项用户提供原则」第一层防线）：
// 实例字段无 default 键 = 必填，缺失/为空时在「生成方案」阶段即拦截，要求向用户逐项询问
function validate_required_config(
    registry: McpServiceRegistry,
    role: "writer" | "reader",
    items: Array<Record<string, unknown>>,
    label: string,
): string[] {
    const errors: string[] = [];
    for (const item of items) {
        const proto = String(item.protocol ?? "").trim();
        if (proto === "") {
            errors.push(
                `${label} "${item.name}" 缺少通信协议，请向用户询问后再调用本工具`,
            );
            continue;
        }
        const svc = find_service_type(registry, normalize_protocol(proto), role);
        if (!svc) {
            const supported = list_supported_protocols(registry, role).join("、");
            errors.push(
                `${label} "${item.name}" 的协议 "${proto}" 未在服务目录中找到。` +
                `当前已部署的${role === "writer" ? "数据采集" : "数据转发"}协议：${supported}。` +
                `请确认协议名称是否正确，或确认该协议对应的 MCP 服务是否已部署。`,
            );
            continue;
        }
        const entry = svc ? registry.queryRegistry(svc) : null;
        const conn = (item.connection ?? {}) as Record<string, unknown>;
        for (const [name, f] of Object.entries(entry?.config_schema.fields ?? {})) {
            if (f.default !== undefined && f.default !== null) continue;
            const v = conn[name] ?? item[name];
            if (v === undefined || v === null || v === "") {
                errors.push(
                    `${label} "${item.name}" 缺少必填配置 "${name}"（${f.description ?? name}）。` +
                    `请向用户询问后再调用本工具，禁止编造或使用默认值`,
                );
            }
        }
    }
    return errors;
}

export function createOutputAccessPlanTool(registry: McpServiceRegistry) {
    return tool(
        async (input: z.infer<typeof accessPlanArgSchema>) => {
            const vr = validate_access_plan(input as unknown as Record<string, unknown>);
            const requiredErrors = validate_required_config(
                registry,
                "writer",
                input.devices as unknown as Array<Record<string, unknown>>,
                "设备",
            ).concat(
                validate_required_config(
                    registry,
                    "reader",
                    (input.forward_targets ?? []) as unknown as Array<Record<string, unknown>>,
                    "转发目标",
                ),
            );
            const errors = [...vr.errors, ...requiredErrors];

            if (errors.length > 0) {
                return JSON.stringify({
                    success: false,
                    errors,
                    hint: "请根据 errors 逐项向用户询问补齐后重试。site.abbr 仅允许 [a-zA-Z_]+。",
                });
            }

            return JSON.stringify({
                success: true,
                site: input.site,
                devices: input.devices,
                forward_targets: input.forward_targets || [],
                summary: {
                    device_count: input.devices.length,
                    total_points: input.devices.reduce((sum, d) => sum + d.points.length, 0),
                    has_forward: (input.forward_targets || []).length > 0,
                },
            });
        },
        {
            name: "output_access_plan",
            description:
                "生成结构化接入方案 (AccessPlan)。" +
                "在获得设备信息后，结合 service_catalog 选择匹配的服务类型，" +
                "推断场站名称和缩写，组织设备清单和转发目标。" +
                "site.abbr 用于生成 instance.id，如 hnals_wt1（hnals=场站缩写, wt1=采集目标标识）。" +
                "调用时机：用户要求生成方案时。",
            schema: accessPlanArgSchema,
        },
    );
}

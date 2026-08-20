// c4/agent/src/subagents/tools/output_plan_steps.ts — step-decomposer
// LLM 提供 deviceInfo，工具确定性生成 ServiceStep[]
// 协议映射、id 生成、默认字段填充全部由 Registry 驱动

import { tool } from "langchain";
import { z } from "zod";
import type { McpServiceRegistry } from "../../registry/registry.js";
import type { ServiceStep, ServicePoint, RegistryEntry } from "../../types/index.js";

// ── Schema（LLM 提供的输入）────────────────────────────────

const devicePointInputSchema = z.object({
    name: z.string().describe("数据点名称"),
    addr: z.number().describe("协议地址"),
    uid: z.number().optional(),
    fun: z.number().optional(),
    type: z.number().optional(),
    swap: z.number().optional(),
});

const deviceInputSchema = z.object({
    name: z.string().describe("设备名称"),
    seq: z.number().optional().describe("设备编号，默认 1"),
    protocol: z.string().describe("通信协议，如 modbus_tcp, modbus"),
    connection: z.object({
        ip: z.string(),
        port: z.number(),
    }),
    points: z.array(devicePointInputSchema).describe("数据点列表"),
});

const forwardTargetInputSchema = z.object({
    name: z.string().describe("转发目标名称"),
    protocol: z.string().describe("转发协议，如 asfp2, influxdb"),
    connection: z.object({
        ip: z.string().optional().describe("目标 IP（asfp2 等 TCP 协议用）"),
        port: z.number().optional().describe("目标端口（asfp2 等 TCP 协议用）"),
        url: z.string().optional().describe("InfluxDB 写入端点 URL（influxdb 用，如 http://172.16.109.12:8086）"),
        token: z.string().optional().describe("InfluxDB 认证 token（influxdb 用）"),
        org: z.string().optional().describe("InfluxDB 组织名（influxdb 用）"),
        bucket: z.string().optional().describe("InfluxDB bucket 名（influxdb 用）"),
    }),
    measurement: z.string().optional().describe("InfluxDB measurement 名（influxdb 用，缺省时用场站缩写）"),
});

// ── 修改/删除步骤（LLM 直接提供，非确定性生成）──────────

const changePointSchema = z.object({
    id: z.string().optional().describe("采集点标识，modify 时按 id 匹配已有 point"),
    addr: z.number().optional(),
    uid: z.number().optional(),
    fun: z.number().optional(),
    type: z.number().optional(),
    swap: z.number().optional(),
    key: z.string().optional(),
}).passthrough();

const changeStepSchema = z.object({
    action: z.enum(["modify", "delete"]).describe("操作类型"),
    service_type: z.string().describe("MCP 服务类型，如 c4_modbus_client"),
    instance: z.object({
        id: z.string().describe("实例唯一标识，modify/delete 按此匹配"),
    }).passthrough(),
    points: z.array(changePointSchema).optional(),
});

const planStepsInputSchema = z.object({
    site: z.object({
        name: z.string().describe("场站名称"),
        abbr: z.string().describe("场站缩写，如 hnals"),
    }).optional(),
    devices: z.array(deviceInputSchema).optional().describe("设备列表（新增接入时提供，从 doc-parser 获得）"),
    forward_targets: z.array(forwardTargetInputSchema).optional().describe("转发目标列表（新增接入时提供）"),
    changes: z.array(changeStepSchema).optional().describe("修改/删除已有实例的步骤列表"),
});

// ── 角色缩写映射 ───────────────────────────────────────────

const ROLE_ABBR: Record<string, string> = {
    "c4_modbus_client": "scada",
    "c4_iec104_client": "transformer",
    "c4_asfp2_client": "asfp2",
    "c4_asfp2_server": "asfp2",
    "c4_influxdb_client": "influx",
};

// ── 映射逻辑 ───────────────────────────────────────────────

/**
 * 从 Registry 中查找匹配 protocol 的 service_type。
 * 对 writer 查找 writer role，对 reader 查找 reader role。
 */
function find_service_type(
    registry: McpServiceRegistry,
    protocol: string,
    role: "writer" | "reader",
): string | null {
    const entries = registry.getServiceCatalogEntries();
    for (const e of entries) {
        if (e.role !== role) continue;
        for (const p of e.protocols) {
            if (p.protocol === protocol || protocol.startsWith(p.protocol)) {
                return e.service_type;
            }
        }
    }
    return null;
}

/**
 * 确定性生成 ServiceStep[]。
 *
 * 输入：deviceInfo（LLM 从 doc-parser 解析的）+ 可选的 site / forward_targets
 * 输出：可直接传入 merge_config_from_steps 的 ServiceStep[]
 */
function generate_steps(
    input: z.infer<typeof planStepsInputSchema>,
    registry: McpServiceRegistry,
): { steps: ServiceStep[]; warnings: string[] } {
    const steps: ServiceStep[] = [];
    const warnings: string[] = [];
    const site_abbr = input.site?.abbr || "";

    for (const dev of input.devices ?? []) {
        const protocol = dev.protocol.replace(/_tcp$/, "").replace(/^tcp_/, "");
        const svc_type = find_service_type(registry, protocol, "writer");

        if (!svc_type) {
            warnings.push(`未找到 ${protocol} 对应的 writer 服务，跳过设备 "${dev.name}"`);
            continue;
        }

        const role_abbr = ROLE_ABBR[svc_type] || svc_type;
        const instance_id = site_abbr
            ? `${site_abbr}_${dev.seq || 1}_${role_abbr}`
            : dev.name.replace(/[^a-zA-Z0-9_]/g, "_").toLowerCase();

        const points: ServicePoint[] = dev.points.map((p) => ({
            id: p.name,
            addr: p.addr,
            uid: p.uid,
            fun: p.fun,
            type: p.type,
            swap: p.swap,
            shm_id: 0,
        }));

        const instance: Record<string, unknown> = {
            id: instance_id,
            name: dev.name,
            ip: dev.connection.ip,
            port: dev.connection.port,
        };

        // 从 Registry 填充 source=default 的字段
        const entry = registry.queryRegistry(svc_type);
        if (entry?.config_schema) {
            for (const [field_name, field_def] of Object.entries(entry.config_schema.fields)) {
                if (field_def.source === "default" && !(field_name in instance)) {
                    instance[field_name] = field_def.default;
                }
            }
        }

        steps.push({
            action: "add",
            service_type: svc_type,
            instance,
            points,
        });
    }

    // 处理转发目标 → reader 服务
    if (input.forward_targets && input.forward_targets.length > 0) {
        for (const ft of input.forward_targets) {
            const protocol = ft.protocol.replace(/_tcp$/, "");
            const svc_type = find_service_type(registry, protocol, "reader");

            if (!svc_type) {
                warnings.push(`未找到 ${protocol} 对应的 reader 服务，跳过转发目标 "${ft.name}"`);
                continue;
            }

            const role_abbr = ROLE_ABBR[svc_type] || svc_type;
            const forward_instance_id = site_abbr
                ? `${site_abbr}_${role_abbr}_${ft.name.replace(/[^a-zA-Z0-9_]/g, "_").toLowerCase()}`
                : `forward_${ft.name.replace(/[^a-zA-Z0-9_]/g, "_").toLowerCase()}`;

            const reader_points: ServicePoint[] = [];
            const is_influxdb = svc_type === "c4_influxdb_client";
            const measurement = ft.measurement || site_abbr;
            let auto_addr = 3001;
            for (const writer_step of steps) {
                for (const pt of writer_step.points) {
                    const point_key = `${writer_step.instance.id}.${pt.id}`;
                    if (is_influxdb) {
                        reader_points.push({
                            id: point_key,
                            key: point_key,
                            measurement,
                            shm_id: 0,
                        });
                    } else {
                        reader_points.push({
                            id: point_key,
                            key: point_key,
                            addr: auto_addr++,
                            shm_id: 0,
                        });
                    }
                }
            }

            const instance: Record<string, unknown> = {
                id: forward_instance_id,
                name: ft.name,
            };
            // 透传 connection 字段：asfp2 提供 ip/port，influxdb 提供 url/token/org/bucket
            for (const [k, v] of Object.entries(ft.connection)) {
                if (v !== undefined && v !== null && v !== "") {
                    instance[k] = v;
                }
            }

            const entry = registry.queryRegistry(svc_type);
            if (entry?.config_schema) {
                for (const [field_name, field_def] of Object.entries(entry.config_schema.fields)) {
                    if (field_def.source === "default" && !(field_name in instance)) {
                        instance[field_name] = field_def.default;
                    }
                }
            }

            steps.push({
                action: "add",
                service_type: svc_type,
                instance,
                points: reader_points,
            });
        }
    }

    return { steps, warnings };
}

// ── 工厂函数 ──────────────────────────────────────────────

export function createOutputPlanStepsTool(registry: McpServiceRegistry) {
    return tool(
        async (input: z.infer<typeof planStepsInputSchema>) => {
            if (input.changes && input.changes.length > 0) {
                const steps: ServiceStep[] = input.changes.map((c) => ({
                    action: c.action,
                    service_type: c.service_type,
                    instance: c.instance as Record<string, unknown>,
                    points: (c.points ?? []).map((p) => ({ ...p, shm_id: 0 } as ServicePoint)),
                }));
                return JSON.stringify({
                    success: true,
                    steps_count: steps.length,
                    steps,
                });
            }

            if (!input.devices || input.devices.length === 0) {
                return JSON.stringify({
                    success: false,
                    error: "devices 不能为空——请先完成 doc-parser 获取设备信息",
                });
            }

            const { steps, warnings } = generate_steps(input, registry);

            if (steps.length === 0) {
                return JSON.stringify({
                    success: false,
                    error: "未能生成任何操作步骤——请检查设备协议是否匹配 Registry 中的服务",
                    warnings,
                });
            }

            return JSON.stringify({
                success: true,
                steps_count: steps.length,
                steps,
                warnings: warnings.length > 0 ? warnings : undefined,
            });
        },
        {
            name: "output_plan_steps",
            description:
                "将接入方案/变更请求转化为增量 MCP 服务配置步骤。" +
                "新增接入：输入 devices（从 doc-parser 获得）、可选的 site 和 forward_targets。" +
                "修改/删除：输入 changes（action=modify/delete + 目标实例 id + 变更字段）。" +
                "内部自动完成：协议→服务类型映射、instance.id 生成、默认字段填充、转发目标映射。" +
                "调用时机：用户确认方案后。",
            schema: planStepsInputSchema,
        },
    );
}

// 保留默认导出供兼容
export const outputPlanStepsTool = createOutputPlanStepsTool(null as unknown as McpServiceRegistry);

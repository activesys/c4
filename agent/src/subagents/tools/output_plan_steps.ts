// c4/agent/src/subagents/tools/output_plan_steps.ts — step-decomposer
// LLM 提供 deviceInfo（info-gatherer 产出），工具确定性生成 ServiceStep[]
// 协议映射、id 生成（abbr 记忆）、默认字段填充、运行时强校验全部由 Registry 驱动

import { tool } from "langchain";
import { z } from "zod";
import type { McpServiceRegistry } from "../../registry/registry.js";
import type {
    PointField,
    RegistryEntry,
    ServicePoint,
    ServiceStep,
} from "../../types/index.js";

// ── Schema（LLM 提供的输入，宽松骨架）─────────────────────
// 实例 plan 字段（ip/port/url/token 等）与点业务字段（addr/uid/fun/type/swap 等）
// 一律 .passthrough() 放行，具体字段名由 registry 的 config_schema/point_fields 声明。

const devicePointInputSchema = z.object({
    name: z.string().describe("数据点名称"),
}).passthrough();

const deviceInputSchema = z.object({
    name: z.string().describe("设备名称"),
    abbr: z.string().describe("采集目标标识（候选，info-gatherer 提取）"),
    protocol: z.string().describe("通信协议，如 modbus_tcp"),
    points: z.array(devicePointInputSchema).describe("数据点列表"),
}).passthrough();

const forwardTargetInputSchema = z.object({
    name: z.string().describe("转发目标名称"),
    abbr: z.string().describe("转发目标标识（候选，info-gatherer 提取）"),
    protocol: z.string().describe("转发协议，如 asfp2"),
}).passthrough();

const changePointSchema = z.object({
    id: z.string().optional(),
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
    devices: z.array(deviceInputSchema).optional().describe("设备列表（新增接入时提供）"),
    forward_targets: z.array(forwardTargetInputSchema).optional().describe("转发目标列表（新增接入时提供）"),
    changes: z.array(changeStepSchema).optional().describe("修改/删除已有实例的步骤列表"),
});

// ── 协议无关通用转换器（写一次，所有协议复用）─────────────
// agent.md §3.2「双层校验」：按 registry 动态构建 Zod，零协议硬编码。

function typeToZod(type: string): z.ZodTypeAny {
    switch (type) {
        case "integer":
            return z.number().int();
        case "number":
            return z.number();
        case "boolean":
            return z.boolean();
        default:
            return z.string();
    }
}

function pointFieldsToZod(pointFields: PointField[]) {
    const shape: Record<string, z.ZodTypeAny> = {};
    for (const f of pointFields) {
        shape[f.name] = typeToZod(f.type);
    }
    return z.object(shape).passthrough();
}

function configFieldsToZod(configSchema: RegistryEntry["config_schema"]) {
    const shape: Record<string, z.ZodTypeAny> = {};
    for (const [name, f] of Object.entries(configSchema.fields)) {
        if (f.source !== "plan") continue;
        const t = typeToZod(f.type);
        shape[name] = f.default === null ? t : t.optional();
    }
    return z.object(shape).strict();
}

function pickPlanFields(
    obj: Record<string, unknown>,
    configSchema: RegistryEntry["config_schema"],
): Record<string, unknown> {
    const out: Record<string, unknown> = {};
    for (const [name, f] of Object.entries(configSchema.fields)) {
        if (f.source === "plan" && name in obj) {
            out[name] = obj[name];
        }
    }
    return out;
}

// ── 映射逻辑 ──────────────────────────────────────────────

function normalize_protocol(protocol: string): string {
    return protocol.replace(/_tcp$/, "").replace(/^tcp_/, "");
}

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

function sanitize_identifier(text: string): string {
    return text.replace(/[^a-zA-Z0-9_]/g, "_").toLowerCase();
}

function flatten_plan_fields(
    raw: Record<string, unknown>,
    skip: string[],
): Record<string, unknown> {
    const out: Record<string, unknown> = {};
    for (const [k, v] of Object.entries(raw)) {
        if (skip.includes(k)) continue;
        if (v === undefined || v === null || v === "") continue;
        out[k] = v;
    }
    return out;
}

function fill_default_fields(
    instance: Record<string, unknown>,
    entry: RegistryEntry | null,
): void {
    if (!entry?.config_schema) return;
    for (const [field_name, field_def] of Object.entries(entry.config_schema.fields)) {
        if (field_def.source === "default" && !(field_name in instance)) {
            instance[field_name] = field_def.default;
        }
    }
}

function generate_steps(
    input: z.infer<typeof planStepsInputSchema>,
    registry: McpServiceRegistry,
    fallback_site_abbr: string,
): { steps: ServiceStep[]; warnings: string[] } {
    const steps: ServiceStep[] = [];
    const warnings: string[] = [];
    const site_abbr = input.site?.abbr || fallback_site_abbr || "";

    for (const dev of input.devices ?? []) {
        const protocol = normalize_protocol(dev.protocol);
        const svc_type = find_service_type(registry, protocol, "writer");

        if (!svc_type) {
            warnings.push(`未找到 ${protocol} 对应的 writer 服务，跳过设备 "${dev.name}"`);
            continue;
        }

        const target_abbr = dev.abbr || sanitize_identifier(dev.name);
        const instance_id = site_abbr
            ? `${site_abbr}_${target_abbr}`
            : target_abbr;

        const points: ServicePoint[] = dev.points.map((p) => {
            const raw = p as unknown as Record<string, unknown>;
            const pt: Record<string, unknown> = { id: p.name, shm_id: 0 };
            for (const [k, v] of Object.entries(raw)) {
                if (k !== "name") pt[k] = v;
            }
            return pt as unknown as ServicePoint;
        });

        const dev_raw = dev as unknown as Record<string, unknown>;
        const instance: Record<string, unknown> = {
            id: instance_id,
            name: dev.name,
        };
        Object.assign(
            instance,
            flatten_plan_fields(dev_raw, ["name", "abbr", "protocol", "points"]),
        );

        fill_default_fields(instance, registry.queryRegistry(svc_type));

        steps.push({
            action: "add",
            service_type: svc_type,
            instance,
            points,
        });
    }

    if (input.forward_targets && input.forward_targets.length > 0) {
        for (const ft of input.forward_targets) {
            const protocol = normalize_protocol(ft.protocol);
            const svc_type = find_service_type(registry, protocol, "reader");

            if (!svc_type) {
                warnings.push(`未找到 ${protocol} 对应的 reader 服务，跳过转发目标 "${ft.name}"`);
                continue;
            }

            const target_abbr = ft.abbr || sanitize_identifier(ft.name);
            const forward_instance_id = site_abbr
                ? `${site_abbr}_${target_abbr}`
                : target_abbr;

            const is_influxdb = svc_type === "c4_influxdb_client";
            const ft_raw = ft as unknown as Record<string, unknown>;
            const measurement =
                typeof ft_raw["measurement"] === "string" && ft_raw["measurement"] !== ""
                    ? ft_raw["measurement"]
                    : site_abbr;

            const reader_points: ServicePoint[] = [];
            let auto_addr = 3001;
            for (const writer_step of steps) {
                for (const pt of writer_step.points) {
                    const point_key = `${writer_step.instance.id}.${pt.id}`;
                    const rp: Record<string, unknown> = { key: point_key, shm_id: 0 };
                    if (is_influxdb) {
                        rp["measurement"] = measurement;
                    } else {
                        rp["addr"] = auto_addr++;
                    }
                    reader_points.push(rp as unknown as ServicePoint);
                }
            }

            const instance: Record<string, unknown> = {
                id: forward_instance_id,
                name: ft.name,
            };
            Object.assign(
                instance,
                flatten_plan_fields(ft_raw, ["name", "abbr", "protocol"]),
            );

            fill_default_fields(instance, registry.queryRegistry(svc_type));

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

// ── 运行时强校验（双层校验 ②，agent.md §3.2）─────────────
// 只有通过 registry 驱动强校验的数据才进入 generate_steps → config.json。

function validate_runtime_input(
    input: z.infer<typeof planStepsInputSchema>,
    registry: McpServiceRegistry,
): string | null {
    for (const dev of input.devices ?? []) {
        const svc_type = find_service_type(registry, normalize_protocol(dev.protocol), "writer");
        if (!svc_type) continue;
        const entry = registry.queryRegistry(svc_type);
        if (!entry) continue;

        const point_schema = pointFieldsToZod(entry.point_fields);
        const config_schema = configFieldsToZod(entry.config_schema);
        for (const pt of dev.points) {
            const r = point_schema.safeParse(pt);
            if (!r.success) {
                return JSON.stringify({ success: false, errors: r.error.issues });
            }
        }
        const r2 = config_schema.safeParse(
            pickPlanFields(dev as unknown as Record<string, unknown>, entry.config_schema),
        );
        if (!r2.success) {
            return JSON.stringify({ success: false, errors: r2.error.issues });
        }
    }

    for (const ft of input.forward_targets ?? []) {
        const svc_type = find_service_type(registry, normalize_protocol(ft.protocol), "reader");
        if (!svc_type) continue;
        const entry = registry.queryRegistry(svc_type);
        if (!entry) continue;

        const config_schema = configFieldsToZod(entry.config_schema);
        const r = config_schema.safeParse(
            pickPlanFields(ft as unknown as Record<string, unknown>, entry.config_schema),
        );
        if (!r.success) {
            return JSON.stringify({ success: false, errors: r.error.issues });
        }
    }

    return null;
}

// ── 工厂函数 ──────────────────────────────────────────────

export function createOutputPlanStepsTool(
    registry: McpServiceRegistry,
    site?: { name: string; abbr: string } | null,
) {
    const fallback_site_abbr = site?.abbr ?? "";
    return tool(
        async (input: z.infer<typeof planStepsInputSchema>) => {
            if (input.changes && input.changes.length > 0) {
                const steps: ServiceStep[] = input.changes.map((c) => ({
                    action: c.action,
                    service_type: c.service_type,
                    instance: c.instance as Record<string, unknown>,
                    points: (c.points ?? []).map(
                        (p) => ({ ...p, shm_id: 0 }) as unknown as ServicePoint,
                    ),
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
                    error: "devices 不能为空——请先完成 info-gatherer 获取设备信息",
                });
            }

            const validation_error = validate_runtime_input(input, registry);
            if (validation_error) {
                return validation_error;
            }

            const { steps, warnings } = generate_steps(input, registry, fallback_site_abbr);

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
                "新增接入：输入 devices（info-gatherer 产出，含 abbr/协议/平铺的实例字段）、可选的 site 和 forward_targets。" +
                "修改/删除：输入 changes（action=modify/delete + 目标实例 id + 变更字段）。" +
                "内部自动完成：协议→服务类型映射、instance.id 生成（{site_abbr}_{abbr}）、默认字段填充、运行时强校验、转发目标映射。" +
                "调用时机：用户确认方案后。",
            schema: planStepsInputSchema,
        },
    );
}

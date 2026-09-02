// c4/agent/src/subagents/tools/output_plan_steps.ts — step-decomposer
// LLM 提供 deviceInfo（info-gatherer 产出），工具确定性生成 ServiceStep[]
// 协议映射、id 生成（abbr 记忆）、默认字段填充、运行时强校验全部由 Registry 驱动

import { tool } from "langchain";
import { z } from "zod";
import type { McpServiceRegistry } from "../../registry/registry.js";
import {
    IDENTIFIER_RE,
    MAX_IDENTIFIER_LENGTH,
    generate_point_id,
    identifier_error,
    identity_field_key,
    point_duplicate_error,
    sanitize_identifier,
} from "../../executor/executor.js";
import type {
    PointField,
    RegistryEntry,
    ServicePoint,
    ServiceStep,
} from "../../types/index.js";

// ── Schema（LLM 提供的输入，宽松骨架）─────────────────────
// 实例 plan 字段（ip/port/url/token 等）与点业务字段（addr/uid/fun/type/swap 等）
// 一律 .passthrough() 放行，具体字段名由 registry 的 config_schema/point_schema.fields 声明。

const devicePointInputSchema = z.object({
    name: z.string().describe("数据点名称（英文标识；无点名传空字符串，系统按身份字段自动生成）"),
}).passthrough();

const deviceInputSchema = z.object({
    name: z.string().describe("设备名称"),
    abbr: z.string().describe("采集目标标识（候选，info-gatherer 提取）"),
    protocol: z.string().describe("通信协议，如 modbus"),
    points: z.array(devicePointInputSchema).min(1).describe("数据点列表（至少一个点）"),
}).passthrough();

const forwardTargetInputSchema = z.object({
    name: z.string().describe("转发目标名称"),
    abbr: z.string().describe("转发目标标识（候选，info-gatherer 提取）"),
    protocol: z.string().describe("转发协议，如 asfp2"),
    points: z.array(devicePointInputSchema).optional().describe(
        "转发点业务字段（必要项）：按采集点顺序与采集点一一对应；" +
        "每个元素必须包含该服务 point_schema.fields 声明的全部业务字段" +
        "（如 ASFP2 转发的 addr 转发地址、InfluxDB 的 measurement/field/type）。" +
        "用户未提供时先询问用户，禁止编造",
    ),
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
        const required = f.default === undefined || f.default === null;
        // 必填 integer（如 port）须为正数——0 会被 OS 解释为随机端口，静默错行为
        const t =
            required && f.type === "integer" ? z.number().int().positive() : typeToZod(f.type);
        shape[name] = required ? t : t.optional();
    }
    return z.object(shape).strict();
}

function pickPlanFields(
    obj: Record<string, unknown>,
    configSchema: RegistryEntry["config_schema"],
): Record<string, unknown> {
    const out: Record<string, unknown> = {};
    for (const [name, _f] of Object.entries(configSchema.fields)) {
        const v = obj[name];
        // 丢弃 undefined/null/空串（与 flatten_plan_fields 同语义），
        // 防止 "ip": "" 经 z.string() 通过后又被 flatten 丢弃、绕过必填校验
        if (v === undefined || v === null || v === "") {
            continue;
        }
        out[name] = v;
    }
    return out;
}

// ── 映射逻辑 ──────────────────────────────────────────────

export function normalize_protocol(protocol: string): string {
    return protocol.replace(/_tcp$/, "").replace(/^tcp_/, "");
}

export function find_service_type(
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
        if (!(field_name in instance)) {
            if (field_def.default !== undefined && field_def.default !== null) {
                instance[field_name] = field_def.default;
            }
            // 无 default 键 = 必填项（用户提供原则），不参与填充；
            // 缺失由 validate_runtime_input 的 Zod 强校验拦截（configFieldsToZod）
        }
    }
}

// ── 端口保障（agent.md「监听端口的必填约束」+ c4_asfp2_server.md §2.2）──
//   端口为必填项（registry 声明：字段无 default 键），必须由用户显式指定，缺失由
//   validate_runtime_input 的 Zod 强校验拦截（fatal，要求询问用户），不自动选择空闲端口。
//   「已接入实例端口保持原值」的真实保障点在执行模块：
//     · handle_add 对已存在 instance.id 早退合并（仅合并 points，不触碰实例字段）
//     · handle_modify 显式排除 port 覆盖
//   （原 assign_port/端口清点逻辑为恒 no-op 死代码，已删除）

function generate_steps(
    input: z.infer<typeof planStepsInputSchema>,
    registry: McpServiceRegistry,
    fallback_site_abbr: string,
): { steps: ServiceStep[]; warnings: string[]; fatal: string | null } {
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

        const writer_entry = registry.queryRegistry(svc_type);
        const identity_fields = writer_entry?.point_schema.identity_fields ?? [];

        const target_abbr = dev.abbr || sanitize_identifier(dev.name);
        const instance_id = site_abbr
            ? `${site_abbr}_${target_abbr}`
            : target_abbr;

        // 点名三态 + 硬约束（§3.2.1.3b）：空字符串视为无点名 → 从身份字段生成；
        // 格式不符 → 从身份字段重新生成；超长 → 报错；
        // 点重复（identity_fields 组合重复）→ 按报告口径返回，不产出任何步骤
        const seen_identity = new Map<string, string>();
        const points: ServicePoint[] = [];

        for (const p of dev.points) {
            const raw = p as unknown as Record<string, unknown>;
            const name_raw =
                typeof raw["name"] === "string" ? (raw["name"] as string).trim() : "";

            let id: string;
            if (name_raw === "" || (!IDENTIFIER_RE.test(name_raw) && name_raw.length <= MAX_IDENTIFIER_LENGTH)) {
                if (identity_fields.length === 0) {
                    return {
                        steps,
                        warnings,
                        fatal: `设备 "${dev.name}" 存在缺少或非法点名的点，且 ${svc_type} 未声明 point_schema.identity_fields，无法生成点名`,
                    };
                }
                id = generate_point_id(raw, identity_fields);
            } else if (name_raw.length > MAX_IDENTIFIER_LENGTH) {
                return {
                    steps,
                    warnings,
                    fatal: `设备 "${dev.name}" 的点名太长（超过 ${MAX_IDENTIFIER_LENGTH} 字节），请保证在 1K 以内`,
                };
            } else {
                id = name_raw;
            }

            if (identity_fields.length > 0) {
                const ikey = identity_field_key(raw, identity_fields);
                if (ikey !== null) {
                    const prev_id = seen_identity.get(ikey);
                    if (prev_id !== undefined) {
                        return {
                            steps,
                            warnings,
                            fatal: point_duplicate_error(
                                `设备 "${dev.name}"`,
                                [
                                    { identity: ikey, id: prev_id },
                                    { identity: ikey, id },
                                ],
                            ),
                        };
                    }
                    seen_identity.set(ikey, id);
                }
            }

            const pt: Record<string, unknown> = { id, shm_id: 0 };
            for (const [k, v] of Object.entries(raw)) {
                if (k !== "name") pt[k] = v;
            }
            points.push(pt as unknown as ServicePoint);
        }

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

            const reader_entry = registry.queryRegistry(svc_type);
            const system_fields = new Set(["key", "shm_id", "id", "name"]);
            const required_fields = (reader_entry?.point_schema.fields ?? [])
                .map((f) => f.name)
                .filter((f) => !system_fields.has(f));

            const ft_raw = ft as unknown as Record<string, unknown>;
            const ft_points_raw = Array.isArray(ft_raw["points"])
                ? (ft_raw["points"] as unknown[]).map(
                      (p) => p as Record<string, unknown>,
                  )
                : null;

            const writer_points_total = steps.reduce(
                (n, s) => n + s.points.length,
                0,
            );

            if (required_fields.length > 0) {
                if (!ft_points_raw) {
                    return {
                        steps,
                        warnings,
                        fatal: `转发目标 "${ft.name}" 缺少点业务字段（${required_fields.join(", ")}）——这些是必要项，用户尚未提供。请向用户逐项询问后再调用本工具，禁止自行编造`,
                    };
                }
                if (ft_points_raw.length !== writer_points_total) {
                    return {
                        steps,
                        warnings,
                        fatal: `转发目标 "${ft.name}" 的 points 数量（${ft_points_raw.length}）与采集点数量（${writer_points_total}）不一致，请按采集点顺序逐点提供 ${required_fields.join(", ")}`,
                    };
                }
            }

            const reader_points: ServicePoint[] = [];
            let point_index = 0;
            for (const writer_step of steps) {
                for (const pt of writer_step.points) {
                    const rp: Record<string, unknown> = {
                        key: `${writer_step.instance.id}.${pt.id}`,
                        shm_id: 0,
                    };
                    if (ft_points_raw) {
                        const src = ft_points_raw[point_index] ?? {};
                        for (const f of required_fields) {
                            const v = src[f];
                            if (v === undefined || v === null || v === "") {
                                return {
                                    steps,
                                    warnings,
                                    fatal: `转发目标 "${ft.name}" 的第 ${point_index + 1} 个转发点缺少必要字段 "${f}"，请向用户询问后重试，禁止编造`,
                                };
                            }
                            rp[f] = v;
                        }
                    }
                    reader_points.push(rp as unknown as ServicePoint);
                    point_index++;
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

    return { steps, warnings, fatal: null };
}

// ── 运行时强校验（双层校验 ②，agent.md §3.2）─────────────
// 只有通过 registry 驱动强校验的数据才进入 generate_steps → config.json。
// 错误信息必须带上下文（哪个设备/转发目标、第几个点、缺什么字段）——
// 模糊的错误会让 LLM 误判出错位置并陷入盲目重试循环。

function zod_issues_to_text(issues: z.ZodIssue[]): string {
    return issues
        .map((iss) => {
            const field = iss.path.join(".") || "(root)";
            if (iss.code === "invalid_type" && String((iss as { received?: unknown }).received) === "undefined") {
                return `缺少必要字段 "${field}"`;
            }
            return `字段 "${field}" 不合法: ${iss.message}`;
        })
        .join("; ");
}

function validate_runtime_input(
    input: z.infer<typeof planStepsInputSchema>,
    registry: McpServiceRegistry,
): string | null {
    for (const dev of input.devices ?? []) {
        const svc_type = find_service_type(registry, normalize_protocol(dev.protocol), "writer");
        if (!svc_type) continue;
        const entry = registry.queryRegistry(svc_type);
        if (!entry) continue;

        const point_schema = pointFieldsToZod(entry.point_schema.fields);
        const config_schema = configFieldsToZod(entry.config_schema);
        for (let i = 0; i < dev.points.length; i++) {
            const r = point_schema.safeParse(dev.points[i]);
            if (!r.success) {
                return JSON.stringify({
                    success: false,
                    error:
                        `设备 "${dev.name}" 的第 ${i + 1} 个点${zod_issues_to_text(r.error.issues)}` +
                        `。请从用户点表逐点补齐后重新调用，禁止编造`,
                });
            }
        }
        const r2 = config_schema.safeParse(
            pickPlanFields(dev as unknown as Record<string, unknown>, entry.config_schema),
        );
        if (!r2.success) {
            return JSON.stringify({
                success: false,
                error: `设备 "${dev.name}" ${zod_issues_to_text(r2.error.issues)}。请向用户确认后重新调用`,
            });
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
            return JSON.stringify({
                success: false,
                error: `转发目标 "${ft.name}" ${zod_issues_to_text(r.error.issues)}。请向用户确认后重新调用`,
            });
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
                for (const c of input.changes) {
                    const inst = c.instance as Record<string, unknown>;
                    const inst_id = inst["id"];
                    if (typeof inst_id === "string" && inst_id.length > 0) {
                        const err = identifier_error(inst_id, "instance.id");
                        if (err) {
                            return JSON.stringify({ success: false, error: err });
                        }
                    }
                    for (const p of c.points ?? []) {
                        const pid = (p as unknown as Record<string, unknown>)["id"];
                        if (typeof pid === "string" && pid.length > 0) {
                            const err = identifier_error(pid, "point.id");
                            if (err) {
                                return JSON.stringify({ success: false, error: err });
                            }
                        }
                    }
                }
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

            const { steps, warnings, fatal } = generate_steps(
                input,
                registry,
                fallback_site_abbr,
            );

            if (fatal) {
                return JSON.stringify({
                    success: false,
                    error: fatal,
                    warnings: warnings.length > 0 ? warnings : undefined,
                });
            }

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

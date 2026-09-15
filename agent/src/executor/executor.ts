// c4/agent/src/executor/executor.ts — Agent 执行模块
// 确定性代码：合并 AccessPlanSteps → config.json + Stop-Start 安全协议
// 设计：agent.md §3.2, §3.2.1.6, §3.2.3

import * as fs from "node:fs/promises";
import type {
    MCPInstanceConfig,
    RegistryEntry,
    ServicePoint,
    ServiceStep,
    SystemConfig,
} from "../types/index.js";
import type { C4McpManager } from "../mcp/client.js";
import { restore_prev1, atomic_write_raw } from "./transaction.js";
import { SHM_SERVICE_TYPE } from "../mcp/client.js";

// ── Point 匹配辅助 ────────────────────────────────────────
// ServicePoint 是判别联合（WriterPoint.id / ReaderPoint.key），
// 合并/匹配逻辑统一按「匹配键」（reader 用 key，writer 用 id）处理。

function point_match_key(pt: ServicePoint): string {
    const rec = pt as unknown as Record<string, unknown>;
    const key = rec["key"];
    const id = rec["id"];
    if (typeof key === "string" && key.length > 0) {
        return key;
    }
    if (typeof id === "string" && id.length > 0) {
        return id;
    }
    return "";
}

// ── MCP Client Interfaces ─────────────────────────────────

/** 数据路径 MCP 服务客户端（stop / start 生命周期工具） */
export interface McpServiceClient {
    /** 服务类型（如 "c4_modbus_client"），用于日志和错误报告 */
    readonly service_type: string;
    /** 关闭全部数据路径，销毁实例状态。IDEMPOTENT：对已停止的服务调用仍返回 success */
    stop(): Promise<string>;
    /** 加载配置，附加共享内存，启动所有数据路径实例 */
    start(): Promise<string>;
}

/** c4_shm_manager 专用客户端——额外提供 create_shm / adjust_shm 工具 */
export interface ShmManagerClient extends McpServiceClient {
    /** 首次启动时创建共享内存（shm 已存在时 shm_manager 返回 SHM_ALREADY_EXISTS） */
    create_shm(): Promise<string>;
    /** 根据配置文件调整共享内存容量和点分配。前置条件：所有数据路径 MCP 已 stop */
    adjust_shm(): Promise<string>;
}

/** Registry 查询接口（执行模块不实现 Registry，只消费其接口） */
export interface RegistryLookup {
    /** 按 service_type 查询完整 Registry 条目 */
    get_entry(service_type: string): RegistryEntry | undefined;
    /** 全部已注册服务类型（Stop 阶段全集用） */
    service_types(): string[];
}

// ── Result Types ──────────────────────────────────────────

/** executeStopAndStart 的执行结果 */
export interface StopStartResult {
    success: boolean;
    /** Start 阶段成功启动的服务列表 */
    started_services: string[];
    /** Start 阶段失败的服务列表（含错误信息） */
    failed_services: Array<{ service_type: string; instance_id?: string; error: string }>;
    /** 若操作被中止，描述原因 */
    abort_reason?: string;
}

/** mergeConfigFromSteps 的合并结果 */
export interface MergeResult {
    success: boolean;
    /** 合并后的全量配置 */
    config: SystemConfig;
    /** 警告信息（如配置文件不存在时创建新文件） */
    warnings: string[];
    /** 失败原因 */
    error?: string;
}

// ── Error Classification ───────────────────────────────────

/** adjust_shm 的 config 类错误码——回滚时恢复 config.json.prev.1（事务层） */
const CONFIG_CLASS_ERRORS = new Set([
    "DUPLICATE_KEY",
    "CONFIG_MISSING_SECTION",
    "UNKNOWN_READER_KEY",
]);

/** adjust_shm 的非 config 类错误码——回滚行为与 config 类一致（agent.md §3.2.2） */
const NON_CONFIG_CLASS_ERRORS = new Set([
    "SHM_SYSCALL_FAILED",
    "SHM_NOT_CREATED",
]);

/** 从 MCP 应答文本中提取错误码（`ERROR_TYPE:` 前缀） */
function extract_error_code(text: string): string | null {
    const match = text.match(/^([A-Z_]+):/);
    return match ? match[1] : null;
}

/** 判断 adjust_shm 错误是否属于 config 类 */
function is_config_class_error(error_text: string): boolean {
    const code = extract_error_code(error_text);
    return code !== null && CONFIG_CLASS_ERRORS.has(code);
}

/** 判断 adjust_shm 错误是否属于非 config 类 */
function is_non_config_class_error(error_text: string): boolean {
    const code = extract_error_code(error_text);
    return code !== null && NON_CONFIG_CLASS_ERRORS.has(code);
}

// ── EMPTY CONFIG ──────────────────────────────────────────

function empty_config(): SystemConfig {
    return {
        c4_shm_manager: { writer: [], reader: [] },
    };
}

// ── mergeConfigFromSteps ───────────────────────────────────

/**
 * 将 AccessPlanSteps 合并到 config.json（全量配置）。
 *
 * 流程（agent.md §3.2，c4_architecture.md §3.1.2 事务步骤 3）：
 * 1. 读取现有 config.json（不存在 → 创建空结构；损坏 → 报错，回滚由事务层负责）
 * 2. 逐一处理 add / modify / delete（§3.2.1.6 规则）
 * 3. 原子写入：临时文件 → fsync → rename → 父目录 fsync
 *    （回滚源 config.json.prev.1 与事务标记由事务层在本函数之前写入）
 *
 * @param steps    本次接入的增量操作步骤
 * @param config_path 配置文件路径（如 ~/.local/c4/config.json）
 * @param registry Registry 查询接口（用于填充技术默认值字段和角色查询）
 */
export async function merge_config_from_steps(
    steps: ServiceStep[],
    config_path: string,
    registry?: RegistryLookup,
): Promise<MergeResult> {
    const warnings: string[] = [];

    // ── Step 1-2: 读取现有配置 ──
    let config: SystemConfig;
    let current_raw = "";

    try {
        current_raw = await fs.readFile(config_path, "utf-8");
        config = JSON.parse(current_raw) as SystemConfig;
        // 确保 c4_shm_manager 段存在
        if (!config.c4_shm_manager) {
            config.c4_shm_manager = { writer: [], reader: [] };
            warnings.push("config.json 缺少 c4_shm_manager 段，已自动补齐");
        }
        if (!Array.isArray(config.c4_shm_manager.writer)) {
            config.c4_shm_manager.writer = [];
        }
        if (!Array.isArray(config.c4_shm_manager.reader)) {
            config.c4_shm_manager.reader = [];
        }
    } catch (err: unknown) {
        if ((err as NodeJS.ErrnoException).code === "ENOENT") {
            // 文件不存在 → 创建空结构
            config = empty_config();
            warnings.push("config.json 不存在，创建新配置文件");
        } else {
            // JSON 损坏 → 中止合并（磁盘 config.json 未被触碰；L0/事务层负责恢复）
            return {
                success: false,
                config: empty_config(),
                warnings,
                error: "config.json 已损坏，无法合并变更，请重启 Agent 以执行启动恢复",
            };
        }
    }
    void current_raw;

    // ── Step 4: 处理 steps ──
    // 两段式处理：先 writer 后 reader（stable partition）。撞名去重的改名传播
    // 依赖处理顺序——reader 步骤若先于 writer 执行，其转发点 key 无法跟随
    // writer 撞名改名（func_test_case 用例 10：LLM 步骤顺序非确定）。
    const writerSteps: ServiceStep[] = [];
    const readerSteps: ServiceStep[] = [];
    for (const step of steps) {
        const entry = registry?.get_entry(step.service_type);
        (entry?.role === "reader" ? readerSteps : writerSteps).push(step);
    }

    // writer 点去重后，同批次 reader 转发点的 key 必须跟随新 id（用例 10）
    const renames = new Map<string, Map<string, string>>();
    for (const step of [...writerSteps, ...readerSteps]) {
        const svc_type = step.service_type;
        if (svc_type === "c4_shm_manager") {
            return {
                success: false,
                config,
                warnings,
                error: "不允许直接操作 c4_shm_manager 配置段——writer/reader 由执行模块自动维护",
            };
        }

        // 确保目标数组存在
        if (!Array.isArray(config[svc_type])) {
            (config as Record<string, unknown>)[svc_type] = [];
        }
        const instances = config[svc_type] as MCPInstanceConfig[];

        // 转发点引用校验（func_test_case 用例 18）：reader 点 key 必须引用已存在的
        // writer 采集点（本批次先处理或既有配置），防止转发侧独立写入造成数据错配
        const entry_r = registry?.get_entry(svc_type);
        if (entry_r?.role === "reader" && Array.isArray(step.points)) {
            for (const p of step.points) {
                const k = point_match_key(p);
                const dot = k.indexOf(".");
                if (dot <= 0) continue;
                const writer_id = k.slice(0, dot);
                const pid = k.slice(dot + 1);
                const exists = Object.entries(config).some(([st2, list2]) => {
                    if (st2 === "c4_shm_manager" || !Array.isArray(list2)) return false;
                    return (list2 as MCPInstanceConfig[]).some(
                        (i2) => i2.id === writer_id &&
                            (i2.points ?? []).some((p2) => point_match_key(p2) === pid),
                    );
                });
                if (!exists) {
                    throw new Error(
                        `转发点 ${k} 引用的采集点不存在（${writer_id} 上没有点 "${pid}"），请先确认采集侧点表`,
                    );
                }
            }
        }

        switch (step.action) {
            case "add":
                await handle_add(step, instances, config, registry, warnings, renames);
                break;
            case "modify":
                handle_modify(step, instances, warnings, renames);
                break;
            case "delete":
                handle_delete(step, instances, config, registry, warnings);
                break;
            default:
                return {
                    success: false,
                    config,
                    warnings,
                    error: `未知操作类型: ${(step as { action: string }).action}`,
                };
        }
    }

    // ── Step 3: 原子写入（临时文件 → fsync → rename → 父目录 fsync）──
    const output = JSON.stringify(config, null, 4) + "\n";
    await atomic_write_raw(config_path, output);

    return { success: true, config, warnings };
}

// ── 标识符校验（§3.2.1.3b）────────────────────────────────
// 不含点号：global key 以 `.` 作为分隔符（{instance.id}.{point.id}）
export const IDENTIFIER_RE = /^[a-zA-Z][a-zA-Z0-9_]*$/;
export const MAX_IDENTIFIER_LENGTH = 1024;

export function identifier_error(value: string, label: string): string | null {
    if (!IDENTIFIER_RE.test(value)) {
        return `${label} "${value}" 包含非法字符，仅允许字母开头`;
    }
    if (value.length > MAX_IDENTIFIER_LENGTH) {
        return `${label} "${value}" 太长（超过 ${MAX_IDENTIFIER_LENGTH} 字节），请保证在 1K 以内`;
    }
    return null;
}

function validate_identifier(value: string, label: string): void {
    const err = identifier_error(value, label);
    if (err) {
        throw new Error(err);
    }
}

// ── 身份字段与点名生成（§3.2.1.3b）────────────────────────

export function sanitize_identifier(text: string): string {
    return text.replace(/[^a-zA-Z0-9_]/g, "_").toLowerCase();
}

/** 身份字段组合键（如 "uid=1, fun=3, addr=1000"）；任一身份字段缺值 → null */
export function identity_field_key(
    rec: Record<string, unknown>,
    identity_fields: string[],
): string | null {
    const parts: string[] = [];
    for (const f of identity_fields) {
        const v = rec[f];
        if (v === undefined || v === null || v === "") {
            return null;
        }
        parts.push(`${f}=${String(v)}`);
    }
    return parts.join(", ");
}

/** 生成名 = `p_` + 身份字段值（先 sanitize_identifier）按 identity_fields 顺序用 `_` 连接 */
export function generate_point_id(
    rec: Record<string, unknown>,
    identity_fields: string[],
): string {
    return "p_" + identity_fields
        .map((f) => sanitize_identifier(String(rec[f] ?? "")))
        .join("_");
}

/** 点重复报告（§3.2.1.3b 报告口径）：仅展示冲突项，二选一——提供新点表 / 结束本次接入任务 */
export function point_duplicate_error(
    context: string,
    conflicts: Array<{ identity: string; id: string }>,
): string {
    const items = conflicts
        .map((c) => `  - ${c.identity}（点名 ${c.id}）`)
        .join("\n");
    return (
        `点重复：${context} 中以下点的身份字段组合重复，这是点表的问题，未写入任何变更。\n` +
        `冲突的点：\n${items}\n` +
        `请选择：提供修正后的新点表（已收集的实例参数无需重复提供），或结束本次接入任务。`
    );
}

// ── handle_add ────────────────────────────────────────────

async function handle_add(
    step: ServiceStep,
    instances: MCPInstanceConfig[],
    config: SystemConfig,
    registry: RegistryLookup | undefined,
    warnings: string[],
    renames?: Map<string, Map<string, string>>,
): Promise<void> {
    const instance_id = step.instance["id"] as string | undefined;
    if (!instance_id || typeof instance_id !== "string") {
        throw new Error(`add 操作缺少 instance.id: ${step.service_type}`);
    }
    validate_identifier(instance_id, "instance.id");

    // reader 点 key 跟随同批次 writer 撞名改名——无论实例是新建还是合并
    // （新建实例路径原先不做改名传播，func_test_case 用例 10 复现）
    if (renames) {
        for (const pt of step.points) {
            const rec_r = pt as unknown as Record<string, unknown>;
            const key_r = typeof rec_r["key"] === "string" ? (rec_r["key"] as string) : "";
            const dot_r = key_r.indexOf(".");
            if (dot_r <= 0) continue;
            const prefix_r = key_r.slice(0, dot_r);
            const pid_r = key_r.slice(dot_r + 1);
            const new_id_r = renames.get(prefix_r)?.get(pid_r);
            if (new_id_r) {
                rec_r["key"] = `${prefix_r}.${new_id_r}`;
            }
        }
    }

    // 有效实例 id：同 id 且端口相同（或无端口语义）→ 合并 points（增量加点）；
    // 同 id 但端口不同 → 语义是不同的转发连接/监听，生成带序号的新实例 id，
    // 禁止把用户指定的新端口静默并入旧实例（func_test_case 用例 21：9901 被并入 9900 实例）
    let effective_id = instance_id;
    {
        const want_raw = (step.instance as Record<string, unknown>)["port"];
        const want_port = want_raw === undefined ? undefined : Number(want_raw);
        for (let guard = 1; guard <= 50; guard++) {
            const existing_same = instances.find((i) => i.id === effective_id);
            if (!existing_same) break;
            const have_raw = (existing_same as Record<string, unknown>)["port"];
            const have_port = have_raw === undefined ? undefined : Number(have_raw);
            if (want_port === undefined || have_port === undefined || have_port === want_port) break;
            effective_id = `${instance_id}_${guard}`;
        }
    }

    // 检查 instance.id 是否与现有冲突：若已存在同名实例（如追加设备时转发目标已存在），
    // 合并 points（追加新 point、更新已有 point），而非报错。
    for (const existing of instances) {
        if (existing.id === effective_id) {
            for (const pt of step.points) {
                // reader 点 key 跟随同批次 writer 撞名改名（func_test_case 用例 10）
                const pt_rec_r = pt as unknown as Record<string, unknown>;
                const rkey = typeof pt_rec_r["key"] === "string" ? (pt_rec_r["key"] as string) : "";
                const dot = rkey.indexOf(".");
                if (dot > 0 && renames) {
                    const prefix = rkey.slice(0, dot);
                    const pid = rkey.slice(dot + 1);
                    const new_id = renames.get(prefix)?.get(pid);
                    if (new_id) {
                        pt_rec_r["key"] = `${prefix}.${new_id}`;
                    }
                }
                const match_key = point_match_key(pt);
                const existing_idx = existing.points.findIndex(
                    (p) => point_match_key(p) === match_key,
                );
                if (existing_idx >= 0) {
                    const existing_rec = existing.points[existing_idx] as unknown as Record<string, unknown>;
                    const pt_rec = pt as unknown as Record<string, unknown>;
                    // 撞名保护（func_test_case 用例 10）：同名点但业务地址不同 → 独立新点
                    // （追加序号去重）。禁止覆盖——覆盖会改写既有点 addr 造成数据损坏。
                    if (
                        existing_rec["addr"] !== pt_rec["addr"] &&
                        typeof pt_rec["id"] === "string" && pt_rec["id"].length > 0
                    ) {
                        const base = String(pt_rec["id"]);
                        let seq = 2;
                        let cand = `${base}_${seq}`;
                        const taken = (k: string) =>
                            existing.points.some((p) => point_match_key(p) === k);
                        while (taken(cand)) {
                            seq += 1;
                            cand = `${base}_${seq}`;
                        }
                        pt_rec["id"] = cand;
                        if (renames && typeof instance_id === "string") {
                            let m = renames.get(instance_id);
                            if (!m) {
                                m = new Map<string, string>();
                                renames.set(instance_id, m);
                            }
                            m.set(base, cand);
                        }
                        existing.points.push(
                            { ...pt, id: cand, shm_id: 0 } as unknown as ServicePoint,
                        );
                        warnings.push(
                            `add: 点 "${base}" 撞名且地址不同（addr ` +
                            `${String(existing_rec["addr"])}→${String(pt_rec["addr"])}），去重为 "${cand}"`,
                        );
                    } else {
                        existing.points[existing_idx] = {
                            ...existing_rec,
                            ...pt_rec,
                            shm_id: existing_rec["shm_id"],
                        } as unknown as ServicePoint;
                    }
                } else {
                    // 地址冲突检查（func_test_case 用例 18）：新增点 addr 与已有点相同 → 可读拒绝
                    const rec2 = pt as unknown as Record<string, unknown>;
                    if (typeof rec2["addr"] === "number") {
                        const clash = existing.points.find(
                            (p) =>
                                (p as unknown as Record<string, unknown>)["addr"] === rec2["addr"],
                        );
                        if (clash) {
                            const cid = (clash as unknown as Record<string, unknown>)["id"];
                            throw new Error(
                                `地址 ${rec2["addr"]} 已被点 "${cid}" 占用` +
                                `（${step.service_type}.${effective_id}），请更换地址或先删除原点`,
                            );
                        }
                    }
                    existing.points.push({ ...pt, shm_id: pt.shm_id ?? 0 });
                }
            }
            warnings.push(
                `add: ${step.service_type}.${effective_id} 已存在，合并 points`,
            );
            return;
        }
    }

    // 检查 points 的 id 不重复（在本次 step 内，仅 writer 点有 id）
    const point_ids = new Set<string>();
    for (const pt of step.points) {
        const rec = pt as unknown as Record<string, unknown>;
        const id = rec["id"];
        if (typeof id !== "string" || id.length === 0) {
            continue;
        }
        validate_identifier(id, "point.id");
        if (point_ids.has(id)) {
            throw new Error(
                `point.id "${id}" 在 ${step.service_type}.${effective_id} 中重复`,
            );
        }
        point_ids.add(id);
    }

    // 点重复（§3.2.1.3b 硬约束 3，最终防线）：identity_fields 组合在本次 step 内不重复
    const entry = registry?.get_entry(step.service_type);
    const identity_fields = entry?.point_schema.identity_fields;
    if (identity_fields && identity_fields.length > 0) {
        const seen_identity = new Map<string, string>();
        for (const pt of step.points) {
            const rec = pt as unknown as Record<string, unknown>;
            const ikey = identity_field_key(rec, identity_fields);
            if (ikey === null) {
                continue;
            }
            const this_id = typeof rec["id"] === "string" ? (rec["id"] as string) : "";
            const prev_id = seen_identity.get(ikey);
            if (prev_id !== undefined) {
                throw new Error(
                    point_duplicate_error(
                        `${step.service_type}.${effective_id}`,
                        [
                            { identity: ikey, id: prev_id },
                            { identity: ikey, id: this_id },
                        ],
                    ),
                );
            }
            seen_identity.set(ikey, this_id);
        }
    }

    // 构建实例配置：合并 instance 字段 + points
    const new_instance: MCPInstanceConfig = {
        id: effective_id,
        name: (step.instance["name"] as string) || instance_id,
        points: step.points.map((pt) => ({ ...pt, shm_id: pt.shm_id ?? 0 })),
    };

    // 从 step.instance 复制其他字段（除 id, name 外）
    for (const [key, value] of Object.entries(step.instance)) {
        if (key !== "id" && key !== "name" && key !== "points") {
            (new_instance as Record<string, unknown>)[key] = value;
        }
    }

    // 字段名归一化: LLM 可能产出 host 而非 ip
    const inst_raw = new_instance as Record<string, unknown>;
    if (inst_raw["host"] && !inst_raw["ip"]) {
        inst_raw["ip"] = inst_raw["host"];
    }

    // 填充 Registry 技术默认值字段（声明了 default 的项）
    if (registry) {
        const entry = registry.get_entry(step.service_type);
        if (entry && entry.config_schema) {
            for (const [field_name, field_def] of Object.entries(
                entry.config_schema.fields,
            )) {
                if (field_def.default !== undefined && field_def.default !== null) {
                    // 只在 step 未提供该字段时才填充默认值
                    if (!(field_name in new_instance)) {
                        (new_instance as Record<string, unknown>)[field_name] =
                            field_def.default;
                    }
                }
            }
        } else {
            warnings.push(
                `${step.service_type} 未在 Registry 中找到，跳过默认字段填充`,
            );
        }
    }

    // 追加到数组
    instances.push(new_instance);

    // 如果是该 service_type 的第一个实例，更新 shm_manager 分类
    if (instances.length === 1) {
        update_shm_classification(step.service_type, "add", config, registry, warnings);
    }
}

// ── handle_modify ─────────────────────────────────────────

function handle_modify(
    step: ServiceStep,
    instances: MCPInstanceConfig[],
    warnings: string[],
    renames?: Map<string, Map<string, string>>,
): void {
    const instance_id = step.instance["id"] as string | undefined;
    if (!instance_id || typeof instance_id !== "string") {
        throw new Error(`modify 操作缺少 instance.id: ${step.service_type}`);
    }
    validate_identifier(instance_id, "instance.id");

    const target = instances.find((inst) => inst.id === instance_id);
    if (!target) {
        throw new Error(
            `modify 目标不存在: ${step.service_type} 中找不到 id="${instance_id}"`,
        );
    }

    // 字段名归一化: LLM 可能产出 host 而非 ip（与 add 路径一致）
    const inst_raw = step.instance as Record<string, unknown>;
    if (inst_raw["host"] && !inst_raw["ip"]) {
        inst_raw["ip"] = inst_raw["host"];
    }

    // 浅合并 instance 字段（除 id, name, points 外）
    // 例外：port 不参与覆盖——已接入实例的端口保持原值（监听端口的必填约束，agent.md §3.3）
    for (const [key, value] of Object.entries(step.instance)) {
        if (key === "host") continue; // 归一化后 host 不入配置
        if (key !== "id" && key !== "name" && key !== "points" && key !== "port") {
            (target as Record<string, unknown>)[key] = value;
        }
    }
    // name 也可更新
    if (typeof step.instance["name"] === "string") {
        target.name = step.instance["name"] as string;
    }

    // points: 按匹配键（writer 用 id，reader 用 key）匹配——同名更新，新 point 追加
    if (Array.isArray(step.points) && step.points.length > 0) {
        if (!Array.isArray(target.points)) {
            target.points = [];
        }
        for (const step_pt of step.points) {
            const rec = step_pt as unknown as Record<string, unknown>;
            const rkey_m = typeof rec["key"] === "string" ? (rec["key"] as string) : "";
            const dot_m = rkey_m.indexOf(".");
            if (dot_m > 0 && renames) {
                const prefix_m = rkey_m.slice(0, dot_m);
                const pid_m = rkey_m.slice(dot_m + 1);
                const new_id_m = renames.get(prefix_m)?.get(pid_m);
                if (new_id_m) {
                    rec["key"] = `${prefix_m}.${new_id_m}`;
                }
            }
            const id = rec["id"];
            if (typeof id === "string" && id.length > 0) {
                validate_identifier(id, "point.id");
            }
            const match_key = point_match_key(step_pt);
            const existing_idx = target.points.findIndex(
                (p) => point_match_key(p) === match_key,
            );
            if (existing_idx >= 0) {
                // 更新已有 point 的字段（保留 shm_id）
                const existing_rec = target.points[existing_idx] as unknown as Record<string, unknown>;
                const pt_rec = step_pt as unknown as Record<string, unknown>;
                target.points[existing_idx] = {
                    ...existing_rec,
                    ...pt_rec,
                    shm_id: existing_rec["shm_id"],
                } as unknown as ServicePoint;
            } else {
                // 地址冲突检查（func_test_case 用例 18）：新增点 addr 与已有点相同 → 可读拒绝
                const rec2 = step_pt as unknown as Record<string, unknown>;
                if (typeof rec2["addr"] === "number") {
                    const clash = target.points.find(
                        (p) =>
                            (p as unknown as Record<string, unknown>)["addr"] === rec2["addr"],
                    );
                    if (clash) {
                        const cid = (clash as unknown as Record<string, unknown>)["id"];
                        throw new Error(
                            `地址 ${rec2["addr"]} 已被点 "${cid}" 占用` +
                            `（${step.service_type}.${instance_id}），请更换地址或先删除原点`,
                        );
                    }
                }
                // 新 point 追加，shm_id = 0
                target.points.push({ ...step_pt, shm_id: step_pt.shm_id ?? 0 });
                warnings.push(
                    `modify: ${step.service_type}.${instance_id} 新增 point "${match_key}"`,
                );
            }
        }
    }
}

// ── handle_delete ─────────────────────────────────────────

function handle_delete(
    step: ServiceStep,
    instances: MCPInstanceConfig[],
    config: SystemConfig,
    registry: RegistryLookup | undefined,
    warnings: string[],
): void {
    const instance_id = step.instance["id"] as string | undefined;
    if (!instance_id || typeof instance_id !== "string") {
        throw new Error(`delete 操作缺少 instance.id: ${step.service_type}`);
    }

    const idx = instances.findIndex((inst) => inst.id === instance_id);
    if (idx < 0) {
        throw new Error(
            `delete 目标不存在: ${step.service_type} 中找不到 id="${instance_id}"`,
        );
    }

    const key_prefix = `${instance_id}.`;

    // 点级删除（func_test_case 用例 17/19）：delete 步骤带 points 时仅删除匹配点，实例保留。
    // 匹配规则：point_match_key（id/key）或 addr 任一命中；未命中 → 可读错误（附当前点表）。
    const step_points = Array.isArray(step.points) ? step.points : [];
    if (step_points.length > 0) {
        const target = instances.find((inst) => inst.id === instance_id);
        if (!target) {
            throw new Error(
                `delete 目标不存在: ${step.service_type} 中找不到 id="${instance_id}"`,
            );
        }
        const removed_ids: string[] = [];
        for (const pt of step_points) {
            const rec = pt as unknown as Record<string, unknown>;
            const mk = point_match_key(pt);
            const addr = typeof rec["addr"] === "number" ? rec["addr"] : undefined;
            const idx = target.points.findIndex((p) => {
                const prec = p as unknown as Record<string, unknown>;
                if (mk && point_match_key(p) === mk) return true;
                if (addr !== undefined && prec["addr"] === addr) return true;
                return false;
            });
            if (idx < 0) {
                const table = target.points
                    .map((p) => {
                        const r = p as unknown as Record<string, unknown>;
                        return `${r.addr}:${r.id ?? r.key ?? "?"}`;
                    })
                    .join(", ");
                const want = [
                    mk ? `点名/键 "${mk}"` : "",
                    addr !== undefined ? `地址 ${addr}` : "",
                ]
                    .filter(Boolean)
                    .join(" 或 ");
                throw new Error(
                    `删除失败: ${step.service_type}.${instance_id} 不存在 ${want} 的点。当前点表: ${table}`,
                );
            }
            const removed = target.points.splice(idx, 1)[0] as unknown as Record<string, unknown>;
            if (typeof removed["id"] === "string") {
                removed_ids.push(removed["id"]);
            }
            warnings.push(
                `delete: 从 ${step.service_type}.${instance_id} 移除点 ${removed["id"] ?? removed["addr"]}`,
            );
        }
        // 级联：移除 Reader 中 key === `${instance_id}.${removed_id}` 的转发点
        for (const [st, svc_instances] of Object.entries(config)) {
            if (st === "c4_shm_manager" || !Array.isArray(svc_instances)) {
                continue;
            }
            const entry = registry?.get_entry(st);
            if (entry?.role !== "reader") {
                continue;
            }
            for (const inst of svc_instances as MCPInstanceConfig[]) {
                if (!Array.isArray(inst.points)) {
                    continue;
                }
                const before_n = inst.points.length;
                inst.points = inst.points.filter((p) => {
                    const k = (p as unknown as Record<string, unknown>)["key"];
                    return !(
                        typeof k === "string" &&
                        removed_ids.some((id) => k === `${instance_id}.${id}`)
                    );
                });
                if (inst.points.length < before_n) {
                    warnings.push(
                        `delete: 从 ${st}.${inst.id} 级联移除 ${before_n - inst.points.length} 个转发点`,
                    );
                }
            }
        }
        return;
    }

    instances.splice(idx, 1);

    // 若删除后该 service_type 数组为空，从 shm_manager 分类中移除
    if (instances.length === 0) {
        update_shm_classification(
            step.service_type,
            "delete",
            config,
            registry,
            warnings,
        );
    }

    // 相关性检查：移除所有 Reader 中引用该实例 key 的 points；Reader 变空则删除实例
    for (const [st, svc_instances] of Object.entries(config)) {
        if (st === "c4_shm_manager" || !Array.isArray(svc_instances)) {
            continue;
        }
        const entry = registry?.get_entry(st);
        if (entry?.role !== "reader") {
            continue;
        }
        const reader_instances = svc_instances as MCPInstanceConfig[];
        for (const inst of reader_instances) {
            if (!Array.isArray(inst.points)) {
                continue;
            }
            const before = inst.points.length;
            inst.points = inst.points.filter(
                (p) => typeof p.key !== "string" || !p.key.startsWith(key_prefix),
            );
            if (inst.points.length < before) {
                warnings.push(
                    `delete: 从 ${st}.${inst.id} 移除引用 ${instance_id} 的 points`,
                );
            }
        }
        const non_empty = reader_instances.filter(
            (inst) => Array.isArray(inst.points) && inst.points.length > 0,
        );
        if (non_empty.length !== reader_instances.length) {
            (config as Record<string, unknown>)[st] = non_empty;
            if (non_empty.length === 0) {
                update_shm_classification(st, "delete", config, registry, warnings);
            }
        }
    }
}

// ── Writer/Reader 自动分类 ────────────────────────────────

function update_shm_classification(
    service_type: string,
    action: "add" | "delete",
    config: SystemConfig,
    registry: RegistryLookup | undefined,
    warnings: string[],
): void {
    let role: "writer" | "reader" | undefined;

    if (registry) {
        const entry = registry.get_entry(service_type);
        if (entry) {
            role = entry.role;
        }
    }

    // 若 Registry 不可用，尝试从已知角色推断
    if (!role) {
        if (
            service_type === "c4_modbus_client" ||
            service_type === "c4_iec104_client" ||
            service_type === "c4_asfp2_server"
        ) {
            role = "writer";
        } else if (
            service_type === "c4_asfp2_client" ||
            service_type === "c4_influxdb_client"
        ) {
            role = "reader";
        } else {
            warnings.push(
                `无法确定 ${service_type} 的 writer/reader 角色（Registry 不可用且非已知类型），跳过 shm_manager 分类更新`,
            );
            return;
        }
    }

    const target_array = role === "writer"
        ? config.c4_shm_manager.writer
        : config.c4_shm_manager.reader;

    if (action === "add") {
        if (!target_array.includes(service_type)) {
            target_array.push(service_type);
        }
    } else {
        // delete
        const idx = target_array.indexOf(service_type);
        if (idx >= 0) {
            target_array.splice(idx, 1);
        }
    }
}

// ── executeStopAndStart ────────────────────────────────────

/**
 * 执行 Stop-Start 安全协议（实例粒度，独立服务模型）。
 *
 * 流程（agent.md §3.2，c4_architecture.md §3.1.2）：
 *   1. Stop 阶段：stop 所有数据路径 MCP 服务实例（进程常驻，不退出）——
 *      stop_clients 必须覆盖全部数据路径服务（Registry 全集），而非仅新配置
 *      中出现的服务：被删除的服务实例同样要停（否则整机删除后端口不释放）
 *   2. adjust_shm 阶段：调用 c4_shm_manager.adjust_shm()（禁止只 restart 不调 adjust_shm）
 *   3. Start 阶段：start start_clients 中的服务实例（ALREADY_RUNNING 视为一等成功路径）
 *
 * 本函数不修改 config.json——变更失败的回滚（恢复 .prev.1 + 完整 Stop-Start）
 * 由事务层（transaction 调用方）负责：任一阶段失败 → 返回 failure 结果 +
 * abort_reason（含错误分类），调用方执行回滚协议。
 *
 * stop() 是幂等的——对已停止的服务调用仍返回 success。
 *
 * @param shm_manager c4_shm_manager MCP 客户端
 * @param stop_clients Stop 阶段覆盖的数据路径服务客户端（Registry 全集）
 * @param start_clients Start 阶段按当前配置拉起的服务客户端（config 子集）
 * @param config 当前全量配置
 * @param _config_path 配置文件路径（工具参数 config_path 透传；当前未使用）
 */
export async function execute_stop_and_start(
    shm_manager: ShmManagerClient,
    stop_clients: McpServiceClient[],
    start_clients: McpServiceClient[],
    _config: SystemConfig,
    _config_path: string,
): Promise<StopStartResult> {
    // ── Phase 1: Stop ──
    for (const client of stop_clients) {
        try {
            await client.stop();
        } catch (err: unknown) {
            const err_msg = err instanceof Error ? err.message : String(err);
            // 失败不在此处回滚——事务层负责「恢复 .prev.1 + 完整 Stop-Start」
            return {
                success: false,
                started_services: [],
                failed_services: [],
                abort_reason: `Stop 阶段失败: ${client.service_type} stop() 报错: ${err_msg}`,
            };
        }
    }

    // ── Phase 2: adjust_shm ──
    // 失败不在此处回滚——事务层负责「恢复 .prev.1 + 完整 Stop-Start（含 adjust_shm）」
    //（agent.md §3.2.2：config 类/非 config 类错误均回滚）；此处仅记录错误分类。
    let adjust_error: string | null = null;
    try {
        const adjust_result = await shm_manager.adjust_shm();
        if (adjust_result !== "success") {
            const cls = is_config_class_error(adjust_result)
                ? "配置类错误"
                : is_non_config_class_error(adjust_result)
                  ? "系统类错误"
                  : "未知错误";
            adjust_error = `adjust_shm 失败（${cls}）: ${adjust_result}`;
        }
    } catch (err: unknown) {
        const err_msg = err instanceof Error ? err.message : String(err);
        adjust_error = `adjust_shm 调用异常: ${err_msg}`;
    }
    if (adjust_error !== null) {
        return {
            success: false,
            started_services: [],
            failed_services: [],
            abort_reason: adjust_error,
        };
    }

    // ── Phase 3: Start ──
    const started: string[] = [];
    const failed: StopStartResult["failed_services"] = [];

    for (const client of start_clients) {
        try {
            const result = await client.start();
            if (result === "success") {
                started.push(client.service_type);
            } else {
                failed.push({
                    service_type: client.service_type,
                    error: result,
                });
            }
        } catch (err: unknown) {
            failed.push({
                service_type: client.service_type,
                error: err instanceof Error ? err.message : String(err),
            });
        }
    }

    const success = failed.length === 0;
    return {
        success,
        started_services: started,
        failed_services: failed,
        abort_reason: success
            ? undefined
            : `Start 阶段: ${failed.length} 个服务启动失败，${started.length} 个成功`,
    };
}

export interface MCPClientHandle {
    callTool(params: { name: string; arguments: Record<string, unknown> }): Promise<unknown>;
}

// ── Manager-based 工具调用（独立服务模型：Unix socket 连接由 C4McpManager 持有）──

/**
 * 经 C4McpManager 调用指定服务的工具，返回应答文本。
 * MCP isError 应答返回其错误文本（如 "DUPLICATE_KEY: ..."）——与错误码分类兼容；
 * 服务未连接（降级）→ 抛出异常。
 */
export async function callToolViaManager(
    manager: C4McpManager,
    serviceType: string,
    toolName: string,
    args: Record<string, unknown>,
): Promise<string> {
    return manager.callToolText(serviceType, toolName, args);
}

/** ALREADY_RUNNING 是一等成功路径结果（isError=false）：无动作、不中断数据路径 */
export function is_success_result(result: string): boolean {
    return result === "success" || result.startsWith("ALREADY_RUNNING");
}

export class McpServiceClientAdapter implements McpServiceClient {
    readonly service_type: string;
    private _instanceId: string;
    private _configPath: string;
    private _manager: C4McpManager;

    constructor(
        manager: C4McpManager,
        serviceType: string,
        instanceId: string,
        configPath: string,
    ) {
        this._manager = manager;
        this.service_type = serviceType;
        this._instanceId = instanceId;
        this._configPath = configPath;
    }

    async stop(): Promise<string> {
        try {
            const r = await callToolViaManager(this._manager, this.service_type, "stop", {});
            return is_success_result(r) ? "success" : r;
        } catch (err: unknown) {
            void err;
            // stop 幂等：服务暂不可达（如正在重启）视为已停止，不阻塞协议推进
            return "success";
        }
    }

    async start(): Promise<string> {
        const r = await callToolViaManager(this._manager, this.service_type, "start", {
            instance_id: this._instanceId,
            config_path: this._configPath,
        });
        return is_success_result(r) ? "success" : r;
    }
}

export class ShmManagerClientAdapter implements ShmManagerClient {
    readonly service_type: string;
    private _instanceId: string;
    private _configPath: string;
    private _manager: C4McpManager;

    constructor(
        manager: C4McpManager,
        instanceId: string,
        configPath: string,
    ) {
        this._manager = manager;
        this.service_type = SHM_SERVICE_TYPE;
        this._instanceId = instanceId;
        this._configPath = configPath;
    }

    async stop(): Promise<string> {
        return "success"; // shm_manager has no stop tool — never called
    }

    async start(): Promise<string> {
        return "success"; // shm_manager has no start tool — never called
    }

    async create_shm(): Promise<string> {
        return callToolViaManager(this._manager, this.service_type, "create_shm", {
            instance_id: this._instanceId,
            config_path: this._configPath,
        });
    }

    async adjust_shm(): Promise<string> {
        return callToolViaManager(this._manager, this.service_type, "adjust_shm", {
            instance_id: this._instanceId,
            config_path: this._configPath,
        });
    }
}

/** 从 config.json 提取非空数据路径服务段（c4_shm_manager 除外） */
export function data_service_types(systemConfig: SystemConfig): string[] {
    const out: string[] = [];
    for (const key of Object.keys(systemConfig)) {
        if (key === "c4_shm_manager") continue;
        const instances = systemConfig[key];
        if (Array.isArray(instances) && instances.length > 0) {
            out.push(key);
        }
    }
    return out;
}

function new_shm_client(
    manager: C4McpManager,
    instanceId: string,
    configPath: string,
): ShmManagerClientAdapter {
    return new ShmManagerClientAdapter(manager, instanceId, configPath);
}

/**
 * 运行期 Stop-Start（变更事务步骤 4）。
 * 连接来自 manager（Unix socket 常驻连接），不拉起任何进程。
 */
export async function run_runtime_stop_start(
    manager: C4McpManager,
    instanceId: string,
    configPath: string,
    registry: RegistryLookup,
): Promise<StopStartResult> {
    let systemConfig: SystemConfig;
    try {
        const raw = await fs.readFile(configPath, "utf-8");
        systemConfig = JSON.parse(raw) as SystemConfig;
    } catch {
        return {
            success: false,
            started_services: [],
            failed_services: [],
            abort_reason: `无法读取 config.json: ${configPath}`,
        };
    }

    const shmClient = new_shm_client(manager, instanceId, configPath);
    const stopClients = all_data_clients(manager, instanceId, configPath, registry);
    const startClients = build_data_clients(manager, systemConfig, instanceId, configPath, registry);

    return execute_stop_and_start(shmClient, stopClients, startClients, systemConfig, configPath);
}

function build_data_clients(
    manager: C4McpManager,
    systemConfig: SystemConfig,
    instanceId: string,
    configPath: string,
    registry: RegistryLookup,
): McpServiceClientAdapter[] {
    const clients: McpServiceClientAdapter[] = [];
    for (const svcType of data_service_types(systemConfig)) {
        if (!registry.get_entry(svcType)) continue;
        clients.push(new McpServiceClientAdapter(manager, svcType, instanceId, configPath));
    }
    return clients;
}

/**
 * Stop 阶段客户端全集：Registry 中全部数据路径服务（agent.md §3.2「for 每个
 * 数据路径 MCP 服务: call stop()」）。stop 幂等，未接入的服务返回 success；
 * 覆盖全集才能停掉被删除的服务实例（整机删除后端口必须释放）。
 */
function all_data_clients(
    manager: C4McpManager,
    instanceId: string,
    configPath: string,
    registry: RegistryLookup,
): McpServiceClientAdapter[] {
    const clients: McpServiceClientAdapter[] = [];
    for (const svcType of registry.service_types()) {
        if (svcType === SHM_SERVICE_TYPE) continue;
        clients.push(new McpServiceClientAdapter(manager, svcType, instanceId, configPath));
    }
    return clients;
}

/**
 * 变更失败回滚（agent.md §3.2.2）：恢复 config.json.prev.1 → 以恢复后的配置
 * 执行完整 Stop-Start（stop → adjust_shm → start，含 adjust_shm——禁止只
 * restart 不调 adjust_shm）→ 删除事务标记由调用方负责。
 *
 * @returns {restored} .prev.1 是否成功恢复（false = .prev 不可用，config.json 保留现状）
 */
export async function rollback_config_change(
    manager: C4McpManager,
    instanceId: string,
    configPath: string,
    registry: RegistryLookup,
): Promise<{ restored: boolean; result: StopStartResult }> {
    const restored = await restore_prev1(configPath);

    // 以恢复后的（或保留现状的）config.json 为准重建客户端清单
    let systemConfig: SystemConfig;
    try {
        const raw = await fs.readFile(configPath, "utf-8");
        systemConfig = JSON.parse(raw) as SystemConfig;
    } catch {
        systemConfig = { c4_shm_manager: { writer: [], reader: [] } };
    }

    const shmClient = new_shm_client(manager, instanceId, configPath);
    const stopClients = all_data_clients(manager, instanceId, configPath, registry);
    const startClients = build_data_clients(manager, systemConfig, instanceId, configPath, registry);
    const result = await execute_stop_and_start(shmClient, stopClients, startClients, systemConfig, configPath);
    return { restored, result };
}

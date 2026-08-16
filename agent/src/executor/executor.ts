// c4/agent/src/executor/executor.ts — Agent 执行模块
// 确定性代码：合并 AccessPlanSteps → config.json + Stop-Start 安全协议
// 设计：agent.md §3.2, §3.2.1.6, §3.2.3

import * as fs from "node:fs/promises";
import * as path from "node:path";
import { MultiServerMCPClient } from "@langchain/mcp-adapters";
import type {
    MCPInstanceConfig,
    RegistryEntry,
    ServiceStep,
    SystemConfig,
} from "../types/index.js";
import { McpServiceRegistry } from "../registry/registry.js";

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

/** adjust_shm 的 config 类错误码——需回退 config.json.bak */
const CONFIG_CLASS_ERRORS = new Set([
    "DUPLICATE_KEY",
    "CONFIG_MISSING_SECTION",
    "UNKNOWN_READER_KEY",
]);

/** adjust_shm 的非 config 类错误码——不回退 config，只重启已停止的服务 */
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
 * 流程（agent.md §3.2）：
 * 1. 读取现有 config.json（不存在 → 创建空结构）
 * 2. 损坏 JSON → 恢复 config.json.bak（若有效）否则创建空结构
 * 3. 备份当前内容到 config.json.bak
 * 4. 逐一处理 add / modify / delete（§3.2.1.6 规则）
 * 5. 原子写入：config.json.tmp → rename → config.json
 *
 * @param steps    本次接入的增量操作步骤
 * @param config_path 配置文件路径（如 /etc/c4/config.json）
 * @param registry Registry 查询接口（用于填充 source=default 字段和角色查询）
 */
export async function merge_config_from_steps(
    steps: ServiceStep[],
    config_path: string,
    registry?: RegistryLookup,
): Promise<MergeResult> {
    const warnings: string[] = [];
    const config_dir = path.dirname(config_path);

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
            // JSON 解析失败 → 尝试恢复 .bak
            const recovered = await try_restore_bak(config_path);
            if (recovered) {
                config = recovered.config;
                warnings.push("config.json 已损坏，已从 config.json.bak 恢复");
                current_raw = recovered.raw;
            } else {
                config = empty_config();
                warnings.push("config.json 已损坏且 config.json.bak 不可用，创建新配置文件");
                current_raw = "";
            }
        }
    }

    // ── Step 3: 备份 ──
    await fs.mkdir(config_dir, { recursive: true });
    const backup_raw = JSON.stringify(config, null, 4) + "\n";
    await fs.writeFile(config_path + ".bak", backup_raw, "utf-8");

    // ── Step 4: 处理 steps ──
    for (const step of steps) {
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

        switch (step.action) {
            case "add":
                await handle_add(step, instances, config, registry, warnings);
                break;
            case "modify":
                handle_modify(step, instances, warnings);
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

    // ── Step 5: 原子写入 ──
    const output = JSON.stringify(config, null, 4) + "\n";
    const tmp_path = config_path + ".tmp";
    await fs.writeFile(tmp_path, output, "utf-8");
    await fs.rename(tmp_path, config_path);

    return { success: true, config, warnings };
}

// ── handle_add ────────────────────────────────────────────

async function handle_add(
    step: ServiceStep,
    instances: MCPInstanceConfig[],
    config: SystemConfig,
    registry: RegistryLookup | undefined,
    warnings: string[],
): Promise<void> {
    const instance_id = step.instance["id"] as string | undefined;
    if (!instance_id || typeof instance_id !== "string") {
        throw new Error(`add 操作缺少 instance.id: ${step.service_type}`);
    }
    if (!/^[a-zA-Z][a-zA-Z0-9_.]*$/.test(instance_id)) {
        throw new Error(
            `instance.id "${instance_id}" 包含非法字符，仅允许字母开头`
        );
    }

    // 检查 instance.id 是否与现有冲突：若已存在同名实例（如追加设备时转发目标已存在），
    // 合并 points（追加新 point、更新已有 point），而非报错。
    for (const existing of instances) {
        if (existing.id === instance_id) {
            for (const pt of step.points) {
                const existing_idx = existing.points.findIndex(
                    (p) => p.id === pt.id || (pt.key !== undefined && p.key === pt.key),
                );
                if (existing_idx >= 0) {
                    existing.points[existing_idx] = {
                        ...existing.points[existing_idx],
                        ...pt,
                        shm_id: existing.points[existing_idx].shm_id,
                    };
                } else {
                    existing.points.push({ ...pt, shm_id: pt.shm_id ?? 0 });
                }
            }
            warnings.push(
                `add: ${step.service_type}.${instance_id} 已存在，合并 points`,
            );
            return;
        }
    }

    // 检查 points 的 id 不重复（在本次 step 内）
    const point_ids = new Set<string>();
    for (const pt of step.points) {
        if (!/^[a-zA-Z][a-zA-Z0-9_.]*$/.test(pt.id)) {
            throw new Error(
                `point.id "${pt.id}" 包含非法字符，仅允许字母开头`,
            );
        }
        if (point_ids.has(pt.id)) {
            throw new Error(
                `point.id "${pt.id}" 在 ${step.service_type}.${instance_id} 中重复`,
            );
        }
        point_ids.add(pt.id);
    }

    // 构建实例配置：合并 instance 字段 + points
    const new_instance: MCPInstanceConfig = {
        id: instance_id,
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

    // 填充 Registry default 字段（source=default）
    if (registry) {
        const entry = registry.get_entry(step.service_type);
        if (entry && entry.config_schema) {
            for (const [field_name, field_def] of Object.entries(
                entry.config_schema.fields,
            )) {
                if (field_def.source === "default") {
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
): void {
    const instance_id = step.instance["id"] as string | undefined;
    if (!instance_id || typeof instance_id !== "string") {
        throw new Error(`modify 操作缺少 instance.id: ${step.service_type}`);
    }

    const target = instances.find((inst) => inst.id === instance_id);
    if (!target) {
        throw new Error(
            `modify 目标不存在: ${step.service_type} 中找不到 id="${instance_id}"`,
        );
    }

    // 浅合并 instance 字段（除 id, name, points 外）
    for (const [key, value] of Object.entries(step.instance)) {
        if (key !== "id" && key !== "name" && key !== "points") {
            (target as Record<string, unknown>)[key] = value;
        }
    }
    // name 也可更新
    if (typeof step.instance["name"] === "string") {
        target.name = step.instance["name"] as string;
    }

    // points: 按 point.id 匹配——同名更新，新 point 追加
    if (step.points.length > 0) {
        if (!Array.isArray(target.points)) {
            target.points = [];
        }
        for (const step_pt of step.points) {
            const existing_idx = target.points.findIndex(
                (p) => p.id === step_pt.id,
            );
            if (existing_idx >= 0) {
                // 更新已有 point 的字段（保留 shm_id）
                target.points[existing_idx] = {
                    ...target.points[existing_idx],
                    ...step_pt,
                    shm_id: target.points[existing_idx].shm_id,
                };
            } else {
                // 新 point 追加，shm_id = 0
                target.points.push({ ...step_pt, shm_id: step_pt.shm_id ?? 0 });
                warnings.push(
                    `modify: ${step.service_type}.${instance_id} 新增 point "${step_pt.id}"`,
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

// ── try_restore_bak ───────────────────────────────────────

async function try_restore_bak(
    config_path: string,
): Promise<{ config: SystemConfig; raw: string } | null> {
    const bak_path = config_path + ".bak";
    try {
        const raw = await fs.readFile(bak_path, "utf-8");
        const config = JSON.parse(raw) as SystemConfig;
        if (!config.c4_shm_manager) {
            return null;
        }
        // 恢复：用 .bak 覆盖损坏的 config.json
        await fs.writeFile(config_path, raw, "utf-8");
        return { config, raw };
    } catch {
        return null;
    }
}

// ── executeStopAndStart ────────────────────────────────────

/**
 * 执行 Stop-Start 安全协议。
 *
 * 流程（agent.md §3.2）：
 *   1. Stop 阶段：stop 所有数据路径 MCP 服务（不含 c4_shm_manager）
 *   2. adjust_shm 阶段：调用 c4_shm_manager.adjust_shm()
 *   3. Start 阶段：start 所有 MCP 服务
 *
 * 错误处理（agent.md §3.2.2）：
 *   - stop 失败 → 回滚（start 已停止的服务），abort
 *   - adjust_shm 失败（config 类）→ 恢复 config.json.bak，start 已停止的服务，abort
 *   - adjust_shm 失败（非 config 类）→ start 已停止的服务（不恢复 config），abort
 *   - start 部分失败 → 不回滚已成功的，只报告失败
 *
 * stop() 是幂等的——对已停止的服务调用仍返回 success。
 *
 * @param shm_manager c4_shm_manager MCP 客户端
 * @param data_clients 数据路径 MCP 服务客户端列表（不含 c4_shm_manager）
 * @param config 当前全量配置
 * @param config_path 配置文件路径（用于恢复 config.json.bak）
 */
export async function execute_stop_and_start(
    shm_manager: ShmManagerClient,
    data_clients: McpServiceClient[],
    _config: SystemConfig,
    config_path: string,
): Promise<StopStartResult> {
    const stopped_clients: McpServiceClient[] = [];

    // ── Phase 1: Stop ──
    for (const client of data_clients) {
        try {
            await client.stop();
            stopped_clients.push(client);
        } catch (err: unknown) {
            const err_msg = err instanceof Error ? err.message : String(err);
            // 回滚：start 已停止的服务
            await rollback_start_services(stopped_clients);
            return {
                success: false,
                started_services: [],
                failed_services: [],
                abort_reason:
                    `Stop 阶段失败: ${client.service_type} stop() 报错: ${err_msg}。已回滚：重启了 ${stopped_clients.length} 个已停止的服务`,
            };
        }
    }

    // ── Phase 2: adjust_shm ──
    try {
        const adjust_result = await shm_manager.adjust_shm();
        if (adjust_result !== "success") {
            if (is_config_class_error(adjust_result)) {
                // Config 类错误：恢复 config.json.bak，重启已停止的服务
                await restore_config_bak(config_path);
                await rollback_start_services(stopped_clients);
                return {
                    success: false,
                    started_services: [],
                    failed_services: [],
                    abort_reason: `adjust_shm 失败（配置类错误），已恢复 config.json.bak 并重启 ${stopped_clients.length} 个服务: ${adjust_result}`,
                };
            }
            if (is_non_config_class_error(adjust_result)) {
                // 非 config 类错误：不恢复 config，仅重启已停止的服务
                await rollback_start_services(stopped_clients);
                return {
                    success: false,
                    started_services: [],
                    failed_services: [],
                    abort_reason: `adjust_shm 失败（系统类错误），已重启 ${stopped_clients.length} 个服务，config 未回退: ${adjust_result}`,
                };
            }
            // 未知错误码：保守处理，重启已停止的服务，不恢复 config
            await rollback_start_services(stopped_clients);
            return {
                success: false,
                started_services: [],
                failed_services: [],
                abort_reason: `adjust_shm 失败（未知错误），已重启 ${stopped_clients.length} 个服务，config 未回退: ${adjust_result}`,
            };
        }
    } catch (err: unknown) {
        const err_msg = err instanceof Error ? err.message : String(err);
        // 调用本身失败（网络/进程异常）→ 保守处理
        await rollback_start_services(stopped_clients);
        return {
            success: false,
            started_services: [],
            failed_services: [],
            abort_reason: `adjust_shm 调用异常，已重启 ${stopped_clients.length} 个服务: ${err_msg}`,
        };
    }

    // ── Phase 3: Start ──
    const started: string[] = [];
    const failed: StopStartResult["failed_services"] = [];

    for (const client of data_clients) {
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

// ── Rollback Helpers ──────────────────────────────────────

/** 回滚：重新 start 所有已停止的服务 */
async function rollback_start_services(
    clients: McpServiceClient[],
): Promise<void> {
    for (const client of clients) {
        try {
            await client.start();
        } catch {
            // 回滚中的再次失败无法恢复，记录并继续
        }
    }
}

/** 用 config.json.bak 恢复 config.json */
async function restore_config_bak(config_path: string): Promise<void> {
    const bak_path = config_path + ".bak";
    try {
        const bak_content = await fs.readFile(bak_path, "utf-8");
        // 验证 bak 是有效 JSON
        JSON.parse(bak_content);
        const tmp_path = config_path + ".tmp";
        await fs.writeFile(tmp_path, bak_content, "utf-8");
        await fs.rename(tmp_path, config_path);
    } catch {
        // bak 也不可用——无法恢复，但不抛异常（调用方已处于错误路径）
    }
}

export interface MCPClientHandle {
    callTool(params: { name: string; arguments: Record<string, unknown> }): Promise<unknown>;
}

async function callToolViaMultiClient(
    multiClient: MultiServerMCPClient,
    serverName: string,
    toolName: string,
    args: Record<string, unknown>,
): Promise<string> {
    const tools = await multiClient.getTools(serverName);
    const tool = tools.find(t => t.name === toolName);
    if (!tool) throw new Error(`tool not found: ${toolName}`);
    const result = await tool.invoke(args);
    return typeof result === "string" ? result : JSON.stringify(result);
}

export class McpServiceClientAdapter implements McpServiceClient {
    readonly service_type: string;
    private _instanceId: string;
    private _configPath: string;

    constructor(
        private _mcp: MCPClientHandle,
        serviceType: string,
        instanceId: string,
        configPath: string,
        private _multiClient: MultiServerMCPClient,
    ) {
        this.service_type = serviceType;
        this._instanceId = instanceId;
        this._configPath = configPath;
    }

    async stop(): Promise<string> {
        return callToolViaMultiClient(this._multiClient, this.service_type, "stop", {});
    }

    async start(): Promise<string> {
        return callToolViaMultiClient(this._multiClient, this.service_type, "start", {
            instance_id: this._instanceId,
            config_path: this._configPath,
        });
    }

    async dispose(): Promise<void> {
        await this._multiClient.close();
    }
}

export class ShmManagerClientAdapter implements ShmManagerClient {
    readonly service_type: string;
    private _instanceId: string;
    private _configPath: string;

    constructor(
        private _multiClient: MultiServerMCPClient,
        serverName: string,
        instanceId: string,
        configPath: string,
    ) {
        this.service_type = serverName;
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
        return callToolViaMultiClient(this._multiClient, this.service_type, "create_shm", {
            instance_id: this._instanceId,
            config_path: this._configPath,
        });
    }

    async adjust_shm(): Promise<string> {
        return callToolViaMultiClient(this._multiClient, this.service_type, "adjust_shm", {
            instance_id: this._instanceId,
            config_path: this._configPath,
        });
    }
}

export async function runRuntimeStopStart(
    shmMultiClient: MultiServerMCPClient,
    shmServerName: string,
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

    const dataServiceTypes: string[] = [];
    for (const key of Object.keys(systemConfig)) {
        if (key === "c4_shm_manager") continue;
        const instances = systemConfig[key];
        if (Array.isArray(instances) && instances.length > 0) {
            dataServiceTypes.push(key);
        }
    }

    const shmClient = new ShmManagerClientAdapter(
        shmMultiClient,
        shmServerName,
        instanceId,
        configPath,
    );

    const dataClients: McpServiceClientAdapter[] = [];
    for (const svcType of dataServiceTypes) {
        const entry = registry.get_entry(svcType);
        if (!entry) continue;

        try {
            const multiClient = new MultiServerMCPClient({
                mcpServers: {
                    [svcType]: {
                        transport: "stdio" as const,
                        command: entry.binary_path,
                        args: [],
                    },
                },
            });
            const mcpClient = await multiClient.getClient(svcType);
            if (!mcpClient) {
                await multiClient.close();
                continue;
            }
            dataClients.push(
                new McpServiceClientAdapter(
                    mcpClient as MCPClientHandle,
                    svcType,
                    instanceId,
                    configPath,
                    multiClient,
                ),
            );
        } catch {
            continue;
        }
    }

    const result = await execute_stop_and_start(
        shmClient,
        dataClients,
        systemConfig,
        configPath,
    );

    return result;
}

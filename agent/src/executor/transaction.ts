// c4/agent/src/executor/transaction.ts — config.json 变更事务协议
// 设计：c4_architecture.md §3.1.2 变更事务协议（固定序列）：
//   1. 写事务标记 pending_change.json（变更描述、涉及服务、回滚源路径），写后 fsync
//   2. 复制 config.json → config.json.prev.1（回滚源，滚动保留 .prev.1~.3），拷贝后 fsync
//   3. 写新 config.json：临时文件 → fsync → 原子 rename → rename 后对父目录 fsync
//   4. 执行 Stop-Start / merge 序列（调用方）
//   5. 成功 → 删除事务标记；失败 → 恢复 .prev.1 + 完整 Stop-Start（含 adjust_shm）+ 报告
// 崩溃恢复语义（§3.1.2 / agent.md §3.2.3 L0）：
//   pending_change.json 存在 → 变更作废不续做：校验 .prev.1（parse+schema）通过 →
//   恢复为 config.json + 报告「接入不成功」；.prev 不可用 → 不得覆盖 config.json、
//   删除标记（防重入）、报告异常等待人工介入。

import * as fs from "node:fs/promises";
import * as path from "node:path";
import type { SystemConfig } from "../types/index.js";

// ── 路径约定 ──────────────────────────────────────────────

export const MARKER_FILENAME = "pending_change.json";

/** 事务标记固定写在 config.json 同目录（pending_change.json） */
export function pending_marker_path(config_path: string): string {
    return path.join(path.dirname(config_path), MARKER_FILENAME);
}

export function prev_path(config_path: string, generation: 1 | 2 | 3): string {
    return `${config_path}.prev.${generation}`;
}

// ── 事务标记内容 ──────────────────────────────────────────

export interface PendingChangeMarker {
    description: string;
    /** 涉及的数据路径服务类型（崩溃恢复时这些服务执行完整 Stop-Start） */
    services: string[];
    rollback_source: string;
    created_at: string;
}

// ── 原子写原语（tmp → fsync → rename → 父目录 fsync）──────

export async function atomic_write_raw(file_path: string, raw: string): Promise<void> {
    const dir = path.dirname(file_path);
    await fs.mkdir(dir, { recursive: true });
    const tmp_path = file_path + ".tmp";
    const handle = await fs.open(tmp_path, "w");
    try {
        await handle.writeFile(raw, "utf-8");
        await handle.sync();
    } finally {
        await handle.close();
    }
    await fs.rename(tmp_path, file_path);
    const dir_handle = await fs.open(dir, "r");
    try {
        await dir_handle.sync();
    } catch {
        // 部分文件系统不支持目录 fsync——rename 原子性不受影响
    } finally {
        await dir_handle.close();
    }
}

// ── schema 校验（L0 / .prev 校验共用）─────────────────────

/**
 * config.json 最小 schema：对象；c4_shm_manager 段为对象（writer/reader 为数组，
 * 可缺省）；其余顶层键的值为实例数组。数组内容不做深校验（各 MCP 工具负责）。
 */
export function validate_config_schema(parsed: unknown): parsed is SystemConfig {
    if (parsed === null || typeof parsed !== "object" || Array.isArray(parsed)) {
        return false;
    }
    const obj = parsed as Record<string, unknown>;
    for (const [key, value] of Object.entries(obj)) {
        if (key === "c4_shm_manager") {
            if (value === null || typeof value !== "object" || Array.isArray(value)) {
                return false;
            }
            const shm = value as Record<string, unknown>;
            for (const role of ["writer", "reader"]) {
                const arr = shm[role];
                if (arr !== undefined && !Array.isArray(arr)) {
                    return false;
                }
            }
            continue;
        }
        if (!Array.isArray(value)) {
            return false;
        }
    }
    return true;
}

/** 读取 + parse + schema 校验；任一步失败返回 null */
export async function load_valid_config(config_path: string): Promise<SystemConfig | null> {
    try {
        const raw = await fs.readFile(config_path, "utf-8");
        const parsed: unknown = JSON.parse(raw);
        return validate_config_schema(parsed) ? parsed : null;
    } catch {
        return null;
    }
}

// ── 事务序列 ──────────────────────────────────────────────

/**
 * 事务步骤 1+2：写事务标记（fsync）→ 滚动复制 .prev（fsync）。
 * 必须在任何 config.json 变更发生之前调用。
 *
 * .prev 滚动：.prev.2 → .prev.3，.prev.1 → .prev.2，当前 config.json → .prev.1。
 * config.json 不存在（首次接入）→ 无 .prev 可回滚（架构 §3.1.2「首次接入尚无 .prev」分支）。
 */
export async function begin_config_transaction(
    config_path: string,
    description: string,
    services: string[],
): Promise<void> {
    // 1. 事务标记
    const marker: PendingChangeMarker = {
        description,
        services,
        rollback_source: prev_path(config_path, 1),
        created_at: new Date().toISOString(),
    };
    await atomic_write_raw(
        pending_marker_path(config_path),
        JSON.stringify(marker, null, 4) + "\n",
    );

    // 2. .prev 滚动复制（源不存在则跳过——首次接入无可回滚对象）
    try {
        await fs.unlink(prev_path(config_path, 3));
    } catch {
        // 不存在则忽略
    }
    for (const [from, to] of [
        [prev_path(config_path, 2), prev_path(config_path, 3)],
        [prev_path(config_path, 1), prev_path(config_path, 2)],
    ] as const) {
        try {
            await fs.rename(from, to);
        } catch {
            // 源不存在则跳过
        }
    }
    try {
        const raw = await fs.readFile(config_path, "utf-8");
        await atomic_write_raw(prev_path(config_path, 1), raw);
    } catch {
        // config.json 不存在 → 首次接入，无回滚源
    }
}

/** 删除事务标记（成功提交 / 作废时调用） */
export async function clear_pending_marker(config_path: string): Promise<void> {
    try {
        await fs.unlink(pending_marker_path(config_path));
    } catch {
        // 已不存在
    }
}

/**
 * 恢复 .prev.1 为 config.json（崩溃恢复 / 变更失败回滚共用）。
 * 恢复前先校验 .prev.1（parse + schema）——不可用时不得覆盖 config.json，返回 false。
 */
export async function restore_prev1(config_path: string): Promise<boolean> {
    const prev = await load_valid_config(prev_path(config_path, 1));
    if (prev === null) {
        return false;
    }
    const raw = await fs.readFile(prev_path(config_path, 1), "utf-8");
    await atomic_write_raw(config_path, raw);
    return true;
}

/**
 * 读取事务标记（崩溃恢复判定用）。损坏的标记按存在处理（返回 description 为空的标记）。
 */
export async function read_pending_marker(
    config_path: string,
): Promise<PendingChangeMarker | null> {
    try {
        const raw = await fs.readFile(pending_marker_path(config_path), "utf-8");
        const parsed = JSON.parse(raw) as Partial<PendingChangeMarker>;
        return {
            description: typeof parsed.description === "string" ? parsed.description : "",
            services: Array.isArray(parsed.services)
                ? parsed.services.filter((s): s is string => typeof s === "string")
                : [],
            rollback_source:
                typeof parsed.rollback_source === "string" ? parsed.rollback_source : "",
            created_at: typeof parsed.created_at === "string" ? parsed.created_at : "",
        };
    } catch {
        // ENOENT → 无标记；标记本身损坏 → 仍视为存在（存在即作废变更）
        try {
            await fs.access(pending_marker_path(config_path));
            return {
                description: "",
                services: [],
                rollback_source: "",
                created_at: "",
            };
        } catch {
            return null;
        }
    }
}

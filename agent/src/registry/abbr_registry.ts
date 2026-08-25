// c4/agent/src/registry/abbr_registry.ts — abbr 记忆库（id 稳定性保障）
// 根据 agent.md §3.2.1.3a 实现。
// 确定性文件读写模块：无 LLM、无网络。记忆库是可重建的派生数据，config.json 是权威数据源。

import { mkdir, readFile, rename, writeFile } from "node:fs/promises";
import { homedir } from "node:os";
import { dirname, join } from "node:path";

import type { MCPInstanceConfig, SystemConfig } from "../types/index.js";

// ── 类型定义 ──────────────────────────────────────────────

export interface AbbrSite {
    name: string;
    abbr: string;
}

export type AbbrRole = "writer" | "reader" | null;

export interface AbbrEntry {
    id: string;           // 稳定实例 id（主键），固化后永不改变
    name: string;         // 设备名称（人可读）
    abbr: string;         // 采集/转发目标标识（候选，最终以本记录为准）
    service_type: string; // 所属服务类型（重建时从 config.json 顶层 key 反推）
    role: AbbrRole;       // 所属角色（重建时无法从 config.json 得知，为 null，由调用方补充）
    description: string;  // 首次接入时的原始描述（用于后续检索匹配）
}

export interface AbbrRegistry {
    entries: AbbrEntry[];    // 在用设备记录（delete 物理删除，不保留历史）
}

export interface RetrieveCandidateParams {
    description: string;
    role?: "writer" | "reader";
    intent: "add" | "modify" | "delete";
}

export interface RetrieveCandidateResult {
    hit: boolean;
    id?: string;
    abbr?: string;
}

export interface AbbrConflictResult {
    conflict: boolean;      // true = 需重新生成不同 abbr（不同设备撞车）
    existing_id?: string;   // 撞 abbr 的已存实例 id
}

export interface AbbrEntryInput {
    id: string;
    name: string;
    abbr: string;
    service_type: string;
    role: AbbrRole;
    description: string;
}

// ── 路径 ──────────────────────────────────────────────────

export function default_abbr_registry_path(): string {
    return join(homedir(), ".local", "c4", "abbr_registry.json");
}

// ── 读写 ──────────────────────────────────────────────────

export async function load_abbr_registry(
    file_path?: string,
    config_json?: SystemConfig,
    site: AbbrSite | null = null,
): Promise<AbbrRegistry> {
    const target = file_path ?? default_abbr_registry_path();
    const site_abbr = site?.abbr ?? null;

    let parsed: AbbrRegistry | null = null;
    try {
        const raw = await readFile(target, "utf-8");
        parsed = _parse_registry(raw);
    } catch (err: unknown) {
        const error = err as NodeJS.ErrnoException;
        if (error.code !== "ENOENT") {
            // 其他读取失败（权限等）按「损坏」处理
            parsed = null;
        }
        // ENOENT → parsed 保持 null，统一走下方重建分支
    }

    // 文件不存在 / 损坏 → entries 从 config.json 重建（site 由调用方从 agent.json 提供）
    if (parsed === null) {
        return _rebuilt_or_empty(config_json, site_abbr);
    }

    // entries 缺失/为空但 config.json 有实例 → 从 config.json 重建 entries
    if (parsed.entries.length === 0 && config_json) {
        const entries = rebuild_entries(config_json, site_abbr);
        if (entries.length > 0) {
            return { entries };
        }
    }

    return parsed;
}

export async function save_abbr_registry(
    registry: AbbrRegistry,
    file_path?: string,
): Promise<void> {
    const target = file_path ?? default_abbr_registry_path();
    const dir = dirname(target);
    await mkdir(dir, { recursive: true });
    const output = JSON.stringify({ entries: registry.entries }, null, 4) + "\n";
    const tmp_path = target + ".tmp";
    await writeFile(tmp_path, output, "utf-8");
    await rename(tmp_path, target);
}

// ── 检索（info-gatherer 用，只读）─────────────────────────

export function retrieve_candidate(
    registry: AbbrRegistry,
    params: RetrieveCandidateParams,
): RetrieveCandidateResult {
    const { description } = params;
    // intent 与 role 仅用于调用方做冲突判定，本函数只做只读检索：
    // 描述匹配 → 复用历史 id/abbr（「想起来可能是谁」）。
    for (const entry of registry.entries) {
        if (_descriptions_match(description, entry)) {
            return { hit: true, id: entry.id, abbr: entry.abbr };
        }
    }
    return { hit: false };
}

// ── 冲突判定 ──────────────────────────────────────────────

export function resolve_abbr_conflict(
    registry: AbbrRegistry,
    candidate_abbr: string,
    description: string,
): AbbrConflictResult {
    const existing = registry.entries.find((e) => e.abbr === candidate_abbr);
    if (!existing) {
        return { conflict: false };
    }
    if (_descriptions_match(description, existing)) {
        // 同一设备加点：abbr 相同且描述匹配 → 复用历史 id（合并，不新建）
        return { conflict: false, existing_id: existing.id };
    }
    // 不同设备撞 abbr：需重新生成不同 abbr，不得复用
    return { conflict: true, existing_id: existing.id };
}

// ── 固化 / 删除 / 重建（SuperWorker 确定性代码用）──────────

export function finalize_entry(
    registry: AbbrRegistry,
    input: AbbrEntryInput,
): AbbrRegistry {
    const entries = [...registry.entries];
    const idx = entries.findIndex((e) => e.id === input.id);
    const entry: AbbrEntry = { ...input };
    if (idx >= 0) {
        entries[idx] = entry;
    } else {
        entries.push(entry);
    }
    return { entries };
}

export function delete_entry(
    registry: AbbrRegistry,
    id: string,
): AbbrRegistry {
    return {
        entries: registry.entries.filter((e) => e.id !== id),
    };
}

export function rebuild_entries(
    config_json: SystemConfig,
    site_abbr: string | null,
): AbbrEntry[] {
    const prefix = site_abbr !== null && site_abbr.length > 0
        ? `${site_abbr}_`
        : "";
    const entries: AbbrEntry[] = [];

    for (const [service_type, value] of Object.entries(config_json)) {
        if (!service_type.startsWith("c4_") || service_type === "c4_shm_manager") {
            continue;
        }
        if (!Array.isArray(value)) {
            continue;
        }
        for (const item of value) {
            const inst = item as MCPInstanceConfig;
            const id = typeof inst.id === "string" ? inst.id : "";
            if (id.length === 0) {
                continue;
            }
            const name = typeof inst.name === "string" && inst.name.length > 0
                ? inst.name
                : id;
            const abbr = prefix.length > 0 && id.startsWith(prefix)
                ? id.slice(prefix.length)
                : id;
            entries.push({
                id,
                name,
                abbr,
                service_type,
                role: null,
                description: name,
            });
        }
    }

    return entries;
}

// ── 内部解析 / 匹配 ───────────────────────────────────────

function _rebuilt_or_empty(config_json?: SystemConfig, site_abbr?: string | null): AbbrRegistry {
    if (!config_json) {
        return { entries: [] };
    }
    return { entries: rebuild_entries(config_json, site_abbr ?? null) };
}

function _parse_registry(raw: string): AbbrRegistry | null {
    let parsed: unknown;
    try {
        parsed = JSON.parse(raw);
    } catch {
        return null;
    }
    if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
        return null;
    }
    const obj = parsed as Record<string, unknown>;
    return {
        entries: _parse_entries(obj["entries"]),
    };
}

function _parse_entries(value: unknown): AbbrEntry[] {
    if (!Array.isArray(value)) {
        return [];
    }
    const entries: AbbrEntry[] = [];
    for (const item of value) {
        const entry = _parse_entry(item);
        if (entry !== null) {
            entries.push(entry);
        }
    }
    return entries;
}

function _parse_entry(value: unknown): AbbrEntry | null {
    if (typeof value !== "object" || value === null || Array.isArray(value)) {
        return null;
    }
    const obj = value as Record<string, unknown>;
    if (typeof obj["id"] !== "string" || obj["id"].length === 0) {
        return null;
    }
    const role_raw = obj["role"];
    const role: AbbrRole =
        role_raw === "writer" || role_raw === "reader" ? role_raw : null;
    return {
        id: obj["id"],
        name: typeof obj["name"] === "string" ? obj["name"] : obj["id"],
        abbr: typeof obj["abbr"] === "string" ? obj["abbr"] : obj["id"],
        service_type: typeof obj["service_type"] === "string"
            ? obj["service_type"]
            : "",
        role,
        description: typeof obj["description"] === "string"
            ? obj["description"]
            : "",
    };
}

function _descriptions_match(description: string, entry: AbbrEntry): boolean {
    const a = _normalize(description);
    const b_name = _normalize(entry.name);
    const b_desc = _normalize(entry.description);
    if (a.length === 0 || (b_name.length === 0 && b_desc.length === 0)) {
        return false;
    }
    return (
        _contains(a, b_name) || _contains(b_name, a) ||
        _contains(a, b_desc) || _contains(b_desc, a)
    );
}

function _normalize(text: string): string {
    return text.trim().toLowerCase().replace(/\s+/g, "");
}

function _contains(haystack: string, needle: string): boolean {
    if (needle.length === 0) {
        return false;
    }
    return haystack.includes(needle);
}

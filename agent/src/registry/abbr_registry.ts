// c4/agent/src/registry/abbr_registry.ts — 设备身份注册表（agent.md §3.2.1.3a）
// 根据 agent.md §3.2.1.3/§3.2.1.3a（2026-10-01 设计修订）实现。
// 确定性文件读写模块：无 LLM、无网络。三层命名职责单一：
//   实例 id = channel{N} 顺序句柄（本文件维护 channelHighWatermark 高位水印，永不复用）；
//   设备身份 = 注册表条目（设备名 → {宿主实例, 点前缀}）；
//   点 key = {设备前缀}_{裸id}（前缀由方案层拼接，本文件存储与查重）。
// 注册表是可重建的派生数据（config.json 为权威），唯 channelHighWatermark 不可重建——
// 丢失时退化为现存实例最大序号（已删序号可能被复用，属可接受降级）。

import { mkdir, readFile, rename, writeFile } from "node:fs/promises";
import { homedir } from "node:os";
import { dirname, join } from "node:path";

import { zh_convert } from "../orchestrator/zh_numeral.js";

import type { MCPInstanceConfig, SystemConfig } from "../types/index.js";

// ── 类型定义 ──────────────────────────────────────────────

export interface AbbrSite {
    name: string;
    abbr: string;
}

/** 注册表条目：设备身份 = 「设备名 → {宿主实例, 点前缀}」（§3.2.1.3a）。 */
export interface AbbrEntry {
    name: string;         // 设备名称（人可读；允许重名，同名须经消歧确认）
    prefix: string;       // 点前缀（注册表内全局唯一）——点 key = {prefix}_{裸id}
    host: string;         // 宿主实例 id（channel{N}）；同端口并入的多台设备共享同一宿主
    service_type: string; // 宿主所属服务类型（重建时从 config.json 顶层 key 反推）
    description: string;  // 首次接入时的原始描述（用于同名消歧的描述匹配仲裁）
    pointMap: Record<string, string>; // 源点名 → 生效点 key（modify/delete 按此匹配旧点）
}

export interface AbbrRegistry {
    /** 序号高位水印（max-ever-assigned）：实例删除只减存活数、不回退水印（§3.2.1.3） */
    channelHighWatermark: number;
    entries: AbbrEntry[]; // 在用设备条目（delete 物理删除，不保留历史）
}

/** 检索结果（§3.2.1.3a id 确定流程第 2 步）：同名命中 → 描述匹配仲裁。 */
export interface DeviceRetrieval {
    decision: "same_device" | "name_conflict" | "no_hit";
    /** same_device → 复用既有 {host, prefix}（修改语义） */
    entry?: AbbrEntry;
    /** name_conflict → 同名多条且描述无法区分，追问用户指认 */
    candidates?: AbbrEntry[];
}

/** 接入管线的同名仲裁增强选项（query_abbr_registry 的 site 归属校验不传、行为不变）。 */
export interface RetrieveOpts {
    /**
     * 点表地址证据回调（2026-10-02 B4 修复）：同设备重入时新消息点表与既有点表
     * addr 集一致；另一台同名设备点表不同。描述含设备名的包含判定在同名检索下
     * 恒真（「1#风机数据」归一化含「1风机」），不能作为匹配证据——提供本回调时
     * 改用强仲裁（名字精确等值 / 描述互含 / 地址证据），未提供保持原行为。
     */
    addr_evidence?: (entry: AbbrEntry) => boolean;
}

// ── 路径 ──────────────────────────────────────────────────

export function default_abbr_registry_path(): string {
    return join(homedir(), ".local", "c4", "abbr_registry.json");
}

// ── 读写 ──────────────────────────────────────────────────

export async function load_abbr_registry(
    file_path?: string,
    config_json?: SystemConfig,
): Promise<AbbrRegistry> {
    const target = file_path ?? default_abbr_registry_path();

    let parsed: AbbrRegistry | null;
    try {
        const raw = await readFile(target, "utf-8");
        parsed = _parse_registry(raw);
    } catch {
        // 读取失败（不存在/权限）按「损坏」处理，统一走重建分支
        parsed = null;
    }

    const derived_watermark = config_json ? channel_watermark_from_config(config_json) : 0;

    // 文件不存在 / 损坏 / 旧格式（无 prefix 条目全被丢弃）→ 从 config.json 重建。
    // 旧格式不兼容（agent.md §3.2.1.3 设计修订）：hnals_wt1 式条目直接废弃。
    if (parsed === null || parsed.entries.length === 0) {
        const entries = config_json ? rebuild_entries(config_json) : [];
        return {
            channelHighWatermark: Math.max(parsed?.channelHighWatermark ?? 0, derived_watermark),
            entries,
        };
    }

    // config.json 是权威、注册表是派生数据（失败回滚/点级删除等都可能造成失步）：
    // 每次加载按 config 对齐——宿主已不存在的条目移除（回滚残留）；
    // pointMap 从宿主点表同源重建；channelHighWatermark 取两者较大值（永不回退）。
    if (config_json) {
        const hosts = _collect_hosts(config_json);
        parsed.entries = parsed.entries.filter((e) => hosts.has(e.host));
        for (const entry of parsed.entries) {
            const rebuilt = _rebuild_point_map(config_json, entry.host, entry.prefix);
            if (rebuilt !== null) {
                entry.pointMap = rebuilt;
                entry.service_type = entry.service_type || _host_service_type(config_json, entry.host);
            }
        }
    }
    parsed.channelHighWatermark = Math.max(parsed.channelHighWatermark, derived_watermark);
    return parsed;
}

export async function save_abbr_registry(
    registry: AbbrRegistry,
    file_path?: string,
): Promise<void> {
    const target = file_path ?? default_abbr_registry_path();
    const dir = dirname(target);
    await mkdir(dir, { recursive: true });
    const output =
        JSON.stringify(
            {
                channelHighWatermark: registry.channelHighWatermark,
                entries: registry.entries,
            },
            null,
            4,
        ) + "\n";
    const tmp_path = target + ".tmp";
    await writeFile(tmp_path, output, "utf-8");
    await rename(tmp_path, target);
}

// ── 检索（§3.2.1.3a id 确定流程第 2 步，只读）──────────────
// 同名命中 → 描述匹配仲裁（判定依据是「描述是否也匹配」，而非仅名字相同——
// 现场可能有两台同名设备）。仲裁对单命中同样生效：描述完全对不上的「同名」
// 返回 name_conflict 追问而非静默并入；描述弱信号（用户只复述设备名）视为
// 匹配，最终判定由方案确认环节兜底（「注册表只提供候选，用户确认负责最终判定」）。

export function retrieve_device(
    registry: AbbrRegistry,
    name: string,
    description?: string,
    opts?: RetrieveOpts,
): DeviceRetrieval {
    const q = _normalize(name);
    if (q.length === 0) {
        return { decision: "no_hit" };
    }
    const hits = registry.entries.filter((e) => _normalize(e.name) === q);
    if (hits.length === 0) {
        return { decision: "no_hit" };
    }
    const desc = description ?? name;
    const a = _normalize(desc);
    const matched = hits.filter((e) => {
        if (opts?.addr_evidence) {
            // 强仲裁（接入管线）：名字精确等值复述 / 描述互含 / 点表地址证据——
            // 名字包含不作证据（同名检索下恒真，2026-10-02 B4 实测静默并入）
            const b_name = _normalize(e.name);
            const b_desc = _normalize(e.description);
            if (a === b_name) return true;
            if (_contains(a, b_desc) || _contains(b_desc, a)) return true;
            return opts.addr_evidence(e);
        }
        return _descriptions_match(desc, e);
    });
    if (matched.length === 1) {
        return { decision: "same_device", entry: matched[0] };
    }
    return { decision: "name_conflict", candidates: hits };
}

// ── 前缀 / 序号分配（确定性，§3.2.1.3/§3.2.1.3c）──────────

/** 候选前缀撞名 → 保留前缀 + 最小未用编号顺延（wt1 已占用 → wt2；dev1 → dev2）。 */
export function resolve_prefix_conflict(
    registry: AbbrRegistry,
    base: string,
): string {
    const taken = new Set(registry.entries.map((e) => e.prefix));
    if (!taken.has(base)) {
        return base;
    }
    const m = base.match(/^(.*?)(\d+)$/);
    const stem = m ? m[1] : base;
    let n = m ? Number(m[2]) + 1 : 2;
    while (taken.has(`${stem}${n}`)) {
        n += 1;
    }
    return `${stem}${n}`;
}

/** 完全匿名设备的自动序列（dev1、dev2…）：取最小未用编号（§3.2.1.3c）。 */
export function next_dev_prefix(registry: AbbrRegistry): string {
    const taken = new Set(registry.entries.map((e) => e.prefix));
    let n = 1;
    while (taken.has(`dev${n}`)) {
        n += 1;
    }
    return `dev${n}`;
}

/** 分配下一个 channel 序号：未使用最小序号 = 水印 + 1（从未分配过，§3.2.1.3）。
 *  注册表 API——供测试与后续直用场景；主流程在方案层以局部水印闭包批量分配。 */
export function next_channel_id(registry: AbbrRegistry): string {
    return `channel${registry.channelHighWatermark + 1}`;
}

/** 固化时推进高位水印（只增不减——实例删除后序号永不复用）。
 *  注册表 API——供测试与后续直用场景；主流程由 execute_steps 写入 watermark。 */
export function bump_channel_watermark(registry: AbbrRegistry, instance_id: string): void {
    const m = instance_id.match(/^channel(\d+)$/);
    if (m) {
        registry.channelHighWatermark = Math.max(
            registry.channelHighWatermark,
            Number(m[1]),
        );
    }
}

// ── 固化 / 删除（执行层确定性代码，挂 merge + Stop-Start 成功路径之后）──

export function finalize_entry(
    registry: AbbrRegistry,
    input: AbbrEntry,
): AbbrRegistry {
    const entries = [...registry.entries];
    const idx = entries.findIndex((e) => e.prefix === input.prefix);
    if (idx >= 0) {
        entries[idx] = { ...input };
    } else {
        entries.push({ ...input });
    }
    return {
        channelHighWatermark: registry.channelHighWatermark,
        entries,
    };
}

export function delete_entry(
    registry: AbbrRegistry,
    prefix: string,
): AbbrRegistry {
    return {
        channelHighWatermark: registry.channelHighWatermark,
        entries: registry.entries.filter((e) => e.prefix !== prefix),
    };
}

// ── 重建（abbr_registry.json 丢失/损坏时；config.json 为权威）──
// host 与 prefix 从点 key 前缀分组确定性重建（key 首个 `_` 之前为前缀，
// §3.2.1.3a——裸 id 内可含 `_`，前缀自身不含 `_`，此为设计认可的确定性近似）；
// name→prefix 对应关系丢失时按前缀枚举退化（name 退化为前缀本身）、
// description 退化为空（不影响 key 稳定性）；pointMap 从点表 name → 点 key 同源重建。

export function rebuild_entries(config_json: SystemConfig): AbbrEntry[] {
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
            const host = typeof inst.id === "string" ? inst.id : "";
            if (host.length === 0) {
                continue;
            }
            const groups = new Map<string, AbbrEntry>();
            for (const pt of (inst.points ?? []) as Array<Record<string, unknown>>) {
                const pid = typeof pt["id"] === "string" ? (pt["id"] as string) : "";
                const us = pid.indexOf("_");
                if (us <= 0) {
                    continue; // 裸 id（旧配置形态）不重建——旧配置废弃，重新接入即得新形态
                }
                const prefix = pid.slice(0, us);
                if (!/^[a-zA-Z][a-zA-Z0-9]*$/.test(prefix)) {
                    continue;
                }
                let entry = groups.get(prefix);
                if (!entry) {
                    entry = {
                        name: prefix,
                        prefix,
                        host,
                        service_type,
                        description: "",
                        pointMap: {},
                    };
                    groups.set(prefix, entry);
                }
                const nm = typeof pt["name"] === "string" ? (pt["name"] as string) : "";
                if (nm.length > 0) {
                    entry.pointMap[nm] = pid;
                }
            }
            entries.push(...groups.values());
        }
    }
    return entries;
}

/** channelHighWatermark 不可重建（已删实例序号在配置中无痕）——丢失后退化为现存实例最大序号。 */
export function channel_watermark_from_config(config_json: SystemConfig): number {
    let max = 0;
    for (const [st, list] of Object.entries(config_json)) {
        if (st === "c4_shm_manager" || !Array.isArray(list)) {
            continue;
        }
        for (const inst of list as Array<Record<string, unknown>>) {
            const m = String(inst["id"] ?? "").match(/^channel(\d+)$/);
            if (m) {
                max = Math.max(max, Number(m[1]));
            }
        }
    }
    return max;
}

// ── 内部解析 / 匹配 ───────────────────────────────────────

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
    const watermark = typeof obj["channelHighWatermark"] === "number"
        ? obj["channelHighWatermark"]
        : 0;
    return {
        channelHighWatermark: watermark,
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
    // 旧格式条目（id/abbr、无 prefix/host）直接丢弃——不保留旧格式兼容（§3.2.1.3）。
    // prefix==="" 是 §2.12.6 下游目标条目的合法形态（按 name upsert/delete），
    // 不得当作损坏数据丢弃（2026-10-05 用例 65 实测：每次加载目标条目凭空消失）
    if (
        typeof obj["name"] !== "string" || obj["name"].length === 0 ||
        typeof obj["prefix"] !== "string" ||
        typeof obj["host"] !== "string" || obj["host"].length === 0
    ) {
        return null;
    }
    const pointMap: Record<string, string> = {};
    if (typeof obj["pointMap"] === "object" && obj["pointMap"] !== null) {
        for (const [k, v] of Object.entries(obj["pointMap"] as Record<string, unknown>)) {
            if (typeof v === "string") {
                pointMap[k] = v;
            }
        }
    }
    return {
        name: obj["name"],
        prefix: obj["prefix"],
        host: obj["host"],
        service_type: typeof obj["service_type"] === "string" ? obj["service_type"] : "",
        description: typeof obj["description"] === "string" ? obj["description"] : "",
        pointMap,
    };
}

function _collect_hosts(config: SystemConfig): Set<string> {
    const hosts = new Set<string>();
    for (const [st, list] of Object.entries(config)) {
        if (st === "c4_shm_manager" || !Array.isArray(list)) {
            continue;
        }
        for (const inst of list as Array<Record<string, unknown>>) {
            if (typeof inst["id"] === "string") {
                hosts.add(inst["id"]);
            }
        }
    }
    return hosts;
}

function _host_service_type(config: SystemConfig, host: string): string {
    for (const [st, list] of Object.entries(config)) {
        if (st === "c4_shm_manager" || !Array.isArray(list)) {
            continue;
        }
        if ((list as Array<Record<string, unknown>>).some((i) => i["id"] === host)) {
            return st;
        }
    }
    return "";
}

/** 从宿主实例点表重建 pointMap（name → 点 key，前缀过滤）。宿主不存在返回 null。 */
function _rebuild_point_map(
    config: SystemConfig,
    host: string,
    prefix: string,
): Record<string, string> | null {
    const prefix_ = `${prefix}_`;
    for (const [st, list] of Object.entries(config)) {
        if (st === "c4_shm_manager" || !Array.isArray(list)) {
            continue;
        }
        for (const inst of list as Array<Record<string, unknown>>) {
            if (inst["id"] !== host) {
                continue;
            }
            const pointMap: Record<string, string> = {};
            for (const pt of (inst["points"] ?? []) as Array<Record<string, unknown>>) {
                const pid = typeof pt["id"] === "string" ? (pt["id"] as string) : "";
                if (!pid.startsWith(prefix_)) {
                    continue;
                }
                const nm = typeof pt["name"] === "string" ? (pt["name"] as string) : "";
                if (nm.length > 0) {
                    pointMap[nm] = pid;
                }
            }
            return pointMap;
        }
    }
    return null;
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
    // 设备命名分隔符归一：「1#风机」「1号风机」「1 风机」视为同一设备——
    // 用户删除/修改时常用「号」而接入时注册库存的是「#」（func_test_case 用例 25/26）；
    // 中文数字编号折算为阿拉伯数字（「三号风机」≡「3号风机」——中文数字支持是
    // §3.2.1.3c 引入的，归一化不跟上会让同一设备重复注册、前缀顺延出第二套身份）
    return text
        .trim()
        .toLowerCase()
        .replace(/\s+/g, "")
        .replace(/([零一二两三四五六七八九十]{1,6})号/g, (m, zh: string) => {
            const n = zh_convert(zh);
            return n !== null ? `${n}号` : m;
        })
        .replace(/[＃#号]/g, "");
}

function _contains(haystack: string, needle: string): boolean {
    if (needle.length === 0) {
        return false;
    }
    return haystack.includes(needle);
}

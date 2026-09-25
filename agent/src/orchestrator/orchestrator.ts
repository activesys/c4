// c4/agent/src/orchestrator/orchestrator.ts — Workflow 编排器
// agent.md §1.4-§3.2：缺口驱动九阶段流水线。
//   回合循环: ①取消检测 ②阶段提取(1-7，提示词驱动) ③缺口计算(聚合提问停等)
//   ④方案层装配(阶段8，纯代码：abbr 记忆 / 确定性推导 / L1+L2 校验) → button_arm 停等
//   ⑤执行层(阶段9，确认按钮后：generate_steps 拆解 + 事务五步 + 回滚协议)。
// 确认通道: 按钮唯一(§2.8)——[C4_BUTTON_CONFIRM]/[C4_BUTTON_CANCEL] 前缀；
//   确认消息内嵌 changes/devices JSON 时走确定性直通路径（测试与高级用户通道）。
// 本文件实现 server/types.ts 的 C4Agent 接口——server 层零改动。

import { existsSync, readFileSync, writeFileSync } from "node:fs";
import * as path from "node:path";
import type {
    C4Agent,
    AgentInvokeInput,
    AgentStreamEvent,
} from "../server/types.js";
import type { McpServiceRegistry } from "../registry/registry.js";
import {
    load_abbr_registry,
    resolve_abbr_conflict,
    save_abbr_registry,
    finalize_entry,
    type AbbrRegistry,
} from "../registry/abbr_registry.js";
import { validate_point_table } from "../executor/point_rules.js";
import {
    generate_steps,
    find_service_type,
    normalize_protocol,
} from "../subagents/tools/output_plan_steps.js";
import {
    IDENTIFIER_RE,
    identifier_error,
    merge_config_from_steps,
    run_runtime_stop_start,
    rollback_config_change,
} from "../executor/executor.js";
import {
    begin_config_transaction,
    clear_pending_marker,
} from "../executor/transaction.js";
import {
    arbitrate_site_tags,
    deterministic_site_tag,
    llm_site_tag,
} from "./site_check.js";
import {
    with_config_lock,
    ConfigBusyError,
    CONFIG_BUSY_MESSAGE,
} from "../executor/single_flight.js";
import { parse_any_file } from "../subagents/tools/doc_parsers.js";
import type { ServiceStep } from "../types/index.js";

// ── 会话状态（agent.md §3.1 SessionState，单设备在途模型）────

interface SiteInfo {
    name: string;
    abbr: string;
}
interface SideDraft {
    protocol: string | null;
    protocolRaw: string | null;
    locked: boolean;
    deviceName: string | null;
    points: Array<Record<string, unknown>> | null;
    declared: number | null;
    conn: Record<string, unknown>;
}

function fresh_side(): SideDraft {
    return {
        protocol: null,
        protocolRaw: null,
        locked: false,
        deviceName: null,
        points: null,
        declared: null,
        conn: {},
    };
}

interface AccessPlan {
    kind: "add" | "changes";
    input?: Record<string, unknown>;
    steps?: ServiceStep[];
    display: string;
}

interface SessionState {
    site: SiteInfo | null;
    forwardIntent: boolean;
    recv: SideDraft;
    fwd: SideDraft;
    locks: { receive: boolean; forward: boolean };
    accessPlan: AccessPlan | null;
    userConfirmed: boolean;
    lastGapSignature: string | null;
    gapRepeat: number;
    /** 本回合场站归属判定结果（null=未触发；ambiguous=归属不明；other=他站资料） */
    turnSiteCheck: "ambiguous" | "other" | null;
}

function fresh_state(): SessionState {
    return {
        site: null,
        forwardIntent: false,
        recv: fresh_side(),
        fwd: fresh_side(),
        locks: { receive: false, forward: false },
        accessPlan: null,
        userConfirmed: false,
        lastGapSignature: null,
        gapRepeat: 0,
        turnSiteCheck: null,
    };
}

// §2.4.2 取消词表——去空白后全等匹配（禁止包含匹配）
const CANCEL_WORDS = new Set(["取消", "算了", "不接了", "放弃", "停止接入"]);

// 转发意图：肯定表述命中且否定表述未命中（"不需要转发/仅采集"不激活转发链）
const FORWARD_ON_RE = /转发|入库|写入|上传|推送|发送到/;
const FORWARD_OFF_RE = /不需要转发|不转发|无需转发|仅采集|只采集|不用转发/;

// 修改/删除意图（针对已接入设备）
const CHANGE_INTENT_RE =
    /不再采集|停用|删除|移除|删了|删掉|去掉|改为|改成|修改|调整|更新|增加.{0,8}点|追加.{0,8}点|添加.{0,8}点|加点|新增点/;

// ── 小工具 ─────────────────────────────────────────────────

function render_prompt(file: string, params: Record<string, string>): string {
    const tpl = readFileSync(
        path.join(
            path.dirname(new URL(import.meta.url).pathname),
            "..",
            "super_worker",
            "prompts",
            file,
        ),
        "utf-8",
    );
    return tpl.replace(/\{\{\s*(\w+)\s*\}\}/g, (_, k: string) => params[k] ?? "");
}

function strip_fence(text: string): string {
    const m = text.match(/```(?:json)?\s*([\s\S]*?)```/);
    return (m ? m[1] : text).trim();
}

function parse_json<T>(text: string): T | null {
    try {
        return JSON.parse(strip_fence(text)) as T;
    } catch {
        return null;
    }
}

/** ChatOpenAI 应答文本提取（string 或 content blocks 数组）。 */
function extract_text(res: { content: unknown }): string {
    const c = res.content;
    if (typeof c === "string") return c;
    if (Array.isArray(c)) {
        return c
            .map((b) =>
                typeof b === "object" && b !== null && "text" in (b as Record<string, unknown>)
                    ? String((b as Record<string, unknown>)["text"] ?? "")
                    : "",
            )
            .join("");
    }
    return JSON.stringify(c ?? "");
}

function content_of(msg: { role: string; content: string }): string {
    return typeof msg.content === "string" ? msg.content : "";
}

/** 从上传路由的复合消息中剥离元信息，得到语义输入。 */
function clean_user_text(text: string): string {
    if (text.includes("用户上传了文件:")) {
        const um = text.match(/用户消息:\s*([\s\S]*?)(?:\n?（展示要求[^\n]*\n?|$)/);
        if (um) return um[1].trim();
    }
    return text.trim();
}

/** 提取消息中内嵌的 JSON 对象（changes / devices 直通通道）。 */
function extract_embedded_json(text: string): Record<string, unknown> | null {
    const m = text.match(/\{[\s\S]*\}/);
    if (!m) return null;
    return parse_json<Record<string, unknown>>(m[0]);
}

// ── 点表文件的确定性列映射 ─────────────────────────────────
// 测试与常见点表 CSV/xlsx 带规范表头（device_name/device_ip/protocol/port/
// point_name/name/addr/uid/fun/type/swap）——直接按列名映射，仅当列名不可识别
// 时才依赖 LLM 语义映射（point_prompt 的 <file_data> 通道）。

const HEADER_ALIASES: Record<string, string[]> = {
    deviceName: ["device_name", "设备名称", "dev_name", "device"],
    ip: ["device_ip", "ip", "ip地址", "ip_addr", "host"],
    protocol: ["protocol", "协议", "potocol"],
    port: ["port", "端口", "potr"],
    name: ["name", "point_name", "点名", "名称", "pont_nam"],
    addr: ["addr", "地址", "adres"],
    uid: ["uid", "从站号", "unt"],
    fun: ["fun", "功能码", "funct"],
    type: ["type", "数据类型", "typ"],
    swap: ["swap", "字节交换", "swp"],
};

function map_header(header: string): string | null {
    const h = header.trim().toLowerCase();
    for (const [key, aliases] of Object.entries(HEADER_ALIASES)) {
        if (aliases.some((a) => a.toLowerCase() === h)) return key;
    }
    return null;
}

interface ParsedTable {
    header: string[];
    rows: Array<Record<string, string>>;
}

function parse_table(raw: string): ParsedTable | null {
    const lines = raw
        .split(/\r?\n/)
        .map((l) => l.trim())
        .filter((l) => l.length > 0);
    if (lines.length < 2) return null;
    const sep = lines[0].includes("\t") ? "\t" : ",";
    const header = lines[0].split(sep).map((c) => c.trim().replace(/^"|"$/g, ""));
    if (header.length < 2) return null;
    if (header.every((h) => map_header(h) === null)) return null; // 无可识别列 → 交 LLM
    const rows: Array<Record<string, string>> = [];
    for (const line of lines.slice(1)) {
        const cells = line.split(sep).map((c) => c.trim().replace(/^"|"$/g, ""));
        const row: Record<string, string> = {};
        header.forEach((h, i) => {
            row[h] = cells[i] ?? "";
        });
        rows.push(row);
    }
    return { header, rows };
}

interface FileParseResult {
    deviceName: string | null;
    protocol: string | null;
    ip: string | null;
    port: number | null;
    points: Array<Record<string, unknown>>;
    raw: string;
}

function parse_file_table(raw: string): FileParseResult {
    const out: FileParseResult = {
        deviceName: null,
        protocol: null,
        ip: null,
        port: null,
        points: [],
        raw,
    };
    // parse_any_file 对 xlsx/csv 返回 TabularData JSON（{headers, rows, rowCount}）——
    // 归一化回「表头行 + 数据行」形态供列映射；txt 等原始文本走 CSV 启发式
    let text = raw;
    const trimmed = raw.trim();
    if (trimmed.startsWith("{") && trimmed.includes('"headers"')) {
        try {
            const td = JSON.parse(trimmed) as {
                headers?: string[];
                rows?: string[][];
                rowCount?: number;
            };
            if (!Array.isArray(td.headers) || td.headers.length === 0 || !Array.isArray(td.rows)) {
                return out;
            }
            const lines = [
                td.headers.join(","),
                ...td.rows.map((r) => r.join(",")),
            ];
            text = lines.join("\n");
        } catch {
            return out;
        }
    }
    const table = parse_table(text);
    if (!table) return out;
    const colOf: Record<string, number> = {};
    table.header.forEach((h, i) => {
        const m = map_header(h);
        if (m !== null && !(m in colOf)) colOf[m] = i;
    });
    const get = (row: Record<string, string>, key: string): string => {
        const idx = colOf[key];
        if (idx === undefined) return "";
        return row[table.header[idx]] ?? "";
    };
    for (const row of table.rows) {
        const pointName = get(row, "name");
        const addrRaw = get(row, "addr");
        if (pointName === "" && addrRaw === "") {
            // 无点信息的行：尝试提取设备级信息
            if (out.deviceName === null && get(row, "deviceName") !== "") {
                out.deviceName = get(row, "deviceName");
            }
            continue;
        }
        const pt: Record<string, unknown> = {};
        if (pointName !== "") pt["name"] = pointName;
        if (addrRaw !== "") pt["addr"] = Number(addrRaw) || addrRaw;
        for (const k of ["uid", "fun", "type", "swap"]) {
            const v = get(row, k);
            if (v !== "") pt[k] = Number(v) || v;
        }
        out.points.push(pt);
        if (out.deviceName === null && get(row, "deviceName") !== "") {
            out.deviceName = get(row, "deviceName");
        }
        if (out.protocol === null && get(row, "protocol") !== "") {
            out.protocol = get(row, "protocol");
        }
        if (out.ip === null && get(row, "ip") !== "") out.ip = get(row, "ip");
        if (out.port === null && get(row, "port") !== "") {
            out.port = Number(get(row, "port")) || null;
        }
    }
    return out;
}

// ── 设备 abbr 候选（确定性推导，§3.2.1.3a）──────────────────

const DEVICE_TYPE_ABBR: Array<[RegExp, string]> = [
    [/风机|风电机组/, "wt"],
    [/主变/, "zy"],
    [/逆变器/, "nb"],
    [/测风塔/, "cft"],
    [/光伏/, "gf"],
    [/储能/, "cn"],
    [/中心|主站|上级/, "center"],
    [/数据源|接收/, "src"],
];

function device_abbr_candidate(name: string): string {
    const numMatch = name.match(/(\d+)\s*#?\s*/);
    const num = numMatch ? numMatch[1] : "";
    for (const [re, abbr] of DEVICE_TYPE_ABBR) {
        if (re.test(name)) return num ? `${abbr}${num}` : abbr;
    }
    const ascii = name.replace(/[^a-zA-Z0-9_]/g, "");
    if (ascii.length > 0) return ascii.toLowerCase();
    return num ? `dev${num}` : "dev";
}

// ── 非技术语言的执行错误翻译（§2.10 / 断言黑名单约束）──────

const ERROR_CODE_FRIENDLY: Array<[RegExp, string]> = [
    [
        /DUPLICATE_KEY/,
        "有数据点的标识相互冲突，无法同时生效，本次变更未执行（配置已恢复原样）。请检查点表后重试，或取消本次接入。",
    ],
    [
        /CONFIG_MISSING_SECTION/,
        "采集的数据点必须同时配置对应的数据转发——当前方案缺少转发侧，无法生效（配置已恢复原样）。请补充转发信息后重试，或取消本次接入。",
    ],
    [
        /UNKNOWN_READER_KEY/,
        "转发点引用的采集点不存在，本次变更未执行（配置未改动）。请确认采集侧点表后重试。",
    ],
    [/PORT_BIND_FAILED|CONNECT_FAILED/, "网络连接出现问题，服务未能启动。本次变更已恢复原样，请检查地址和端口后重试。"],
    [/INVALID_POINT/, "有数据点的配置不合法（地址、类型或功能码组合不正确），本次变更已恢复原样。请检查点表后重试。"],
    [/SHM_SYSCALL_FAILED|SHM_NOT_CREATED|SHM_OPEN_FAILED|SHM_CORRUPTED|SHM_ID_NOT_ASSIGNED/,
        "数据存储通道出现问题，本次变更已恢复原样。请稍后重试；若多次失败请联系管理员检查。"],
    [/ALREADY_RUNNING/, ""],
];

function friendly_exec_error(raw: string): string {
    const code = (raw.match(/^([A-Z_]+):/) ?? [])[1] ?? "";
    for (const [re, msg] of ERROR_CODE_FRIENDLY) {
        if (msg && (re.test(code) || re.test(raw))) return msg;
    }
    // 兜底：剥离错误码与英文技术词，保留可读片段
    const cleaned = raw
        .replace(/^[A-Z_]+:\s*/, "")
        .replace(/[\w.]*(?:shm|mcp|config|json|socket)[\w.]*/gi, "系统")
        .trim();
    return `执行过程中出现问题，本次变更未生效（已恢复原样）：${cleaned.slice(0, 160)}`;
}

// ── 编排器工厂 ─────────────────────────────────────────────

export interface OrchestratorConfig {
    model: {
        invoke(msgs: Array<{ role: string; content: string }>): Promise<{ content: unknown }>;
    };
    registry: McpServiceRegistry;
    mcpManager: import("../mcp/client.js").C4McpManager;
    configPath: string;
    agentConfigPath: string;
    instanceId: string;
    site: SiteInfo | null;
    state: {
        setPhase(p: string): void;
        setAccessPlan(e: boolean): void;
        setError(e: string | null): void;
    };
    agentLogger: import("../logging/agent_logger.js").AgentLogger;
    displayTools?: unknown;
}

export function createOrchestrator(cfg: OrchestratorConfig): C4Agent {
    // 会话草稿按 conversationId 隔离（web.md §3.1.2：conversationId 是会话主键）——
    // 修复跨会话接入草稿串台（2026-09-23）。实例级场站绑定全局共享（§3.2.1.3a：
    // 一个 C4 实例绑定唯一场站）：新会话草稿继承绑定场站，绑定动作写回全局与全部活草稿。
    let boundSite: SiteInfo | null = cfg.site;
    const drafts = new Map<string, SessionState>();
    const DRAFT_CAP = 32;
    function draft_of(conversationId: string | undefined): SessionState {
        const key = conversationId ?? "orchestrator";
        const hit = drafts.get(key);
        if (hit) {
            drafts.delete(key);
            drafts.set(key, hit); // LRU 触碰
            return hit;
        }
        const fresh = fresh_state();
        fresh.site = boundSite;
        drafts.set(key, fresh);
        if (drafts.size > DRAFT_CAP) {
            const oldest = drafts.keys().next().value as string;
            drafts.delete(oldest);
        }
        return fresh;
    }
    const { model, registry, mcpManager } = cfg;
    const cfgDir = path.dirname(cfg.configPath);
    const abbrPath = path.join(cfgDir, "abbr_registry.json");
    const stateWriter = cfg.state;

    // ── LLM 调用 ───────────────────────────────────────────
    async function llm_json(
        prompt_file: string,
        params: Record<string, string>,
        user_input: string,
    ): Promise<Record<string, unknown> | null> {
        const rendered = render_prompt(prompt_file, params);
        for (let attempt = 0; attempt < 3; attempt++) {
            try {
                const res = await model.invoke([
                    { role: "system", content: rendered },
                    { role: "user", content: `<user_input>\n${user_input}\n</user_input>` },
                ]);
                const parsed = parse_json<Record<string, unknown>>(extract_text(res));
                if (parsed !== null) return parsed;
            } catch (err) {
                if (attempt === 2) {
                    cfg.agentLogger.error(
                        "orchestrator",
                        `LLM 调用失败: ${err instanceof Error ? err.message : String(err)}`,
                    );
                }
            }
        }
        return null;
    }

    // 场站固化：写入 agent.json 的 site 字段（§3.2.1.3a 权威配置）
    function persist_site(site: SiteInfo): void {
        try {
            const raw = readFileSync(cfg.agentConfigPath, "utf-8");
            const obj = JSON.parse(raw) as Record<string, unknown>;
            obj["site"] = site;
            writeFileSync(cfg.agentConfigPath, JSON.stringify(obj, null, 4) + "\n");
        } catch {
            // agent.json 不可写时仅保留会话内 site
        }
    }

    // ── registry 知识切片（§3.2.0 注入参数）────────────────
    function protocol_candidates(role: "writer" | "reader"): string[] {
        const names: string[] = [];
        for (const e of registry.getServiceCatalogEntries()) {
            if (e.role !== role) continue;
            for (const p of e.protocols) {
                const n = normalize_protocol(p.protocol);
                if (n && !names.includes(n)) names.push(n);
            }
        }
        return names;
    }

    function match_hints_payload(role: "writer" | "reader"): string {
        const hints: Array<Record<string, unknown>> = [];
        for (const e of registry.getServiceCatalogEntries()) {
            if (e.role !== role) continue;
            for (const p of e.protocols) {
                const ph = (p as unknown as Record<string, unknown>)["prompt_hints"] as
                    | Record<string, unknown>
                    | undefined;
                const pm = (ph?.["protocol_match"] ?? null) as Record<string, unknown> | null;
                hints.push({
                    protocol: normalize_protocol(p.protocol),
                    aliases: pm?.["aliases"] ?? [p.protocol],
                    rejected_variants: pm?.["rejected_variants"] ?? [],
                });
            }
        }
        return JSON.stringify(hints);
    }

    // L0 确定性协议预匹配（registry aliases，侧别分词）：命中即免 LLM；未命中交协议提示词
    function alias_match_protocol(
        text: string,
        role: "writer" | "reader",
        side_keywords: RegExp,
    ): string | null {
        const clauses = text.split(/[,,。;;\n\n]/).map((c) => c.trim()).filter((c) => c.length > 0);
        const scoped = clauses.filter((c) => side_keywords.test(c));
        if (scoped.length === 0) return null;
        for (const e of registry.getServiceCatalogEntries()) {
            if (e.role !== role) continue;
            for (const p of e.protocols) {
                const ph = (p as unknown as Record<string, unknown>)["prompt_hints"] as
                    | Record<string, unknown>
                    | undefined;
                const pm = (ph?.["protocol_match"] ?? null) as Record<string, unknown> | null;
                const aliases = [
                    normalize_protocol(p.protocol),
                    ...((pm?.["aliases"] ?? []) as string[]),
                ];
                for (const alias of aliases) {
                    const a = alias.trim().toLowerCase();
                    if (a.length >= 3 && scoped.some((c) => c.toLowerCase().includes(a))) {
                        return normalize_protocol(p.protocol);
                    }
                }
            }
        }
        return null;
    }

    // 显式协议声明的 token 提取（"使用 DNP3 协议"/"协议：modbus"）——
    // 仅在带侧别关键词的子句内扫描，避免把历史回显（上一步解析结果）误当本侧声明
    function declared_protocol_token(text: string, clauseFilter: RegExp): string | null {
        const clauses = text
            .split(/[,,。;;\n]+/)
            .map((c) => c.trim())
            .filter((c) => c.length > 0 && clauseFilter.test(c));
        for (const clause of clauses) {
            const m =
                clause.match(/([A-Za-z][A-Za-z0-9]{2,15})\s*(?:协议|规约)/) ??
                clause.match(/协议[:：=\s]*([A-Za-z][A-Za-z0-9-]{2,15})/);
            if (m) return m[1];
        }
        return null;
    }

    // token 是否命中支持列表（协议名或别名，大小写不敏感）
    function supported_protocol_token(
        token: string,
        reg: McpServiceRegistry,
        role: "writer" | "reader",
    ): boolean {
        const t = token.toLowerCase();
        for (const e of reg.getServiceCatalogEntries()) {
            if (e.role !== role) continue;
            for (const p of e.protocols) {
                const ph = (p as unknown as Record<string, unknown>)["prompt_hints"] as
                    | Record<string, unknown>
                    | undefined;
                const pm = (ph?.["protocol_match"] ?? null) as Record<string, unknown> | null;
                const aliases = [
                    normalize_protocol(p.protocol),
                    ...((pm?.["aliases"] ?? []) as string[]),
                ];
                for (const alias of aliases) {
                    const a = alias.trim().toLowerCase();
                    if (a.length >= 3 && (a === t || a.includes(t) || t.includes(a))) {
                        return true;
                    }
                }
            }
        }
        return false;
    }

    function entry_of_side(side: SideDraft, role: "writer" | "reader") {
        if (!side.protocol) return null;
        const svc = find_service_type(registry, normalize_protocol(side.protocol), role);
        if (!svc) return null;
        return { svc, entry: registry.get_entry(svc) };
    }

    function required_config_fields(side: SideDraft, role: "writer" | "reader"): string[] {
        const hit = entry_of_side(side, role);
        const fields = hit?.entry?.config_schema?.fields ?? {};
        return Object.entries(fields)
            .filter(([, f]) => (f as { default?: unknown }).default === undefined)
            .map(([k]) => k);
    }

    // ── 阶段提取器（§3.2.0，提示词驱动）────────────────────
    async function run_stage_extraction(
        user_text: string,
        file_data: string | null,
        state: SessionState,
    ): Promise<boolean> {
        let progress = false;
        const semantic = clean_user_text(user_text);

        // 文件点表确定性列映射——设备级信息直接落位（协议列属用户显式声明）
        let filePoints: Array<Record<string, unknown>> | null = null;
        if (file_data !== null) {
            const parsed = parse_file_table(file_data);
            if (parsed.points.length > 0) {
                filePoints = parsed.points;
                progress = true;
                // 新点表 → 开启新一轮接入草稿（保留场站与记忆库）
                state.recv = fresh_side();
                state.fwd = fresh_side();
                state.forwardIntent = false;
                state.accessPlan = null;
                state.recv.points = parsed.points;
                if (parsed.deviceName) state.recv.deviceName = parsed.deviceName;
                if (parsed.ip) state.recv.conn["ip"] = parsed.ip;
                if (parsed.port) state.recv.conn["port"] = parsed.port;
                if (parsed.protocol) {
                    // 文件协议列 = 用户显式声明：走匹配判定（拒绝不在支持列表的值）
                    const declared = parsed.protocol;
                    const supported = protocol_candidates("writer");
                    const hit = supported.find(
                        (s) =>
                            s.toLowerCase() === declared.toLowerCase() ||
                            declared.toLowerCase().includes(s.toLowerCase()),
                    );
                    if (hit) {
                        state.recv.protocol = hit;
                    }
                    // 未命中 → 交协议阶段提示词/缺口处理（declared 保留在 raw）
                    else {
                        state.recv.protocolRaw = declared;
                    }
                }
            }
        }

        // 阶段1 场站（§3.2.1.3a：site 存于 agent.json 权威配置）
        // 未绑定 → 不自动提取，必须显式询问（首次接入契约）；用户按格式答复后确定性固化
        if (!state.site) {
            const m = semantic.match(
                /场站名称[:：]\s*([^\s，,。]{2,20})\s*[，,]?\s*(?:缩写|简称)[:：]\s*([^\s，,。]{1,12})/,
            );
            if (m) {
                state.site = { name: m[1], abbr: m[2] };
                boundSite = state.site;
                for (const d of drafts.values()) {
                    if (d !== state) d.site = boundSite;
                }
                persist_site(state.site);
                progress = true;
            }
        }
        // 归属判定（已绑定场站，agent.md「场地判定仲裁规则」）：location_prompt LLM
        // 语义判定在此先行发起（与本回合其余提取阶段并行），确定性地名比对在阶段
        // 提取出口执行，两者仲裁取更保守方（见本函数尾「归属判定合并」）。
        state.turnSiteCheck = null;
        const siteTagLlm = state.site
            ? llm_json(
                  "location_prompt.txt",
                  { known_site: `${state.site.name}（缩写 ${state.site.abbr}）` },
                  semantic,
              ).catch(() => null)
            : null;

        // 阶段2 接入协议（L0 确定性：显式声明 token 对齐支持列表 → 别名预匹配 → 提示词）
        if (!state.recv.protocol && semantic.length > 0) {
            const declared = declared_protocol_token(
                semantic,
                /采集|接入|接收|采用|数据源|上传|设备|协议|规约/,
            );
            if (declared !== null && !supported_protocol_token(declared, registry, "writer")) {
                state.recv.protocolRaw = declared;
                progress = true;
            }
        }
        if (!state.recv.protocol && !state.recv.protocolRaw && semantic.length > 0) {
            const aliasHit = alias_match_protocol(semantic, "writer", /采集|接入|接收|采用|数据源|上传|设备|协议/);
            if (aliasHit) {
                if (!state.locks.receive || state.recv.protocol === null) {
                    state.recv.protocol = aliasHit;
                    progress = true;
                }
            }
        }
        if (!state.recv.protocol && !state.recv.protocolRaw && semantic.length > 0) {
            const r = await llm_json(
                "protocol_prompt.txt",
                {
                    side: "receive",
                    supported_list: JSON.stringify(protocol_candidates("writer")),
                    match_hints: match_hints_payload("writer"),
                },
                file_data !== null && filePoints === null
                    ? `${semantic}\n<file_data>\n${file_data.slice(0, 2000)}\n</file_data>`
                    : semantic,
            );
            if (r) {
                const match = String(r["match"] ?? "not_mentioned");
                const canonical = r["canonical_name"];
                if (match === "matched" && typeof canonical === "string") {
                    if (state.locks.receive && state.recv.protocol && state.recv.protocol !== canonical) {
                        // §2.4.1 锁后变更 → 丢弃（拒绝文案在缺口/回复层）
                    } else {
                        state.recv.protocol = canonical;
                        progress = true;
                    }
                } else if (match === "not_in_list") {
                    state.recv.protocolRaw = String(r["user_protocol"] ?? "未知协议");
                    progress = true;
                }
            }
        }
        // 连接信息中的 ip/port 兜底捕获（确定性；提示词未覆盖的简写形式）
        if (state.recv.conn["ip"] === undefined) {
            const ipM = semantic.match(/(?:IP|ip|地址)[^\d]{0,4}(\d{1,3}(?:\.\d{1,3}){3})/);
            const bareIp = semantic.match(/(\d{1,3}(?:\.\d{1,3}){3})/);
            if (ipM) {
                state.recv.conn["ip"] = ipM[1];
                progress = true;
            } else if (bareIp && !/转发|目标/.test(semantic)) {
                state.recv.conn["ip"] = bareIp[1];
                progress = true;
            }
        }
        if (state.recv.conn["port"] === undefined) {
            const portM = semantic.match(/(?:端口|port)[:：]?\s*(\d{2,5})/i);
            if (portM) {
                state.recv.conn["port"] = Number(portM[1]);
                progress = true;
            }
        }
        if (state.recv.deviceName === null) {
            const stop = new Set(["使用", "的", "是", "叫", "不", "已", "在", "为", "与", "和"]);
            const dm = semantic.match(/(?:设备名称|设备)[:：]?\s*([^\s，。,]{2,24})/);
            const dm2 = dm && !stop.has(dm[1].slice(0, 2)) ? dm[1] : null;
            const hm = semantic.match(/接入(?:另一个设备|华能)?[：:]?\s*([^\s，。,]*\d+#\S+)/);
            // 常见编号表述（func_test_case 用例 1 形态）：「1号风机」「1#风机」
            // 「2号升压站」——编号 + 设备类型词，无「设备名称:」前缀
            const nm = semantic.match(
                /(\d{1,3}\s*[#号]\s*(?:风机|主变|升压站|逆变器|测风塔|机组|变压器|数据源))/,
            );
            if (dm2) {
                state.recv.deviceName = dm2;
                progress = true;
            } else if (hm) {
                state.recv.deviceName = hm[1];
                progress = true;
            } else if (nm) {
                state.recv.deviceName = nm[1].replace(/\s+/g, "");
                progress = true;
            }
        }

        // 转发意图（§2.2 阶段5 条件）
        if (!state.forwardIntent && FORWARD_ON_RE.test(semantic) && !FORWARD_OFF_RE.test(semantic)) {
            state.forwardIntent = true;
            progress = true;
        }
        // 显式否认转发 → 回退（即使用户此前提过）
        if (state.forwardIntent && FORWARD_OFF_RE.test(semantic) && !FORWARD_ON_RE.test(semantic)) {
            state.forwardIntent = false;
            state.fwd = fresh_side();
        }

        // 阶段5 转发协议（L0 别名预匹配 → 提示词）
        if (state.forwardIntent && !state.fwd.protocol && !state.fwd.protocolRaw) {
            const declaredF = declared_protocol_token(
                semantic,
                /转发|入库|写入|上传|推送|发送|目标|服务器/,
            );
            if (declaredF !== null && !supported_protocol_token(declaredF, registry, "reader")) {
                state.fwd.protocolRaw = declaredF;
                progress = true;
            }
        }
        if (state.forwardIntent && !state.fwd.protocol && !state.fwd.protocolRaw) {
            const fwdAlias = alias_match_protocol(semantic, "reader", /转发|入库|写入|上传|推送|发送/);
            if (fwdAlias) {
                state.fwd.protocol = fwdAlias;
                progress = true;
            }
        }
        if (state.forwardIntent && !state.fwd.protocol && !state.fwd.protocolRaw) {
            const r = await llm_json(
                "protocol_prompt.txt",
                {
                    side: "forward",
                    supported_list: JSON.stringify(protocol_candidates("reader")),
                    match_hints: match_hints_payload("reader"),
                },
                semantic,
            );
            if (r) {
                const match = String(r["match"] ?? "not_mentioned");
                const canonical = r["canonical_name"];
                if (match === "matched" && typeof canonical === "string") {
                    state.fwd.protocol = canonical;
                    progress = true;
                } else if (match === "not_in_list") {
                    state.fwd.protocolRaw = String(r["user_protocol"] ?? "未知协议");
                    progress = true;
                }
            }
        }

        // 阶段3 接入点表（协议就绪后；LLM 提取，文件数据经 <file_data> 注入）
        if (state.recv.protocol && !state.recv.points) {
            const hit = entry_of_side(state.recv, "writer");
            if (hit) {
                const fields = (hit.entry?.point_schema?.fields ?? []).map(
                    (f: { name: string }) => f.name,
                );
                const hints = JSON.stringify(
                    (hit.entry?.prompt_hints as Record<string, unknown> | undefined)?.[
                        "point_field_hints"
                    ] ?? {},
                );
                const input_text =
                    (file_data !== null && filePoints === null
                        ? `<file_data>\n${file_data.slice(0, 4000)}\n</file_data>\n`
                        : "") + semantic;
                const r = await llm_json(
                    "point_prompt.txt",
                    {
                        side: "receive",
                        protocol: state.recv.protocol,
                        point_fields: JSON.stringify(fields),
                        point_field_hints: hints,
                    },
                    input_text,
                );
                if (r && Array.isArray(r["points"]) && (r["points"] as unknown[]).length > 0) {
                    state.recv.points = r["points"] as Array<Record<string, unknown>>;
                    state.recv.declared =
                        typeof r["declared_count"] === "number"
                            ? (r["declared_count"] as number)
                            : null;
                    progress = true;
                }
            }
        }

        // 阶段6 转发点表：确定性地址范围展开优先，其余交 LLM
        if (state.forwardIntent && state.fwd.protocol && state.recv.points && !state.fwd.points) {
            const n = state.recv.points.length;
            const rangeM = semantic.match(
                /(?:转发地址|转发点表|点表)[^\d\n]{0,6}(\d{2,7})\s*(?:到|~|-|—|开始)?\s*(\d+)?/,
            );
            if (rangeM) {
                const start = Number(rangeM[1]);
                const end = rangeM[2] !== undefined ? Number(rangeM[2]) : start + n - 1;
                const span = end >= start ? end - start + 1 : n;
                const count = Math.min(n, span);
                const pts: Array<Record<string, unknown>> = [];
                for (let i = 0; i < count; i++) pts.push({ addr: start + i });
                if (count === n) {
                    state.fwd.points = pts;
                    progress = true;
                }
            } else if (state.fwd.protocol === "influxdb") {
                // §2.7.1 确定性推导：influxdb 点字段（measurement/field/type）全部可由
                // 源点/场站/用户表名映射，无需 LLM 提取——此处仅落 addr 骨架，
                // 方案层装配时填充推导字段
                state.fwd.points = (state.recv.points ?? []).map((p) => ({
                    addr: p["addr"],
                }));
                progress = true;
            } else {
                const hit = entry_of_side(state.fwd, "reader");
                if (hit) {
                    const fields = (hit.entry?.point_schema?.fields ?? []).map(
                        (f: { name: string }) => f.name,
                    );
                    const hints = JSON.stringify(
                        (hit.entry?.prompt_hints as Record<string, unknown> | undefined)?.[
                            "point_field_hints"
                        ] ?? {},
                    );
                    const r = await llm_json(
                        "point_prompt.txt",
                        {
                            side: "forward",
                            protocol: state.fwd.protocol,
                            point_fields: JSON.stringify(fields),
                            point_field_hints: hints,
                        },
                        semantic,
                    );
                    if (r && Array.isArray(r["points"]) && (r["points"] as unknown[]).length > 0) {
                        state.fwd.points = r["points"] as Array<Record<string, unknown>>;
                        progress = true;
                    }
                }
            }
        }

        // 阶段7 转发连接（ip:port 形式 + 提示词提取）
        if (state.forwardIntent && state.fwd.protocol) {
            if (state.fwd.conn["ip"] === undefined || state.fwd.conn["port"] === undefined) {
                const m = semantic.match(
                    /(?:转发到|目标|服务器)[^\d]{0,6}(\d{1,3}(?:\.\d{1,3}){3})[:：](\d{2,5})/,
                );
                if (m) {
                    state.fwd.conn["ip"] = m[1];
                    state.fwd.conn["port"] = Number(m[2]);
                    progress = true;
                } else {
                    const r = await llm_json(
                        "connection_prompt.txt",
                        {
                            side: "forward",
                            protocol: state.fwd.protocol,
                            config_fields: JSON.stringify(
                                Object.entries(
                                    entry_of_side(state.fwd, "reader")?.entry?.config_schema
                                        ?.fields ?? {},
                                ).map(([name, f]) => ({
                                    name,
                                    required:
                                        (f as { default?: unknown }).default === undefined,
                                    description:
                                        (f as { description?: string }).description ?? "",
                                })),
                            ),
                            connection_hints: JSON.stringify(
                                (entry_of_side(state.fwd, "reader")?.entry?.prompt_hints as
                                    | Record<string, unknown>
                                    | undefined)?.["connection_hints"] ?? [],
                            ),
                        },
                        semantic,
                    );
                    if (r && typeof r["connection"] === "object" && r["connection"] !== null) {
                        Object.assign(
                            state.fwd.conn,
                            r["connection"] as Record<string, unknown>,
                        );
                        progress = true;
                    }
                }
            }
            if (state.fwd.deviceName === null) {
                const tm = semantic.match(/(?:转发到|发送到|上传到|推送[^\s]{0,6})\s*([^\s，。,：:]{2,20})/);
                if (tm && !/^\d/.test(tm[1])) state.fwd.deviceName = tm[1];
            }
        }

        // influxdb 连接参数（url/token/org/bucket 的确定性捕获：消息中显式给出的键值）
        if (state.forwardIntent && state.fwd.protocol === "influxdb") {
            const kv: Array<[RegExp, string]> = [
                [/(?:数据库地址|url)[:：]?\s*(\S+:\/\/\S+)/i, "url"],
                [/(?:数据库地址|url)[:：]?\s*(https?:\/\/\S+)/i, "url"],
                [/token[=：:\s]+(\S+)/i, "token"],
                [/(?:组织名?|org)[:：=\s]+([^\s，。,]+)/i, "org"],
                [/(?:数据库名|bucket|表名)[:：=\s]+([^\s，。,]+)/i, "bucket"],
            ];
            for (const [re, key] of kv) {
                if (state.fwd.conn[key] === undefined) {
                    const m = semantic.match(re);
                    if (m) state.fwd.conn[key] = m[1];
                }
            }
            const measure = semantic.match(/表名[:：=\s]*([^\s，。,]+)/);
            if (measure) state.fwd.conn["measurement"] = measure[1];
        }

        // 归属判定合并（阶段1 出口判据，agent.md「场地判定仲裁规则」）：确定性
        // 地名比对以消息原文为输入（设备名提取会剥掉场站前缀，不能作为比对源，
        // func_test_case 用例 3 回归根因），与 location_prompt 语义判定取更保守方。
        if (state.site && siteTagLlm) {
            const [detTag, llmTag] = await Promise.all([
                Promise.resolve(deterministic_site_tag(semantic, state.site.name)),
                siteTagLlm.then(llm_site_tag),
            ]);
            state.turnSiteCheck = arbitrate_site_tags(detTag, llmTag);
        }

        // 协议锁（§2.4.1）：下游产出非空数据即上锁
        if (state.recv.points && state.recv.points.length > 0) state.locks.receive = true;
        if (state.fwd.points && state.fwd.points.length > 0) state.locks.forward = true;
        return progress;
    }

    // ── 缺口计算（§2.3 ④，含 L1 出口判据）──────────────────
    function side_gaps(
        label: string,
        side: SideDraft,
        role: "writer" | "reader",
    ): { gaps: string[]; recap: string[] } {
        const gaps: string[] = [];
        const recap: string[] = [];
        if (!side.protocol) {
            gaps.push(
                side.protocolRaw
                    ? `「${side.protocolRaw}」暂不支持——${label}协议目前支持：${protocol_candidates(role).join("、")}`
                    : `${label}协议（${label === "转发" ? "数据要转发到哪里、用什么方式" : "设备使用哪种通信方式"}）`,
            );
            // 已收到点表时仍复述已理解内容（4.2.1：解析结果出现在对话文本中）
            if (side.points && side.points.length > 0) {
                recap.push(
                    `${label}点表 ${side.points.length} 个点：${side.points
                        .slice(0, 5)
                        .map((p) => `${String(p["name"] || `地址${p["addr"]}`)}(${String(p["addr"])})`)
                        .join("、")}${side.points.length > 5 ? "等" : ""}`,
                );
            }
            return { gaps, recap };
        }
        recap.push(`${label}协议：${side.protocol}`);
        if (!side.points || side.points.length === 0) {
            gaps.push(`${label}点表（各点的${label === "转发" ? "转发地址" : "地址与类型"}等信息）`);
            return { gaps, recap };
        }
        const svc = find_service_type(registry, normalize_protocol(side.protocol), role);
        const entry = svc ? registry.get_entry(svc) : null;
        const fields: string[] = (entry?.point_schema?.fields ?? []).map(
            (f: { name: string }) => f.name,
        );
        // 推导字段放行（§2.7.1 确定性推导：influxdb 的 measurement/field/type、点名）
        const derivable = new Set<string>(["name", "measurement", "field", "type"]);
        // L1 必填检查对可推导字段放行缺失（方案层填充兜底，§2.7.1）
        const missingFields = fields.filter((f) => !derivable.has(f));
        const missing: string[] = [];
        for (const p of side.points) {
            const miss = missingFields.filter(
                (f) => p[f] === undefined || p[f] === null || p[f] === "",
            );
            if (miss.length > 0) {
                missing.push(`点「${String(p["name"] || p["addr"] || "?")}」缺 ${miss.join("、")}`);
            }
        }
        if (missing.length > 0) {
            gaps.push(`${label}点表字段不完整：${missing.slice(0, 3).join("；")}${missing.length > 3 ? "等" : ""}`);
        }
        // L1：数量对账 + 身份查重/重叠（point_rules 共享契约）
        if (side.declared !== null && side.declared !== side.points.length) {
            gaps.push(
                `${label}点表数量与声明不符：声明 ${side.declared} 个，实际 ${side.points.length} 个`,
            );
        }
        if (svc) {
            const issues = validate_point_table(side.points as never[], {
                required: missingFields,
                label: svc,
            });
            for (const it of issues) gaps.push(`${label}点表问题：${it}`);
        }
        recap.push(
            `${label}点表 ${side.points.length} 个点：${side.points
                .slice(0, 5)
                .map((p) => `${String(p["name"] || `地址${p["addr"]}`)}(${String(p["addr"])})`)
                .join("、")}${side.points.length > 5 ? "等" : ""}`,
        );
        return { gaps, recap };
    }

    function conn_missing(side: SideDraft, role: "writer" | "reader"): string[] {
        return required_config_fields(side, role).filter(
            (k) =>
                side.conn[k] === undefined ||
                side.conn[k] === null ||
                side.conn[k] === "",
        );
    }

    function compute_gaps(state: SessionState): { gaps: string[]; recap: string[] } {
        const gaps: string[] = [];
        const recap: string[] = [];
        if (!state.site) {
            gaps.push("场站名称与缩写（首次接入需要绑定场站，如：场站名称：华能阿拉善，缩写：hnals）");
        } else {
            recap.push(`场站：${state.site.name}`);
        }
        if (state.recv.deviceName) recap.push(`设备：${state.recv.deviceName}`);

        const rg = side_gaps("接入", state.recv, "writer");
        gaps.push(...rg.gaps);
        recap.push(...rg.recap);
        if (state.recv.protocol) {
            const miss = conn_missing(state.recv, "writer");
            if (miss.length > 0) gaps.push(`设备连接信息还差：${miss.join("、")}`);
            else {
                const c = state.recv.conn;
                recap.push(
                    `设备连接：${String(c["ip"] ?? "")}${c["port"] ? `，端口 ${String(c["port"])}` : ""}`,
                );
            }
        }

        if (state.forwardIntent) {
            if (!state.fwd.protocol) recap.push("转发：意向已明确，细节待补充");
            const fg = side_gaps("转发", state.fwd, "reader");
            gaps.push(...fg.gaps);
            recap.push(...fg.recap);
            if (state.fwd.protocol && state.fwd.points) {
                const miss = conn_missing(state.fwd, "reader");
                if (miss.length > 0) gaps.push(`转发目标连接信息还差：${miss.join("、")}`);
            }
        }
        return { gaps, recap };
    }

    // ── 方案层装配（§3.2.0.1，纯代码）──────────────────────
    // 当前 config.json 中的既有转发链路（reader 角色实例，取点数最多者）
    function find_existing_reader_info(state: SessionState): {
        id: string;
        name: string;
        abbr: string;
        protocol: string;
        ip: string | null;
        port: number | null;
        maxAddr: number;
    } | null {
        let current: Record<string, unknown>;
        try {
            current = JSON.parse(readFileSync(cfg.configPath, "utf-8"));
        } catch {
            return null;
        }
        void current;
        let best: {
            id: string;
            name: string;
            abbr: string;
            protocol: string;
            ip: string | null;
            port: number | null;
            maxAddr: number;
        } | null = null;
        for (const [st, list] of Object.entries(current ?? {})) {
            if (st === "c4_shm_manager" || !Array.isArray(list)) continue;
            const entry = registry.get_entry(st);
            if (entry?.role !== "reader") continue;
            for (const inst of list as Array<Record<string, unknown>>) {
                const pts = (inst["points"] ?? []) as Array<Record<string, unknown>>;
                if (pts.length === 0) continue;
                const addrs = pts
                    .map((p) => Number(p["addr"]))
                    .filter((n) => !Number.isNaN(n));
                const maxAddr = addrs.length > 0 ? Math.max(...addrs) : 0;
                const id = String(inst["id"] ?? "");
                const sitePrefix = state.site?.abbr ? `${state.site.abbr}_` : "";
                const abbr = id.startsWith(sitePrefix) ? id.slice(sitePrefix.length) : id;
                if (!best || maxAddr > best.maxAddr) {
                    best = {
                        id,
                        name: String(inst["name"] ?? id),
                        abbr,
                        protocol: entry.protocols?.[0]?.protocol ?? st,
                        ip: typeof inst["ip"] === "string" ? (inst["ip"] as string) : null,
                        port: typeof inst["port"] === "number" ? (inst["port"] as number) : null,
                        maxAddr,
                    };
                }
            }
        }
        return best;
    }

    // 变更流配对查找：writer 实例 id → 引用其数据的 reader 实例（key 前缀匹配）
    function find_reader_for_writer(
        current: Record<string, unknown>,
        writerId: string,
    ): { id: string; service_type: string; maxAddr: number } | null {
        let best: {
            id: string;
            service_type: string;
            hits: number;
            maxAddr: number;
        } | null = null;
        for (const [st, list] of Object.entries(current)) {
            if (st === "c4_shm_manager" || !Array.isArray(list)) continue;
            if (registry.get_entry(st)?.role !== "reader") continue;
            for (const inst of list as Array<Record<string, unknown>>) {
                let hits = 0;
                const addrs: number[] = [];
                for (const p of (inst["points"] ?? []) as Array<Record<string, unknown>>) {
                    const k = String(p["key"] ?? "");
                    const dot = k.indexOf(".");
                    if (dot > 0 && k.slice(0, dot) === writerId) hits++;
                    const a = Number(p["addr"]);
                    if (!Number.isNaN(a)) addrs.push(a);
                }
                if (hits > 0 && (!best || hits > best.hits)) {
                    best = {
                        id: String(inst["id"] ?? ""),
                        service_type: st,
                        hits,
                        maxAddr: addrs.length > 0 ? Math.max(...addrs) : 0,
                    };
                }
            }
        }
        return best
            ? { id: best.id, service_type: best.service_type, maxAddr: best.maxAddr }
            : null;
    }

    async function load_abbr(state: SessionState): Promise<AbbrRegistry> {
        let data_config: Record<string, unknown> | null = null;
        try {
            data_config = JSON.parse(readFileSync(cfg.configPath, "utf-8"));
        } catch {
            // config.json 不存在或损坏 → 记忆库走空/重建分支
        }
        return load_abbr_registry(
            abbrPath,
            data_config as never,
            state.site,
        );
    }

    function plan_device_points(state: SessionState, dev: Record<string, unknown>): void {
        // influxdb 确定性推导（§2.7.1：measurement/field/type 由源点映射，展示中标注）
        const points = dev["points"] as Array<Record<string, unknown>> | undefined;
        if (!points || points.length === 0) return;
        const src = state.recv.points ?? [];
        const TYPE_MAP: Record<string, string> = {
            "0": "bool", "15": "bool",
            "3": "int", "5": "int", "7": "int",
            "4": "uint", "6": "uint", "8": "uint",
            "10": "float", "11": "float",
        };
        for (let i = 0; i < points.length; i++) {
            const p = points[i];
            const srcPt = src[i] ?? {};
            if (p["measurement"] === undefined || p["measurement"] === "") {
                p["measurement"] =
                    (state.fwd.conn["measurement"] as string) ?? state.site?.abbr ?? "data";
                p["_derived"] = "measurement";
            }
            if (p["field"] === undefined || p["field"] === "") {
                p["field"] = String(srcPt["name"] ?? p["name"] ?? `f_${p["addr"] ?? i}`);
                p["_derived"] = "field";
            }
            if (p["type"] === undefined || p["type"] === "") {
                p["type"] = TYPE_MAP[String(srcPt["type"] ?? "")] ?? "float";
                p["_derived"] = "type";
            }
        }
    }

    /** 展平内嵌方案 JSON 的 connection 形状 + 合成缺失的 abbr（直通通道归一化）。 */
    function normalize_embedded_device(item: Record<string, unknown>): void {
        const conn = item["connection"];
        if (conn && typeof conn === "object") {
            Object.assign(item, conn as Record<string, unknown>);
            delete item["connection"];
        }
        if (!item["abbr"]) {
            item["abbr"] = device_abbr_candidate(String(item["name"] ?? "dev"));
        }
        const abbr = String(item["abbr"]);
        if (!/^[a-zA-Z]/.test(abbr)) {
            item["abbr"] = `d${abbr.replace(/[^a-zA-Z0-9_]/g, "")}`;
        }
    }

    async function assemble_access_plan(state: SessionState): Promise<{
        plan: Record<string, unknown>;
        display: string;
        issue?: string;
    } | null> {
        const abbr = await load_abbr(state);
        const siteAbbr = state.site?.abbr ?? "";
        const devName = state.recv.deviceName ?? "接入设备";
        let devAbbr = device_abbr_candidate(devName);
        const conflict = resolve_abbr_conflict(abbr, devAbbr, devName);
        let devId: string;
        if (!conflict.conflict && conflict.existing_id) {
            devId = conflict.existing_id; // 记忆命中：同一设备 → 复用 id（修改语义）
            devAbbr = devId.startsWith(`${siteAbbr}_`)
                ? devId.slice(siteAbbr.length + 1)
                : devId;
        } else {
            if (conflict.conflict) {
                let seq = 2;
                while (abbr.entries.some((e) => e.abbr === `${devAbbr}${seq}`)) seq++;
                devAbbr = `${devAbbr}${seq}`;
            }
            devId = siteAbbr ? `${siteAbbr}_${devAbbr}` : devAbbr;
        }

        const devConn = { ...state.recv.conn };
        const device: Record<string, unknown> = {
            name: devName,
            abbr: devAbbr,
            protocol: state.recv.protocol,
            ...devConn,
            points: state.recv.points ?? [],
        };

        const forward_targets: Array<Record<string, unknown>> = [];
        // 裁定（func_test_case 用例语义）：新增采集点必须同时转发——既有转发链路存在时，
        // 未声明转发意向的追加接入自动沿用该链路（转发地址顺延，方案中标注）
        const existingReader = find_existing_reader_info(state);
        if (!state.forwardIntent && existingReader) {
            const basePoints = state.recv.points ?? [];
            const mirror = basePoints.map((p, i) => ({
                addr: existingReader.maxAddr + 1 + i,
                _derived: "addr",
            }));
            forward_targets.push({
                name: existingReader.name,
                abbr: existingReader.abbr,
                protocol: existingReader.protocol,
                ...(existingReader.ip !== null ? { ip: existingReader.ip } : {}),
                ...(existingReader.port !== null ? { port: existingReader.port } : {}),
                points: mirror,
            });
        }
        if (state.forwardIntent && state.fwd.protocol) {
            const ftName = state.fwd.deviceName ?? "转发目标";
            let ftAbbr = device_abbr_candidate(ftName);
            if (ftAbbr === devAbbr) ftAbbr = `${ftAbbr}fwd`;
            const ftPoints =
                state.fwd.points && state.fwd.points.length > 0
                    ? state.fwd.points.map((p) => ({ ...p }))
                    : // 确定性推导：转发地址未提供时与采集地址一致（方案中标注，确认即批准）
                      (state.recv.points ?? []).map((p) => ({
                            addr: p["addr"],
                            _derived: "addr",
                      }));
            const ft: Record<string, unknown> = {
                name: ftName,
                abbr: ftAbbr,
                protocol: state.fwd.protocol,
                ...state.fwd.conn,
                points: ftPoints,
            };
            if (state.fwd.protocol === "influxdb") {
                plan_device_points(state, ft);
            }
            forward_targets.push(ft);
        }

        // 一一对应强制（agent.md §3.2.1.3b，2026-09-23 裁定）：采集/转发点表必须
        // 数量相等；不等属点表错误——询问澄清修正，不得进入方案确认（不截断、不补齐）
        for (const ft of forward_targets) {
            const fwdPts = (ft as Record<string, unknown>)["points"] as Array<
                Record<string, unknown>
            >;
            const recvCount = (state.recv.points ?? []).length;
            if (fwdPts.length !== recvCount) {
                return {
                    plan: {},
                    display: "",
                    issue:
                        `转发点表与采集点数量不一致：采集 ${recvCount} 个，转发 ${fwdPts.length} 个` +
                        `（转发目标：${String(ft["name"])}）——转发与采集必须按序一一对应。` +
                        `请核对转发点表的数量与顺序后重新提交。`,
                };
            }
        }

        const plan: Record<string, unknown> = {
            site: state.site ?? undefined,
            devices: [device],
            forward_targets,
        };

        // 展示文本（逐条「地址 ↔ 点名」；协议/端口豁免场景）
        const lines: string[] = ["接入方案如下："];
        lines.push(
            `· 场站：${state.site?.name ?? "（未绑定）"}`,
            `· 将新建设备 ${devId}（${devName}）——采用 ${state.recv.protocol} 协议`,
        );
        const c = state.recv.conn;
        lines.push(
            `· 设备连接：${String(c["ip"] ?? "")}${c["port"] ? `，端口 ${String(c["port"])}` : ""}`,
        );
        lines.push(`· 采集点（${state.recv.points?.length ?? 0} 个）：`);
        for (const p of state.recv.points ?? []) {
            lines.push(`    - 地址 ${String(p["addr"])} ↔ ${String(p["name"] || "（未命名）")}`);
        }
        for (const ft of forward_targets) {
            const mirrored = state.forwardIntent ? null : ft;
            lines.push(
                `· ${mirrored ? "沿用既有转发链路" : state.locks.forward ? "使用" : "新建"}转发 ${String(ft["name"])}（${String(ft["protocol"])}）`,
            );
            const fc = ft as Record<string, unknown>;
            if (fc["ip"]) {
                lines.push(
                    `    - 目标 ${String(fc["ip"])}${fc["port"] ? `，端口 ${String(fc["port"])}` : ""}`,
                );
            }
            // 转发对应展示（agent.md §3.2.1.3b）：转发点无自身点名，按序引用采集点
            // 点名逐条对应呈现——禁止把两侧点表作为孤立列表分别罗列
            const pts = fc["points"] as Array<Record<string, unknown>>;
            const recvPts = state.recv.points ?? [];
            lines.push(
                `    - 转发点（${pts.length} 个，与采集点按序一一对应）：`,
            );
            for (let i = 0; i < pts.length; i++) {
                const fp = pts[i];
                const sp = (recvPts[i] ?? {}) as Record<string, unknown>;
                const derived = fp["_derived"] ? "（自动推导）" : "";
                lines.push(
                    `      · 采集 ${String(sp["addr"] ?? "?")}（${String(sp["name"] ?? "") || "（未命名）"}） → 转发 ${String(fp["addr"])}${derived}`,
                );
            }
        }
        lines.push("是否确认执行？请点击下方「确认」按钮；如需取消请点击「取消」。");
        return { plan, display: lines.join("\n") };
    }

    // ── L2 validate_points（§2.7.1，编排器调用）────────────
    async function validate_points_l2(
        svcType: string,
        points: Array<Record<string, unknown>>,
    ): Promise<string | null> {
        if (!svcType || !registry.get_entry(svcType)) return null;
        try {
            const vp = await mcpManager.callToolText(svcType, "validate_points", {
                points,
            });
            const parsed = parse_json<{
                valid: boolean;
                errors: Array<{ message: string }>;
            }>(vp);
            if (parsed && parsed.valid === false && parsed.errors.length > 0) {
                return parsed.errors.map((e) => `· ${e.message}`).join("\n");
            }
        } catch {
            /* 工具不可用（旧二进制）：L1 已兜底 */
        }
        return null;
    }

    // ── 执行层（阶段9 + 事务协议 §3.2.2 / c4_architecture §3.1.2）──
    async function execute_steps(state: SessionState, steps: ServiceStep[]): Promise<string> {
        const lookup = {
            get_entry: (st: string) => registry.get_entry(st),
            service_types: () => registry.getServiceTypes(),
        };
        const services = [...new Set(steps.map((s) => s.service_type))];
        try {
            return await with_config_lock(async () => {
                await begin_config_transaction(
                    cfg.configPath,
                    "接入变更（对话确认触发）",
                    services,
                );
                try {
                    const merged = await merge_config_from_steps(
                        steps,
                        cfg.configPath,
                        lookup,
                    );
                    if (!merged.success) {
                        throw new Error(merged.error ?? "配置合并未通过");
                    }
                    const rr = await run_runtime_stop_start(
                        mcpManager,
                        cfg.instanceId,
                        cfg.configPath,
                        lookup,
                    );
                    if (!rr.success) {
                        throw new Error(rr.abort_reason ?? "服务启动失败");
                    }
                    await clear_pending_marker(cfg.configPath);
                    // id 固化（§3.2.1.3a）：成功执行后把新增实例写入记忆库
                    try {
                        const abbrReg = await load_abbr(state);
                        for (const st of steps) {
                            if (st.action !== "add") continue;
                            const inst = st.instance as Record<string, unknown>;
                            const instId = String(inst["id"] ?? "");
                            if (!instId) continue;
                            const instName = String(inst["name"] ?? instId);
                            const prefix = state.site?.abbr ? `${state.site.abbr}_` : "";
                            const ab = instId.startsWith(prefix)
                                ? instId.slice(prefix.length)
                                : instId;
                            const next = finalize_entry(abbrReg, {
                                id: instId,
                                name: instName,
                                abbr: ab,
                                service_type: st.service_type,
                                role: registry.get_entry(st.service_type)?.role ?? null,
                                description: instName,
                            });
                            abbrReg.entries = next.entries;
                        }
                        await save_abbr_registry(abbrReg, abbrPath);
                    } catch {
                        /* 记忆库写入失败不阻塞接入结果 */
                    }
                    return "接入已完成！数据点已按方案配置并启动，您可以随时查看数据，或继续追加、修改设备。";
                } catch (err) {
                    // §3.2.2 回滚协议：恢复 .prev.1 + 完整 Stop-Start
                    let restored = false;
                    try {
                        const rb = await rollback_config_change(
                            mcpManager,
                            cfg.instanceId,
                            cfg.configPath,
                            lookup,
                        );
                        restored = rb.restored;
                    } catch {
                        // 回滚自身失败 → 保持标记，交 L0 重启收敛
                    }
                    if (restored) await clear_pending_marker(cfg.configPath);
                    // .prev 不可用 → 保留标记（§2.10 降级，交 L0 重启收敛）
                    const raw =
                        err instanceof Error ? err.message : String(err);
                    const friendly = friendly_exec_error(raw);
                    stateWriter.setError(friendly);
                    throw new Error(friendly, { cause: err });
                }
            });
        } catch (err) {
            if (err instanceof ConfigBusyError) {
                return CONFIG_BUSY_MESSAGE;
            }
            throw err;
        }
    }

    // ── 变更流（modify/delete，针对已接入设备）──────────────
    // 确定性变更解析（agent.md §3.2.0 query_abbr_registry 修改/删除入口的确定性路径）：
    // 目标解析（id/名称尾号）+ 常见表述 → 与 change_prompt 相同的 JSON 形状
    function deterministic_change_parse(
        semantic: string,
        devices: Array<Record<string, unknown>>,
    ): Record<string, unknown> | null {
        const norm = (t: string): string =>
            t.toLowerCase().replace(/\s+/g, "").replace(/[#号]/g, "");
        let target: Record<string, unknown> | null = null;
        const idTok = semantic.match(/\b([a-z][a-z0-9]*(?:_[a-z0-9]+)+)\b/i);
        if (idTok) {
            target =
                devices.find(
                    (d) => String(d["id"]).toLowerCase() === idTok[1].toLowerCase(),
                ) ?? null;
        }
        if (!target) {
            const mnum = norm(semantic).match(
                /(\d+)(?:#|号)?(风机|主变|逆变器|测风塔|机组|数据源|变压器|设备)/,
            );
            if (mnum) {
                const cands = devices.filter((d) =>
                    norm(String(d["name"])).includes(`${mnum[1]}${mnum[2]}`),
                );
                if (cands.length === 1) target = cands[0];
            }
        }
        if (!target) {
            for (const d of devices) {
                const nm = norm(String(d["name"]));
                if (nm.length >= 4 && norm(semantic).includes(nm)) {
                    target = d;
                    break;
                }
            }
        }
        if (!target) {
            console.error(`[det] target 未解析 (semantic=${JSON.stringify(semantic.slice(0, 40))}, devices=${JSON.stringify(devices.map((d) => String(d["id"])))})`);
            const deleteish = /停用|删除|移除|删了|删掉/.test(semantic) &&
                !/数据点|采集点|点位|的点|点[（(]|点名|地址\s*\d/.test(semantic);
            if (deleteish) {
                // 明确编号但设备不存在（用例 27：「删除3号风机」）→ 回复不存在；
                // 未指明编号（「把风机都删了」）→ 空 target_id，由上层列清单询问
                const mnum = norm(semantic).match(
                    /(\d+)(?:#|号)?(风机|主变|逆变器|测风塔|机组|数据源|变压器|设备)/,
                );
                return {
                    intent: "delete",
                    target_id: mnum ? `__missing__${mnum[1]}${mnum[2]}` : "",
                    instance_fields: {},
                    point_updates: [],
                    points: [],
                    add_points: [],
                };
            }
            return null;
        }
        const targetId = String(target["id"]);
        const points = (target["points"] ?? []) as Array<Record<string, unknown>>;
        console.error(`[det] target=${targetId} pts=${JSON.stringify(points)} mentions=${/数据点|采集点|点位|的点|点[（(]|点名|地址\s*\d/.test(semantic)}`);

        // 点参数修改："windspeed 的(寄存器)地址(从 1000)改为 1010"
        const puM = semantic.match(
            /([A-Za-z][A-Za-z0-9_]{1,30})\s*的?\s*(?:寄存器)?地址(?:从\s*\d+)?\s*(?:改为|改成|修改为|更新为)\s*(\d{1,6})/,
        );
        if (puM) {
            const pt = points.find(
                (p) => String(p["id"] ?? "").toLowerCase() === puM[1].toLowerCase(),
            );
            if (pt) {
                return {
                    intent: "modify",
                    target_id: targetId,
                    instance_fields: {},
                    point_updates: [{ id: String(pt["id"]), addr: Number(puM[2]) }],
                    points: [],
                    add_points: [],
                };
            }
        }

        // 实例字段修改："IP 改为 192.168.110.5"
        if (/改为|改成|修改|调整|更新/.test(semantic)) {
            const fields: Record<string, unknown> = {};
            const ipM = semantic.match(/IP[^\d.]{0,6}((?:\d{1,3}\.){3}\d{1,3})/i);
            if (ipM) fields["ip"] = ipM[1];
            const portM = semantic.match(/端口[^\d]{0,4}(\d{2,5})/);
            if (portM) fields["port"] = Number(portM[1]);
            if (Object.keys(fields).length > 0) {
                return {
                    intent: "modify",
                    target_id: targetId,
                    instance_fields: fields,
                    point_updates: [],
                    points: [],
                    add_points: [],
                };
            }
        }

        // 整实例删除："停用 2#风机" / "删除 2#风机"（点级删除表述不在此列——
        // 「塔筒温度点(地址1006)」「点位1006」「点名xx」等均为点级线索）
        const mentionsPoint =
            /数据点|采集点|点位|的点|点[（(]|点名|地址\s*\d/.test(semantic);
        if (/停用|删除|移除/.test(semantic) && !mentionsPoint) {
            return {
                intent: "delete",
                target_id: targetId,
                instance_fields: {},
                point_updates: [],
                points: [],
                add_points: [],
            };
        }
        // 点级删除（确定性，2026-09-23）：「删除…塔筒温度点(地址1006)」——按地址
        // 直接定位真实点 id，避免 change_prompt 二次翻译点名造成 id 错位
        if (/停用|删除|移除/.test(semantic) && mentionsPoint) {
            const addrM = semantic.match(/地址[（(：:]?\s*(\d{2,7})/);
            console.error(`[det] addrM=${addrM ? addrM[1] : "null"}`);
            if (addrM) {
                const aid = Number(addrM[1]);
                const pt = points.find((p) => Number(p["addr"]) === aid);
                if (pt && pt["id"]) {
                    return {
                        intent: "delete_points",
                        target_id: targetId,
                        instance_fields: {},
                        point_updates: [],
                        points: [{ id: String(pt["id"]) }],
                        add_points: [],
                    };
                }
                // 地址明确给出但点表中不存在 → 确定性回复「点不存在」（func_test_case
                // 用例 19），不得落入接入草稿空转（2026-09-25）
                const table = points
                    .map((p) => `${String(p["addr"])}（${String(p["name"] ?? "")}）`)
                    .join("、");
                return {
                    intent: "point_not_found",
                    steps: [],
                    display: `没有找到地址 ${aid} 对应的数据点——该点不存在或已被删除。当前点表：${table}。`,
                };
            }
        }
        return null;
    }

    async function build_change_plan(user_text: string): Promise<{
        steps: ServiceStep[];
        display: string;
    } | null> {
        const semantic = clean_user_text(user_text);
        let current: Record<string, unknown> | null;
        try {
            current = JSON.parse(readFileSync(cfg.configPath, "utf-8"));
        } catch {
            current = null;
        }
        if (!current) return null;

        // 汇总已接入设备（id/name/points）
        const devices: Array<Record<string, unknown>> = [];
        for (const [st, list] of Object.entries(current)) {
            if (st === "c4_shm_manager" || !Array.isArray(list)) continue;
            for (const inst of list as Array<Record<string, unknown>>) {
                devices.push({
                    id: inst["id"],
                    name: inst["name"],
                    service_type: st,
                    points: ((inst["points"] ?? []) as Array<Record<string, unknown>>).map(
                        (p) => ({
                            id: p["id"] ?? p["key"],
                            name: p["name"],
                            addr: p["addr"],
                        }),
                    ),
                });
            }
        }
        if (devices.length === 0) return null;

        const embedded = extract_embedded_json(semantic);
        if (
            embedded &&
            (embedded["changes"] !== undefined ||
                embedded["devices"] !== undefined ||
                embedded["forward_targets"] !== undefined)
        ) {
            // 确定性直通：消息内嵌 changes/devices JSON（协议允许的旁路通道）
            if (Array.isArray(embedded["changes"])) {
                return finalize_changes(embedded, current, devices);
            }
            return null; // devices 形状 → 交由确认通道直接执行
        }

        // 确定性解析优先（常见表述），LLM change_prompt 兜底长尾表述
        let r = deterministic_change_parse(semantic, devices);
        if (r === null) {
            r = await llm_json(
                "change_prompt.txt",
                { devices_json: JSON.stringify(devices) },
                semantic,
            );
            if (!r) return null;
        }
        const action = String(r["intent"] ?? "");
        if (action === "point_not_found") {
            return { steps: [], display: String(r["display"] ?? "") };
        }
        if (!action) return null;
        const targetId = String(r["target_id"] ?? "");
        // 明确编号但设备不存在（func_test_case 用例 27）→ 直接回复不存在
        if (targetId.startsWith("__missing__")) {
            return {
                steps: [],
                display: `没有找到您提到的设备——${targetId.replace("__missing__", "")}不存在或从未接入。当前已接入的设备有：${devices
                    .map((d) => String(d["id"]))
                    .join("、")}。`,
            };
        }
        // 模糊删除（func_test_case 用例 28）：意图明确但未指明设备 → 列出清单询问，
        // 不得直接出确认方案，也不得回复"设备不存在"
        if (!targetId && action === "delete") {
            return {
                steps: [],
                display: `您想删除哪一台设备？当前已接入：${devices
                    .map((d) => `${String(d["id"])}（${String(d["name"])}）`)
                    .join("、")}。请明确设备后再确认。`,
            };
        }
        const dev = devices.find((d) => String(d["id"]) === targetId);
        if (!dev) {
            // 意图明确但目标不存在 → 友好错误（4.6.2.5/4.6.3.4，非技术语言）
            return {
                steps: [],
                display: `没有找到您提到的设备——该设备不存在或从未接入。当前已接入的设备有：${devices
                    .map((d) => String(d["id"]))
                    .join("、")}。请确认设备名称后重试。`,
            };
        }
        if (!targetId) {
            return {
                steps: [],
                display: `您想删除哪一台设备？当前已接入：${devices
                    .map((d) => `${String(d["id"])}（${String(d["name"])}）`)
                    .join("、")}。请明确设备后再确认。`,
            };
        }
        const svcType = String(dev["service_type"]);
        const changes: Array<Record<string, unknown>> = [];
        let detail: string;
        if (action === "delete") {
            // 整实例删除：reader 侧成对清理由 merge 级联完成（handle_delete 删除
            // writer 实例后，自动移除引用其 key 的 reader 转发点，reader 变空则
            // 连实例一并删除）——方案层不得重复追加 reader 删除变更（重复会因
            // 实例已被级联移除而"找不到目标"回滚，2026-09-24）。
            changes.push({
                action: "delete",
                service_type: svcType,
                instance: { id: targetId },
            });
            detail = `删除设备 ${targetId} 及其全部数据点（关联转发配置一并清理）`;
                } else if (action === "delete_points") {
            const pts = (r["points"] ?? []) as Array<Record<string, unknown>>;
            if (pts.length === 0) return null;
            const delIds = pts.map((p) => String(p["id"]));
            changes.push({
                action: "delete",
                service_type: svcType,
                instance: { id: targetId },
                points: delIds.map((id) => ({ id })),
            });
            // reader 侧成对删除由 merge 级联完成（handle_delete 级联移除
            // key === writer实例id.点id 的转发点）——方案层不得重复追加
            // reader 变更（重复追加会因点已被级联移除而扑空回滚，2026-09-24）
            detail = `从 ${targetId} 移除点：${delIds.join("、")}`;
        } else if (action === "modify") {
            const fields = (r["instance_fields"] ?? {}) as Record<string, unknown>;
            const pu = (r["point_updates"] ?? []) as Array<Record<string, unknown>>;
            const inst: Record<string, unknown> = { id: targetId, ...fields };
            const ch: Record<string, unknown> = {
                action: "modify",
                service_type: svcType,
                instance: inst,
            };
            if (pu.length > 0) {
                ch["points"] = pu.map((p) => ({ ...p }));
            }
            changes.push(ch);
            const ftxt = Object.entries(fields)
                .map(([k, v]) => `${k} 改为 ${String(v)}`)
                .join("，");
            const ptxt = pu
                .map((p) => `点 ${String(p["id"])} 的参数调整为 ${JSON.stringify(p)}`)
                .join("；");
            detail = `在 ${targetId} 上修改：${[ftxt, ptxt].filter(Boolean).join("；")}`;
        } else if (action === "add_points") {
            const ap = (r["add_points"] ?? []) as Array<Record<string, unknown>>;
            if (ap.length === 0) return null;
            // 点名→id（agent.md §3.2.1.3b）：id 由 change_prompt 翻译/用户原文提供；
            // 缺失且点名非合规英文 → 可读拒绝（系统不自动生成）
            for (const p of ap) {
                const nm = String(p["name"] ?? "").trim();
                let id = String(p["id"] ?? "").trim();
                if (id === "" && IDENTIFIER_RE.test(nm)) id = nm;
                const err = id === "" ? "缺少英文标识 id" : identifier_error(id, "point.id");
                if (err) {
                    return {
                        steps: [],
                        display: `新增点「${nm || "?"}」${err}——请提供英文点名或确认中文名的英文翻译后重试（系统不自动生成）。`,
                    };
                }
                p["id"] = id;
            }
            // addr 数值化（change_prompt 可能产出字符串数字——字符串 addr 会绕过
            // merge 的地址冲突检查，造成同址双点，2026-09-24 用例 18）
            for (const p of ap) {
                if (p["addr"] !== undefined) p["addr"] = Number(p["addr"]);
                if (p["forward_addr"] !== undefined) p["forward_addr"] = Number(p["forward_addr"]);
            }
            changes.push({
                action: "modify",
                service_type: svcType,
                instance: { id: targetId },
                points: ap,
            });
            detail = `给 ${targetId} 增加点：${ap
                .map((p) => `${String(p["name"] ?? "")}(地址 ${String(p["addr"] ?? "?")})`)
                .join("、")}`;
            // 新增采集点必须同时转发（func_test_case 用例 16/20 裁定）：存在既有
            // 转发链路而用户未给出转发地址 → 先询问（不静默顺延、不遗漏转发）；
            // 给出转发地址 → reader 侧成对追加（key = writer 实例 id.点 id）
            const reader = find_reader_for_writer(current, targetId);
            if (reader) {
                const missingForward = ap.filter(
                    (p) => p["forward_addr"] === undefined || p["forward_addr"] === null,
                );
                if (missingForward.length > 0) {
                    return {
                        steps: [],
                        display:
                            `新增点${missingForward
                                .map(
                                    (p) =>
                                        `「${String(p["name"] ?? "")}（地址 ${String(p["addr"] ?? "?")}）」`,
                                )
                                .join("、")}还需要转发地址——` +
                            `当前转发链路 ${reader.id} 的转发地址已用到 ${reader.maxAddr}。` +
                            `请告知每个新增点的转发地址后重试。`,
                    };
                }
            }
            // 地址占用预检（func_test_case 用例 18）：新增点地址与既有采集点相同 →
            // 可读拒绝（转发询问在前——用例 20 场景先问转发，补齐后再验占用）
            const devPts = (dev["points"] ?? []) as Array<Record<string, unknown>>;
            for (const p of ap) {
                const occupied = devPts.find(
                    (q) => Number(q["addr"]) === Number(p["addr"]),
                );
                if (occupied) {
                    return {
                        steps: [],
                        display: `地址 ${String(p["addr"])} 已被点「${String(
                            occupied["name"] ?? occupied["id"] ?? "?",
                        )}」占用，无法新增。请更换地址，或先删除原点后再试。`,
                    };
                }
            }
            const fwdPts = ap
                .filter((p) => p["forward_addr"] !== undefined && p["forward_addr"] !== null)
                .map((p) => ({
                    key: `${targetId}.${String(p["id"])}`,
                    addr: Number(p["forward_addr"]),
                    shm_id: 0,
                }));
            if (fwdPts.length > 0 && reader) {
                changes.push({
                    action: "modify",
                    service_type: reader.service_type,
                    instance: { id: reader.id },
                    points: fwdPts,
                });
                detail += `；成对转发至 ${reader.id}`;
            }
        } else {
            return null;
        }

        if (changes.length === 0) return null;
        const display =
            `变更方案如下：\n· ${detail}\n是否确认执行？请点击下方「确认」按钮；如需取消请点击「取消」。`;
        return {
            steps: changes.map((c) => ({
                action: c["action"] as ServiceStep["action"],
                service_type: c["service_type"] as string,
                instance: c["instance"] as Record<string, unknown>,
                points: (c["points"] ?? []) as ServiceStep["points"],
            })),
            display,
        };
    }

    function finalize_changes(
        embedded: Record<string, unknown>,
        current: Record<string, unknown>,
        devices: Array<Record<string, unknown>>,
    ): { steps: ServiceStep[]; display: string } | null {
        const rawChanges = embedded["changes"] as Array<Record<string, unknown>>;
        const steps: ServiceStep[] = [];
        const details: string[] = [];
        for (const c of rawChanges) {
            const inst = (c["instance"] ?? {}) as Record<string, unknown>;
            const instId = String(inst["id"] ?? "");
            // service_type 以实例真实归属为准（LLM 记忆缺失时的猜测不采信）
            let svc = String(c["service_type"] ?? "");
            const hitDev = devices.find((d) => String(d["id"]) === instId);
            if (instId && hitDev) svc = String(hitDev["service_type"]);
            else if (instId) {
                for (const [st, list] of Object.entries(current)) {
                    if (st === "c4_shm_manager" || !Array.isArray(list)) continue;
                    if ((list as Array<Record<string, unknown>>).some((i) => i["id"] === instId)) {
                        svc = st;
                        break;
                    }
                }
            }
            if (!svc) continue;
            const action = String(c["action"] ?? "");
            if (!["add", "modify", "delete"].includes(action)) continue;
            details.push(`${action} ${svc}.${instId}`);
            steps.push({
                action: action as ServiceStep["action"],
                service_type: svc,
                instance: inst,
                points: ((c["points"] ?? []) as Array<Record<string, unknown>>).map(
                    (p) => ({ ...p }),
                ) as ServiceStep["points"],
            });
        }
        if (steps.length === 0) return null;
        return {
            steps,
            display: `变更方案如下：\n· ${details.join("；")}\n是否确认执行？请点击下方「确认」按钮；如需取消请点击「取消」。`,
        };
    }

    // ── 确认通道执行（§2.8 按钮唯一）───────────────────────
    async function* handle_confirm(user_text: string, state: SessionState): AsyncGenerator<AgentStreamEvent> {
            // ① 确定执行来源：会话方案（含完整转发链信息）优先，内嵌 JSON 直通兜底
            let steps: ServiceStep[] | null = null;
            if (state.accessPlan) {
                if (state.accessPlan.kind === "changes") {
                    steps = state.accessPlan.steps ?? [];
                } else if (state.accessPlan.input) {
                    const result = generate_steps(
                        state.accessPlan.input as never,
                        registry,
                        state.site?.abbr ?? "",
                    );
                    if (!result.fatal) steps = result.steps;
                }
            }
            const embedded = steps === null ? extract_embedded_json(user_text) : null;
            if (embedded) {
                if (Array.isArray(embedded["changes"])) {
                    const current = (() => {
                        try {
                            return JSON.parse(readFileSync(cfg.configPath, "utf-8"));
                        } catch {
                            return null;
                        }
                    })();
                    const devices: Array<Record<string, unknown>> = [];
                    if (current) {
                        for (const [st, list] of Object.entries(current)) {
                            if (st === "c4_shm_manager" || !Array.isArray(list)) continue;
                            for (const inst of list as Array<Record<string, unknown>>) {
                                devices.push({
                                    id: inst["id"],
                                    name: inst["name"],
                                    service_type: st,
                                });
                            }
                        }
                    }
                    const built = current
                        ? finalize_changes(embedded, current, devices)
                        : null;
                    if (built) steps = built.steps;
                } else if (
                    Array.isArray(embedded["devices"]) ||
                    Array.isArray(embedded["forward_targets"])
                ) {
                    // 内嵌 AccessPlan 形状 → 归一化后确定性拆解
                    for (const dev of ((embedded["devices"] ?? []) as Array<Record<string, unknown>>)) {
                        normalize_embedded_device(dev);
                    }
                    for (const ft of ((embedded["forward_targets"] ?? []) as Array<Record<string, unknown>>)) {
                        normalize_embedded_device(ft);
                        // 转发点表缺省 → 与采集点一一映射（addr 相同，确定性推导）
                        if (!Array.isArray(ft["points"]) || (ft["points"] as unknown[]).length === 0) {
                            const srcPts = ((embedded["devices"] ?? []) as Array<Record<string, unknown>>)[0]?.[
                                "points"
                            ] as Array<Record<string, unknown>> | undefined;
                            if (srcPts && srcPts.length > 0) {
                                ft["points"] = srcPts.map((pt) => ({ addr: pt["addr"] }));
                            }
                        }
                        if (String(ft["protocol"]) === "influxdb") plan_device_points(state, ft);
                    }
                    const input = {
                        site: (embedded["site"] ?? state.site ?? undefined) as never,
                        devices: embedded["devices"] as never,
                        forward_targets: (embedded["forward_targets"] ?? []) as never,
                    };
                    const result = generate_steps(
                        input,
                        registry,
                        state.site?.abbr ?? "",
                    );
                    if (result.fatal) {
                        yield {
                            type: "text",
                            content: `方案无法执行：${result.fatal}`,
                        };
                        yield { type: "done" };
                        return;
                    }
                    steps = result.steps;
                }
            }
            if (!steps || steps.length === 0) {
                yield { type: "text", content: "当前没有待执行的接入方案。请先提供设备信息，我生成方案后再确认。" };
                yield { type: "done" };
                return;
            }

            // ② 执行（事务 + 回滚协议）
            state.userConfirmed = true;
            stateWriter.setPhase("executing");
            try {
                const okMsg = await execute_steps(state, steps);
                // 成功：方案被消耗 + 在途态清空（等价新会话，§2.4.2）
                state.accessPlan = null;
                state.userConfirmed = false;
                state.recv = fresh_side();
                state.fwd = fresh_side();
                state.forwardIntent = false;
                state.locks = { receive: false, forward: false };
                stateWriter.setAccessPlan(false);
                stateWriter.setPhase("idle");
                stateWriter.setError(null);
                yield { type: "text", content: okMsg };
            } catch (e) {
                // 回滚不销毁方案（§2.8）：回到方案展示态，按钮重新武装
                state.userConfirmed = false;
                const msg = e instanceof Error ? e.message : String(e);
                yield {
                    type: "text",
                    content: state.accessPlan
                        ? `${msg}\n方案已保留，您可以点击「确认」重试，或点击「取消」结束本次接入。`
                        : msg,
                };
                if (state.accessPlan) yield { type: "button_arm" };
            }
            yield { type: "done" };
    }

    // ── 会话级轻量回复（问候/查询/介绍，确定性，无 LLM）────
    function chit_chat_reply(user_text: string): string | null {
        const t = user_text.trim();
        if (t.length === 0) {
            return "请描述您要接入的设备信息，或上传点表文件，我来帮您完成接入。";
        }
        if (/^(你好|您好|hi|hello|嗨|在吗|早上好|下午好|晚上好)[！!。.~\s]*$/i.test(t)) {
            return "您好！我是数据接入助手，可以帮您把设备数据接入监控系统。您可以直接描述设备信息，或上传点表文件。";
        }
        if (/介绍一下|你能做什么|你会什么|你是谁|能帮.*什么/i.test(t)) {
            return "我可以帮您完成数据接入：理解您对设备的描述或点表文件，生成接入方案供您确认，并在确认后自动完成配置和启动。支持的接入协议有 Modbus、IEC 104、ASFP2 等，也支持把采集到的数据转发到上级平台或写入时序数据库。";
        }
        if (/哪些设备|什么设备|设备.*在.*运行|当前.*设备|已接入/i.test(t)) {
            try {
                const config = JSON.parse(readFileSync(cfg.configPath, "utf-8"));
                const names: string[] = [];
                for (const [st, list] of Object.entries(config)) {
                    if (st === "c4_shm_manager" || !Array.isArray(list)) continue;
                    for (const inst of list as Array<Record<string, unknown>>) {
                        names.push(`${String(inst["name"] ?? inst["id"])}（${st.replace("c4_", "")}）`);
                    }
                }
                return names.length > 0
                    ? `当前已接入的设备：${names.join("、")}。`
                    : "当前还没有已接入的设备。您可以告诉我设备信息或上传点表，我来帮您接入。";
            } catch {
                return "当前还没有已接入的设备。您可以告诉我设备信息或上传点表，我来帮您接入。";
            }
        }
        if (/^(谢谢|感谢|辛苦|再见|拜拜)[！!。.~\s]*$/i.test(t)) {
            return "不客气！如需接入新设备或调整已有设备，随时告诉我。";
        }
        return null;
    }

    // ── 显示意图（对点核验控制面，§3.6.4 确定性等价路径）───
    async function handle_display_intent(user_text: string): Promise<string | null> {
        if (!/显示|展示|查看|看看|瞬时值|实时值/.test(user_text)) return null;
        if (!/点|数据/.test(user_text)) return null;
        const tools = (cfg.displayTools ?? []) as Array<{
            name: string;
            invoke(args: Record<string, unknown>): Promise<string>;
        }>;
        const listTool = tools.find((t) => t.name === "list_points");
        const displayTool = tools.find((t) => t.name === "display_points");
        if (!listTool || !displayTool) return null;
        try {
            const filterMatch = user_text.match(/(?:显示|查看|看看)\s*([^\s，。,的]+)/);
            const filter =
                filterMatch && !/显示|查看|看看|所有|全部|数据|点/.test(filterMatch[1])
                    ? filterMatch[1]
                    : undefined;
            const listed = await listTool.invoke({ filter });
            const parsed = parse_json<{
                count: number;
                points: Array<{ pointKey: string; addr: number; device: string }>;
            }>(listed);
            if (!parsed || parsed.count === 0) {
                return "当前还没有已接入的数据点，无法显示。请先完成设备接入。";
            }
            await displayTool.invoke({ pointKeys: parsed.points.map((p) => p.pointKey) });
            return `已建立显示：共 ${parsed.count} 个数据点，正在按秒刷新。您可以在页面的点位显示卡片中查看实时数值；说「停止显示」即可终止。`;
        } catch {
            return null;
        }
    }

    // ── 主回合（§2.3 缺口驱动回合模型）─────────────────────
    return {
        async *invoke(input: AgentInvokeInput): AsyncGenerator<AgentStreamEvent> {
            const state = draft_of(input.conversationId);
            const msgs = input.messages ?? [];
            const userMsg = msgs.filter((m) => m.role === "user").pop();
            const userText = userMsg ? content_of(userMsg) : "";
            const trimmed = userText.trim();

            // ① 取消检测（§2.4.2 全等匹配，顶层确定性拦截）
            if (CANCEL_WORDS.has(trimmed)) {
                if (state.userConfirmed) {
                    yield {
                        type: "text",
                        content: "当前正在执行接入变更，无法取消。请等待执行完成或回滚。",
                    };
                    yield { type: "done" };
                    return;
                }
                state.recv = fresh_side();
                state.fwd = fresh_side();
                state.forwardIntent = false;
                state.accessPlan = null;
                state.userConfirmed = false;
                state.locks = { receive: false, forward: false };
                state.gapRepeat = 0;
                stateWriter.setPhase("idle");
                stateWriter.setAccessPlan(false);
                yield { type: "button_disarm", reason: "用户取消本次接入" };
                yield {
                    type: "text",
                    content:
                        "好的，已取消本次接入。已确认的信息（场站、设备记忆）已保留，您可以随时重新发起接入。",
                };
                yield { type: "done" };
                return;
            }

            // ② 按钮通道（§2.8）
            if (trimmed.startsWith("[C4_BUTTON_CANCEL]")) {
                state.accessPlan = null;
                state.userConfirmed = false;
                stateWriter.setAccessPlan(false);
                stateWriter.setPhase("idle");
                yield { type: "button_disarm", reason: "用户取消执行" };
                yield { type: "text", content: "好的，已取消本次接入方案，未做任何变更。" };
                yield { type: "done" };
                return;
            }
            if (trimmed.startsWith("[C4_BUTTON_CONFIRM]")) {
                yield* handle_confirm(userText, state);
                return;
            }
            if (state.userConfirmed) {
                // 确认态的普通消息：确认是硬边界，继续等待按钮而非执行
                state.userConfirmed = false;
            }

            // ③ 轻量回复分叉（问候/查询/介绍——不进入接入流程）
            const chit = chit_chat_reply(userText);
            if (chit !== null && !state.recv.points && !state.accessPlan) {
                stateWriter.setPhase("idle");
                yield { type: "text", content: chit };
                yield { type: "done" };
                return;
            }

            // ④ 显示意图分叉（对点核验，§3.6）
            const displayReply = await handle_display_intent(userText);
            if (displayReply !== null) {
                yield { type: "text", content: displayReply };
                yield { type: "done" };
                return;
            }

            stateWriter.setPhase("collecting");

            // ⑤ 变更流分叉（modify/delete，已接入设备的调整）
            if (CHANGE_INTENT_RE.test(userText) && !/^(接入|解析)/.test(trimmed)) {
                console.error(`[route] change-intent hit: ${JSON.stringify(trimmed.slice(0, 50))}`);
                const change = await build_change_plan(userText);
                console.error(
                    `[route] build_change_plan -> ${change === null ? "null" : `steps=${change.steps.length} display=${JSON.stringify(change.display.slice(0, 80))}`}`,
                );
                if (change !== null) {
                    if (change.steps.length === 0) {
                        // 目标不存在的友好错误
                        yield { type: "text", content: change.display };
                        yield { type: "done" };
                        return;
                    }
                    state.accessPlan = {
                        kind: "changes",
                        steps: change.steps,
                        display: change.display,
                    };
                    stateWriter.setAccessPlan(true);
                    stateWriter.setPhase("planning");
                    yield { type: "text", content: change.display };
                    yield { type: "button_arm" };
                    yield { type: "done" };
                    return;
                }
            }

            // ⑥ 阶段提取（1-7）
            let file_data: string | null = null;
            let file_error = false;
            const pathM = userText.match(/path=([^\s,，]+)/);
            if (pathM) {
                if (existsSync(pathM[1])) {
                    try {
                        const parsed = parse_any_file(pathM[1]);
                        file_data = parsed.length > 8000 ? parsed.slice(0, 8000) : parsed;
                    } catch {
                        file_error = true;
                    }
                } else {
                    file_error = true;
                }
            }
            if (file_error) {
                const sem = clean_user_text(userText);
                if (/解析|文件/.test(sem) && sem.length <= 30) {
                    yield {
                        type: "text",
                        content:
                            "您上传的文件无法解析，可能是文件已损坏或内容不是有效的点表。请检查后重新上传。",
                    };
                    yield { type: "done" };
                    return;
                }
            }
            let extraction_progress = false;
            try {
                extraction_progress = await run_stage_extraction(userText, file_data, state);
            } catch (e) {
                cfg.agentLogger.error(
                    input.conversationId ?? "orchestrator",
                    `阶段提取异常: ${e instanceof Error ? e.message : String(e)}`,
                );
            }

            // ⑥.5 场站归属判定（§3.2.0 阶段1 出口判据）
            if (state.turnSiteCheck === "other") {
                yield {
                    type: "text",
                    content: `该资料不属于当前场站（${state.site?.name ?? ""}），本次接入已停止。如确需接入其他场站的设备，请先更换部署配置中的场站绑定。`,
                };
                yield { type: "done" };
                return;
            }
            if (state.turnSiteCheck === "ambiguous") {
                yield {
                    type: "text",
                    content: `该资料标注的场站归属不明确，请确认：这些数据是否属于当前场站「${state.site?.name ?? ""}」？确认后请重新发起接入。`,
                };
                yield { type: "done" };
                return;
            }

            // ⑦ 缺口计算 + 聚合提问（提问即终局，§2.6）
            const { gaps, recap } = compute_gaps(state);
            if (gaps.length > 0) {
                const signature = gaps.join("|");
                if (extraction_progress) {
                    // 本回合有实质进展 → 连续追问计数归零（重发/补充信息均不算空转）
                    state.gapRepeat = 0;
                    state.lastGapSignature = null;
                } else {
                    state.gapRepeat =
                        signature === state.lastGapSignature ? state.gapRepeat + 1 : 0;
                }
                state.lastGapSignature = signature;
                // 方案失效传播（§2.5）：信息仍在补充 → 撤销按钮
                if (state.accessPlan) {
                    state.accessPlan = null;
                    stateWriter.setAccessPlan(false);
                    yield { type: "button_disarm", reason: "接入信息变更，原方案失效" };
                }
                if (state.gapRepeat >= 2) {
                    // 连续追问上限：同一缺口两轮未收敛 → 强制收摊（§2.6）
                    state.gapRepeat = 0;
                    yield {
                        type: "text",
                        content: `这些信息我连续几轮没能确认到，先为您收个尾：\n${gaps
                            .map((g) => `· ${g}`)
                            .join(
                                "\n",
                            )}\n您可以回复「取消」重新开始，或补充上述信息后再继续。`,
                    };
                    yield { type: "done" };
                    return;
                }
                const head =
                    recap.length > 0
                        ? `本次接入目前已确认：\n${recap.map((r) => `· ${r}`).join("\n")}\n`
                        : "";
                const question = `${head}还需要补充以下信息：\n${gaps
                    .map((g) => `· ${g}`)
                    .join("\n")}\n请提供后我将继续。`;
                yield { type: "text", content: question };
                yield { type: "done" };
                return;
            }
            state.gapRepeat = 0;

            // ⑧ 方案层（阶段8，纯代码）：装配（含确定性推导）→ L2 同源校验 → 展示
            //（§2.7.1：推导填充发生在 L2 之前的方案层，故先装配后校验）
            const assembled = await assemble_access_plan(state);
            if (assembled === null) {
                yield {
                    type: "text",
                    content: "方案装配出现问题，请补充或确认设备信息后重试。",
                };
                yield { type: "done" };
                return;
            }
            if (assembled.issue) {
                // 点表错误（如采集/转发数量不等）——询问澄清修正，不进入方案确认
                yield { type: "text", content: assembled.issue };
                yield { type: "done" };
                return;
            }
            {
                const devices = (assembled.plan["devices"] ?? []) as Array<Record<string, unknown>>;
                const recvProto = String(devices[0]?.["protocol"] ?? "");
                const recvSvc = recvProto
                    ? find_service_type(registry, normalize_protocol(recvProto), "writer")
                    : null;
                const l2Recv = await validate_points_l2(
                    recvSvc ?? "",
                    (devices[0]?.["points"] ?? []) as Array<Record<string, unknown>>,
                );
                if (l2Recv) {
                    yield {
                        type: "text",
                        content: `点表校验未通过：\n${l2Recv}\n请修正后重新提交。`,
                    };
                    yield { type: "done" };
                    return;
                }
                const fts = (assembled.plan["forward_targets"] ?? []) as Array<
                    Record<string, unknown>
                >;
                for (const ft of fts) {
                    const ftProto = String(ft["protocol"] ?? "");
                    const ftSvc = ftProto
                        ? find_service_type(registry, normalize_protocol(ftProto), "reader")
                        : null;
                    const l2Fwd = await validate_points_l2(
                        ftSvc ?? "",
                        (ft["points"] ?? []) as Array<Record<string, unknown>>,
                    );
                    if (l2Fwd) {
                        yield {
                            type: "text",
                            content: `转发点表校验未通过：\n${l2Fwd}\n请修正后重新提交。`,
                        };
                        yield { type: "done" };
                        return;
                    }
                }
            }
            state.accessPlan = { kind: "add", input: assembled.plan, display: assembled.display };
            stateWriter.setAccessPlan(true);
            stateWriter.setPhase("planning");
            // 方案展示含 abbr 绑定（§4.5.3 确认文本列「将新建设备 hnals_wt1」）
            yield { type: "text", content: assembled.display };
            yield { type: "button_arm" };
            yield { type: "done" };
        },
    };
}

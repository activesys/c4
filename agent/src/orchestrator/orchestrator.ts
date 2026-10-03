// c4/agent/src/orchestrator/orchestrator.ts — Workflow 编排器
// agent.md §1.4-§3.2：缺口驱动九阶段流水线。
//   回合循环: ①取消检测 ②阶段提取(1-7，提示词驱动) ③缺口计算(单缺口顺序提问停等)
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
    save_abbr_registry,
    retrieve_device,
    resolve_prefix_conflict,
    next_dev_prefix,
    finalize_entry,
    delete_entry,
    channel_watermark_from_config,
    type AbbrEntry,
    type AbbrRegistry,
} from "../registry/abbr_registry.js";
import { validate_point_table, derive_point_id } from "../executor/point_rules.js";
import {
    generate_steps,
    find_service_type,
    normalize_protocol,
} from "../subagents/tools/output_plan_steps.js";
import {
    IDENTIFIER_RE,
    LISTENER_SERVICES,
    MAX_IDENTIFIER_LENGTH,
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
import { zh_start_address } from "./zh_numeral.js";
import { device_prefix_candidate } from "../registry/device_prefix.js";
import {
    ask_conn,
    ask_points,
    ask_protocol,
    bind_bare,
    bind_change_answer,
    bind_device_answer,
    is_forward_mirror_answer,
    parse_bare_value,
    parse_receive_port,
    type Gap,
} from "./gap_question.js";
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
    /** 转发点表等价应答已受理（「与接收侧一致」）→ 不设缺口，方案层确定性推导=采集地址 */
    pointsMirror: boolean;
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
        pointsMirror: false,
    };
}

interface AccessPlan {
    kind: "add" | "changes";
    input?: Record<string, unknown>;
    steps?: ServiceStep[];
    display: string;
    /** 注册表固化载荷（§3.2.1.3a 第 4 步）：merge 与 Stop-Start 全部成功后写入——
     *  回滚发生时注册表尚未写入，无幽灵条目问题；取消（执行前）同样不触碰注册表 */
    registryWrites?: {
        upserts: AbbrEntry[];
        deletes: string[];
        pointMapDrops: Array<{ prefix: string; keys: string[] }>;
        channelHighWatermark: number;
    };
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
    /** 上一回合提问的缺口键（§2.6 单缺口顺序提问；裸值兜底绑定的目标） */
    pendingGap: string | null;
    /** 累积用户消息原文（catch-up 补提取，2026-09-27 用例6）：闸门后到时对累积文本补跑 */
    userTexts: string[];
    /** 会话内最近一次上传文件的解析结果（2026-09-29 用例11 缺陷B）：file_data 原为回合
     *  局部变量，协议等闸门后到时补提取拿不到已上传的点表，导致重复追问。新上传覆盖 */
    fileTable: string | null;
    /** 变更流程追问中（缺点名/缺转发地址/待选设备）——应答回合强制走变更分叉 */
    pendingChangeAsk: boolean;
    /** 同名多候选消歧应答语境（§3.2.1.3a「以点 key 前缀指认目标」）——应答回合
     *  接受裸前缀 token 指认（确定性定目标，不交 LLM 兜底） */
    pendingDisambig: boolean;
    /** 消歧锚（§3.2.1.3a）：已确定性指认的宿主实例 id——锚定后变更解析（含 LLM
     *  兜底）锁定该设备（devices 只含锚），防同名另一台被误选；方案产出/取消/
     *  终态时清除 */
    disambigHostId: string | null;
    /** 同名坚持新增语境（用例 49②）：同名冲突追问后等待用户「坚持新增/改名/取消」
     *  应答——应答含新增语义 → 跳过检索按前缀顺延新增（确定性，不交 LLM）；
     *  方案产出/取消时清除 */
    pendingNewDevice: boolean;
    /** 变更流追加草稿（单调累积，addr 为键——已确认字段不被后续轮次重解析覆盖，
     *  2026-09-27 用例10：轮4 重解析曾丢已确认点名并漏绑转发地址） */
    changeAddPoints: Array<Record<string, unknown>> | null;
    /** 变更流追加草稿的目标设备 id（草稿创建时确定，应答轮复用） */
    changeTargetId: string | null;
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
        pendingGap: null,
        userTexts: [],
        fileTable: null,
        pendingChangeAsk: false,
        pendingDisambig: false,
        disambigHostId: null,
        pendingNewDevice: false,
        changeAddPoints: null,
        changeTargetId: null,
    };
}

// §2.4.2 取消词表——去空白后全等匹配（禁止包含匹配）
const CANCEL_WORDS = new Set(["取消", "算了", "不接了", "放弃", "停止接入"]);

// 同名坚持新增的应答语义（用例 49②）：含新增语义即确认新增同名设备（前缀顺延），
// 由语境（pendingNewDevice）限定生效范围，不影响正常消息流
const NEW_DEVICE_CONFIRM_RE = /新增一台|另一台|就是新增|确认新增|坚持新增|坚持用/;

// 会话累积文本（接入管线内与 state.userTexts 同源；语义层小助手，避免长表达式重复）
function semanticOf(state: { userTexts: string[] }): string {
    return state.userTexts.join("\n");
}

// 转发意图：肯定表述命中且否定表述未命中（"不需要转发/仅采集"不激活转发链）
const FORWARD_ON_RE = /转发|入库|写入|上传|推送|发送到/;
const FORWARD_OFF_RE = /不需要转发|不转发|无需转发|仅采集|只采集|不用转发/;
// 否定短语剥除后再判肯定语境：OFF 短语（「不需要转发」）本身含「转发」字样，直接用
// FORWARD_ON_RE 判定会把拒绝误判为肯定（2026-09-29 用例11 拒绝转发应答处理）
function forward_on_without_off(text: string): boolean {
    return FORWARD_ON_RE.test(
        text.replace(/不需要转发|不转发|无需转发|不用转发|仅采集|只采集/g, ""),
    );
}

// 删除意图的点级线索检测（2026-10-01 fail-safe 重写）：设备名模式先剥除（「10号风机」
// 的编号不是点号），其余任意 点/地址/点名/两位以上数字 均视为点级线索——设备级删除
// 只允许零线索的纯设备表述，识别失败必须交 LLM 兜底或澄清，不得默认成整设备删除
//（「大气压强点」「1013（大风告警点）」两连误删事故的根因即白名单漏句式后短路）
function deletion_pointish(text: string): boolean {
    const t = text.replace(
        /(\d+)(?:#|号)?(风机|主变|逆变器|测风塔|机组|数据源|变压器|设备)/g,
        "",
    );
    return /点|地址|点名|\d{2,7}/.test(t);
}

// catch-up 句级作用域筛选（2026-09-27 用例6）：逐轮提取只看当前消息，闸门后到的
// 信息需对累积文本补提取——但累积文本常同时含接收表与转发表，9a/规则9 的提示词
// 纪律压不住小模型（实测 receive 侧把「点表5000~5009」当接收表提取）。两侧各自
// 只喂本侧句子：转发侧取含转发关键词的句子，接收侧取不含的
function forward_scoped_text(texts: string[]): string {
    if (texts.length === 0) return "";
    return texts
        .join("\n")
        .split(/[。．！!？?]+/)
        .filter((s) => /转发|入库|写入|上传|推送|发送到|送到|发到|送往|送至|目标|服务器/.test(s))
        .join("\n");
}

function receive_scoped_text(texts: string[]): string {
    if (texts.length === 0) return "";
    return texts
        .join("\n")
        .split(/[。．！!？?]+/)
        .filter((s) => !/转发|入库|写入|上传|推送|发送到/.test(s))
        .join("\n");
}

// location_prompt first_access（rule6）产物中的拼音缩写（§3.2.1.3a：华能阿拉善→hnals、
// 开鲁→kl，泛化后缀不计入）：仅收 2~12 位字母数字，其余视为未生成（不猜缩写）
function generated_abbr_of(r: Record<string, unknown> | null): string {
    const abbr =
        r && typeof r["generated_abbr"] === "string" ? (r["generated_abbr"] as string).trim() : "";
    return /^[a-z0-9]{2,12}$/i.test(abbr) ? abbr.toLowerCase() : "";
}

// 转发点表幂等比较（地址骨架）：既有点表与新生成骨架逐位同址 → 无实质进展。
// 累积文本补扫描（fwdScoped）每回合重复命中同一范围时，重复置 progress 会把
// 「连续追问」计数清零（2026-10-02 用例52 实测：缺口收摊永不触发、无进展死循环）。
// 既有点表更丰富（含点名）且地址相同时不覆盖——保留丰富提取结果
function same_addr_points(
    a: Array<Record<string, unknown>> | null,
    b: Array<Record<string, unknown>>,
): boolean {
    if (a === null || a.length !== b.length) return false;
    return a.every((p, i) => p["addr"] === b[i]["addr"]);
}

// 转发点表幂等比较（LLM 提取结果，深比较）：同序同字段视为无实质进展
function same_points_deep(
    a: Array<Record<string, unknown>> | null,
    b: Array<Record<string, unknown>>,
): boolean {
    if (a === null || a.length !== b.length) return false;
    const norm = (pts: Array<Record<string, unknown>>): string =>
        pts.map((p) => JSON.stringify(p, Object.keys(p).sort())).join("\n");
    return norm(a) === norm(b);
}

// 修改/删除意图（针对已接入设备）
const CHANGE_INTENT_RE =
    /不再采集|停用|删除|移除|删了|删掉|去掉|改为|改成|修改|调整|更新|增加.{0,8}点|追加.{0,8}点|添加.{0,8}点|加点|新增点|再加|再添|再写|同步写|多写一份|再映射/;

// point_prompt 9a 条按侧别注入（2026-09-27 用例4 线上事故：receive 侧 prompt 携带
// forward 侧禁抄规则时，消息中的「点表与I区一致」触发词把提取带偏为空数组——
// 侧别限定行压不住小模型的短语模式匹配，必须让触发文案不出现在对侧 prompt 中）
const RECEIVE_SIDE_RULES =
    "receive 侧必须从输入逐点提取接收点表：输入含「addr:点名」「addr是点名」" +
    "「点表X~Y」等点表形态时，禁止返回空数组。";
const FORWARD_SIDE_RULES =
    "forward 侧转发 addr 禁止借用采集点表的地址，只能来自用户对转发侧的明确表述" +
    "（如「点表5000~5009」「从一万开始」）。用户未提供转发地址、或仅给出等价描述" +
    "（「点表与I区/采集/接收一致」）而无具体地址数字时——禁止把采集侧 addr（或任何" +
    "未经用户给出的数字）复制为转发 addr，此时 points 返回空数组 []，reason 注明" +
    "「用户未提供转发地址，需向用户询问」（上游会以缺口追问；等价描述由上游在缺口" +
    "应答层受理，走方案层与采集一致的确定性推导）。" +
    "influxdb 侧：field/measurement/type 仅当用户明确给出时提取（如「写进wind_turbine" +
    "这个measurement」「字段名跟点名对应（windspeed、power…）」「类型统一float」——" +
    "「跟点名对应」= field 取该点英文名/翻译）；用户未给出映射规则时 points 返回空数组 []" +
    "（上游推导 measurement/type、向用户追问 field），禁止编造 field 名。" +
    "例外——输入含 <file_data> 时：该文件是用户针对转发点表的显式提供（用户在被询问" +
    "转发点表时上传），文件中的地址列即用户给出的转发地址，逐行提取为各点的 addr" +
    "（点名/名称列忽略，转发点无点名），此情形不算借用采集地址。";

// ── 小工具 ─────────────────────────────────────────────────

function render_prompt(file: string, params: Record<string, string>): string {
    const tpl = readFileSync(
        path.join(
            path.dirname(new URL(import.meta.url).pathname),
            "..",
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

// ── 设备前缀派生（agent.md §3.2.1.3c，2026-10-01）──────────
// 实现移至 src/registry/device_prefix.ts（方案层与拆解器共用）。

// ── 消歧前缀指认（§3.2.1.3a「以点 key 前缀指认目标」）──────
/**
 * 消歧应答语境下的前缀 token 指认：消息中的英文 token 与注册表前缀精确匹配，
 * 唯一命中 → 返回该设备条目；多命中/无命中 → null（保持消歧态，宁可放过不可错认）。
 * 含下划线的点 key 形态（「删除 wt1_temperature 点」）取首段作前缀候选——完整
 * key 因 `\b` 边界不产 token，首段提取让该形态仍可指认（不可解析时交 LLM 兜底）。
 */
export function parse_disambig_target(
    text: string,
    devices: Array<Record<string, unknown>>,
): Record<string, unknown> | null {
    const toks: string[] = text.toLowerCase().match(/\b[a-z][a-z0-9]{0,23}\b/g) ?? [];
    for (const m of text.toLowerCase().matchAll(/\b([a-z][a-z0-9]{0,23})_[a-z0-9_]/g)) {
        toks.push(m[1]);
    }
    const hits = devices.filter((d) => {
        const pre = String(d["prefix"] ?? "").toLowerCase();
        return pre !== "" && toks.includes(pre);
    });
    return hits.length === 1 ? hits[0] : null;
}

/**
 * 消歧锚解析（§3.2.1.3a「指认的确定性消费」状态机核心，纯函数）。
 * 优先级：**用户新鲜指认 > 陈旧锚**——消歧再询问后用户的改选必须生效，
 * 陈旧锚不得覆盖显式指认（同名两台下反向删除方案的根因即优先级倒置）。
 * 返回新状态三元组：anchor（本回合解析目标）/ pending（语境是否保持）/
 * hostId（锚落盘值；方案产出/取消/终态时由调用方清除）。
 */
export function resolve_disambig_anchor(
    pending: boolean,
    stale_host_id: string | null,
    text: string,
    devices: Array<Record<string, unknown>>,
): { anchor: Record<string, unknown> | null; pending: boolean; hostId: string | null } {
    if (pending) {
        const hit = parse_disambig_target(text, devices);
        if (hit !== null) {
            return { anchor: hit, pending: false, hostId: String(hit["id"]) };
        }
        // 指认失败（消息无候选前缀 token）→ 回落陈旧锚（「已选定」延续语义）
        if (stale_host_id !== null) {
            const stale =
                devices.find((d) => String(d["id"]) === stale_host_id) ?? null;
            return {
                anchor: stale,
                pending: false,
                hostId: stale !== null ? stale_host_id : null,
            };
        }
        return { anchor: null, pending: false, hostId: null };
    }
    if (stale_host_id !== null) {
        const stale =
            devices.find((d) => String(d["id"]) === stale_host_id) ?? null;
        if (stale === null) {
            // 锚设备已不存在（被删/回滚）→ 锚失效
            return { anchor: null, pending: false, hostId: null };
        }
        return { anchor: stale, pending: false, hostId: stale_host_id };
    }
    return { anchor: null, pending: false, hostId: null };
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
    // 本轮 LLM 提取调用序号（invoke_with_logging 每轮清零；仅作日志排序用）
    let turnLlmRound = 0;
    async function llm_json(
        prompt_file: string,
        params: Record<string, string>,
        user_input: string,
        conversation: string,
    ): Promise<Record<string, unknown> | null> {
        const rendered = render_prompt(prompt_file, params);
        cfg.agentLogger.llm_call(conversation, ++turnLlmRound, [
            { role: "system", content: rendered },
            { role: "user", content: `<user_input>\n${user_input}\n</user_input>` },
        ]);
        let sawNonEmpty = false;
        for (let attempt = 0; attempt < 3; attempt++) {
            try {
                const res = await model.invoke([
                    { role: "system", content: rendered },
                    { role: "user", content: `<user_input>\n${user_input}\n</user_input>` },
                ]);
                const text = extract_text(res);
                cfg.agentLogger.llm_text(conversation, text);
                if (text.trim() !== "") sawNonEmpty = true;
                const parsed = parse_json<Record<string, unknown>>(text);
                if (parsed !== null) return parsed;
            } catch (err) {
                if (attempt === 2) {
                    cfg.agentLogger.error(
                        conversation,
                        `LLM 调用失败: ${err instanceof Error ? err.message : String(err)}`,
                    );
                }
            }
        }
        // 连续空响应（3 次全空，glm-4.5-air 偶发，2026-09-27 用例10 实测）——显式记
        // error 事件，调用方据此向用户降级提示而非静默转换话题
        if (!sawNonEmpty) {
            cfg.agentLogger.error(
                conversation,
                `LLM 连续 3 次空响应（${prompt_file}）——提取失败，走调用方降级路径`,
            );
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
        side_exclude?: RegExp,
        side_keep?: RegExp,
    ): string | null {
        const clauses = text.split(/[，,。；;;\n]+/).map((c) => c.trim()).filter((c) => c.length > 0);
        const scoped = clauses.filter(
            (c) =>
                side_keywords.test(c) &&
                !(side_exclude && side_exclude.test(c) && !(side_keep && side_keep.test(c))),
        );
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
    // 转发语境子句——接入侧协议捕获必须排除（「转发采用asfp2协议」含裸「采用/协议」
    // 关键词会通过接入侧过滤，被误记为接入协议。2026-09-28 用例7 实测）
    const FORWARD_CLAUSE_RE = /转发|入库|写入|推送|发送|目标|服务器/;
    // 双侧同时声明（2026-09-29 用例11 循环实测：「接收和转发都是用asfp2协议」单子句
    // 同时含接收与转发表述——被 FORWARD_CLAUSE_RE 整句排除，接收侧的显式声明丢失）。
    // 命中本模式的子句不被转发排除；纯转发子句（「接入成功后转发采用asfp2协议」无
    // 并列/概括词）仍照旧排除，防止转发声明误入接收侧
    const BOTH_SIDES_RE =
        /(?:(?:接收|采集|接入|数据源)[^\d]{0,6}(?:和|与|及|都|均|同时)[^\d]{0,6}(?:转发|发送|上传|入库|推送))|(?:(?:转发|发送|上传|入库|推送)[^\d]{0,6}(?:和|与|及|都|均|同时)[^\d]{0,6}(?:接收|采集|接入|数据源))|都是用|均用/;

    function declared_protocol_token(
        text: string,
        clauseFilter: RegExp,
        clauseExclude?: RegExp,
        clauseKeep?: RegExp,
    ): string | null {
        const clauses = text
            .split(/[，,。；;;\n]+/)
            .map((c) => c.trim())
            .filter(
                (c) =>
                    c.length > 0 &&
                    clauseFilter.test(c) &&
                    !(clauseExclude && clauseExclude.test(c) && !(clauseKeep && clauseKeep.test(c))),
            );
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
        conversation: string,
    ): Promise<boolean> {
        let progress = false;
        const semantic = clean_user_text(user_text);

        // 累积用户文本（catch-up 补提取，2026-09-27 用例6）：逐轮提取只看当前消息，
        // 协议等闸门后到时，先前消息里已给出的点表/连接信息会被永久跳过——原文按序
        // 累积，闸门打开的回合对累积文本补提取。按钮/取消回合不进入本函数，不入缓冲
        // 相邻去重：变更分叉已累积过当前消息时不再重复入缓冲
        if (state.userTexts[state.userTexts.length - 1] !== semantic) {
            state.userTexts.push(semantic);
        }
        const semanticAll = state.userTexts.join("\n");
        const fwdScoped = forward_scoped_text(state.userTexts);
        const recvScoped = receive_scoped_text(state.userTexts);

        // 转发意图 LLM 兜底判定（阶段5-7 激活条件）：仅当关键词快路未命中时发起，
        // 与后续提取阶段并行以摊薄延迟。func_test_case 用例 4：去向表述（「II服务器
        // 地址是…」）不含 FORWARD_ON_RE 关键词，确定性闸门不得静默降级为纯采集方案。
        const fwdIntentLlm =
            FORWARD_ON_RE.test(semantic) || FORWARD_OFF_RE.test(semantic)
                ? null
                : llm_json("forward_intent_prompt.txt", {}, semantic, conversation).catch(() => null);

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

        // 阶段1 场站（§3.2.1.3a：site 存于 agent.json 权威配置；2026-10-02 修订）
        // 未绑定 → 不自动提取，必须显式询问（首次接入契约）：只询问场站名称，缩写由
        // LLM 按拼音首字母自动生成（华能阿拉善→hnals、开鲁→kl）。确定性捕获显式声明
        // 格式（「场站名称：X」，用户自愿提供缩写时仍成对采纳）；缺口追问
        //（pendingGap === "site"）下的自由文本应答交 location_prompt rule6
        //（first_access 模式）兜底提取——提问即上下文锁定，非无询问的自动提取
        if (!state.site) {
            let siteName = "";
            let siteAbbr = "";
            const pair = semantic.match(
                /场站名称[:：]\s*([^\s，,。]{2,20})\s*[，,]?\s*(?:缩写|简称)[:：]\s*([^\s，,。]{1,12})/,
            );
            const nameOnly = pair
                ? null
                : semantic.match(/场站名称\s*(?:[:：]|是|为)\s*([^\s，,。]{2,20})/);
            if (pair) {
                siteName = pair[1];
                siteAbbr = pair[2];
            } else if (nameOnly) {
                siteName = nameOnly[1];
            } else if (state.pendingGap === "site") {
                const r = await llm_json(
                    "location_prompt.txt",
                    { known_site: "（未设置）" },
                    semantic,
                    conversation,
                ).catch(() => null);
                const exSite =
                    r && r["mode"] === "first_access" && typeof r["user_site"] === "string"
                        ? (r["user_site"] as string).trim()
                        : "";
                if (exSite.length >= 2 && exSite.length <= 20) {
                    siteName = exSite;
                    siteAbbr = generated_abbr_of(r);
                }
            }
            if (siteName !== "" && siteAbbr === "") {
                // 有名称无缩写：LLM 按拼音首字母生成，随回复与接入方案展示给用户；
                // 生成失败 → 本回合不固化，缺口保持追问（不猜缩写）
                const r = await llm_json(
                    "location_prompt.txt",
                    { known_site: "（未设置）" },
                    siteName,
                    conversation,
                ).catch(() => null);
                siteAbbr = generated_abbr_of(r);
            }
            if (siteName !== "" && siteAbbr !== "") {
                state.site = { name: siteName, abbr: siteAbbr };
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
        // 纯值片段应答（裸端口/IP/地址范围、协议名）不含任何场站信息，跳过归属
        // 判定——location_prompt 对此类消息的保守误判（rule5 ambiguous）会以
        // 「归属不明确」中断正常应答流（2026-09-27 用例4 e2e 实测：应答「asfp2」
        // 被误判停机两轮）
        const siteFragment =
            semantic.trim().length <= 24 &&
            (parse_bare_value(semantic) !== null ||
                supported_protocol_token(semantic.trim(), registry, "reader"));
        const siteTagLlm = state.site && !siteFragment
            ? llm_json(
                  "location_prompt.txt",
                  { known_site: `${state.site.name}（缩写 ${state.site.abbr}）` },
                  semantic,
                  conversation,
              ).catch(() => null)
            : null;

        // 裸协议名快捷命中（2026-09-29 用例11 循环实测）：整条消息恰为支持列表中的
        // 协议名（应答协议缺口的「asfp2」形态）——L0/L1 要求子句触发词、L2 可能被
        // protocolRaw 关死，裸名曾落在三级漏斗之外使缺口永不闭合。精确等值判定，
        // 含变体（Modbus RTU 等）仍交 L2 按 match_hints 裁决
        const semanticTrim = semantic.trim().toLowerCase();
        if (!state.recv.protocol && !state.locks.receive && semanticTrim.length > 0) {
            const bareRecv = protocol_candidates("writer").find(
                (p) => p.toLowerCase() === semanticTrim,
            );
            if (bareRecv !== undefined) {
                state.recv.protocol = bareRecv;
                state.recv.protocolRaw = null;
                progress = true;
            }
        }
        if (
            state.forwardIntent &&
            !state.fwd.protocol &&
            !state.locks.forward &&
            semanticTrim.length > 0
        ) {
            const bareFwd = protocol_candidates("reader").find(
                (p) => p.toLowerCase() === semanticTrim,
            );
            if (bareFwd !== undefined) {
                state.fwd.protocol = bareFwd;
                state.fwd.protocolRaw = null;
                progress = true;
            }
        }

        // 阶段2 接入协议（L0 确定性：显式声明 token 对齐支持列表 → 别名预匹配 → 提示词）
        if (!state.recv.protocol && semantic.length > 0) {
            const declared = declared_protocol_token(
                semantic,
                /采集|接入|接收|采用|数据源|上传|设备|协议|规约/,
                FORWARD_CLAUSE_RE,
                BOTH_SIDES_RE,
            );
            if (declared !== null && !supported_protocol_token(declared, registry, "writer")) {
                state.recv.protocolRaw = declared;
                progress = true;
            }
        }
        if (!state.recv.protocol && semantic.length > 0) {
            const aliasHit = alias_match_protocol(
                semantic,
                "writer",
                /采集|接入|接收|采用|数据源|上传|设备|协议/,
                FORWARD_CLAUSE_RE,
                BOTH_SIDES_RE,
            );
            if (aliasHit) {
                if (!state.locks.receive || state.recv.protocol === null) {
                    state.recv.protocol = aliasHit;
                    state.recv.protocolRaw = null;
                    progress = true;
                }
            }
        }
        // L2 门不含 protocolRaw（2026-09-29 用例11 循环实测）：raw 有值（曾报不支持
        // 的名字）时关死 LLM 提取器，用户此后正确声明（裸「asfp2」/「接收和转发都是
        // 用asfp2」）无法覆盖，缺口永不闭合——有效声明必须能顶掉无效 raw
        if (!state.recv.protocol && semantic.length > 0) {
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
                conversation,
            );
            if (r) {
                const match = String(r["match"] ?? "not_mentioned");
                const canonical = r["canonical_name"];
                if (match === "matched" && typeof canonical === "string") {
                    if (state.locks.receive && state.recv.protocol && state.recv.protocol !== canonical) {
                        // §2.4.1 锁后变更 → 丢弃（拒绝文案在缺口/回复层）
                    } else {
                        state.recv.protocol = canonical;
                        state.recv.protocolRaw = null;
                        progress = true;
                    }
                } else if (match === "not_in_list") {
                    state.recv.protocolRaw = String(r["user_protocol"] ?? "未知协议");
                    progress = true;
                }
            }
        }
        // 转发意图判定（阶段5-7 激活条件）：关键词快路 + LLM 语义兜底。须先于采集侧
        // 裸 IP 兜底捕获——意图为真时消息中的地址属转发目标，不得误入采集侧连接
        // （func_test_case 用例 4）。
        let fwdIntentHit = FORWARD_ON_RE.test(semantic);
        // 追问应答闸门（2026-09-29 用例11）：上轮问的是「转发协议与目标」（平台要求
        // writer/reader 成对，纯采集无法生效），本轮应答除明确拒绝外一律视为表达转发
        // 意图——纯协议名/纯地址应答不含转发关键词，不能依赖 LLM 意图判定
        if (
            !fwdIntentHit &&
            state.pendingGap === "fwd.required" &&
            !FORWARD_OFF_RE.test(semantic)
        ) {
            fwdIntentHit = true;
        }
        if (!fwdIntentHit && !FORWARD_OFF_RE.test(semantic) && fwdIntentLlm) {
            const v = await fwdIntentLlm;
            let llmHit = v !== null && (v["intent"] === true || v["intent"] === "true");
            // 非转发缺口的应答语境（2026-10-03 用例 47A 实测）：待答「设备叫什么」时
            // 用户答「2」被保守提示词误判 intent=true → 置 forwardIntent 并烧掉
            // progress → pendingGap 应答绑定失效、设备缺口死循环。此语境（pendingGap
            // 为非 fwd.* 缺口且消息不含转发关键词）下 LLM 的肯定判定不采信——
            // 真正的转发意图表述必然含关键词（快路已覆盖），此处只拦误判
            if (
                state.pendingGap !== null &&
                !String(state.pendingGap).startsWith("fwd.") &&
                !FORWARD_ON_RE.test(semantic)
            ) {
                llmHit = false;
            }
            fwdIntentHit = llmHit;
        }
        if (!state.forwardIntent && fwdIntentHit && !FORWARD_OFF_RE.test(semantic)) {
            state.forwardIntent = true;
            progress = true;
        }

        // 连接信息中的 ip/port 兜底捕获（确定性；提示词未覆盖的简写形式）
        if (state.recv.conn["ip"] === undefined) {
            // 带标签 IP 若处于去向语境（「II服务器地址是…」「转发目标地址…」）则属转发
            // 目标，不得误入采集侧连接（func_test_case 用例 4，2026-09-26 recap 实测暴露）；
            // 跳过去向语境命中，取第一个采集语境的带标签 IP
            const ipMatches = [
                ...semantic.matchAll(/(?:IP|ip|地址)[^\d]{0,4}(\d{1,3}(?:\.\d{1,3}){3})/g),
            ];
            for (const m of ipMatches) {
                const idx = m.index ?? 0;
                const ctx = semantic.slice(Math.max(0, idx - 12), idx);
                if (/服务器|转发|目标|去向|II区/.test(ctx)) continue;
                state.recv.conn["ip"] = m[1];
                progress = true;
                break;
            }
            const bareIp = semantic.match(/(\d{1,3}(?:\.\d{1,3}){3})/);
            if (state.recv.conn["ip"] === undefined && bareIp && !/转发|目标/.test(semantic) && !state.forwardIntent) {
                state.recv.conn["ip"] = bareIp[1];
                progress = true;
            }
        }
        if (state.recv.conn["port"] === undefined) {
            // 确定性端口捕获（parse_receive_port）：前缀「端口9001」/后缀「9001端口」
            // /「监听 9001」三种表述，含转发关键词的子句整体排除
            //（2026-09-28 用例7：后缀「监听9001端口」曾漏捕，LLM 兜底也返回空）
            const recvPort = parse_receive_port(semantic);
            if (recvPort !== null) {
                state.recv.conn["port"] = recvPort;
                progress = true;
            }
        }
        // 阶段4 接入连接 LLM 兜底（connection_prompt side=receive）：确定性正则未捕获、
        // 消息疑似含端口/地址表述时交提示词提取（长尾表述如「端口使用9001」）；
        // ip/port 齐全但消息含 schema 其余实例级字段的声明语境（如 iec104「公共地址2」，
        // 2026-10-02 链步37 实测——无提取通道导致用户值被 schema 默认值顶替）时同样发起。
        // 仅填充缺失字段，不覆盖确定性捕获。
        const recvConnFields = Object.keys(
            entry_of_side(state.recv, "writer")?.entry?.config_schema?.fields ?? {},
        );
        const extraConnCtx =
            recvConnFields.filter((f) => f !== "ip" && f !== "port").length > 0 &&
            /公共地址|装置地址|ASDU|源地址/.test(semantic);
        if (
            ((state.recv.conn["ip"] === undefined ||
                state.recv.conn["port"] === undefined) &&
                /端口|port|ip/i.test(semantic)) ||
            extraConnCtx
        ) {
            const r = await llm_json(
                "connection_prompt.txt",
                {
                    side: "receive",
                    protocol: state.recv.protocol ?? "",
                    config_fields: JSON.stringify(
                        Object.entries(
                            entry_of_side(state.recv, "writer")?.entry?.config_schema
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
                        (entry_of_side(state.recv, "writer")?.entry?.prompt_hints as
                            | Record<string, unknown>
                            | undefined)?.["connection_hints"] ?? [],
                    ),
                },
                // catch-up：协议后到时，先前消息里的长尾连接表述按正确 schema 补提取
                //（只喂接收侧句子，防串侧）
                recvScoped.trim() !== "" ? recvScoped : semanticAll,
                conversation,
            );
            if (r && typeof r["connection"] === "object" && r["connection"] !== null) {
                const conn = r["connection"] as Record<string, unknown>;
                // 采纳白名单 = 服务 config_schema 全部字段（连接型协议的实例级字段
                // 如 iec104 common_address 也经此落位）——LLM 只会返回 schema 内字段，
                // 硬编码 ip/port 会把其余用户值静默丢弃（2026-10-02 链步37 实测）
                for (const key of recvConnFields.length > 0
                    ? recvConnFields
                    : ["ip", "port"]) {
                    if (state.recv.conn[key] === undefined && conn[key] !== undefined) {
                        state.recv.conn[key] = conn[key];
                        progress = true;
                    }
                }
            }
        }
        if (state.recv.deviceName === null) {
            const stop = new Set(["使用", "的", "是", "叫", "不", "已", "在", "为", "与", "和"]);
            // (?!信息)：上传信封的固定话术「请解析此文件中的设备信息」会把「信息」
            // 误提为设备名（2026-09-28 上传实测，recap 曾显示「设备：信息」）；
            // (?!IP|ip|地址|端口)：「设备IP是x.x.x.x」的宾语是连接参数不是设备名
            //（2026-10-02 modbus 链步30 实测，设备名曾成「IP是192.168.110.51」）
            const dm = semantic.match(
                // 系动词「叫/名为/名称为」随引导词一并消耗（2026-10-03 用例 47D 实测：
                // 「设备名叫 power_forecast_1」曾把「名叫」捕获为设备名）
                /(?:设备名称|设备)(?:名字叫|名称为|名为|叫)?[:：]?\s*(?!信息|IP|ip|地址|端口|叫|名)([^\s，。,]{2,24})/,
            );
            const dm2 = dm && !stop.has(dm[1].slice(0, 2)) ? dm[1] : null;
            const hm = semantic.match(/接入(?:另一个设备|华能)?[：:]?\s*([^\s，。,]*\d+#\S+)/);
            // 「接入X的数据 / 接入X，」句式（modbus/104 链路常见形态）：取「接入」与
            //「的数据」/逗号之间的完整设备名——含类型词链（「1号风机变桨控制器」
            //「1号主变测控装置」），不得截断为编号+类型词（前缀派生依赖全名）
            const acc =
                semantic.match(/接入([^\s，。，:：]{2,24}?)的数据/) ??
                semantic.match(/接入([^\s，。，:：]{2,24}?)，/);
            // 纯类型词不是设备名（func_test_case 用例 47①：不得以类型词蒙混、不得编造
            // 默认名）——「接入风机的数据」的「风机」无编号/名称信息，交 recv.device
            // 缺口向用户追问（宁可放过不可错绑）。**升压站除外**：单台无编号设备按
            // 设计以类型词指称即合法设备名（§3.2.1.3c 前缀派生 syz，47 追问示例同）
            const TYPE_ONLY_RE =
                /^(?:风机|风电机组|主变|变压器|逆变器|测风塔|机组|光伏|储能|数据源|设备)$/;
            const accName =
                acc &&
                !/^(?:另一个设备|华能)/.test(acc[1]) &&
                !/^\d+#/.test(acc[1]) &&
                !TYPE_ONLY_RE.test(acc[1])
                    ? acc[1]
                    : null;
            // 常见编号表述（func_test_case 用例 1 形态）：「1号风机」「1#风机」
            // 「2号升压站」——编号 + 设备类型词，无「设备名称:」前缀；中文数字
            // （「三号风机」→ 三号）同样命中（§3.2.1.3c L0，编号转换复用 zh_numeral）
            const nm = semantic.match(
                /((?:\d{1,3}|[零一二两三四五六七八九十]{1,3})\s*[#号]\s*(?:风机|主变|升压站|逆变器|测风塔|机组|变压器|数据源))/,
            );
            if (dm2) {
                state.recv.deviceName = dm2;
                progress = true;
            } else if (hm) {
                state.recv.deviceName = hm[1];
                progress = true;
            } else if (accName) {
                state.recv.deviceName = accName;
                progress = true;
            } else if (nm) {
                state.recv.deviceName = nm[1].replace(/\s+/g, "");
                progress = true;
            }
        }

        // 显式否认转发 → 回退（即使用户此前提过）
        // （转发意图的正向判定已在采集侧连接捕获前完成——意图为真时消息中的地址属转发目标）
        if (
            state.forwardIntent &&
            FORWARD_OFF_RE.test(semantic) &&
            !forward_on_without_off(semantic)
        ) {
            state.forwardIntent = false;
            state.fwd = fresh_side();
        }

        // 阶段5 转发协议（L0 别名预匹配 → 提示词）。门不含 protocolRaw（与阶段2 同理，
        // 2026-09-29 用例11：有效声明必须能顶掉曾报不支持的 raw，否则缺口死锁）
        if (state.forwardIntent && !state.fwd.protocol) {
            const declaredF = declared_protocol_token(
                semantic,
                /转发|入库|写入|上传|推送|发送|目标|服务器/,
            );
            if (declaredF !== null && !supported_protocol_token(declaredF, registry, "reader")) {
                state.fwd.protocolRaw = declaredF;
                progress = true;
            }
        }
        if (state.forwardIntent && !state.fwd.protocol) {
            const fwdAlias = alias_match_protocol(semantic, "reader", /转发|入库|写入|上传|推送|发送/);
            if (fwdAlias) {
                state.fwd.protocol = fwdAlias;
                state.fwd.protocolRaw = null;
                progress = true;
            }
        }
        if (state.forwardIntent && !state.fwd.protocol) {
            const r = await llm_json(
                "protocol_prompt.txt",
                {
                    side: "forward",
                    supported_list: JSON.stringify(protocol_candidates("reader")),
                    match_hints: match_hints_payload("reader"),
                },
                semantic,
                conversation,
            );
            if (r) {
                const match = String(r["match"] ?? "not_mentioned");
                const canonical = r["canonical_name"];
                if (match === "matched" && typeof canonical === "string") {
                    state.fwd.protocol = canonical;
                    state.fwd.protocolRaw = null;
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
                // 文件数据通道（2026-09-29 用例11 缺陷B）：本回合新上传优先，否则复用
                // 会话内已解析文件（行737 的草稿重置仍只由当回合新文件触发）——协议等
                // 闸门后到时，补提取不再丢已上传的点表
                const fileTable = file_data ?? state.fileTable;
                const input_text =
                    (fileTable !== null && filePoints === null
                        ? `<file_data>\n${fileTable.slice(0, 4000)}\n</file_data>\n`
                        : "") +
                    // catch-up（2026-09-27 用例6）：协议后到时对接收侧句子补提取，
                    // 不把含转发关键词的句子喂给 receive 提取器（防串侧）
                    (recvScoped.trim() !== "" ? recvScoped : semanticAll);
                const protocol = state.recv.protocol;
                const extract = () =>
                    llm_json(
                        "point_prompt.txt",
                        {
                            side: "receive",
                            protocol,
                            point_fields: JSON.stringify(fields),
                            point_field_hints: hints,
                            side_rules: RECEIVE_SIDE_RULES,
                        },
                        input_text,
                        conversation,
                    );
                let r = await extract();
                // 语义重试兜底（llm_json 只重试解析失败）：①空提取但消息含点表形态
                // （9a 串味，2026-09-26 用例4 实测，temperature=0 仍出现）；②点缺点名或
                // 英文标识 id——两者均由提取层提供（非 schema 强制字段），模型偶发遗漏，
                // 方案层会因缺 id/缺点名拒绝执行（2026-09-27 用例5/用例6 线上实测）
                const incomplete = (
                    pts: Array<Record<string, unknown>> | null,
                ): boolean =>
                    pts !== null &&
                    pts.some(
                        (p) =>
                            typeof p["name"] !== "string" ||
                            p["name"].trim() === "" ||
                            typeof p["id"] !== "string" ||
                            p["id"].trim() === "",
                    );
                let pts =
                    r && Array.isArray(r["points"])
                        ? (r["points"] as Array<Record<string, unknown>>)
                        : null;
                if (
                    ((!pts || pts.length === 0) &&
                        /\d{2,7}\s*[:：是]/.test(semantic)) ||
                    incomplete(pts)
                ) {
                    r = await extract();
                    pts =
                        r && Array.isArray(r["points"])
                            ? (r["points"] as Array<Record<string, unknown>>)
                            : null;
                }
                // 验收：点缺点名/缺 id 不落槽位（§3.2.1.3b 由缺口向用户追问），
                // 防止空名点/错侧点流入方案（2026-09-27 用例6 实测：错侧点表
                // 5000~5009 空名流入方案，确认时才 fatal）
                if (pts && pts.length > 0 && !incomplete(pts)) {
                    state.recv.points = pts;
                    state.recv.declared =
                        typeof r?.["declared_count"] === "number"
                            ? (r?.["declared_count"] as number)
                            : null;
                    progress = true;
                }
            }
        }

        // 阶段6 转发点表：确定性地址范围展开优先——阿拉伯数字范围（「点表5000~5009」）
        // 与自然语言起始地址（「从一万开始」→ 10000 起，func_test_case 用例 5）。用户
        // 显式给出的范围总是采纳（含对既有值的修正，不静默吞）；无范围信息时 influxdb
        // 走确定性推导、其余协议交 LLM——文本重述或 <file_data> 文件通道（2026-09-29
        // 用例11 缺陷C）。point_prompt（side=forward）禁止以采集点表编造转发地址
        //（9a 条），空结果不落槽位，缺失即走缺口追问
        if (state.forwardIntent && state.fwd.protocol && state.recv.points) {
            const n = state.recv.points.length;
            const fwdProtocol = state.fwd.protocol;
            // catch-up（2026-09-27 用例6）：确定性扫描扩展到「含转发关键词的句子」的
            // 累积文本——当前消息优先（显式重述总是采纳），无命中再扫历史转发句，
            // 避免把接收侧点表表述误认成转发地址
            const fwdScopedNow = fwdScoped ? `${semantic}\n${fwdScoped}` : semantic;
            // 转发点表 LLM 提取（首提/重提共用）：输入只能是用户对转发侧的显式表述
            //（文本重述或文件通道），空结果不落槽位
            const extract_fwd_points = async (input: string): Promise<void> => {
                const hit = entry_of_side(state.fwd, "reader");
                if (!hit) {
                    return;
                }
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
                        protocol: fwdProtocol,
                        point_fields: JSON.stringify(fields),
                        point_field_hints: hints,
                        side_rules: FORWARD_SIDE_RULES,
                    },
                    input,
                    conversation,
                );
                const pts =
                    r && Array.isArray(r["points"])
                        ? (r["points"] as Array<Record<string, unknown>>)
                        : null;
                if (pts && pts.length > 0 && !same_points_deep(state.fwd.points, pts)) {
                    state.fwd.points = pts;
                    progress = true;
                }
            };
            const rangeM = fwdScopedNow.match(
                /(?:转发地址|转发点表|点表)[^\d\n]{0,6}(\d{2,7})(?![个点])\s*(?:到|~|-|—|开始)?\s*(\d+)?/,
            );
            const zhStart = rangeM ? null : zh_start_address(fwdScopedNow);
            if (rangeM) {
                const start = Number(rangeM[1]);
                const end = rangeM[2] !== undefined ? Number(rangeM[2]) : start + n - 1;
                const span = end >= start ? end - start + 1 : n;
                const count = Math.min(n, span);
                const pts: Array<Record<string, unknown>> = [];
                for (let i = 0; i < count; i++) pts.push({ addr: start + i });
                if (count === n && !same_addr_points(state.fwd.points, pts)) {
                    state.fwd.points = pts;
                    progress = true;
                }
            } else if (zhStart !== null) {
                const pts: Array<Record<string, unknown>> = [];
                for (let i = 0; i < n; i++) pts.push({ addr: zhStart + i });
                if (!same_addr_points(state.fwd.points, pts)) {
                    state.fwd.points = pts;
                    progress = true;
                }
            } else if (!state.fwd.points) {
                if (state.fwd.protocol === "influxdb") {
                    // 用户显式点表描述优先提取（2026-10-02 用例 38 实测）：「字段名跟
                    // 点名对应（windspeed…）」是用户提供而非推导——确定性骨架先行会
                    // 吞掉首轮语义（field 按裁定不推导，缺口又无法从历史文本闭合，
                    // 死循环）；提取为空（用户真未给映射）→ 回落 §2.7.1 确定性推导
                    // 骨架（measurement/type 由方案层源点映射填充）
                    await extract_fwd_points(fwdScopedNow);
                    if (!state.fwd.points) {
                        state.fwd.points = (state.recv.points ?? []).map((p) => ({
                            addr: p["addr"],
                        }));
                        progress = true;
                    }
                } else {
                    // 文件数据通道（2026-09-29 用例11 缺陷C）：文本无显式范围时注入
                    // <file_data>——被问「转发点表」时上传点表文件是常见应答。仅限本回合
                    // 新上传（2026-09-29 用户裁定：会话内历史文件不得自动充作转发点表，
                    // 否则转发点表缺口被静默跳过、不再询问；接入侧 catch-up 仍可用
                    // state.fileTable，两侧语义不同）
                    if (file_data !== null) {
                        await extract_fwd_points(
                            `<file_data>\n${file_data.slice(0, 4000)}\n</file_data>\n${fwdScopedNow}`,
                        );
                    }
                }
            } else {
                await extract_fwd_points(semantic);
            }
        }

        // 阶段7 转发连接：ip:port 确定性捕获只依赖转发意图（协议未明也要保住去向信息，
        // 用例 4 轮次4「转发地址127.0.0.1:9900」曾因协议未明被整段跳过而丢失）；
        // connection_prompt 提取需注入协议，仍在协议已明时进行。发起条件按 schema
        // required 字段驱动（2026-10-02 用例 39 实测）：influxdb 的必要项是
        // url/token/org/bucket 而非 ip/port——写死 ip/port 判空会对 influxdb 恒真、
        // 每轮空转 LLM 并置 progress，把 conn 键值应答绑定（⑥.6）活活跳过
        if (state.forwardIntent) {
            const fwdRequired = required_config_fields(state.fwd, "reader");
            const fwdConnMissing = fwdRequired.some((k) => {
                const v = state.fwd.conn[k];
                return v === undefined || v === null || v === "";
            });
            if (fwdConnMissing) {
                // catch-up：当前消息无 ip:port 时，扫累积文本中的转发关键词句子
                //（2026-09-27 用例6：目标地址在协议之前的消息里给出）
                const m =
                    semantic.match(
                        /(?:转发到|目标|服务器)[^\d]{0,6}(\d{1,3}(?:\.\d{1,3}){3})[:：](\d{2,5})/,
                    ) ??
                    (fwdScoped
                        ? fwdScoped.match(
                              /(?:转发到|目标|服务器)[^\d]{0,6}(\d{1,3}(?:\.\d{1,3}){3})[:：](\d{2,5})/,
                          )
                        : null);
                if (m) {
                    state.fwd.conn["ip"] = m[1];
                    state.fwd.conn["port"] = Number(m[2]);
                    progress = true;
                } else if (state.fwd.protocol) {
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
                        // catch-up：协议后到时按正确 schema 对转发侧句子补提取（如
                        // influxdb 的 url 在协议之前的消息里给出）
                        fwdScoped.trim() !== "" ? fwdScoped : semanticAll,
                        conversation,
                    );
                    if (r && typeof r["connection"] === "object" && r["connection"] !== null) {
                        // 仅新增字段算进展（2026-10-02 用例 39 实测）：重复提取到已有
                        // 字段（url/token/org）时置 progress 会把「连续追问」计数清零、
                        // 缺失字段（bucket）的收摊永不触发
                        let gotNew = false;
                        for (const [k, v] of Object.entries(
                            r["connection"] as Record<string, unknown>,
                        )) {
                            if (
                                state.fwd.conn[k] === undefined &&
                                v !== undefined &&
                                v !== ""
                            ) {
                                state.fwd.conn[k] = v;
                                gotNew = true;
                            }
                        }
                        if (gotNew) progress = true;
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
    ): { gaps: Gap[]; recap: string[] } {
        const keyPrefix = label === "转发" ? "fwd" : "recv";
        // ask 生成（§2.6 确切格式引导）：协议/点表缺口由 schema 生成确切问题；
        // 校验类缺口（字段不完整/数量不符/点表问题）文案本身已具体，ask 即 text
        const ask_of = (key: string, text: string): string => {
            if (key.endsWith(".protocol")) {
                // 已捕获到不支持协议名时，ask 必须告知「X 不支持」而非通用文案——
                // 2026-09-29 用例11 循环实测：用户答了「abc」只看到千篇一律的
                // 「请提供接入协议」，不知错在哪，回「我已经说过了」无法收敛
                return side.protocolRaw
                    ? text
                    : ask_protocol(label, protocol_candidates(role));
            }
            if (key === `${keyPrefix}.points`) {
                const entry = entry_of_side(side, role);
                const fields = (entry?.entry?.point_schema?.fields ?? []).map(
                    (f: { name: string }) => ({ name: f.name }),
                );
                if (fields.length > 0) {
                    const base = ask_points(label, fields);
                    // 等价应答提示（func_test_case 用例4：「点表与I区一致」是常见表述）
                    return label === "转发"
                        ? `${base}如与接收/采集侧点表一致，直接回复「与接收侧一致」即可。`
                        : base;
                }
            }
            return text;
        };
        const mk = (key: string, text: string): Gap => ({ key, text, ask: ask_of(key, text) });
        const gaps: Gap[] = [];
        const recap: string[] = [];
        // 点标签协议感知（2026-10-02 用例 38 实测）：influxdb 点无 addr（形态为
        // field/measurement/type），写死 `${name}(${addr})` 会渲染成 undefined——
        // 有 addr 按地址形态，无 addr 按 measurement:field 形态展示
        //（如 wind_turbine:windspeed，2026-10-03 用户裁定）
        const pt_label = (p: Record<string, unknown>): string => {
            if (p["addr"] !== undefined) {
                return `${String(p["name"] || `地址${p["addr"]}`)}(${String(p["addr"])})`;
            }
            const f = String(p["field"] ?? p["id"] ?? "?");
            const m = String(p["measurement"] ?? "");
            return m !== "" ? `${m}:${f}` : f;
        };
        if (!side.protocol) {
            gaps.push(
                mk(
                    `${keyPrefix}.protocol`,
                    side.protocolRaw
                        ? `「${side.protocolRaw}」暂不支持——${label}协议目前支持：${protocol_candidates(role).join("、")}`
                        : `${label}协议（${label === "转发" ? "数据要转发到哪里、用什么方式" : "设备使用哪种通信方式"}）`,
                ),
            );
            // 已收到点表时仍复述已理解内容（4.2.1：解析结果出现在对话文本中）
            if (side.points && side.points.length > 0) {
                recap.push(
                    `${label}点表 ${side.points.length} 个点：${side.points
                        .slice(0, 5)
                        .map(pt_label)
                        .join("、")}${side.points.length > 5 ? "等" : ""}`,
                );
            }
            return { gaps, recap };
        }
        recap.push(`${label}协议：${side.protocol}`);
        if (!side.points || side.points.length === 0) {
            if (side.pointsMirror) {
                // 等价应答已受理：不设缺口，方案层确定性推导（转发=采集地址，方案中标注）
                recap.push(`${label}点表：与采集侧一致（方案中标注，确认即批准）`);
            } else {
                gaps.push(
                    mk(
                        `${keyPrefix}.points`,
                        `${label}点表（各点的${label === "转发" ? "转发地址" : "地址与类型"}等信息）`,
                    ),
                );
            }
            return { gaps, recap };
        }
        const svc = find_service_type(registry, normalize_protocol(side.protocol), role);
        const entry = svc ? registry.get_entry(svc) : null;
        const fields: string[] = (entry?.point_schema?.fields ?? []).map(
            (f: { name: string }) => f.name,
        );
        // 推导字段放行（§2.7.1 确定性推导：influxdb 的 measurement/type、点名）。
        // field 已于 2026-10-01 裁定必填、无默认值、不推导（c4_influxdb_client.md §2）——
        // 缺失即走缺口追问，不再放行
        const derivable = new Set<string>(["name", "measurement", "type"]);
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
        // 校验类缺口聚合为单条（2026-10-02 用例 32 口径：逐项指出全部问题）——点表
        // 校验错误是用户已给信息的错误清单，一次性列出供逐项更正；拆成多条走单缺口
        // 顺序提问会把「重复/非法」藏到后续轮次（每轮只发 gaps[0]）
        const problems: string[] = [];
        if (missing.length > 0) {
            problems.push(
                `${label}点表字段不完整：${missing.slice(0, 3).join("；")}${missing.length > 3 ? "等" : ""}`,
            );
        }
        if (side.declared !== null && side.declared !== side.points.length) {
            problems.push(
                `${label}点表数量与声明不符：声明 ${side.declared} 个，实际 ${side.points.length} 个`,
            );
        }
        if (svc) {
            const issues = validate_point_table(side.points as never[], {
                required: missingFields,
                label: svc,
            });
            problems.push(...issues.map((it) => `${label}点表问题：${it}`));
        }
        if (problems.length > 0) {
            gaps.push(mk(`${keyPrefix}.points.fields`, problems.join("\n")));
        }
        recap.push(
            `${label}点表 ${side.points.length} 个点：${side.points
                .slice(0, 5)
                .map(pt_label)
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

    function compute_gaps(state: SessionState): { gaps: Gap[]; recap: string[] } {
        const gaps: Gap[] = [];
        const recap: string[] = [];
        if (!state.site) {
            gaps.push({
                key: "site",
                // §3.2.1.3a（2026-10-02 修订）：只询问场站名称，缩写由 LLM 按拼音首字母
                // 自动生成——不再要求用户按「名称+缩写」成对提供
                text: "场站名称（首次接入需要绑定场站，如：场站名称：华能阿拉善）",
                ask: "请提供场站名称，例如：场站名称：华能阿拉善",
            });
        } else {
            recap.push(`场站：${state.site.name}（缩写 ${state.site.abbr}）`);
        }
        // 设备名称/编号必答缺口（recv.device，§3.2.1.3c）：点 key 前缀依赖设备身份，
        // 依赖序位于场站之后、接入协议之前——名称或编号任一即闭合
        if (state.recv.deviceName) {
            recap.push(`设备：${state.recv.deviceName}`);
        } else {
            gaps.push({
                key: "recv.device",
                text: "设备名称/编号（点 key 前缀由此派生）",
                ask: "这台设备叫什么？（如：2号风机、升压站）",
            });
        }

        const rg = side_gaps("接入", state.recv, "writer");
        gaps.push(...rg.gaps);
        recap.push(...rg.recap);
        if (state.recv.protocol) {
            const miss = conn_missing(state.recv, "writer");
            if (miss.length > 0) {
                gaps.push({
                    key: "recv.conn",
                    text: `设备连接信息还差：${miss.join("、")}`,
                    ask: ask_conn("接入", conn_fields_of(state.recv, "writer", miss)),
                });
            } else {
                const c = state.recv.conn;
                const ipPart = String(c["ip"] ?? "");
                const portPart = c["port"] !== undefined ? `端口 ${String(c["port"])}` : "";
                recap.push(`设备连接：${[ipPart, portPart].filter(Boolean).join("，")}`);
            }
        }

        // 平台硬约束（2026-09-29 用例11 实测）：c4_shm_manager 要求设备配置 writer/reader
        // 成对（CONFIG_MISSING_SECTION），纯采集方案必然执行失败——接收点表就绪而用户无
        // 转发意向、且无既有转发链路可沿用时，转发是必答缺口，不再放行只采集方案
        if (
            state.recv.points !== null &&
            !state.forwardIntent &&
            find_existing_reader_info() === null
        ) {
            gaps.push({
                key: "fwd.required",
                text: "转发协议与转发目标（平台要求数据点必须同时配置转发，不支持只采集）",
                ask: "采集数据需要同时配置转发。请提供转发协议与转发目标，例如：转发采用asfp2协议到127.0.0.1:9900",
            });
        }
        if (state.forwardIntent) {
            if (!state.fwd.protocol) recap.push("转发：意向已明确，细节待补充");
            const fg = side_gaps("转发", state.fwd, "reader");
            gaps.push(...fg.gaps);
            recap.push(...fg.recap);
            if (state.fwd.protocol && (state.fwd.points || state.fwd.pointsMirror)) {
                const miss = conn_missing(state.fwd, "reader");
                if (miss.length > 0) {
                    gaps.push({
                        key: "fwd.conn",
                        text: `转发目标连接信息还差：${miss.join("、")}`,
                        ask: ask_conn("转发", conn_fields_of(state.fwd, "reader", miss)),
                    });
                }
            }
        }
        return { gaps, recap };
    }

    /** 待补连接字段 → 提问字段清单（label 取 schema 描述主体，field_label 内部再截断） */
    function conn_fields_of(
        side: SideDraft,
        role: "writer" | "reader",
        miss: string[],
    ): Array<{ name: string; description?: string }> {
        const schema = (entry_of_side(side, role)?.entry?.config_schema?.fields ??
            {}) as Record<string, { description?: string } | undefined>;
        return miss.map((name) => ({ name, description: schema[name]?.description }));
    }

    // ── 方案层装配（§3.2.0.1，纯代码）──────────────────────
    // 监听型服务集合 LISTENER_SERVICES 自 executor 导入（单一事实源，§3.2.1.3
    // 同端口并入判定与 §3.2.1.6 端口冻结共用同一「监听型」定义）

    /** 注册表高位水印（同步读；旁路路径 channelStart 需要它防已删序号复用）。 */
    function registry_watermark(): number {
        try {
            const reg = JSON.parse(readFileSync(abbrPath, "utf-8")) as {
                channelHighWatermark?: unknown;
            };
            return typeof reg.channelHighWatermark === "number"
                ? reg.channelHighWatermark
                : 0;
        } catch {
            return 0;
        }
    }

    /** 现存实例是否已占用该 id（确认时并发撞号重校验用）。 */
    function config_has_instance(current: Record<string, unknown>, id: string): boolean {
        for (const [, list] of Object.entries(current)) {
            if (!Array.isArray(list)) continue;
            if (
                (list as Array<Record<string, unknown>>).some(
                    (i) => String(i["id"] ?? "") === id,
                )
            ) {
                return true;
            }
        }
        return false;
    }

    function read_current_config(): Record<string, unknown> | null {
        try {
            return JSON.parse(readFileSync(cfg.configPath, "utf-8")) as Record<string, unknown>;
        } catch {
            return null;
        }
    }

    /** 实例点表读取（地址占用方案期预检用）；实例不存在返回空表。 */
    function _instance_points(
        current: Record<string, unknown>,
        instance_id: string,
    ): Array<Record<string, unknown>> {
        for (const [, list] of Object.entries(current)) {
            if (!Array.isArray(list)) continue;
            for (const inst of list as Array<Record<string, unknown>>) {
                if (String(inst["id"] ?? "") === instance_id) {
                    return (inst["points"] ?? []) as Array<Record<string, unknown>>;
                }
            }
        }
        return [];
    }

    /** 监听端口占用查找（含跨服务类型——返回占用了该端口的监听型实例）。 */
    function find_listener_host(
        current: Record<string, unknown>,
        port: number,
    ): { id: string; name: string; service_type: string } | null {
        for (const [st, list] of Object.entries(current)) {
            if (st === "c4_shm_manager" || !Array.isArray(list)) continue;
            if (!LISTENER_SERVICES.has(st)) continue;
            for (const inst of list as Array<Record<string, unknown>>) {
                if (Number(inst["port"]) === port) {
                    return {
                        id: String(inst["id"] ?? ""),
                        name: String(inst["name"] ?? ""),
                        service_type: st,
                    };
                }
            }
        }
        return null;
    }

    /** reader 链路信息（沿用既有转发链路时填 plan；id 为实例句柄，展示层不使用）。 */
    function _reader_chain_info(
        current: Record<string, unknown>,
        readerId: string,
        serviceTypeHint: string,
    ): {
        id: string;
        name: string;
        protocol: string;
        ip: string | null;
        port: number | null;
        maxAddr: number;
    } | null {
        for (const [st, list] of Object.entries(current)) {
            if (st === "c4_shm_manager" || !Array.isArray(list)) continue;
            if (serviceTypeHint !== "" && st !== serviceTypeHint) continue;
            const entry = registry.get_entry(st);
            if (entry?.role !== "reader") continue;
            for (const inst of list as Array<Record<string, unknown>>) {
                if (String(inst["id"] ?? "") !== readerId) continue;
                const pts = (inst["points"] ?? []) as Array<Record<string, unknown>>;
                const addrs = pts
                    .map((p) => Number(p["addr"]))
                    .filter((n) => !Number.isNaN(n));
                return {
                    id: readerId,
                    name: String(inst["name"] ?? readerId),
                    protocol: entry.protocols?.[0]?.protocol ?? st,
                    ip: typeof inst["ip"] === "string" ? (inst["ip"] as string) : null,
                    port: typeof inst["port"] === "number" ? (inst["port"] as number) : null,
                    maxAddr: addrs.length > 0 ? Math.max(...addrs) : 0,
                };
            }
        }
        return null;
    }

    // 当前 config.json 中的既有转发链路（reader 角色实例，取点数最多者）
    function find_existing_reader_info(): {
        id: string;
        name: string;
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
        let best: {
            id: string;
            name: string;
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
                if (!best || maxAddr > best.maxAddr) {
                    best = {
                        id,
                        name: String(inst["name"] ?? id),
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
        devicePrefix = "",
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
                    if (dot > 0 && k.slice(0, dot) === writerId) {
                        // 共享宿主按设备前缀归属 reader（channel1.wt3_* → wt3 设备）：
                        // 仅按 writer 实例 id 匹配会在多台并入设备的转发实例间错落
                        //（2026-10-02 B1 链步54 实测：7010 落到 1号转发实例）
                        if (
                            devicePrefix === "" ||
                            k.slice(dot + 1).startsWith(`${devicePrefix}_`)
                        ) {
                            hits++;
                            const a = Number(p["addr"]);
                            if (!Number.isNaN(a)) addrs.push(a);
                        }
                    }
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
        void state;
        return load_abbr_registry(abbrPath, data_config as never);
    }

    function plan_device_points(state: SessionState, dev: Record<string, unknown>): void {
        // influxdb 确定性推导（§2.7.1：measurement/type 由源点映射，展示中标注）。
        // field 不推导（2026-10-01 裁定必填，由点表/用户提供，缺失走缺口追问）
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
            if (p["type"] === undefined || p["type"] === "") {
                p["type"] = TYPE_MAP[String(srcPt["type"] ?? "")] ?? "float";
                p["_derived"] = "type";
            }
        }
    }

    /** 展平内嵌方案 JSON 的 connection 形状 + 合成缺失的点前缀（直通通道归一化）。 */
    function normalize_embedded_device(item: Record<string, unknown>): void {
        const conn = item["connection"];
        if (conn && typeof conn === "object") {
            Object.assign(item, conn as Record<string, unknown>);
            delete item["connection"];
        }
        if (!item["prefix"]) {
            const cand = device_prefix_candidate(String(item["name"] ?? "dev"));
            // 直通通道（测试/高级用户）无注册表上下文：匿名设备给基名 dev1
            item["prefix"] = cand === "dev" ? "dev1" : cand;
        }
    }

    async function assemble_access_plan(state: SessionState): Promise<{
        plan: Record<string, unknown>;
        display: string;
        registryWrites?: AccessPlan["registryWrites"];
        issue?: string;
    } | null> {
        const abbr = await load_abbr(state);
        const devName = state.recv.deviceName ?? "";
        if (devName === "") {
            // 方案层最终防线（§3.2.1.3c）：设备名未就绪 → 拒绝装配、不出确认按钮
            return {
                plan: {},
                display: "",
                issue: "这台设备叫什么？（如：2号风机、升压站）——设备名称/编号是必答项。",
            };
        }

        // ── 设备身份（§3.2.1.3a id 确定流程 1-2 步的确定性部分）──
        // 同名坚持语境（用例 49②）：同名冲突追问后用户坚持新增同一名称 → 跳过
        // 检索直接走新设备流程（前缀撞名顺延），不交 LLM；应答为取消 → 清语境回落
        const current0 = read_current_config();
        let forceNewDevice = false;
        if (state.pendingNewDevice) {
            if (NEW_DEVICE_CONFIRM_RE.test(semanticOf(state))) {
                state.pendingNewDevice = false;
                forceNewDevice = true;
            } else if (CANCEL_WORDS.has(clean_user_text(semanticOf(state)).trim())) {
                state.pendingNewDevice = false;
            }
        }
        // 检索注册表：同名命中 → 描述匹配仲裁（强仲裁含点表地址证据——描述含设备
        // 名的包含判定在同名检索下恒真，不能区分「同一设备重入」与「另一台同名」，
        // 2026-10-02 B4 实测静默并入）；无命中 → 新设备（前缀确定性派生 + 撞名顺延 /
        // 匿名 dev{N} 序列）；同名多条无法区分 → 追问用户
        let prefix: string;
        let reused: AbbrEntry | null = null;
        if (forceNewDevice) {
            const cand = device_prefix_candidate(devName);
            prefix =
                cand === "dev"
                    ? next_dev_prefix(abbr)
                    : resolve_prefix_conflict(abbr, cand);
        } else {
        const retrieval = retrieve_device(abbr, devName, state.userTexts.join("\n"), {
            addr_evidence: (entry) => {
                // 点表地址证据：本轮提取点表与既有条目宿主上同前缀点表 addr 集一致
                // → 同一设备重入（用例 2 重接入语义）；不一致/无点表 → 非证据
                const newAddrs = (state.recv.points ?? [])
                    .map((p) => Number(p["addr"]))
                    .filter((n) => !Number.isNaN(n));
                if (newAddrs.length === 0) return false;
                const inst = (current0?.[entry.service_type] ?? []) as Array<
                    Record<string, unknown>
                >;
                const hit = inst.find((i) => String(i["id"] ?? "") === entry.host);
                if (!hit) return false;
                const pre = `${entry.prefix}_`;
                const oldAddrs = ((hit["points"] ?? []) as Array<Record<string, unknown>>)
                    .filter((p) => String(p["id"] ?? p["key"] ?? "").startsWith(pre))
                    .map((p) => Number(p["addr"]))
                    .filter((n) => !Number.isNaN(n));
                if (oldAddrs.length !== newAddrs.length) return false;
                const oldSet = new Set(oldAddrs);
                return newAddrs.every((n) => oldSet.has(n));
            },
        });
        if (retrieval.decision === "same_device" && retrieval.entry) {
            reused = retrieval.entry;
            prefix = retrieval.entry.prefix;
        } else if (retrieval.decision === "name_conflict") {
            // 同名冲突（§3.2.1.3a）：列区分建议追问，同时开启「坚持新增」应答语境
            //（用例 49②：用户坚持同名 → 前缀顺延；改口可区分名称/取消 → 正常流程）
            state.pendingNewDevice = true;
            const cands = (retrieval.candidates ?? [])
                .map((c) => `点 key 前缀 ${c.prefix}_（${c.description || "无描述"}）`)
                .join("；");
            return {
                plan: {},
                display: "",
                issue:
                    `已有一台「${devName}」注册在案（${cands}）——现场可能存在同名设备。` +
                    `若要接入的是另一台设备，请用可区分的名称重新说明（如「2号风机」「1号风机B」），` +
                    `或回复「就是新增一台，也叫${devName}」确认新增同名设备（点 key 前缀将顺延）；` +
                    `若要操作的是已有设备，请直接说明要做的变更（如「给${devName}加点」「删除${devName}」）。`,
            };
        } else {
            const cand = device_prefix_candidate(devName);
            prefix =
                cand === "dev"
                    ? next_dev_prefix(abbr)
                    : resolve_prefix_conflict(abbr, cand);
        }
        }

        const svcType = state.recv.protocol
            ? find_service_type(registry, normalize_protocol(state.recv.protocol), "writer")
            : null;
        if (!svcType) {
            return null; // 协议缺口未闭合——缺口层已拦，此处防御
        }
        const current = current0;

        // ── channel 序号分配（§3.2.1.3）：未使用最小序号，全服务类型共享同一序号空间；
        //  高位水印取注册表与现存实例的较大值（永不回退）──
        let highWater = Math.max(
            abbr.channelHighWatermark,
            current ? channel_watermark_from_config(current as never) : 0,
        );
        const alloc_channel = (): string => {
            highWater += 1;
            return `channel${highWater}`;
        };

        // ── Writer 宿主：既有设备复用（修改语义）/ 同端口并入（仅监听型）/ 新建实例 ──
        let writerAction: "add" | "modify" = "add";
        let writerId: string;
        const notes: string[] = [];
        if (reused) {
            // 协议一致性校验：同名设备此前经其他协议/服务类型接入时，modify 目标在
            // 本次服务类型的数组中不存在 → 执行期必然回滚——方案期拦截并引导
            if (reused.service_type !== "" && reused.service_type !== svcType) {
                return {
                    plan: {},
                    display: "",
                    issue:
                        `设备「${devName}」此前通过 ${reused.service_type.replace("c4_", "")} 协议接入，` +
                        `与本次声明的 ${state.recv.protocol} 不一致。如需更换协议，请先删除该设备后重新接入；` +
                        `如协议表述有误，请更正协议名后重试。`,
                };
            }
            writerAction = "modify";
            writerId = reused.host;
        } else if (LISTENER_SERVICES.has(svcType)) {
            const port = Number(state.recv.conn["port"] ?? NaN);
            const host =
                Number.isInteger(port) && current ? find_listener_host(current, port) : null;
            if (host && host.service_type === svcType) {
                // 同端口并入（用户零交互）：点表追加到宿主实例，实例 id 不变
                writerAction = "modify";
                writerId = host.id;
                notes.push(
                    `${devName} 将与${host.name || "既有设备"}共用端口 ${port} 的数据接收服务`,
                );
            } else if (host) {
                // 真冲突：监听端口被跨服务类型占用（绑定必然失败）——拦截前移到方案期
                //（不暴露实例句柄 channel{N}，用户不可见，§3.2.1.3）
                return {
                    plan: {},
                    display: "",
                    issue:
                        `监听端口 ${port} 已被其他数据接收服务（${host.service_type.replace("c4_", "")} 协议）占用，` +
                        `无法在相同端口上再启动 ${state.recv.protocol} 数据接收服务。请更换端口后重试。`,
                };
            } else {
                writerId = alloc_channel();
            }
        } else {
            // 连接型服务不并入——每设备一实例
            writerId = alloc_channel();
        }

        // ── 点 key 生成（§3.2.1.3b 无条件前缀）：{设备前缀}_{裸id}，逐点明示供确认。
        // 复用路径（既有设备加点/重接）先查注册表 pointMap——命中则沿用既有 key
        //（禁止仅凭重新翻译的裸 id 匹配，翻译漂移会误建新点而非更新既有点，
        // §3.2.1.3a/§3.2.1.3b）；未命中（新点或首次接入）按前缀拼接 ──
        const writerPoints: Array<Record<string, unknown>> = [];
        const pointMap: Record<string, string> = {};
        const seenKeys = new Set<string>();
        for (const p of state.recv.points ?? []) {
            const rawId = typeof p["id"] === "string" ? p["id"].trim() : "";
            const nameRaw = typeof p["name"] === "string" ? p["name"].trim() : "";
            const derived = derive_point_id(rawId, nameRaw, IDENTIFIER_RE, MAX_IDENTIFIER_LENGTH);
            const idErr = derived.error ?? identifier_error(derived.id, "point.id");
            if (idErr !== null) {
                return {
                    plan: {},
                    display: "",
                    issue:
                        `点「${nameRaw || String(p["addr"] ?? "?")}」${idErr}` +
                        `——请提供合规英文标识（字母开头，仅字母/数字/下划线）后重试。`,
                };
            }
            const mapped = reused?.pointMap[nameRaw];
            const key = typeof mapped === "string" && mapped !== ""
                ? mapped
                : `${prefix}_${derived.id}`;
            if (seenKeys.has(key)) {
                return {
                    plan: {},
                    display: "",
                    issue:
                        `点 key「${key}」在本次点表中重复（同一设备内部真重名）——` +
                        `请为重复的点名提供不同的英文标识。`,
                };
            }
            seenKeys.add(key);
            const out: Record<string, unknown> = { ...p, id: key };
            delete out["_derived"];
            writerPoints.push(out);
            if (nameRaw !== "") {
                pointMap[nameRaw] = key;
            }
        }

        // 地址占用方案期预检（§3.2.1.3b 唯一性作用域：addr 实例内唯一）——
        // 复用/并入场景新点表与宿主既有点同址不同 key 属录入错误，确认前拦截
        //（否则确认后 merge 才回滚，报错与「修改设备」意图对不上）
        if (writerAction === "modify" && current) {
            const hostPts = _instance_points(current, writerId);
            for (const p of writerPoints) {
                const occ = hostPts.find(
                    (q) =>
                        Number(q["addr"]) === Number(p["addr"]) &&
                        String(q["id"] ?? q["key"] ?? "") !== String(p["id"]),
                );
                if (occ) {
                    return {
                        plan: {},
                        display: "",
                        issue:
                            `地址 ${String(p["addr"])}（点 ${String(p["name"] || p["id"])}）已被宿主上既有点` +
                            `「${String(occ["id"] ?? occ["key"] ?? "?")}」（${String(occ["name"] ?? "")}）占用——` +
                            `两台设备/两张点表的地址重叠会让数据互相覆盖。请调整新点表的地址后重试。`,
                    };
                }
                // 同 key 不同 addr →「更新点地址」与「新增撞名」不可确定性区分
                //（executor 的新增点保护会对后者改名）——方案期消歧，不静默改名
                //（确认即批准原则：用户确认的对象必须是执行将发生的动作）
                const sameKey = hostPts.find(
                    (q) => String(q["id"] ?? q["key"] ?? "") === String(p["id"]),
                );
                if (sameKey && Number(sameKey["addr"]) !== Number(p["addr"])) {
                    return {
                        plan: {},
                        display: "",
                        issue:
                            `点「${String(p["name"] || String(p["id"]))}」（${String(p["id"])}）的地址与既有接入不一致` +
                            `（现有 ${String(sameKey["addr"])}，本次提供 ${String(p["addr"])}）。请明确意图：` +
                            `修改该点地址请回复「取消」后直接说「修改点 ${String(p["id"])} 的地址为 ${String(p["addr"])}」；` +
                            `这是新点请更换英文标识（不得与 ${String(p["id"])} 重名）后重试。`,
                    };
                }
            }
        }

        const devConn = { ...state.recv.conn };
        const device: Record<string, unknown> = {
            name: devName,
            prefix,
            action: writerAction,
            instanceId: writerId,
            protocol: state.recv.protocol,
            ...devConn,
            points: writerPoints,
        };

        const forward_targets: Array<Record<string, unknown>> = [];
        // 裁定（func_test_case 用例语义）：新增采集点必须同时转发——既有转发链路存在时，
        // 未声明转发意向的追加接入自动沿用该链路（转发地址顺延，方案中标注）。
        // 链路优先取引用本宿主的 reader（并入/复用场景成对追加，§3.2.1.3 示例 4）。
        // 例外：链路为 influxdb 时不沿用——field 已裁定必填、不推导（2026-10-01），
        // 镜像点无法确定性补齐 field，沿用会在确认后 fatal；forward_targets 为空时
        // 由下方「转发必答」硬约束 issue 要求用户明确转发（用户声明 influxdb 后走
        // 正常收集，缺 field 由 points.fields 缺口追问）
        if (!state.forwardIntent && current) {
            const hostReader = writerId !== "" ? find_reader_for_writer(current, writerId) : null;
            const info = hostReader
                ? _reader_chain_info(current, hostReader.id, hostReader.service_type)
                : (() => {
                      const best = find_existing_reader_info();
                      return best ? _reader_chain_info(current, best.id, "") : null;
                  })();
            if (info && info.protocol !== "influxdb") {
                // 镜像点只带 addr（_derived 标注）：key 由拆解器按 writer 点 key 与
                // reader 实例 id 确定性生成，方案载荷不携带（避免 reader id 前缀的
                // 误导性 key 字样）
                const mirror = writerPoints.map((p, i) => ({
                    addr: info.maxAddr + 1 + i,
                    _derived: "addr",
                }));
                forward_targets.push({
                    name: info.name,
                    action: "modify",
                    instanceId: info.id,
                    protocol: info.protocol,
                    ...(info.ip !== null ? { ip: info.ip } : {}),
                    ...(info.port !== null ? { port: info.port } : {}),
                    points: mirror,
                });
            }
        }
        if (state.forwardIntent && state.fwd.protocol) {
            const ftName = state.fwd.deviceName ?? "转发目标";
            const ftPoints =
                state.fwd.points && state.fwd.points.length > 0
                    ? state.fwd.points.map((p) => ({ ...p }))
                    : // 确定性推导：转发地址未提供时与采集地址一致（方案中标注，确认即批准）
                      writerPoints.map((p) => ({
                            addr: p["addr"],
                            _derived: "addr",
                      }));
            const ft: Record<string, unknown> = {
                name: ftName,
                action: "add",
                instanceId: alloc_channel(),
                protocol: state.fwd.protocol,
                ...state.fwd.conn,
                points: ftPoints,
            };
            if (state.fwd.protocol === "influxdb") {
                plan_device_points(state, ft);
            }
            forward_targets.push(ft);
        }

        // ── 同目标转发 addr 查重（agent.md §2.7.1 接入层 2，2026-10-02 回填）──
        // 同一转发目标（ip:port）上全部转发实例（含既有实例）的 addr 不得重复——
        // 下游（II区）按 addr 唯一区分数据，两条连接同 addr 即互踩。比较域跨实例
        // 聚合：连接型转发每设备一实例，单实例校验（POINT_DUP）查不到跨实例冲突。
        // 方案期拦截（确认按钮之前），不进入执行
        for (const ft of forward_targets) {
            const ftIp = String(ft["ip"] ?? "");
            const ftPort = Number(ft["port"] ?? NaN);
            if (ftIp === "" || !Number.isInteger(ftPort)) continue;
            const ftSvc = find_service_type(
                registry,
                normalize_protocol(String(ft["protocol"] ?? "")),
                "reader",
            );
            if (!ftSvc) continue;
            const batchAddrs = ((ft["points"] ?? []) as Array<Record<string, unknown>>)
                .map((p) => Number(p["addr"]))
                .filter((n) => !Number.isNaN(n));
            const batchSet = new Set(batchAddrs);
            const seen = new Map<number, string>();
            const existingInsts = ((current?.[ftSvc] ?? []) as Array<Record<string, unknown>>)
                .filter((i) => {
                    if (String(i["id"] ?? "") === String(ft["instanceId"])) return false;
                    return (
                        String(i["ip"] ?? "") === ftIp && Number(i["port"] ?? NaN) === ftPort
                    );
                });
            for (const inst of existingInsts) {
                for (const p of (inst["points"] ?? []) as Array<Record<string, unknown>>) {
                    const a = Number(p["addr"]);
                    if (!Number.isNaN(a)) seen.set(a, String(p["key"] ?? a));
                }
            }
            const clash = batchAddrs.find((a) => seen.has(a));
            if (clash !== undefined) {
                // 不引用既有转发点 key（含实例句柄前缀）——句柄不出现在对话文本
                return {
                    plan: {},
                    display: "",
                    issue:
                        `转发地址 ${clash} 已被发往 ${ftIp}:${ftPort} 的既有转发配置占用——` +
                        `同一转发目标上转发地址必须全局唯一，重叠会使 II区 侧数据互踩。` +
                        `请提供不重叠的转发点表后重试（如 7100~7109）。`,
                };
            }
            // 批次内重复（同一新实例两点半址）由 POINT_DUP（L2/merge 前置）兜底；
            // 此处补跨目标批次互撞（两个新目标同 ip:port）——同批 multiple targets 少见
            if (batchSet.size !== batchAddrs.length) {
                return {
                    plan: {},
                    display: "",
                    issue: `转发点表存在重复地址——同一转发目标上转发地址必须唯一，请检查后重试。`,
                };
            }
        }

        // 平台硬约束兜底（2026-09-29 用例11）：缺转发目标的纯采集方案不得进入确认——
        // 正常路径由 compute_gaps 的 fwd.required 缺口提前追问，此处防两处判定不一致时
        // 漏拦（确认后执行才报 CONFIG_MISSING_SECTION 的体验不可接受）
        if (writerPoints.length > 0 && forward_targets.length === 0) {
            return {
                plan: {},
                display: "",
                issue:
                    "采集数据需要同时配置转发（平台要求数据点必须成对配置转发，不支持只采集）。" +
                    "请提供转发协议与转发目标，例如：转发采用asfp2协议到127.0.0.1:9900。",
            };
        }

        // 一一对应强制（agent.md §3.2.1.3b，2026-09-23 裁定）：采集/转发点表必须
        // 数量相等；不等属点表错误——询问澄清修正，不得进入方案确认（不截断、不补齐）
        for (const ft of forward_targets) {
            const fwdPts = (ft as Record<string, unknown>)["points"] as Array<
                Record<string, unknown>
            >;
            if (fwdPts.length !== writerPoints.length) {
                return {
                    plan: {},
                    display: "",
                    issue:
                        `转发点表与采集点数量不一致：采集 ${writerPoints.length} 个，转发 ${fwdPts.length} 个` +
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

        // 注册表固化载荷（§3.2.1.3a 第 4 步）：执行成功后才写入（execute_steps 消费）
        const registryWrites: NonNullable<AccessPlan["registryWrites"]> = {
            upserts: [
                {
                    name: devName,
                    prefix,
                    host: writerId,
                    service_type: svcType,
                    description: reused?.description || state.userTexts.join("；").slice(0, 120),
                    pointMap,
                },
            ],
            deletes: [],
            pointMapDrops: [],
            channelHighWatermark: highWater,
        };

        // 展示文本（逐条「地址 ↔ 点名 → 点 key」；实例句柄 channel{N} 不出现在对话文本，
        // §3.2.1.3——用户确认的业务对象是设备名与点 key）
        const lines: string[] = ["接入方案如下："];
        lines.push(`· 场站：${state.site?.name ?? "（未绑定）"}`);
        lines.push(
            writerAction === "add"
                ? `· 将新建设备 ${devName}（点 key 前缀 ${prefix}_）——采用 ${state.recv.protocol} 协议`
                : `· 将在已有设备 ${devName}（${prefix}_ 前缀）上追加/更新数据点`,
        );
        for (const note of notes) lines.push(`· ${note}`);
        const c = state.recv.conn;
        const ipPart = String(c["ip"] ?? "");
        const portPart = c["port"] !== undefined ? `端口 ${String(c["port"])}` : "";
        lines.push(`· 设备连接：${[ipPart, portPart].filter(Boolean).join("，")}`);
        lines.push(`· 采集点（${writerPoints.length} 个）：`);
        for (const p of writerPoints) {
            lines.push(
                `    - 地址 ${String(p["addr"])} ↔ ${String(p["name"] || "（未命名）")} → ${String(p["id"])}`,
            );
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
            lines.push(
                `    - 转发点（${pts.length} 个，与采集点按序一一对应）：`,
            );
            for (let i = 0; i < pts.length; i++) {
                const fp = pts[i];
                const sp = (writerPoints[i] ?? {}) as Record<string, unknown>;
                const derived = fp["_derived"] ? "（自动推导）" : "";
                // 转发点标签协议感知：influxdb 点无 addr，按 measurement:field 展示
                //（如 wind_turbine:windspeed，2026-10-03 用户裁定）；其余协议按地址
                const fwdLabel =
                    fp["addr"] !== undefined
                        ? String(fp["addr"])
                        : `${String(fp["measurement"] ?? "")}:${String(fp["field"] ?? "?")}`;
                lines.push(
                    `      · 采集 ${String(sp["addr"] ?? "?")}（${String(sp["name"] ?? "") || "（未命名）"}，${String(sp["id"] ?? "")}） → 转发 ${fwdLabel}${derived}`,
                );
            }
        }
        lines.push("是否确认执行？请点击下方「确认」按钮；如需取消请点击「取消」。");
        return { plan, display: lines.join("\n"), registryWrites };
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
    async function execute_steps(
        state: SessionState,
        steps: ServiceStep[],
        conversation: string,
    ): Promise<string> {
        const lookup = {
            get_entry: (st: string) => registry.get_entry(st),
            service_types: () => registry.getServiceTypes(),
        };
        const services = [...new Set(steps.map((s) => s.service_type))];
        cfg.agentLogger.tool_call(conversation, "apply_config_steps", {
            services,
            changes: steps.map((s) => ({ action: s.action, service_type: s.service_type })),
        });
        try {
            const okMsg = await with_config_lock(async () => {
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
                    // 注册表固化（§3.2.1.3a 第 4 步）：merge 与 Stop-Start 全部成功后写入——
                    // 执行失败回滚 config.json 时不写注册表（避免幽灵条目指向已回滚掉的
                    // 不存在实例）；载荷由方案层/变更流装配时产出（accessPlan.registryWrites）
                    try {
                        const writes = state.accessPlan?.registryWrites;
                        if (
                            writes &&
                            (writes.upserts.length > 0 ||
                                writes.deletes.length > 0 ||
                                writes.pointMapDrops.length > 0)
                        ) {
                            const abbrReg = await load_abbr(state);
                            for (const up of writes.upserts) {
                                const next = finalize_entry(abbrReg, up);
                                abbrReg.entries = next.entries;
                            }
                            for (const pre of writes.deletes) {
                                const next = delete_entry(abbrReg, pre);
                                abbrReg.entries = next.entries;
                            }
                            for (const drop of writes.pointMapDrops) {
                                const entry = abbrReg.entries.find(
                                    (e) => e.prefix === drop.prefix,
                                );
                                if (entry) {
                                    const dropSet = new Set(drop.keys);
                                    for (const [nm, key] of Object.entries(entry.pointMap)) {
                                        if (dropSet.has(key)) {
                                            delete entry.pointMap[nm];
                                        }
                                    }
                                }
                            }
                            abbrReg.channelHighWatermark = Math.max(
                                abbrReg.channelHighWatermark,
                                writes.channelHighWatermark,
                            );
                            await save_abbr_registry(abbrReg, abbrPath);
                            cfg.agentLogger.memory(conversation, "save_abbr_registry", {
                                entries: abbrReg.entries.length,
                                watermark: abbrReg.channelHighWatermark,
                            });
                        }
                    } catch {
                        /* 注册表写入失败不阻塞接入结果 */
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
            cfg.agentLogger.tool_result(conversation, "apply_config_steps", {
                success: true,
                summary: okMsg,
            });
            return okMsg;
        } catch (err) {
            if (err instanceof ConfigBusyError) {
                cfg.agentLogger.tool_result(conversation, "apply_config_steps", {
                    success: false,
                    busy: true,
                });
                return CONFIG_BUSY_MESSAGE;
            }
            cfg.agentLogger.tool_result(conversation, "apply_config_steps", {
                success: false,
                error: err instanceof Error ? err.message : String(err),
            });
            throw err;
        }
    }

    // ── 变更流（modify/delete，针对已接入设备）──────────────
    // 确定性变更解析（agent.md §3.2.0 query_abbr_registry 修改/删除入口的确定性路径）：
    // 目标解析（id/名称尾号）+ 常见表述 → 与 change_prompt 相同的 JSON 形状
    function deterministic_change_parse(
        semantic: string,
        devices: Array<Record<string, unknown>>,
        forcedTarget?: Record<string, unknown>,
    ): Record<string, unknown> | null {
        const norm = (t: string): string =>
            t.toLowerCase().replace(/\s+/g, "").replace(/[#号]/g, "");
        let target: Record<string, unknown> | null = null;
        // 消歧应答语境（§3.2.1.3a 前缀指认）：目标已由前缀 token 确定性定出
        //（build_change_plan 的 parse_disambig_target），跳过按名检索——同名多条
        // 会再次触发消歧询问造成死循环
        if (forcedTarget) {
            target = forcedTarget;
        }
        const idTok = target
            ? null
            : semantic.match(/\b([a-z][a-z0-9]*(?:_[a-z0-9]+)+)\b/i);
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
                if (cands.length === 1) {
                    target = cands[0];
                } else if (cands.length > 1) {
                    // 同名多条（§3.2.1.3a：同名必须经消歧确认）——按名 first-match
                    // 会静默落到第一台，列出候选前缀请用户指认。指认形态 = 前缀 +
                    // 变更短语（parse_disambig_target 可确定性消费，不交 LLM 兜底）；
                    // disambig 标记由调用方置位 pendingDisambig（本函数保持无状态）
                    const candsTxt = cands
                        .map((d) => `· ${String(d["name"])}（${String(d["prefix"] ?? "")}_ 前缀，回复时说「${String(d["prefix"] ?? "")}」）`)
                        .join("\n");
                    return {
                        intent: "disambiguate",
                        steps: [],
                        display:
                            `存在多台「${mnum[1]}${mnum[2]}」，请指认要操作哪一台：\n${candsTxt}\n` +
                            `回复示例：「给 ${String(cands[0]["prefix"] ?? "")} 那台加点」「${String(cands[1] ? cands[1]["prefix"] ?? "" : cands[0]["prefix"] ?? "")} 删除」。`,
                    };
                }
            }
        }
        if (!target) {
            const hits: Array<Record<string, unknown>> = [];
            for (const d of devices) {
                const nm = norm(String(d["name"]));
                if (nm.length >= 4 && norm(semantic).includes(nm)) {
                    hits.push(d);
                }
            }
            if (hits.length === 1) {
                target = hits[0];
            } else if (hits.length > 1) {
                const candsTxt = hits
                    .map((d) => `· ${String(d["name"])}（${String(d["prefix"] ?? "")}_ 前缀，回复时说「${String(d["prefix"] ?? "")}」）`)
                    .join("\n");
                return {
                    intent: "disambiguate",
                    steps: [],
                    display:
                        `有多个设备与您的描述匹配，请指认要操作哪一台：\n${candsTxt}\n` +
                        `回复示例：「给 ${String(hits[0]["prefix"] ?? "")} 那台加点」「${String(hits[1] ? hits[1]["prefix"] ?? "" : hits[0]["prefix"] ?? "")} 删除」。`,
                };
            }
        }
        if (!target) {
            const deleteish = /停用|删除|移除|删了|删掉/.test(semantic) &&
                !/数据点|采集点|点位|的点|点[（(]|点名|地址\s*\d/.test(semantic);
            if (deleteish) {
                // 明确编号但设备不存在（用例 27：「删除3号风机」）→ 回复不存在；
                // 未指明编号（「把风机都删了」）→ 批量范围圈定：消息含设备类型词时
                // 按类型词确定性圈定受影响设备集（target_ids，func_test_case 用例 28
                // 2026-10-02 口径：列清单 + 单次确认整体执行，确认按钮出现）；
                // 圈定失败 → 空 target_id，由上层列清单询问（不得静默全删、不得猜）
                const mnum = norm(semantic).match(
                    /(\d+)(?:#|号)?(风机|主变|逆变器|测风塔|机组|数据源|变压器|设备)/,
                );
                if (!mnum) {
                    const tm = semantic.match(
                        /风电机组|风机|主变|变压器|逆变器|测风塔|机组|光伏|储能|升压站|数据源|设备/,
                    );
                    if (tm) {
                        const tw = tm[0];
                        const generic = tw === "设备";
                        const plurality = /都|全部|所有|一并|一起|统统/.test(semantic);
                        const affected = generic
                            ? devices
                            : devices.filter((d) =>
                                  norm(String(d["name"])).includes(norm(tw)),
                              );
                        if (
                            affected.length > 0 &&
                            (!generic || plurality)
                        ) {
                            return {
                                intent: "delete",
                                target_id: "",
                                target_ids: affected.map((d) => String(d["id"])),
                                instance_fields: {},
                                point_updates: [],
                                points: [],
                                add_points: [],
                            };
                        }
                    }
                }
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

        // 删除意图分类（2026-10-01 fail-safe 重写，两连整设备误删事故驱动）：
        // 设备级删除必须零点级线索（deletion_pointish）；含线索而解析不出具体点时
        // return null 交 LLM 兜底——识别失败不得默认成破坏性最大的解释
        //（「大气压强点」「1013（大风告警点）」曾短路成整实例删除）
        const mentionsPoint = deletion_pointish(semantic);
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
            // 裸点号+（点名）形态（2026-10-01 事故句）：「删除1#风机的1013（大风告警点）」
            // ——无「地址」二字、点后跟），正则白名单曾漏判
            const parenM = addrM
                ? null
                : semantic.match(/(\d{2,7})\s*[（(]\s*([^（）()]{1,16}?)\s*[）)]/);
            // 冒号形态（2026-10-01 用户裁定两种都要适配）：「1002:风向」与「风向:1002」
            // ——数字组为地址、文本组为点名；尾部「N点」的点字可跟在任一侧。
            // 在剥除删除动词后的文本上匹配、名字组排除 的 字——「删除1#风机的风向:1002」
            // 的名字不得吞入设备前缀
            const delStripped = semantic.replace(/删除|移除|停用/g, "");
            const colonM =
                addrM || parenM
                    ? null
                    : (delStripped.match(
                          /(\d{2,7})\s*[:：]\s*([^\s，。；：（）():：]{2,16}?)\s*点?[。.？！]?\s*$/,
                      ) ??
                      delStripped.match(
                          /([^\s，。；：（）():：的]{2,16}?)\s*点?\s*[:：]\s*(\d{2,7})\s*点?[。.？！]?\s*$/,
                      ));
            if (addrM || parenM || colonM) {
                const aid = addrM
                    ? Number(addrM[1])
                    : parenM
                      ? Number(parenM[1])
                      : /^\d{2,7}$/.test(String(colonM![1]))
                        ? Number(colonM![1])
                        : Number(colonM![2]);
                const pt = points.find((p) => Number(p["addr"]) === aid);
                if (pt && pt["id"]) {
                    // 点名-地址矛盾检测（2026-10-01 用户测试）：消息同时给出点名与地址、
                    // 两者指向不同点时，不得按地址静默删错点——列出矛盾请用户裁定。
                    // 点名来源：地址形态取「…点（地址N」紧邻段（剥删除动词），括号/冒号
                    // 形态取括号内/冒号侧全文；尾部 点 字两侧对齐后比对
                    const nameM = addrM
                        ? semantic
                              .replace(/删除|移除|停用/g, "")
                              .match(/([^\s，。；（）()的]{2,16}?)点?\s*[（(]?\s*地址\s*[（(：:]?\s*\d/)
                        : null;
                    const saidName = addrM
                        ? (nameM ? nameM[1] : "")
                        : parenM
                          ? String(parenM[2]).trim()
                          : /^\d{2,7}$/.test(String(colonM![1]))
                            ? String(colonM![2]).trim()
                            : String(colonM![1]).trim();
                    const stripPoint = (s: string): string => s.replace(/点$/, "");
                    const realName = String(pt["name"] ?? "");
                    const realId = String(pt["id"] ?? "").toLowerCase();
                    if (
                        saidName !== "" &&
                        realName !== "" &&
                        saidName !== realName &&
                        stripPoint(saidName) !== stripPoint(realName) &&
                        saidName.toLowerCase() !== realId &&
                        stripPoint(saidName).toLowerCase() !== realId
                    ) {
                        const real = points.find(
                            (p) => String(p["name"] ?? "") === saidName,
                        );
                        const table2 = points
                            .map((p) => `${String(p["addr"])}（${String(p["name"] ?? "")}）`)
                            .join("、");
                        // 明确二选一指引（2026-10-01：矛盾拦截后用户面对死路，确认按钮
                        // 点击落入「没有待执行方案」）——选项措辞与确定性解析的可行
                        // 表述一致（删除地址N的点 / 删除X点），确保回复后能直接闭环
                        const opts = [`· 如要删除地址 ${aid} 的点，请回复「删除地址${aid}的点」`];
                        if (real) {
                            opts.push(
                                `· 如要删除「${saidName}」，请回复「删除${saidName}点」（其地址是 ${String(real["addr"])}）`,
                            );
                        }
                        return {
                            intent: "point_not_found",
                            steps: [],
                            display:
                                `地址 ${aid} 对应的点「${realName}」与您提到的点名「${saidName}」不一致，未执行删除。\n${opts.join("\n")}\n或回复「取消」结束本次变更。当前点表：${table2}。`,
                        };
                    }
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
            // 无地址的点级删除（2026-10-01 大气压强事故补齐）：「删除…大气压强点」
            // 按点名直接定位；点表中无此名 → 确定性回复「点不存在」，不交 LLM 兜底
            //（change_prompt 曾把带「点」字样的消息误归为整实例删除）
            const nameOnly = semantic
                .replace(/删除|移除|停用/g, "")
                .match(/(?:的)?([^\s，。；（）()的]{2,16}?)点[。.？！]?\s*$/);
            if (nameOnly) {
                const said = nameOnly[1];
                // 纯数字「名字」（「删除…1002点」）按地址处理（2026-10-01 冒号形态
                // 同批裁定：数字在删除表述里始终是地址不是点名）
                if (/^\d{2,7}$/.test(said)) {
                    const aid = Number(said);
                    const ptA = points.find((p) => Number(p["addr"]) === aid);
                    if (ptA && ptA["id"]) {
                        return {
                            intent: "delete_points",
                            target_id: targetId,
                            instance_fields: {},
                            point_updates: [],
                            points: [{ id: String(ptA["id"]) }],
                            add_points: [],
                        };
                    }
                    const tableA = points
                        .map((p) => `${String(p["addr"])}（${String(p["name"] ?? "")}）`)
                        .join("、");
                    return {
                        intent: "point_not_found",
                        steps: [],
                        display: `没有找到地址 ${aid} 对应的数据点——该点不存在或已被删除。当前点表：${tableA}。`,
                    };
                }
                const pt = points.find(
                    (p) =>
                        String(p["name"] ?? "") === said ||
                        String(p["id"] ?? "").toLowerCase() === said.toLowerCase(),
                );
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
                const table3 = points
                    .map((p) => `${String(p["addr"])}（${String(p["name"] ?? "")}）`)
                    .join("、");
                return {
                    intent: "point_not_found",
                    steps: [],
                    display: `没有找到名为「${said}」的数据点——该点不存在或已被删除。当前点表：${table3}。`,
                };
            }
            // 点级线索存在但解析不出具体点（未知句式）→ 交 LLM 兜底，不得短路成整设备
            return null;
        }
        return null;
    }

    async function build_change_plan(
        user_text: string,
        conversation: string,
        state: SessionState,
    ): Promise<{
        steps: ServiceStep[];
        display: string;
        /** 追问类返回（缺英文标识/缺转发地址/待选设备）：用户应答后须重入本分叉 */
        ask?: boolean;
        /** 注册表固化载荷（§3.2.1.3a 第 4 步）：挂 merge + Stop-Start 成功路径 */
        registryWrites?: AccessPlan["registryWrites"];
    } | null> {
        // catch-up（2026-09-27 用例10）：变更追问的应答（「风速」「5010」）需要与原始
        // 请求拼接才有语义——解析输入为累积用户文本（含当前消息，由调用方先行累积）
        const semantic = clean_user_text(state.userTexts.join("\n"));
        let current: Record<string, unknown> | null;
        try {
            current = JSON.parse(readFileSync(cfg.configPath, "utf-8"));
        } catch {
            current = null;
        }
        if (!current) return null;

        // 汇总已接入设备（注册表驱动，§3.2.1.3a 变更流目标定位）：
        // 设备名 → {宿主实例, 点前缀}；点集 = 宿主点表按前缀合成的虚拟设备视图
        //（独占形态与实例等价；共用形态多台设备同住一个宿主实例）。
        // hostPoints 供 addr 实例内唯一性检查（同宿主设备共享地址空间）
        const reg = await load_abbr(state);
        const hostIndex = new Map<string, { st: string; inst: Record<string, unknown> }>();
        for (const [st, list] of Object.entries(current)) {
            if (st === "c4_shm_manager" || !Array.isArray(list)) continue;
            for (const inst of list as Array<Record<string, unknown>>) {
                hostIndex.set(String(inst["id"] ?? ""), { st, inst });
            }
        }
        const devices: Array<Record<string, unknown>> = [];
        for (const entry of reg.entries) {
            const hit = hostIndex.get(entry.host);
            if (!hit) continue;
            const prefix_ = `${entry.prefix}_`;
            const hostPts = (hit.inst["points"] ?? []) as Array<Record<string, unknown>>;
            const pts = hostPts
                .filter((p) =>
                    String(p["id"] ?? p["key"] ?? "").startsWith(prefix_),
                )
                .map((p) => ({
                    id: String(p["id"] ?? p["key"] ?? ""),
                    name: p["name"],
                    addr: p["addr"],
                }));
            devices.push({
                // id = 设备唯一身份（注册表前缀——同名设备经前缀顺延保证唯一）；
                // host = 宿主实例 id。二者不可混用：并入形态多台设备同宿主，
                // 以 host 作 id 会让 devices.find 永远命中第一条（2026-10-02
                // B1 链步54 实测：给3号加点错落到1号、wt1_ 前缀）
                id: entry.prefix,
                host: entry.host,
                name: entry.name,
                prefix: entry.prefix,
                service_type: hit.st,
                pointMap: entry.pointMap,
                points: pts,
                hostPoints: hostPts.map((p) => ({
                    id: String(p["id"] ?? p["key"] ?? ""),
                    name: p["name"],
                    addr: p["addr"],
                })),
            });
        }
        if (devices.length === 0) {
            // config 有运行数据但注册表无条目（旧版命名格式，§3.2.1.3 裁定废弃）——
            // 不得静默返回 null 跌入接入管线（用户问「删除风机」却被问「设备叫什么」）
            const hasConfigData = Object.entries(current).some(
                ([st, l]) =>
                    st !== "c4_shm_manager" &&
                    Array.isArray(l) &&
                    (l as unknown[]).length > 0,
            );
            if (hasConfigData) {
                return {
                    steps: [],
                    display:
                        "检测到系统中已有运行中的服务实例，但设备身份注册表中没有对应的设备条目" +
                        "（旧版命名格式的配置）。旧格式不再支持变更操作——请重新接入设备" +
                        "（重新接入会以新格式登记设备身份），或回复「取消」。",
                    ask: false,
                };
            }
            return null;
        }

        // 入库再映射拦截（func_test_case 用例 41，C4_FUN_00090 同 Writer key 双映射）：
        // 「给X的入库再加一条/同步写一份到Y measurement」= 同一采集点在入库实例内的
        // 第二份映射——同实例 shm_id 重复（start 校验 INVALID_POINT）。方案期可读
        // 拒绝并引导独立入库实例（多路下游共用同一采集点 key 是设计支持的形态）
        if (
            /(?:入库|入库实例).{0,14}再加|同步写一份到|再写一份到|再写一份进|再映射/.test(
                semantic,
            )
        ) {
            return {
                steps: [],
                display:
                    "同一采集点的数据在当前入库实例内已有一份映射，无法在同一实例内再次映射" +
                    "（同一入库数据点会被重复引用，数据点配置无效）。确需把数据同时写入多个" +
                    "measurement，请作为独立入库实例另行接入（指向同一批采集点即可）。",
            };
        }

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

        // 裸值兜底绑定（§2.6 变更流，2026-09-27 用例10）：追问点名/英文标识/转发地址
        // 后的纯值应答直接落入追加草稿——全量重解析曾丢已确认字段（轮4 实测）并
        // 漏绑转发地址。绑定后：
        //   - 英文标识/转发地址应答 → 草稿已齐备，直接评估（零 LLM）
        //   - 中文点名应答（英文标识未定）→ 专用微翻译（id_translate_prompt）补 id
        //     ——不得落回全量重解析：累积文本含被拒旧名，change_prompt 会把 id 回填
        //     为旧名派生值，再次误报重名（「角度 vs 功率」死循环实测）
        if (
            state.pendingGap !== null &&
            state.pendingGap.startsWith("change.") &&
            state.changeAddPoints !== null &&
            state.changeAddPoints.length > 0 &&
            state.changeTargetId !== null
        ) {
            const bound = bind_change_answer(
                state.pendingGap as
                    | "change.name"
                    | "change.id"
                    | "change.forward_addr"
                    | "change.addr",
                user_text,
                state.changeAddPoints,
            );
            if (bound) {
                cfg.agentLogger.memory(conversation, "pending_bind", {
                    gap: state.pendingGap,
                    draft: JSON.stringify(state.changeAddPoints),
                });
                // 中文点名待翻译 → 专用微翻译补 id（干净上下文，不重解析）
                const pt0 = state.changeAddPoints[0];
                const nm0 = String(pt0["name"] ?? "").trim();
                const id0 = String(pt0["id"] ?? "").trim();
                if (
                    state.pendingGap === "change.name" &&
                    id0 === "" &&
                    nm0 !== "" &&
                    !IDENTIFIER_RE.test(nm0)
                ) {
                    const existing = devices
                        .map((d) => {
                            const pts = (d["points"] ?? []) as Array<
                                Record<string, unknown>
                            >;
                            return `${String(d["id"])}：${pts
                                .map(
                                    (p) =>
                                        `${String(p["name"] ?? "")}(${String(p["id"] ?? "")})`,
                                )
                                .join("、")}`;
                        })
                        .join("\n");
                    const tr = await llm_json(
                        "id_translate_prompt.txt",
                        { existing_points: existing, name: nm0 },
                        nm0,
                        conversation,
                    );
                    const tid = tr ? String(tr["id"] ?? "").trim() : "";
                    if (tid !== "" && IDENTIFIER_RE.test(tid)) {
                        pt0["id"] = tid;
                    } else {
                        // 翻译失败/不合规 → 降级问用户英文标识（系统不自动生成）
                        state.pendingGap = "change.id";
                        return {
                            steps: [],
                            display: `新增点「${nm0}」英文标识自动翻译失败——请直接提供英文标识后重试（系统不自动生成）。`,
                            ask: true,
                        };
                    }
                }
                const bdev = devices.find(
                    (d) => String(d["id"]) === state.changeTargetId,
                );
                if (!bdev) {
                    state.changeAddPoints = null;
                    state.pendingGap = null;
                    return null;
                }
                return evaluate_change_add(
                    state,
                    state.changeTargetId,
                    String(bdev["service_type"]),
                    bdev,
                    current,
                    reg,
                );
            }
        }

        // 确定性解析优先（常见表述），LLM change_prompt 兜底长尾表述。
        // 确定性分支只扫当前消息（2026-10-01 删除循环实测）：catch-up 累积全文曾使
        // 历史消息里的「地址3000」在后续每轮删除中被重新扫中，确定性短路把任何新
        // 请求都回放成同一条「没有找到地址 3000」——历史上下文只供 LLM 兜底使用
        let r = deterministic_change_parse(clean_user_text(user_text), devices);
        // 消歧锚（§3.2.1.3a 前缀指认）：同名指认是设计明令的确定性场景。锚定后
        // （指认回合锚定，或上一回合已锚定的应答回合）本回合解析锁定该设备——
        // forced 确定性分类、LLM 兜底的设备清单都只含锚，同名另一台不可被误选；
        // 加点类表述不可确定性分类时经 LLM(锚) 收敛，不再回到「请说明变更」空转
        // 消歧锚解析（resolve_disambig_anchor）：新鲜指认优先于陈旧锚——消歧
        // 再询问后的改选（「wt2 删除」）必须生效，不得被上一轮锚（wt1）覆盖
        const anchorRes = resolve_disambig_anchor(
            state.pendingDisambig,
            state.disambigHostId,
            clean_user_text(user_text),
            devices,
        );
        state.pendingDisambig = anchorRes.pending;
        state.disambigHostId = anchorRes.hostId;
        const anchor: Record<string, unknown> | null = anchorRes.anchor;
        if (anchor !== null) {
            // 锚定定向：无锚解析失败（长尾表述）或产出「空目标 delete」（deleteish
            // 兜底不认识前缀 token——消歧后「wt1 删除」会先被误回「您想删除哪一台」）
            // 时，以锚为目标强制重解析——锚是用户显式指认的结果，优先于无锚兜底；
            // 无锚解析已唯一定目标（点名明确的另一台设备）则采信，不被锚覆盖
            const needsAnchor =
                r === null ||
                (String(r["target_id"] ?? "") === "" &&
                    String(r["intent"] ?? "") === "delete" &&
                    // 批量删除（用例 28）已圈定受影响设备集 → 不被锚覆盖为单台删除
                    !(
                        Array.isArray(r["target_ids"]) &&
                        (r["target_ids"] as unknown[]).length > 0
                    ));
            if (needsAnchor) {
                const forced = deterministic_change_parse(
                    clean_user_text(user_text),
                    devices,
                    anchor,
                );
                if (forced !== null) {
                    r = forced;
                }
            }
        }
        if (r === null) {
            // LLM 兜底：锚存在时设备清单只含锚，并对结果强制 target 覆写——
            // LLM 拿不到同名另一台，指认结果不被丢弃
            r = await llm_json(
                "change_prompt.txt",
                {
                    devices_json: JSON.stringify(
                        anchor !== null ? [anchor] : devices,
                    ),
                },
                semantic,
                conversation,
            );
            if (r !== null && anchor !== null) {
                r["target_id"] = anchor["id"];
            }
            if (!r) {
                // 降级（§2.6）：草稿已有内容时按草稿评估——缺什么问什么（如点名已
                // 确认仅缺英文标识翻译失败 → 请用户提供英文标识），草稿为空才提示
                // 服务异常。不再静默跌落进接入管线造成答非所问（用例10 轮5 实测）
                if (
                    state.changeAddPoints !== null &&
                    state.changeAddPoints.length > 0 &&
                    state.changeTargetId !== null
                ) {
                    const ddev = devices.find(
                        (d) => String(d["id"]) === state.changeTargetId,
                    );
                    if (ddev) {
                        return evaluate_change_add(
                            state,
                            state.changeTargetId,
                            String(ddev["service_type"]),
                            ddev,
                            current,
                            reg,
                        );
                    }
                }
                if (anchor !== null) {
                    // 锚已确立（如用户只回了裸前缀）→ 请继续说明变更，锚保持生效
                    return {
                        steps: [],
                        display:
                            `已选定「${String(anchor["name"])}」（${String(anchor["prefix"] ?? "")}_ 前缀）。` +
                            `请说明要做的变更，如：给${String(anchor["name"])}加点、删除${String(anchor["name"])}、` +
                            `删除它的某个点（说明点名或地址）。`,
                        ask: true,
                    };
                }
                return {
                    steps: [],
                    display:
                        "变更解析服务暂时异常，未能理解本次应答。请稍后重试，或换一种表述（例如：点名 vibration，地址 2000，转发地址 6000）。",
                    ask: true,
                };
            }
        }
        const action = String(r["intent"] ?? "");
        if (action === "disambiguate") {
            // 同名多候选询问（独立 intent，与终态错误显式区分）→ 置位应答语境，
            // 应答回合经 parse_disambig_target 确定性指认并锚定
            state.pendingDisambig = true;
            return { steps: [], display: String(r["display"] ?? ""), ask: true };
        }
        if (action === "point_not_found") {
            // 点不存在等终态 → 复位语境与锚
            state.pendingDisambig = false;
            state.disambigHostId = null;
            return { steps: [], display: String(r["display"] ?? "") };
        }
        if (!action) {
            // 意图不可解析：锚已确立 → 请继续说明变更（锚保留）；否则交上层路由
            if (anchor !== null) {
                return {
                    steps: [],
                    display:
                        `已选定「${String(anchor["name"])}」（${String(anchor["prefix"] ?? "")}_ 前缀）。` +
                        `请说明要做的变更，如：给${String(anchor["name"])}加点、删除${String(anchor["name"])}。`,
                    ask: true,
                };
            }
            return null;
        }
        // fail-safe 闸（2026-10-01 两连整设备误删）：整实例删除与当前消息的点级线索
        // 矛盾 → 不出方案改澄清。确定性分类收紧后本闸主要拦 LLM 兜底——change_prompt
        // 也可能把点级删除误归为设备级（「大气压强点」首例）——与判断来源无关一律拦截
        if (action === "delete" && deletion_pointish(clean_user_text(user_text))) {
            return {
                steps: [],
                display:
                    "您要删除整个设备，还是删除设备上的某个数据点？\n· 删除整个设备：请回复「删除 设备名」（如：删除 1#风机）\n· 删除某个数据点：请说明点名或地址（如：删除塔筒温度点（地址1006））",
                ask: true,
            };
        }
        const targetId = String(r["target_id"] ?? "");
        const device_label = (d: Record<string, unknown>): string => {
            const pre = String(d["prefix"] ?? "");
            return pre !== ""
                ? `${String(d["name"])}（${pre}_ 前缀）`
                : String(d["name"]);
        };
        // 明确编号但设备不存在（func_test_case 用例 27）→ 直接回复不存在。
        // 注意：本终态有意不清消歧锚（与下方 point_not_found 终态不同）——
        // 用户指认的前缀不存在时 forced 到旧锚会错删，「宁可放过」；锚的
        // 「已选定」延续语义保持，用户换正确前缀即可继续
        if (targetId.startsWith("__missing__")) {
            return {
                steps: [],
                display: `没有找到您提到的设备——${targetId.replace("__missing__", "")}不存在或从未接入。当前已接入的设备有：${devices
                    .map(device_label)
                    .join("、")}。`,
            };
        }
        // 模糊删除（func_test_case 用例 28）：意图明确但未指明单台设备 → 批量范围
        //（target_ids，确定性类型词圈定/LLM 兜底）非空时列出受影响清单、逐台装配
        // 删除步骤，单次确认整体执行（确认按钮出现）；范围无法确定 → 列出清单询问
        //（不得静默全删，也不得模糊不作为）
        if (!targetId && action === "delete") {
            const batchIds = Array.isArray(r["target_ids"])
                ? (r["target_ids"] as unknown[])
                      .map((x) => String(x))
                      .filter((id) => devices.some((d) => String(d["id"]) === id))
                : [];
            if (batchIds.length === 0) {
                return {
                    steps: [],
                    display: `您想删除哪一台设备？当前已接入：${devices
                        .map(device_label)
                        .join("、")}。请明确设备后再确认。`,
                    ask: true,
                };
            }
            const batchChanges: Array<Record<string, unknown>> = [];
            const batchRegDeletes: string[] = [];
            const batchLines: string[] = [];
            for (const bid of batchIds) {
                const bd = devices.find((d) => String(d["id"]) === bid);
                if (!bd) continue;
                const bs = String(bd["service_type"]);
                const bp = String(bd["prefix"] ?? "");
                const bn = String(bd["name"] ?? bid);
                const bHost = String(bd["host"]);
                // 独占/共用判定与单台删除同源（§3.2.1.3a）：共用实例做前缀点组手术，
                // 独占（删除后宿主无条目）整实例删除，reader 侧成对清理由 merge 级联完成
                const hostEntries = reg.entries.filter((e) => e.host === bHost);
                if (bp !== "" && hostEntries.length > 1) {
                    const delPts = ((bd["points"] ?? []) as Array<Record<string, unknown>>).map(
                        (p) => ({ id: String(p["id"]) }),
                    );
                    batchChanges.push({
                        action: "delete",
                        service_type: bs,
                        instance: { id: bHost },
                        points: delPts,
                    });
                    batchLines.push(
                        `· 删除「${bn}」的全部 ${delPts.length} 个数据点（${bp}_ 前缀；该实例与其他设备共用，实例保留）`,
                    );
                } else {
                    batchChanges.push({
                        action: "delete",
                        service_type: bs,
                        instance: { id: bHost },
                    });
                    batchLines.push(
                        `· 删除设备「${bn}」${bp !== "" ? `（${bp}_ 前缀）` : ""}及其全部数据点（关联转发配置一并清理）`,
                    );
                }
                if (bp !== "") batchRegDeletes.push(bp);
            }
            if (batchChanges.length === 0) return null;
            // 方案已产出 → 消歧语境与锚消费完毕
            state.pendingDisambig = false;
            state.disambigHostId = null;
            return {
                steps: batchChanges.map((c) => ({
                    action: c["action"] as ServiceStep["action"],
                    service_type: c["service_type"] as string,
                    instance: c["instance"] as Record<string, unknown>,
                    points: (c["points"] ?? []) as ServiceStep["points"],
                })),
                display:
                    `变更方案如下（批量删除，逐台核对）：\n${batchLines.join(
                        "\n",
                    )}\n是否确认执行？请点击下方「确认」按钮；如需取消请点击「取消」。`,
                registryWrites: {
                    upserts: [],
                    deletes: batchRegDeletes,
                    pointMapDrops: [],
                    channelHighWatermark: reg.channelHighWatermark,
                },
            };
        }
        const dev = devices.find((d) => String(d["id"]) === targetId);
        if (!dev) {
            // 意图明确但目标不存在 → 友好错误（4.6.2.5/4.6.3.4，非技术语言）
            return {
                steps: [],
                display: `没有找到您提到的设备——该设备不存在或从未接入。当前已接入的设备有：${devices
                    .map(device_label)
                    .join("、")}。请确认设备名称后重试。`,
            };
        }
        if (!targetId) {
            return {
                steps: [],
                display: `您想删除哪一台设备？当前已接入：${devices
                    .map(device_label)
                    .join("、")}。请明确设备后再确认。`,
            };
        }
        const svcType = String(dev["service_type"]);
        const devPrefix = String(dev["prefix"] ?? "");
        const devDisplayName = String(dev["name"] ?? targetId);
        // targetId = 设备身份（注册表前缀）；config 实例操作一律用宿主实例 id
        //（并入形态下二者不同，2026-10-02 B1 链步54 错宿主缺陷）
        const hostId = String(dev["host"]);
        const changes: Array<Record<string, unknown>> = [];
        let detail: string;
        const regDeletes: string[] = [];
        const pointMapDrops: Array<{ prefix: string; keys: string[] }> = [];
        if (action === "delete") {
            // 独占/共用判定（§3.2.1.3a）＝宿主上注册表条目数：删除后归零 → 空实例移除
            //（独占形态设备即实例整体，整实例删除）；条目数 ≥1 → 实例保留，
            //「删除设备」= 前缀点组手术（action=delete + points[] 逐点列出待删 key）
            const hostEntries = reg.entries.filter((e) => e.host === hostId);
            if (devPrefix !== "" && hostEntries.length > 1) {
                const delPts = ((dev["points"] ?? []) as Array<Record<string, unknown>>).map(
                    (p) => ({ id: String(p["id"]) }),
                );
                if (delPts.length === 0) {
                    return {
                        steps: [],
                        display: `设备「${devDisplayName}」当前没有数据点，无需删除。`,
                    };
                }
                changes.push({
                    action: "delete",
                    service_type: svcType,
                    instance: { id: hostId },
                    points: delPts,
                });
                detail =
                    `删除设备「${devDisplayName}」（${devPrefix}_ 前缀的全部 ${delPts.length} 个数据点：` +
                    `${delPts.map((p) => String(p["id"])).join("、")}；该实例与其他设备共用，实例保留）`;
            } else {
                // 独占形态整实例删除：reader 侧成对清理由 merge 级联完成（handle_delete
                // 删除 writer 实例后，自动移除引用其 key 的 reader 转发点，reader 变空则
                // 连实例一并删除）——方案层不得重复追加 reader 删除变更（重复会因
                // 实例已被级联移除而"找不到目标"回滚，2026-09-24）。channel 序号不回收
                changes.push({
                    action: "delete",
                    service_type: svcType,
                    instance: { id: hostId },
                });
                detail = `删除设备「${devDisplayName}」及其全部数据点（关联转发配置一并清理）`;
            }
            if (devPrefix !== "") regDeletes.push(devPrefix);
        } else if (action === "delete_points") {
            const pts = (r["points"] ?? []) as Array<Record<string, unknown>>;
            if (pts.length === 0) return null;
            // id 存在性校验（2026-10-01）：LLM 兜底可能虚构不存在的点 id，直通 executor
            // 会执行失败回滚——先过滤，全部不存在时直接回复点不存在（附当前点表）
            const devPts = (dev["points"] ?? []) as Array<Record<string, unknown>>;
            const delIds = pts
                .map((p) => String(p["id"]))
                .filter((id) => devPts.some((p) => String(p["id"] ?? "") === id));
            if (delIds.length === 0) {
                const table = devPts
                    .map((p) => `${String(p["addr"])}（${String(p["name"] ?? "")}）`)
                    .join("、");
                return {
                    steps: [],
                    display: `没有找到您提到的数据点——该点不存在或已被删除。当前点表：${table}。`,
                };
            }
            changes.push({
                action: "delete",
                service_type: svcType,
                instance: { id: hostId },
                points: delIds.map((id) => ({ id })),
            });
            if (devPrefix !== "") {
                pointMapDrops.push({ prefix: devPrefix, keys: delIds });
            }
            // reader 侧成对删除由 merge 级联完成（handle_delete 级联移除
            // key === writer实例id.点id 的转发点）——方案层不得重复追加
            // reader 变更（重复追加会因点已被级联移除而扑空回滚，2026-09-24）
            detail = `从「${devDisplayName}」移除点：${delIds.join("、")}`;
        } else if (action === "modify") {
            const fields = (r["instance_fields"] ?? {}) as Record<string, unknown>;
            const pu = (r["point_updates"] ?? []) as Array<Record<string, unknown>>;
            const inst: Record<string, unknown> = { id: hostId, ...fields };
            const ch: Record<string, unknown> = {
                action: "modify",
                service_type: svcType,
                instance: inst,
            };
            if (pu.length > 0) {
                // point_updates 语义即更新既有点：打 _update 标记让 executor 撞名
                // 裁定放行（否则同名点改地址会被误判为新增撞名而改名）
                ch["points"] = pu.map((p) => ({ ...p, _update: true }));
            }
            changes.push(ch);
            // 监听型服务端口冻结（LISTENER_SERVICES，§3.2.1.6）：用户要求改监听端口
            // 时执行期保持原值——文案如实标注，不照印「port 改为 X」误导确认
            const portFrozenHere = LISTENER_SERVICES.has(svcType);
            const ftxt = Object.entries(fields)
                .filter(([k]) => !(k === "port" && portFrozenHere))
                .map(([k, v]) => `${k} 改为 ${String(v)}`)
                .concat(
                    fields["port"] !== undefined && portFrozenHere
                        ? ["监听端口保持原值（已接入实例的监听端口不可变更，如需更换请先删除设备重新接入）"]
                        : [],
                )
                .join("，");
            const ptxt = pu
                .map((p) => `点 ${String(p["id"])} 的参数调整为 ${JSON.stringify(p)}`)
                .join("；");
            detail = `在「${devDisplayName}」上修改：${[ftxt, ptxt].filter(Boolean).join("；")}`;
        } else if (action === "add_points") {
            // 草稿合并（单调累积）：有 addr 按 addr 为键；无 addr（先给点名的场景，
            // 2026-09-27「反向有功」实测）按点名匹配，匹配不到以无址条目入草稿，
            // 地址成为后续缺口——不再因缺 addr 丢弃整点
            const parsed = (r["add_points"] ?? []) as Array<Record<string, unknown>>;
            const draft = state.changeAddPoints ?? [];
            for (const p of parsed) {
                let d: Record<string, unknown> | undefined;
                if (p["addr"] !== undefined) {
                    const addr = Number(p["addr"]);
                    d = draft.find((q) => Number(q["addr"]) === addr);
                    if (!d) {
                        d = { addr };
                        draft.push(d);
                    }
                } else {
                    const nm = String(p["name"] ?? "").trim();
                    d =
                        nm !== ""
                            ? draft.find((q) => String(q["name"] ?? "").trim() === nm)
                            : undefined;
                    if (!d) {
                        d = {};
                        draft.push(d);
                    }
                }
                for (const k of ["name", "id", "addr", "forward_addr"] as const) {
                    const v = p[k];
                    if (d[k] === undefined && v !== undefined && String(v).trim() !== "") {
                        d[k] = v;
                    }
                }
            }
            state.changeAddPoints = draft;
            state.changeTargetId = targetId;
            if (draft.length === 0) return null;
            return evaluate_change_add(state, targetId, svcType, dev, current, reg);
        } else {
            return null;
        }

        if (changes.length === 0) return null;
        // 方案已产出 → 消歧语境与锚消费完毕
        state.pendingDisambig = false;
        state.disambigHostId = null;
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
            registryWrites: {
                upserts: [],
                deletes: regDeletes,
                pointMapDrops,
                channelHighWatermark: reg.channelHighWatermark,
            },
        };
    }

    // ── 变更追加草稿评估（§2.6 单缺口顺序提问 + 草稿单调累积，2026-09-27 用例10）──
    // 以 changeAddPoints 草稿为准逐项检查：点名 → 英文标识 → 撞名 → 转发地址 →
    // 地址占用。每个追问置 pendingGap（change.*）供下一轮裸值应答经 bind_change_answer
    // 直接绑定；草稿在方案产出或终态错误时消费/清空
    function evaluate_change_add(
        state: SessionState,
        targetId: string,
        svcType: string,
        dev: Record<string, unknown>,
        current: Record<string, unknown>,
        reg: AbbrRegistry,
    ): {
        steps: ServiceStep[];
        display: string;
        ask?: boolean;
        registryWrites?: AccessPlan["registryWrites"];
    } | null {
        const ap = state.changeAddPoints ?? [];
        if (ap.length === 0) return null;
        // ① 点名 / 英文标识（agent.md §3.2.1.3b：不自动生成，逐项问齐）
        for (const p of ap) {
            const nm = String(p["name"] ?? "").trim();
            let id = String(p["id"] ?? "").trim();
            // 点名为合规英文标识 → 原文即 id（用户原文提供，非系统生成）
            if (id === "" && nm !== "" && IDENTIFIER_RE.test(nm)) id = nm;
            if (nm === "") {
                state.pendingGap = "change.name";
                return {
                    steps: [],
                    display:
                        "请提供新增点的点名（中文名即可；英文标识确认点名后另行提供——系统不自动生成）。",
                    ask: true,
                };
            }
            if (id === "") {
                state.pendingGap = "change.id";
                return {
                    steps: [],
                    display: `新增点「${nm}」缺少英文标识 id——请提供英文点名或确认中文名的英文翻译后重试（系统不自动生成）。`,
                    ask: true,
                };
            }
            const err = identifier_error(id, "point.id");
            if (err) {
                state.pendingGap = "change.id";
                return {
                    steps: [],
                    display: `新增点「${nm}」${err}——请更换英文标识后重试（系统不自动生成）。`,
                    ask: true,
                };
            }
        }
        // 点 key 无条件前缀（§3.2.1.3b）：裸 id → {设备前缀}_{裸id}。add_points 是
        // 新增点（无既有点可更新），不涉及 pointMap 解析——「经 pointMap 沿用既有
        // key」仅适用于更新既有点的复用路径（assemble_access_plan）
        const devPrefix = String(dev["prefix"] ?? "");
        if (devPrefix !== "") {
            for (const p of ap) {
                const pid = String(p["id"] ?? "");
                if (pid !== "" && !pid.startsWith(`${devPrefix}_`)) {
                    p["id"] = `${devPrefix}_${pid}`;
                }
            }
        }
        // addr 数值化（字符串数字会绕过 merge 的地址冲突检查——2026-09-24 用例 18）
        for (const p of ap) {
            if (p["addr"] !== undefined) p["addr"] = Number(p["addr"]);
            if (p["forward_addr"] !== undefined) p["forward_addr"] = Number(p["forward_addr"]);
        }
        // ② 禁止重名（2026-09-27 裁定）：与设备已有点重名 → 拒绝并要求换名
        const devPts0 = (dev["points"] ?? []) as Array<Record<string, unknown>>;
        for (const p of ap) {
            const nm = String(p["name"] ?? "").trim();
            const pid = String(p["id"] ?? "");
            const dup = devPts0.find(
                (q) =>
                    (nm !== "" && String(q["name"] ?? "").trim() === nm) ||
                    (pid !== "" &&
                        String(q["id"] ?? "") !== "" &&
                        String(q["id"]) === pid),
            );
            if (dup) {
                state.pendingGap = "change.name";
                return {
                    steps: [],
                    display: `点名「${nm || pid}」与已有点「${String(
                        dup["name"] ?? "",
                    )}」（addr ${String(dup["addr"] ?? "?")}）重名——禁止重名，请更换点名。`,
                    ask: true,
                };
            }
        }
        // ③ 地址占用预检（func_test_case 用例 18）——先于转发询问：地址已被占用的
        // 点没有必要问转发地址（2026-09-27 实测：占用晚检导致用户白答一轮转发地址）。
        // 占用拒绝保留点名/英文标识、仅作废被占地址——用户换址后草稿直接续用，
        // 不再清空草稿丢失上下文。地址实例内唯一（§3.2.1.3b 唯一性作用域）：
        // 同宿主多设备共享地址空间，占用检查用宿主全量点表而非虚拟设备视图
        const hostPts = (dev["hostPoints"] ?? dev["points"] ?? []) as Array<
            Record<string, unknown>
        >;
        for (const p of ap) {
            if (p["addr"] === undefined) continue;
            const occupied = hostPts.find(
                (q) => Number(q["addr"]) === Number(p["addr"]),
            );
            if (occupied) {
                const occupiedAddr = p["addr"];
                delete p["addr"];
                state.pendingGap = "change.addr";
                return {
                    steps: [],
                    display: `地址 ${String(occupiedAddr)} 已被点「${String(
                        occupied["name"] ?? occupied["id"] ?? "?",
                    )}」占用，无法新增——点名与英文标识已保留，请更换数据点地址（如 1011），或回复「取消」结束本次变更。`,
                    ask: true,
                };
            }
        }
        // ④ 数据点地址（先给点名后补地址的场景——2026-09-27「反向有功」实测）
        for (const p of ap) {
            if (p["addr"] === undefined) {
                state.pendingGap = "change.addr";
                return {
                    steps: [],
                    display: `请提供新增点「${String(
                        p["name"] ?? "",
                    )}」的数据点地址（采集侧点位地址，如 1010）。`,
                    ask: true,
                };
            }
        }
        // ⑤ 新增采集点必须同时转发（func_test_case 用例 16/20 裁定）：缺转发地址
        // → 询问（不静默顺延）；给出 → reader 侧成对追加（key = writer 实例 id.点 id）。
        // 共享宿主按设备前缀归属 reader——否则成对转发会落到别的并入设备实例
        const reader = find_reader_for_writer(
            current,
            String(dev["host"]),
            String(dev["prefix"] ?? ""),
        );
        if (reader) {
            const missingForward = ap.filter(
                (p) => p["forward_addr"] === undefined || p["forward_addr"] === null,
            );
            if (missingForward.length > 0) {
                state.pendingGap = "change.forward_addr";
                // 句柄不出现在对话文本（§3.2.1.3）：转发链路用业务目标（ip:port）指代，
                // 目标信息缺失时省略指代——实例 id（channel{N}）绝不拼入用户可见文本
                //（2026-10-02 链步20+16 实测：${reader.id} 泄漏 channel6）
                const chain = _reader_chain_info(current, reader.id, reader.service_type);
                const target =
                    chain && chain.ip !== null
                        ? `（${chain.ip}${chain.port !== null ? `:${chain.port}` : ""}）`
                        : "";
                return {
                    steps: [],
                    display:
                        `新增点${missingForward
                            .map(
                                (p) =>
                                    `「${String(p["name"] ?? "")}（地址 ${String(p["addr"] ?? "?")}）」`,
                            )
                            .join("、")}还需要转发地址——` +
                        `当前转发链路${target}的转发地址已用到 ${reader.maxAddr}。` +
                        `请告知每个新增点的转发地址后重试。`,
                    ask: true,
                };
            }
            // 转发地址占用预检：reader 侧既有同址点（key 不同）→ 执行期合并会拦截
            // 回滚（2026-09-27「正向有功 9999 vs wind_frequency」实测）——前置到方案
            // 期，保留草稿只作废冲突的转发地址
            const readerInst = (
                (current[reader.service_type] ?? []) as Array<Record<string, unknown>>
            ).find((i) => String(i["id"]) === reader.id);
            const readerPts = (readerInst?.["points"] ?? []) as Array<
                Record<string, unknown>
            >;
            for (const p of ap) {
                const fwd = p["forward_addr"];
                const occ = readerPts.find(
                    (q) => Number(q["addr"]) === Number(fwd),
                );
                if (occ) {
                    delete p["forward_addr"];
                    state.pendingGap = "change.forward_addr";
                    return {
                        steps: [],
                        display: `转发地址 ${String(fwd)} 已被既有转发点「${String(
                            occ["key"] ?? "?",
                        )}」占用——点名与英文标识已保留，请更换转发地址，或回复「取消」结束本次变更。`,
                        ask: true,
                    };
                }
            }
        }
        // ⑥ 组装 steps：writer 点追加 + reader 成对转发
        const steps: ServiceStep[] = [
            {
                action: "modify",
                service_type: svcType,
                instance: { id: String(dev["host"]) },
                points: ap as ServiceStep["points"],
            },
        ];
        let detail = `给「${String(dev["name"] ?? targetId)}」增加点：${ap
            .map((p) =>
                `${String(p["name"] ?? "")}(地址 ${String(p["addr"] ?? "?")}，key ${String(p["id"])})`,
            )
            .join("、")}`;
        const fwdPts = ap.map((p) => ({
            key: `${String(dev["host"])}.${String(p["id"])}`,
            addr: Number(p["forward_addr"]),
            shm_id: 0,
        }));
        if (reader) {
            steps.push({
                action: "modify",
                service_type: reader.service_type,
                instance: { id: reader.id },
                points: fwdPts,
            });
            detail += `；成对转发`;
        }
        // ⑥ 完成：草稿消费（方案待确认；确认/取消由 accessPlan 生命周期管理）。
        // pointMap 增量（源点名 → 生效点 key）随成功路径固化（§3.2.1.3a 第 4 步）
        state.changeAddPoints = null;
        state.pendingGap = null;
        const regEntry = reg.entries.find((e) => e.prefix === devPrefix);
        const pointMapAdds: Record<string, string> = {};
        for (const p of ap) {
            const nm = String(p["name"] ?? "").trim();
            if (nm !== "") pointMapAdds[nm] = String(p["id"]);
        }
        const registryWrites: AccessPlan["registryWrites"] | undefined = regEntry
            ? {
                  upserts: [
                      { ...regEntry, pointMap: { ...regEntry.pointMap, ...pointMapAdds } },
                  ],
                  deletes: [],
                  pointMapDrops: [],
                  channelHighWatermark: reg.channelHighWatermark,
              }
            : undefined;
        return {
            steps,
            display: `变更方案如下：\n· ${detail}\n是否确认执行？请点击下方「确认」按钮；如需取消请点击「取消」。`,
            registryWrites,
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
            // 内嵌载荷的 instance.id 是宿主实例 id——按 host 匹配虚拟设备视图
            const hitDev = devices.find((d) => String(d["host"]) === instId);
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
                // 剥离过程标记（_update/_derived 等）——内嵌 JSON 直通不经方案层，
                // handle_add 无逐点清理，标记会原样落盘 config.json
                points: ((c["points"] ?? []) as Array<Record<string, unknown>>).map(
                    (p) => {
                        const clean: Record<string, unknown> = {};
                        for (const [k, v] of Object.entries(p)) {
                            if (!k.startsWith("_")) clean[k] = v;
                        }
                        return clean;
                    },
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
    async function* handle_confirm(
        user_text: string,
        state: SessionState,
        conversation: string,
    ): AsyncGenerator<AgentStreamEvent> {
            // ① 确定执行来源：会话方案（含完整转发链信息）优先，内嵌 JSON 直通兜底
            let steps: ServiceStep[] | null = null;
            let lastGen: ReturnType<typeof generate_steps> | null = null;
            if (state.accessPlan) {
                if (state.accessPlan.kind === "changes") {
                    steps = state.accessPlan.steps ?? [];
                } else if (state.accessPlan.input) {
                    lastGen = generate_steps(state.accessPlan.input as never, registry, {
                        channelStart: state.accessPlan.registryWrites?.channelHighWatermark ?? 0,
                    });
                    if (!lastGen.fatal) {
                        steps = lastGen.steps;
                        // 并发撞号重校验：方案装配与确认之间另一会话可能已占用同一
                        // channel 序号（装配期分配、执行期单飞串行）——执行前重查，
                        // 占用则方案失效重新装配，而非静默并入他人实例
                        const cfgNow = read_current_config();
                        const clash = steps.find(
                            (sp) =>
                                sp.action === "add" &&
                                cfgNow !== null &&
                                config_has_instance(
                                    cfgNow,
                                    String(
                                        (sp.instance as Record<string, unknown>)["id"] ?? "",
                                    ),
                                ),
                        );
                        if (clash) {
                            state.accessPlan = null;
                            stateWriter.setAccessPlan(false);
                            yield { type: "button_disarm", reason: "实例序号已被占用，方案失效" };
                            yield {
                                type: "text",
                                content:
                                    "接入方案已失效：在方案等待确认期间，系统接入了新的设备，实例序号已被占用。请重新发起接入。",
                            };
                            yield { type: "done" };
                            return;
                        }
                    }
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
                    // channelStart 取 config 现存最大序号与注册表水印的较大值——
                    // 只看现存实例会让已删实例的序号被复用（违背「永不复用」水印语义）
                    const result = generate_steps(input, registry, {
                        channelStart: (() => {
                            const cfgNow = read_current_config();
                            return Math.max(
                                cfgNow
                                    ? channel_watermark_from_config(cfgNow as never)
                                    : 0,
                                registry_watermark(),
                            );
                        })(),
                    });
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
                console.error(
                    `[confirm] no steps: hasPlan=${!!state.accessPlan} kind=${state.accessPlan?.kind ?? "-"} hasInput=${!!state.accessPlan?.input} ` +
                        `genFatal=${lastGen?.fatal ?? "-"} genSteps=${lastGen ? lastGen.steps.length : "-"} genWarn=${JSON.stringify(lastGen?.warnings ?? [])}`,
                );
                // fatal 必须透出原因——此前被误吞为「没有待执行方案」，用户无从修正
                //（2026-09-27 用例5：缺英文标识 id 被误报为没有方案）
                if (lastGen?.fatal) {
                    yield {
                        type: "text",
                        content: `方案无法执行：${lastGen.fatal}`,
                    };
                    yield { type: "done" };
                    return;
                }
                yield {
                    type: "text",
                    content:
                        "当前没有待执行的方案。如需删除或修改数据点，请直接说明（如：删除塔筒温度点）；如需接入新设备，请提供设备信息。",
                };
                yield { type: "done" };
                return;
            }

            // ② 执行（事务 + 回滚协议）
            state.userConfirmed = true;
            stateWriter.setPhase("executing");
            cfg.agentLogger.phase(conversation, "executing");
            try {
                const okMsg = await execute_steps(state, steps, conversation);
                // 成功：方案被消耗 + 在途态清空（等价新会话，§2.4.2）
                state.accessPlan = null;
                state.userConfirmed = false;
                state.recv = fresh_side();
                state.fwd = fresh_side();
                state.forwardIntent = false;
                state.locks = { receive: false, forward: false };
                state.pendingGap = null;
                state.pendingChangeAsk = false;
                state.pendingDisambig = false;
                state.disambigHostId = null;
                state.pendingNewDevice = false;
                state.changeAddPoints = null;
                state.userTexts = [];
                state.fileTable = null;
                stateWriter.setAccessPlan(false);
                stateWriter.setPhase("idle");
                cfg.agentLogger.phase(conversation, "idle");
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
    // ── 第二层运行日志接线（§5.2）──────────────────────────
    // invoke 入口统一记录 user_input / done；事件顺序：
    // user_input → (llm_call/llm_text | tool_call/tool_result | phase | memory) → done
    async function* invoke_with_logging(
        input: AgentInvokeInput,
    ): AsyncGenerator<AgentStreamEvent> {
        const conversation = input.conversationId ?? "orchestrator";
        const lastMsg = (input.messages ?? []).filter((m) => m.role === "user").pop();
        if (lastMsg) {
            cfg.agentLogger.user_input(conversation, lastMsg.role, content_of(lastMsg));
        }
        turnLlmRound = 0;
        try {
            for await (const ev of invoke_turn(input, conversation)) {
                // 确定性回复落盘（§5.2）：提问/方案/错误文本与 LLM 输出同等可追溯
                //（2026-09-27 用例10 排查盲区补齐——此前提问文本不落盘）
                if (ev.type === "text") {
                    cfg.agentLogger.assistant_text(conversation, ev.content);
                }
                yield ev;
            }
        } finally {
            cfg.agentLogger.done(conversation);
        }
    }

    return {
        invoke: invoke_with_logging,
    };

    async function* invoke_turn(
        input: AgentInvokeInput,
        conversation: string,
    ): AsyncGenerator<AgentStreamEvent> {
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
                state.pendingGap = null;
                state.pendingChangeAsk = false;
                state.pendingDisambig = false;
                state.disambigHostId = null;
                state.pendingNewDevice = false;
                state.changeAddPoints = null;
                state.userTexts = [];
                state.fileTable = null;
                stateWriter.setPhase("idle");
                cfg.agentLogger.phase(conversation, "idle");
                stateWriter.setAccessPlan(false);
                yield { type: "button_disarm", reason: "用户取消本次接入" };
                yield {
                    type: "text",
                    content:
                        "好的，已取消本次接入。已确认的信息（场站绑定、设备注册表）已保留，您可以随时重新发起接入。",
                };
                yield { type: "done" };
                return;
            }

            // ② 按钮通道（§2.8）
            if (trimmed.startsWith("[C4_BUTTON_CANCEL]")) {
                state.accessPlan = null;
                state.userConfirmed = false;
                state.pendingGap = null;
                state.pendingChangeAsk = false;
                state.pendingDisambig = false;
                state.disambigHostId = null;
                state.pendingNewDevice = false;
                state.changeAddPoints = null;
                stateWriter.setAccessPlan(false);
                stateWriter.setPhase("idle");
                cfg.agentLogger.phase(conversation, "idle");
                yield { type: "button_disarm", reason: "用户取消执行" };
                yield { type: "text", content: "好的，已取消本次接入方案，未做任何变更。" };
                yield { type: "done" };
                return;
            }
            if (trimmed.startsWith("[C4_BUTTON_CONFIRM]")) {
                yield* handle_confirm(userText, state, conversation);
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
                cfg.agentLogger.phase(conversation, "idle");
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
            cfg.agentLogger.phase(conversation, "collecting");

            // ⑤ 变更流分叉（modify/delete/add_points，已接入设备的调整）；
            // pendingChangeAsk：变更追问（缺点名/缺转发地址/待选设备）的应答回合
            // 强制走本分叉，解析输入为累积用户文本（2026-09-27 用例10）
            if (
                state.pendingChangeAsk ||
                (CHANGE_INTENT_RE.test(userText) && !/^(接入|解析)/.test(trimmed))
            ) {
                console.error(
                    `[route] change-intent hit: ${JSON.stringify(trimmed.slice(0, 50))} pending=${state.pendingChangeAsk}`,
                );
                state.userTexts.push(clean_user_text(userText));
                const change = await build_change_plan(userText, conversation, state);
                if (change !== null) {
                    if (change.steps.length === 0) {
                        // 追问类（缺点名/缺转发地址/待选设备）→ 置位等待应答；
                        // 终态错误（设备/点不存在等）→ 复位。
                        // 无方案出口一律撤钮（2026-10-01）：上一方案执行/失效后前端残留
                        // 确认按钮，用户点击落入「没有待执行方案」死路
                        state.pendingChangeAsk = change.ask === true;
                        yield { type: "button_disarm", reason: "变更需澄清或已结束，无待执行方案" };
                        yield { type: "text", content: change.display };
                        yield { type: "done" };
                        return;
                    }
                    state.pendingChangeAsk = false;
                    state.accessPlan = {
                        kind: "changes",
                        steps: change.steps,
                        display: change.display,
                        registryWrites: change.registryWrites,
                    };
                    stateWriter.setAccessPlan(true);
                    stateWriter.setPhase("planning");
                    cfg.agentLogger.phase(conversation, "planning");
                    yield { type: "text", content: change.display };
                    yield { type: "button_arm" };
                    yield { type: "done" };
                    return;
                }
                // build 失败（如变更意图 LLM 空响应）：不得跌回新接入流程问协议
                //（2026-09-27 用例10 实测：追加请求被误导入接入问答）
                if (state.pendingChangeAsk) {
                    yield {
                        type: "text",
                        content: "变更请求处理失败，请重试，或回复「取消」结束本次变更。",
                    };
                    yield { type: "done" };
                    return;
                }
            }

            // ⑥ 阶段提取（1-7）
            let file_data: string | null = null;
            let file_error = false;
            const pathM = userText.match(/path=([^\s,，]+)/);
            if (pathM) {
                cfg.agentLogger.tool_call(conversation, "parse_any_file", { path: pathM[1] });
                if (existsSync(pathM[1])) {
                    try {
                        const parsed = parse_any_file(pathM[1]);
                        file_data = parsed.length > 8000 ? parsed.slice(0, 8000) : parsed;
                        state.fileTable = file_data;
                        cfg.agentLogger.tool_result(conversation, "parse_any_file", {
                            success: true,
                            bytes: parsed.length,
                        });
                        // 即时回执（§3.5）：上传回合的回显气泡在 LLM 提取期间（20~35s）
                        // 需有可见反馈，否则用户感知「没有反应」而重复上传
                        yield {
                            type: "text",
                            content: `已收到文件（${parsed.length} 字节），正在解析设备信息…`,
                        };
                    } catch {
                        file_error = true;
                        cfg.agentLogger.tool_result(conversation, "parse_any_file", {
                            success: false,
                            error: "解析异常",
                        });
                    }
                } else {
                    file_error = true;
                    cfg.agentLogger.tool_result(conversation, "parse_any_file", {
                        success: false,
                        error: "文件不存在",
                    });
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
                extraction_progress = await run_stage_extraction(
                    userText,
                    file_data,
                    state,
                    conversation,
                );
            } catch (e) {
                cfg.agentLogger.error(
                    conversation,
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

            // ⑥.6 裸值兜底绑定（§2.6 单缺口顺序提问）：上一回合提问的缺口仍未闭合、
            // 本回合各提取器无进展、消息为纯值片段（端口/IP/地址范围）→ 直接绑定给
            // pending 缺口。宁可放过（走正常缺口追问）不可错绑。
            if (!extraction_progress && state.pendingGap !== null) {
                // 转发点表等价应答（「与接入点表一致」）：提取层按 9a 禁止采纳等价
                // 描述，置 mirror 标记交方案层确定性推导（转发=采集地址，方案中标注，
                // 确认即批准）——2026-09-27 用例4 线上事故：等价应答无法收敛，用户
                // 被迫改口具体地址，错装 1000~1009
                if (
                    state.pendingGap === "fwd.points" &&
                    is_forward_mirror_answer(userText)
                ) {
                    state.fwd.pointsMirror = true;
                    extraction_progress = true;
                    cfg.agentLogger.memory(conversation, "pending_bind", {
                        gap: "fwd.points",
                        addrs: "与接收侧一致（方案层推导）",
                    });
                } else if (state.pendingGap === "recv.device") {
                    // 设备缺口裸值绑定（§3.2.1.3c）：全名原样接受；裸数字/中文数字仅当
                    // 累积文本存在设备类型词时绑定（"2" → 2号X），类型词缺失 → 不猜，
                    // 重复追问全名（宁可放过不可错绑）
                    const dev = bind_device_answer(userText, state.userTexts);
                    if (dev !== null) {
                        state.recv.deviceName = dev;
                        extraction_progress = true;
                        cfg.agentLogger.memory(conversation, "pending_bind", {
                            gap: "recv.device",
                            device: dev,
                        });
                    }
                } else if (
                    state.pendingGap === "recv.points.fields" &&
                    state.recv.points !== null
                ) {
                    // 从站号缺口应答绑定（modbus 点级必填，2026-10-02 链步31 实测）：
                    // 「从站号都是1」批量绑定到点表全部缺 uid 的点——点表落槽后阶段3
                    // 不再重提取，无绑定通路则 uid 缺口永不闭合（死循环收摊）
                    const um = userText.match(/从站号?(?:都|全)?(?:是|为|[:：])?\s*(\d{1,3})/);
                    if (um) {
                        const uid = Number(um[1]);
                        let bound = 0;
                        for (const p of state.recv.points) {
                            const rec = p as Record<string, unknown>;
                            if (
                                rec["uid"] === undefined ||
                                rec["uid"] === null ||
                                rec["uid"] === ""
                            ) {
                                rec["uid"] = uid;
                                bound++;
                            }
                        }
                        if (bound > 0) {
                            extraction_progress = true;
                            cfg.agentLogger.memory(conversation, "pending_bind", {
                                gap: "recv.points.fields",
                                uid,
                                bound,
                            });
                        }
                    }
                } else {
                    // 文字键值应答绑定（conn 类缺口，2026-10-02 用例 39 实测）：
                    // influxdb 的 url/token/org/bucket 追问后用户按「bucket是hnals」
                    // 形态应答——parse_bare_value 只认数字形态，文字键值无绑定通路
                    // 则缺口永不闭合。关键词路由（宁缺勿错：仅绑定用户明确点名的键）
                    if (state.pendingGap === "fwd.conn" || state.pendingGap === "recv.conn") {
                        const target =
                            state.pendingGap === "fwd.conn" ? state.fwd.conn : state.recv.conn;
                        const kvRes: Array<[RegExp, string]> = [
                            [/url(?:地址)?(?:是|为|[:：])\s*([^\s，。]+)/i, "url"],
                            [/token(?:是|为|[:：])\s*([^\s，。]+)/i, "token"],
                            [/org(?:是|为|[:：])\s*([^\s，。]+)/i, "org"],
                            [/bucket(?:名)?(?:是|为|[:：])\s*([^\s，。]+)/i, "bucket"],
                        ];
                        let boundKv = 0;
                        for (const [re, key] of kvRes) {
                            const m = userText.match(re);
                            if (m && target[key] === undefined) {
                                target[key] = m[1];
                                boundKv++;
                            }
                        }
                        if (boundKv > 0) {
                            extraction_progress = true;
                            cfg.agentLogger.memory(conversation, "pending_bind", {
                                gap: state.pendingGap,
                                kv: boundKv,
                            });
                        }
                    }
                    const bare = parse_bare_value(userText);
                    const bind = bare
                        ? bind_bare(
                              state.pendingGap,
                              bare,
                              state.pendingGap === "fwd.points"
                                  ? (state.recv.points?.length ?? null)
                                  : null,
                          )
                        : null;
                    if (bind?.kind === "conn") {
                        const target = bind.side === "fwd" ? state.fwd.conn : state.recv.conn;
                        if (bind.ip !== undefined) target["ip"] = bind.ip;
                        if (bind.port !== undefined) target["port"] = bind.port;
                        extraction_progress = true;
                        cfg.agentLogger.memory(conversation, "pending_bind", {
                            gap: state.pendingGap,
                            ip: bind.ip,
                            port: bind.port,
                        });
                    } else if (bind?.kind === "points") {
                        state.fwd.points = bind.addrs.map((a) => ({ addr: a }));
                        state.fwd.pointsMirror = false;
                        extraction_progress = true;
                        cfg.agentLogger.memory(conversation, "pending_bind", {
                            gap: state.pendingGap,
                            addrs: `${bind.addrs[0]}~${bind.addrs[bind.addrs.length - 1]}（${bind.addrs.length} 个）`,
                        });
                    }
                }
            }

            // 拒绝转发应答（2026-09-29 用例11）：平台要求 writer/reader 成对，只采集无法
            // 生效——明确告知约束并引导补充，而非重复追问至收摊
            if (
                !extraction_progress &&
                state.pendingGap === "fwd.required" &&
                FORWARD_OFF_RE.test(userText) &&
                !forward_on_without_off(userText)
            ) {
                yield {
                    type: "text",
                    content:
                        "平台要求数据点必须同时配置转发，无法只采集不转发。请提供转发协议与转发目标（例如：转发采用asfp2协议到127.0.0.1:9900）；或回复「取消」结束本次接入。",
                };
                yield { type: "done" };
                return;
            }

            // ⑦ 缺口计算 + 单缺口顺序提问（提问即终局，§2.6）
            const { gaps, recap } = compute_gaps(state);
            if (gaps.length > 0) {
                // 只问依赖序最靠前的一个缺口——协议决定点表/连接的问题内容，
                // 聚合清单会迫使用户面对尚无法确切回答的问题（C4He 设计修订）
                const asked = gaps[0];
                if (extraction_progress) {
                    // 本回合有实质进展 → 连续追问计数归零（重发/补充信息均不算空转）
                    state.gapRepeat = 0;
                    state.lastGapSignature = null;
                } else {
                    state.gapRepeat =
                        asked.key === state.lastGapSignature ? state.gapRepeat + 1 : 0;
                }
                state.lastGapSignature = asked.key;
                state.pendingGap = asked.key;
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
                            .map((g) => `· ${g.text}`)
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
                yield { type: "text", content: `${head}${asked.ask}\n请提供后我将继续。` };
                yield { type: "done" };
                return;
            }
            state.gapRepeat = 0;
            state.pendingGap = null;

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
                // 点表错误/身份冲突（如采集/转发数量不等、同名消歧）——询问澄清修正，
                // 不进入方案确认。连续两轮同一处装配失败 → 收摊（与缺口追问上限同口径，
                // 防澄清死循环）
                state.gapRepeat =
                    state.lastGapSignature === "assemble.issue"
                        ? state.gapRepeat + 1
                        : 0;
                state.lastGapSignature = "assemble.issue";
                if (state.gapRepeat >= 2) {
                    state.gapRepeat = 0;
                    state.lastGapSignature = null;
                    yield {
                        type: "text",
                        content:
                            `这个问题我连续几轮没能确认到，先为您收个尾：
· ${assembled.issue}
` +
                            `您可以回复「取消」重新开始，或换一种说法后再继续。`,
                    };
                    yield { type: "done" };
                    return;
                }
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
            state.accessPlan = {
                kind: "add",
                input: assembled.plan,
                display: assembled.display,
                registryWrites: assembled.registryWrites,
            };
            // 方案产出 → 同名坚持语境消费完毕（后续轮次不得再绕过检索）
            state.pendingNewDevice = false;
            stateWriter.setAccessPlan(true);
            stateWriter.setPhase("planning");
            cfg.agentLogger.phase(conversation, "planning");
            // 方案展示含设备身份（§4.5.3 确认文本列「将新建设备 1号风机（点 key 前缀 wt1_）」；
            // 实例句柄 channel{N} 不出现在对话文本，agent.md §3.2.1.3）
            yield { type: "text", content: assembled.display };
            yield { type: "button_arm" };
            yield { type: "done" };
    }
}

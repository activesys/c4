// c4/agent/src/orchestrator/gap_question.ts — 单缺口顺序提问文本生成（agent.md §2.6）
// 设计原则（C4He：聚合提问 → 单缺口顺序提问）：
//   - 每回合只提问依赖序最靠前的一个缺口，问题文本由 registry schema 生成；
//   - 要求 = 字段完备性（schema 必填字段清单，缺了走字段级缺口追问）；
//   - 格式 = 示例（"写法不限"，解析宽容不变，不引入书写格式校验器）；
//   - 点表大时提示可上传 xlsx/csv（doc_parsers 通道）；
//   - 裸值兜底绑定：提问后用户以纯值片段（端口/IP/地址范围）作答时，
//     绑定给 pending 缺口——仅接受"整条消息就是一个值"的形态，宁可放过不可错绑。

import { zh_convert } from "./zh_numeral.js";

/** 结构化缺口：key 供 pending 绑定与日志使用，text 复述/收摊，ask 确切提问 */
export interface Gap {
    key: string;
    text: string;
    ask: string;
}

// ── 连接信息提问 ────────────────────────────────────────────

const CONN_FIELD_LABELS: Record<string, string> = {
    ip: "IP 地址",
    port: "端口",
    uid: "从站号(uid)",
    url: "写入地址(url)",
    token: "token 令牌",
    org: "组织(org)",
    bucket: "bucket 名",
};

const CONN_FIELD_EXAMPLES: Record<string, string> = {
    ip: "192.168.1.5",
    port: "9001",
    uid: "1",
    url: "http://127.0.0.1:8086",
    token: "your-token",
    org: "my-org",
    bucket: "my-bucket",
};

/** 提问文本中的字段展示名：已知字段用短标签，未知字段回退 schema 描述截断/原名 */
function field_label(name: string, description?: string): string {
    if (CONN_FIELD_LABELS[name]) return CONN_FIELD_LABELS[name];
    if (description) {
        // 描述常带"必填：必须由用户显式指定…"等元指令，截取首个分隔符前的主体；
        // 截断残段防御：截断点落在括号内会留下孤立「（」（如「bucket 名（必填」）
        // ——去掉残括号及其后（2026-10-03 用例 39 实测）
        const head = description.split(/[，,；;：:]/)[0];
        const open = head.indexOf("（");
        if (open >= 0 && !head.includes("）")) {
            return head.slice(0, open);
        }
        return head;
    }
    return name;
}

/** 生成连接类缺口的确切提问（fields=待补必填字段） */
export function ask_conn(
    label: string,
    fields: Array<{ name: string; description?: string }>,
): string {
    const items = fields.map((f) => {
        const label0 = field_label(f.name, f.description);
        const example = CONN_FIELD_EXAMPLES[f.name];
        return example ? `${label0}（如 ${example}）` : label0;
    });
    return `请提供${label}连接信息：${items.join("、")}`;
}

// ── 点表提问 ────────────────────────────────────────────────

const POINT_FIELD_LABELS: Record<string, string> = {
    addr: "地址",
    name: "点名",
    uid: "从站号(uid)",
    fun: "功能码(fun)",
    type: "数据类型(type)",
    swap: "字节交换(swap，填 0 表示不交换)",
    measurement: "measurement",
    field: "field",
};

/** 示例行的字段顺序与示例值（展示用途；解析不做格式校验） */
const POINT_EXAMPLE_ORDER: Array<{ name: string; value: string }> = [
    { name: "addr", value: "3000" },
    { name: "name", value: "风速" },
    { name: "uid", value: "1" },
    { name: "fun", value: "3" },
    { name: "type", value: "4" },
    { name: "swap", value: "0" },
    { name: "measurement", value: "wind" },
    { name: "field", value: "speed" },
];

/** 生成点表类缺口的确切提问（fields=该协议 point_schema 的字段清单） */
export function ask_points(label: string, fields: Array<{ name: string }>): string {
    const known = POINT_EXAMPLE_ORDER.filter((e) => fields.some((f) => f.name === e.name));
    const unknown = fields.filter((f) => !known.some((e) => e.name === f.name));
    const names = [...known, ...unknown.map((f) => ({ name: f.name, value: f.name }))].map(
        (e) => POINT_FIELD_LABELS[e.name] ?? e.name,
    );
    const example = known.map((e) => e.value).join(":");
    const lines = [
        `请提供${label}点表：每个点需要 ${names.join("、")}。`,
        `写法不限（冒号、逗号、空格分隔均可）`,
    ];
    if (example) lines.push(`，例如：${example}`);
    lines.push(`。点表大也可以直接上传 xlsx/csv 文件。`);
    return lines.join("");
}

// ── 协议提问 ────────────────────────────────────────────────

/** 生成协议类缺口的确切提问（protocols=支持列表） */
export function ask_protocol(label: string, protocols: string[]): string {
    return `请提供${label}协议，回复协议名即可：${protocols.join("、")}`;
}

// ── 裸值解析与绑定（pending 兜底）─────────────────────────

/** 一条消息可同时呈现多种值形态，按 pending 缺口取用；null=非裸值消息 */
export interface BareValue {
    /** 2~5 位纯数字（可带"端口/port"前缀）——同时可解释为地址起始 */
    port?: number;
    /** 点分四段 IP */
    ip?: string;
    /** 地址范围：2390-2399 / 2390~2399 / 2390到2399 */
    range?: { start: number; end: number };
    /** 不带范围的裸数字（作为起始地址解释） */
    number?: number;
    /** 起始地址表述附带的点数（"从1000开始，10个点"）——pointCount 未知时的兜底 */
    count?: number;
}

export function parse_bare_value(message: string): BareValue | null {
    const t = message.trim();
    if (t.length === 0 || t.length > 64) return null;
    const bare: BareValue = {};
    let m: RegExpMatchArray | null;
    if ((m = t.match(/^(\d{1,3}(?:\.\d{1,3}){3})[:：](\d{2,5})$/))) {
        bare.ip = m[1];
        bare.port = Number(m[2]);
        return bare;
    }
    if ((m = t.match(/^(\d{1,3}(?:\.\d{1,3}){3})$/))) {
        bare.ip = m[1];
        return bare;
    }
    if ((m = t.match(/^(?:端口|port)?[:：]?\s*(\d{2,5})\s*(?:端口?号?)?$/i))) {
        bare.port = Number(m[1]);
    }
    if ((m = t.match(/^(\d{2,7})\s*(?:[-~—]|到)\s*(\d{2,7})$/))) {
        bare.range = { start: Number(m[1]), end: Number(m[2]) };
        return bare;
    }
    if (
        (m = t.match(
            /^(?:从)?\s*(\d{2,7})\s*(?:号|开始|起)?(?:\s*[，,、]\s*共?\s*(\d{1,5})\s*个点)?[。.]?$/,
        ))
    ) {
        bare.number = Number(m[1]);
        if (m[2] !== undefined) bare.count = Number(m[2]);
    }
    return Object.keys(bare).length > 0 ? bare : null;
}

/** 绑定结果：由 orchestrator 落到对应槽位 */
export type GapBind =
    | { kind: "conn"; side: "recv" | "fwd"; ip?: string; port?: number }
    | { kind: "points"; side: "fwd"; addrs: number[] }
    | null;

// ── 转发点表等价应答（mirror）─────────────────────────────────
// 「与接收/接入/采集/I区一致」类应答：提取层按 9a 禁止采纳等价描述（不编造），
// 但可交方案层确定性推导（转发=采集，方案中标注，确认即批准）。判定从宽收窄到
// 短句 + 等价词，仅用于 pendingGap=fwd.points 的应答语境，宁可放过不可错绑。
export function is_forward_mirror_answer(text: string): boolean {
    const t = text.trim();
    if (t.length === 0 || t.length > 32) return false;
    // 消息已含显式地址（范围/起始）→ 显式表述优先，不按等价受理
    if (/\d{2,7}\s*(?:[-~—]|到)\s*\d{2,7}/.test(t)) return false;
    if (/(?:从|自)\s*\d{2,7}\s*(?:开始|起)/.test(t)) return false;
    return (
        /(与|同|跟)(接收|接入|采集|本侧|上侧|I区|Ⅰ区).{0,4}(?<![不非没])(?:一致|相同|一样)/.test(
            t,
        ) ||
        /^(?<![不非没])(?:一致|相同|一样)(吧|即可|就可以了|可以了)?$/.test(t)
    );
}

/**
 * 把裸值绑定给 pending 缺口。
 * 协议/接入点表/场站不参与绑定：协议提取无门禁本就收裸名；接入点表需点名；
 * 场站需要名称+缩写成对。转发点表只绑地址（点名与采集点按序一一对应）。
 */
export function bind_bare(key: string, bare: BareValue, pointCount: number | null): GapBind {
    if (key === "recv.conn" || key === "fwd.conn") {
        const side = key === "fwd.conn" ? "fwd" : "recv";
        const ip = bare.ip;
        const port = bare.port;
        if (port !== undefined && !(port >= 1 && port <= 65535)) return null;
        if (ip === undefined && port === undefined) return null;
        return { kind: "conn", side, ...(ip !== undefined ? { ip } : {}), ...(port !== undefined ? { port } : {}) };
    }
    if (key === "fwd.points") {
        if (bare.range) {
            const { start, end } = bare.range;
            if (end < start) return null;
            const span = end - start + 1;
            const count = pointCount !== null ? Math.min(pointCount, span) : span;
            const addrs = Array.from({ length: count }, (_, i) => start + i);
            return { kind: "points", side: "fwd", addrs };
        }
        if (bare.number !== undefined) {
            // 点数优先用接收侧点表（一一对应强制在方案层把关），未知时回退用户
            // 显式给出的点数（"从1000开始，10个点"）——都没有则放过，走缺口追问
            const count =
                pointCount !== null && pointCount > 0 ? pointCount : (bare.count ?? 0);
            if (count > 0) {
                const start = bare.number;
                const addrs = Array.from({ length: count }, (_, i) => start + i);
                return { kind: "points", side: "fwd", addrs };
            }
        }
    }
    return null;
}

// ── 设备缺口应答绑定（recv.device，agent.md §3.2.1.3c）──────
// 设备名称/编号必答缺口（依赖序：场站后、接入协议前）的追问应答确定性落位：
//   - 全名（「2号风机」「升压站」「3#主变」）→ 原样接受；
//   - 裸数字/中文数字（「2」「三号」）→ 仅当累积文本存在设备类型词时绑定
//     （"2" → 2号风机），类型词缺失 → 不猜，返回 null 追问全名（宁可放过不可错绑）。
//     裸数字限 1~2 位——3 位以上数字（端口/地址量级）不作设备编号解释。

const DEVICE_TYPE_WORD_RE =
    /(风机|风电机组|升压站|测风塔|主变|变压器|逆变器|机组|光伏|储能|数据源)/;

export function bind_device_answer(
    message: string,
    accumulated: string[],
): string | null {
    const t = message.trim().replace(/[。.！!？?，,]\s*$/, "");
    if (t.length === 0 || t.length > 24) {
        return null;
    }
    // 裸编号形态（「2」「3号」「三号」「3#」）→ 受类型词守卫约束（§3.2.1.3c：类型词
    // 缺失 → 不猜，追问全名）。纯「N号」不是名称——无类型词可依附时宁可放过
    const bareAr = t.match(/^(\d{1,4})\s*[#号]?$/);
    const bareZh = /^[零一二两三四五六七八九十]{1,6}号?$/.test(t) ? t : null;
    if (!bareAr && !bareZh) {
        // 端口/地址/IP 量级的数字串（「9001」「192.168.1.5」）不是设备名——放过，
        // 走正常缺口追问（宁可放过不可错绑）
        if (/^[\d.]+$/.test(t)) {
            return null;
        }
        // 全名（含设备类型词或显式命名）→ 原样接受
        return t;
    }
    // 裸数字/中文数字 → 需累积文本存在设备类型词
    const ctx = accumulated.join("");
    const typeM = ctx.match(DEVICE_TYPE_WORD_RE);
    if (!typeM) {
        return null;
    }
    let num: number;
    if (bareAr) {
        num = Number(bareAr[1]);
        if (num >= 100) {
            return null; // 端口/地址量级的数字不猜成设备编号
        }
    } else {
        const n = zh_convert(bareZh!.replace(/号$/, ""));
        if (n === null) {
            return null;
        }
        num = n;
    }
    if (num <= 0) {
        return null;
    }
    return `${num}号${typeM[1]}`;
}

// ── 接收端口确定性捕获（2026-09-28 用例7：监听9001端口）──────

/** 转发语境子句——接收端口捕获须排除（「转发到9999端口」不是接收端口） */
const RECEIVE_PORT_EXCLUDE_RE = /转发|入库|写入|推送|发送|目标|服务器/;

/**
 * 从消息中确定性捕获数据接收（监听）端口。覆盖三种表述：
 * 前缀「端口9001/端口：9001」、后缀「9001端口」「监听9001端口」、「监听 9001」。
 * 含转发关键词的子句整体排除（转发端口不是接收端口，宁可放过不可错绑）。
 */
export function parse_receive_port(message: string): number | null {
    const clauses = message
        .split(/[，,。；;;\n]+/)
        .map((c) => c.trim())
        .filter((c) => c.length > 0 && !RECEIVE_PORT_EXCLUDE_RE.test(c));
    for (const clause of clauses) {
        // 前缀：「端口9001」「端口：9001」「接收端口使用7867」（端口后可有少量连接词）
        let m = clause.match(/(?:接收|监听|接入)?端口[^0-9]{0,6}(\d{2,5})/);
        if (m) return Number(m[1]);
        // 后缀：「9001端口」「监听9001端口」
        m = clause.match(/(?:监听|接收)?\s*(\d{2,5})\s*端口/);
        if (m) return Number(m[1]);
        // 「监听 9001」「接收 9001」
        m = clause.match(/(?:监听|接收)\s*(\d{2,5})/);
        if (m) return Number(m[1]);
    }
    return null;
}

// ── 变更流应答绑定（§2.6 裸值兜底，2026-09-27 用例10）────────
// 变更追问（点名/英文标识/转发地址）的应答此前走全量重解析——累积文本重解析
// 曾丢已确认字段、漏绑转发地址。此处将纯值应答直接落入追加草稿：
// 宁可放过（走正常解析）不可错绑。

/** 英文标识形态（与 executor.IDENTIFIER_RE 一致的保守复刻，仅用于绑定预判） */
const CHANGE_ID_RE = /^[a-zA-Z][a-zA-Z0-9_]*$/;

/**
 * 应答性/催促性词语——不是点名也不是英文标识（2026-10-04 用例 54 实测：
 * 追问点名后驱动器答「继续」，被 change.name 裸值绑定收编成点「继续」、
 * id=continue）。全等匹配，宁可放过（交回正常解析）不可错绑
 */
const CHANGE_STOPWORD_RE =
    /^(?:继续|接着|好的?|是的?|嗯+|哦|可以|确认|行|对|ok|okay|yes|no)$/i;

/**
 * 把变更追问的应答绑定给追加草稿（原地修改 draft 中的点条目）。
 * - change.name：单词应答（中/英文，非纯数字）→ 覆盖草稿唯一点的点名
 *   （撞名换名场景依赖覆盖语义）；英文形态同时落 id（用户原文提供，不自动生成）
 * - change.id：英文标识应答 → 落 id
 * - change.forward_addr：纯数字应答 → 落 forward_addr（仅当恰有一点缺转发地址）
 * 返回 false = 未绑定（非裸值/多点歧义），交回正常解析。
 */
export function bind_change_answer(
    key: "change.name" | "change.id" | "change.forward_addr" | "change.addr",
    message: string,
    draft: Array<Record<string, unknown>>,
): boolean {
    const t = message.trim();
    if (t.length === 0 || t.length > 32) return false;
    if (key === "change.forward_addr") {
        // 多点缺转发地址时无法对应，不绑（宁可放过不可错绑）
        const missing = draft.filter(
            (p) => p["forward_addr"] === undefined || p["forward_addr"] === null,
        );
        if (missing.length !== 1) return false;
        const bare = parse_bare_value(t);
        // 仅接受纯数字形态（端口/IP:port 不作转发地址）
        if (!bare || bare.range || bare.ip !== undefined || bare.number === undefined) {
            return false;
        }
        missing[0]["forward_addr"] = bare.number;
        return true;
    }
    if (key === "change.addr") {
        // 数据点地址应答：纯数字 → 绑给唯一点缺地址的草稿条目（多点歧义不绑）
        const missing = draft.filter((p) => p["addr"] === undefined);
        if (missing.length !== 1) return false;
        const bare = parse_bare_value(t);
        if (!bare || bare.range || bare.ip !== undefined || bare.number === undefined) {
            return false;
        }
        missing[0]["addr"] = bare.number;
        return true;
    }
    // 点名/英文标识：仅绑单点草稿（多点追加由 change_prompt 整体给出）
    if (draft.length !== 1) return false;
    if (CHANGE_STOPWORD_RE.test(t)) return false;
    if (key === "change.name") {
        if (/^[0-9]+$/.test(t)) return false;
        if (!/^[\u4e00-\u9fa5A-Za-z][\u4e00-\u9fa5A-Za-z0-9_]{0,23}$/.test(t)) return false;
        draft[0]["name"] = t;
        // 换名连带：旧名派生的 id 一并作废——撞名换名后残留旧 id 会被 id 重复比对
        // 误判重名（2026-09-27 用例10「角度/压强 vs 功率(id=power)」死循环实测）；
        // id 作废后由 orchestrator 落回 change_prompt 重新翻译，英文形态以新名原文为 id
        delete draft[0]["id"];
        if (CHANGE_ID_RE.test(t)) draft[0]["id"] = t;
        return true;
    }
    // change.id
    if (!CHANGE_ID_RE.test(t)) return false;
    draft[0]["id"] = t;
    return true;
}

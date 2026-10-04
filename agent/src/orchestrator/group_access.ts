// c4/agent/src/orchestrator/group_access.ts — 设备组批量接入的确定性解析（agent.md §2.11）
//
// 组模式（func_test_case 用例 57~62，2026-10-03 用户裁定独立需求）：一条消息描述多台
// 同构设备（同模板点表、仅连接参数逐台不同），触发条件封闭枚举（§2.11 组识别条）：
//   ① 区间表述（1#~11# / 1-11号 / A01~A10，含非纯数字编号）
//   ② 数量词（N台）+ 逐台参数列举（N# 形态出现 ≥2 次）
//   ③ 数量词 + 批量语汇（每台点表相同 / 都相同）
// 纯两台（含）以下的复合设备名（无上述任一形态）→ 用例 47 逐台拆分口径，不走本模块。
//
// 本模块只做确定性解析与展开（宁可放过不可错配）：解析不到的字段留空，交由组级缺口
// 追问，不猜绑。连接参数覆盖 §2.11 三变体：
//   a 每台独立 ip:port（显式列表 / 规则「ip 从 x.x.x.x 起每台 +1」「端口从 2404 到 2413」）
//   b 共用 ip:port + 从站号各异（范围「从站号 1~6」，与编号序列一一对应）
//   c 共用 ip:port + 从站号相同 + 地址偏移（「2# 从 2000 开始」）
// 协议无关：本模块不解读协议语义（ip 是否必填由编排器按服务角色判定）。

import { device_type_prefix_of } from "../registry/device_prefix.js";

/** 组成员连接参数（展开后逐台一份；字段缺失 = 待组级缺口追问） */
export interface GroupMemberConn {
    ip?: string;
    port?: number;
    uid?: number;
}

/** 设备组规格（一条消息可并存多组，§2.11） */
export interface GroupSpec {
    /** 类型词（风机/逆变器…，来自类型映射表命中项） */
    typeWord: string;
    /** 组头描述原文（如「倍福PLC风机」，供追问与展示） */
    headDesc: string;
    /** 成员编号原样序列（["1","2","3"] / ["A01"…"A10"]，零填充与字母前缀保留） */
    rawNums: string[];
    /** 成员设备名（数值编号 → 「N号风机」；字母数字编号 → 「A01逆变器」） */
    memberNames: string[];
    /** 模板点表段原文（供 point_prompt 提取翻译英文标识；null=消息未给出） */
    templateText: string | null;
    /** 确定性解析的模板点 [{addr, name}]（与 LLM 提取对账） */
    templateDraft: Array<{ addr: number; name: string }> | null;
    /** 模板点表（编排器 LLM 提取后填充：完整点记录，addr 为成员 1 基址体系） */
    templatePoints: Array<Record<string, unknown>> | null;
    /** 成员连接参数（按 memberNames 序展开；null=消息未给出任何参数） */
    conns: GroupMemberConn[] | null;
    /** 成员采集地址偏移（相对模板 addr 的增量；null=全部 0，§2.11 变体 c） */
    addrOffsets: number[] | null;
    /** 共用监听端口形态（asfp2 组批量并入，用例 62） */
    listenShared: boolean;
}

// ── 触发识别（封闭枚举，§2.11 组识别条）────────────────────

const GROUP_TYPE_WORD_RE =
    /(风电机组|风机|主变|变压器|逆变器|测风塔|机组|光伏|储能|升压站)/;
/** 区间表述：两侧编号形态一致（同字母前缀）；分隔符收紧为 ~～-—至到，
 *  「从2404开始一直到2413」中「开始一直」不在分隔符类内、不误命中 */
const GROUP_RANGE_RE =
    /([A-Za-z]{0,3}\d{1,4})\s*[#号]?\s*[~～\-—至到]\s*([A-Za-z]{0,3}\d{1,4})\s*[#号]?/g;
/** 数量词：N台（「这5台风机」「10台逆变器」） */
const GROUP_COUNT_RE = /(\d{1,3})\s*台/;
/** 逐台参数列举：N# 形态出现 ≥2 次（「1#监听9101，2#监听9102」） */
const GROUP_DEVICE_REF_RE = /(?:^|[^\dA-Za-z])([A-Za-z]{0,3}\d{1,4})\s*[#号](?![\dA-Za-z])/g;
/** 批量语汇 */
const GROUP_BULK_RE = /每台[^。，；\n]{0,12}相同|点表相同|都相同/;

/** 组模式触发判定（§2.11：命中其一即组模式；调用方还须保证会话处于全新态）。 */
export function detect_group_access(text: string): boolean {
    if (!GROUP_TYPE_WORD_RE.test(text)) return false;
    const hasCount = GROUP_COUNT_RE.test(text);
    const hasRange = find_device_ranges(text).length > 0;
    const refCount = (() => {
        GROUP_DEVICE_REF_RE.lastIndex = 0;
        let n = 0;
        while (GROUP_DEVICE_REF_RE.exec(text) !== null) n++;
        return n;
    })();
    const hasBulk = GROUP_BULK_RE.test(text);
    return (
        (hasCount && (hasRange || refCount >= 2 || hasBulk)) ||
        (hasRange && hasBulk)
    );
}

// ── 编号序列展开 ───────────────────────────────────────────

/** 设备区间头甄别：跳过点表/端口/从站号/起始地址语汇里的数字区间——「点表5000~5009」
 *  「从站号1~6」「从1000到1009」是点表/从站号区间，不是设备编号序列（宁可放过） */
const RANGE_CONTEXT_BLOCK_RE = /(?:从站号|点表|端口|地址|从)$/;

function find_device_ranges(text: string): RangeHeader[] {
    const out: RangeHeader[] = [];
    GROUP_RANGE_RE.lastIndex = 0;
    let hm: RegExpExecArray | null;
    while ((hm = GROUP_RANGE_RE.exec(text)) !== null) {
        if (RANGE_CONTEXT_BLOCK_RE.test(text.slice(Math.max(0, hm.index - 6), hm.index))) {
            continue;
        }
        const nums = expand_range(hm[1], hm[2]);
        if (nums !== null) {
            out.push({
                start: hm.index,
                end: hm.index + hm[0].length,
                rawNums: nums,
                a: hm[1],
                b: hm[2],
            });
        }
    }
    return out;
}

/** 组头描述清洗：从窗口截取「类型词短语」（品牌/PLC 前缀 + 类型词），剥掉连接参数
 *  噪声与「是/为/台」引导字（如「是倍福PLC风机」→「倍福PLC风机」） */
function pretty_head(window: string, typeWord: string): string {
    const idx = window.indexOf(typeWord);
    if (idx < 0) return typeWord;
    let start = idx;
    while (start > 0 && /[\u4e00-\u9fa5A-Za-z]/.test(window[start - 1])) start--;
    let head = window.slice(start, idx + typeWord.length);
    head = head.replace(/^(?:是|为|这批|该|一?台|以及)/, "");
    return head === "" ? typeWord : head;
}

/** 展开区间两端（A01~A10 / 1#~3#）→ 编号原样序列；形态不一致/倒序/超 200 台 → null */
function expand_range(a: string, b: string): string[] | null {
    const ma = a.match(/^([A-Za-z]*)(\d+)$/);
    const mb = b.match(/^([A-Za-z]*)(\d+)$/);
    if (!ma || !mb) return null;
    if (ma[1].toLowerCase() !== mb[1].toLowerCase()) return null;
    const start = parseInt(ma[2], 10);
    const end = parseInt(mb[2], 10);
    if (!Number.isInteger(start) || !Number.isInteger(end)) return null;
    if (start > end || end - start + 1 > 200) return null;
    const width = ma[2].length;
    const out: string[] = [];
    for (let n = start; n <= end; n++) {
        out.push(`${ma[1]}${String(n).padStart(width, "0")}`);
    }
    return out;
}

/** 编号是否含字母（A01 → 非纯数字；成员名与前缀拼法随之不同） */
function is_alpha_num(raw: string): boolean {
    return /[A-Za-z]/.test(raw);
}

// ── 组内参数解析 ───────────────────────────────────────────

interface RangeHeader {
    start: number;
    end: number;
    rawNums: string[] | null;
    a: string;
    b: string;
}

/** 模板点表确定性解析：「addr:点名」枚举（点名须含非数字字符——排除「192.168.2.10:502」
 *  的端口尾巴被当点名；顿号「、」不入点名——否则「风速、1001」被吞成点名且隔点漏解析）。
 *  上限 2000 点（§2.11 容量护栏的解析侧镜像）。 */
function parse_template_draft(seg: string): Array<{ addr: number; name: string }> | null {
    const out: Array<{ addr: number; name: string }> = [];
    const re = /(\d{1,7})\s*[:：]\s*([^\s，、。；:：]{1,40})/g;
    let m: RegExpExecArray | null;
    while ((m = re.exec(seg)) !== null) {
        const name = m[2];
        if (!/[\u4e00-\u9fa5A-Za-z]/.test(name)) continue;
        const addr = Number(m[1]);
        if (!Number.isInteger(addr)) continue;
        out.push({ addr, name });
        if (out.length >= 2000) break;
    }
    return out.length > 0 ? out : null;
}

/** 逐台「从 X 开始」基址（§2.11 变体 c）：N#…从2000开始 → num→base */
function parse_addr_bases(seg: string): Map<string, number> {
    const out = new Map<string, number>();
    const re = /([A-Za-z]{0,3}\d{1,4})\s*[#号][^，。；]{0,16}?从(\d{2,7})开始/g;
    let m: RegExpExecArray | null;
    while ((m = re.exec(seg)) !== null) {
        const base = Number(m[2]);
        if (Number.isInteger(base) && !out.has(m[1])) out.set(m[1], base);
    }
    return out;
}

/** ip 末段递增（变体 a 规则）：越界（末段 + 步进×(n-1) > 255）→ null（边界即判失败
 *  降级组级追问，§2.11；不做跨段进位——跨网段的「每台加1」语义不明） */
function inc_ip(base: string, step: number, index: number): string | null {
    const parts = base.split(".");
    if (parts.length !== 4) return null;
    const last = Number(parts[3]) + step * index;
    if (last > 255) return null;
    parts[3] = String(last);
    return parts.join(".");
}

/** 组段内连接参数 → 逐台展开（解析不到的成员字段留空，交组级缺口追问） */
function parse_group_conns(
    seg: string,
    rawNums: string[],
): { conns: GroupMemberConn[]; listenShared: boolean } {
    const conns: GroupMemberConn[] = rawNums.map(() => ({}));
    const indexOfNum = new Map<string, number>();
    rawNums.forEach((n, i) => indexOfNum.set(n, i));

    // 逐台显式 ip（可带 :port）——优先级最高（显式列表 > 规则，§2.11）
    const perIp = new Map<string, GroupMemberConn>();
    const rePerIp = /([A-Za-z]{0,3}\d{1,4})\s*[#号]\s*的?ip是\s*([\d.]{7,15})(?:[:：](\d{2,5}))?/gi;
    let m: RegExpExecArray | null;
    while ((m = rePerIp.exec(seg)) !== null) {
        perIp.set(m[1], {
            ip: m[2],
            ...(m[3] !== undefined ? { port: Number(m[3]) } : {}),
        });
    }
    // 逐台监听端口（asfp2 组，「1#监听9101」）
    const perPort = new Map<string, number>();
    const rePerPort = /([A-Za-z]{0,3}\d{1,4})\s*[#号]\s*监听\s*(\d{2,5})/g;
    while ((m = rePerPort.exec(seg)) !== null) {
        perPort.set(m[1], Number(m[2]));
    }
    // 逐台从站号
    const perUid = new Map<string, number>();
    const rePerUid = /([A-Za-z]{0,3}\d{1,4})\s*[#号][^，。；]{0,10}?从站号\s*(\d{1,3})/g;
    while ((m = rePerUid.exec(seg)) !== null) {
        perUid.set(m[1], Number(m[2]));
    }

    // ip 递增规则（「ip从192.168.1.101开始每台加1」）
    const ipRule = seg.match(/ip从(\d{1,3}(?:\.\d{1,3}){3})开始每台加(\d{1,3})/);
    // 端口范围规则（「每台一个端口，从2404开始一直到2413」——与编号序列一一对应）
    const portRange = seg.match(/从(\d{2,5})开始一直到(\d{2,5})/);
    // 共用 ip（可带 :port）
    const sharedPair = seg.match(/(?:共用\s*ip|IP是统一的|ip是统一的|统一ip)\s*([\d.]{7,15})(?:[:：](\d{2,5}))?/i);
    // 「共用ip 192.168.2.1:502」变体（共用 与 ip 分写）
    const sharedAlt = seg.match(/共用\s*([\d.]{7,15})[:：](\d{2,5})/);
    // 统一端口（「端口都是502」）
    const portAll = seg.match(/端口都是(\d{2,5})/);
    // 共用监听端口（「都发到我们这边的9201端口」——asfp2 组批量并入形态，用例 62）
    const listenShared = seg.match(/都(?:发到|发送到|送到)我们这边的(\d{2,5})端口/);
    // 从站号：统一值 / 范围（与编号序列一一对应）
    const uidAll = seg.match(/从站号都是(\d{1,3})/);
    const uidRange = seg.match(/从站号(\d{1,3})\s*[~～\-至到]\s*(\d{1,3})/);

    for (let i = 0; i < rawNums.length; i++) {
        const num = rawNums[i];
        const c = conns[i];
        const explicit = perIp.get(num);
        if (explicit) {
            if (explicit.ip !== undefined) c.ip = explicit.ip;
            if (explicit.port !== undefined) c.port = explicit.port;
        }
        if (c.ip === undefined && ipRule) {
            const inc = inc_ip(ipRule[1], Number(ipRule[2]), i);
            if (inc !== null) c.ip = inc;
        }
        if (c.ip === undefined && sharedPair) c.ip = sharedPair[1];
        if (c.ip === undefined && sharedAlt) c.ip = sharedAlt[1];

        if (c.port === undefined) {
            const pp = perPort.get(num);
            if (pp !== undefined) c.port = pp;
        }
        if (c.port === undefined && portRange) {
            c.port = Number(portRange[1]) + i;
        }
        if (c.port === undefined && portAll) c.port = Number(portAll[1]);
        if (c.port === undefined && sharedPair && sharedPair[2] !== undefined) {
            c.port = Number(sharedPair[2]);
        }
        if (c.port === undefined && sharedAlt) c.port = Number(sharedAlt[2]);
        if (c.port === undefined && listenShared) c.port = Number(listenShared[1]);

        if (c.uid === undefined) {
            const pu = perUid.get(num);
            if (pu !== undefined) c.uid = pu;
        }
        if (c.uid === undefined && uidRange) {
            c.uid = Number(uidRange[1]) + i;
        }
        if (c.uid === undefined && uidAll) c.uid = Number(uidAll[1]);
    }
    return { conns, listenShared: listenShared !== null };
}

// ── 组解析主入口 ───────────────────────────────────────────

/** 从消息解析设备组（§2.11）：返回全部组规格；无组命中 → null。
 *  解析纯确定性——模板点表的英文标识翻译由编排器 LLM 提取补齐（阶段 3 语义）。 */
export function parse_group_access(text: string): GroupSpec[] | null {
    if (!detect_group_access(text)) return null;

    // 区间头切段：每个区间头开启一个组；头前文本（含「N台类型词」）归属首个组
    const headers = find_device_ranges(text);

    const groups: GroupSpec[] = [];
    const push_group = (
        typeWord: string,
        headDesc: string,
        rawNums: string[],
        seg: string,
    ): void => {
        if (typeWord === "" || rawNums.length < 2) return;
        const memberNames = rawNums.map((n) =>
            is_alpha_num(n) ? `${n}${typeWord}` : `${n}号${typeWord}`,
        );
        const templateDraft = parse_template_draft(seg);
        const bases = parse_addr_bases(seg);
        // 模板基址 = 首成员的「从 X 开始」基址；成员偏移 = 基址差（变体 c）
        let addrOffsets: number[] | null = null;
        if (bases.size > 0) {
            const base0 = bases.get(rawNums[0]);
            if (base0 !== undefined) {
                addrOffsets = rawNums.map((n) => {
                    const b = bases.get(n);
                    return b !== undefined ? b - base0 : 0;
                });
            }
        }
        const { conns, listenShared } = parse_group_conns(seg, rawNums);
        groups.push({
            typeWord,
            headDesc,
            rawNums,
            memberNames,
            templateText: templateDraft !== null ? seg : null,
            templateDraft,
            templatePoints: null,
            conns,
            addrOffsets,
            listenShared,
        });
    };

    if (headers.length > 0) {
        for (let h = 0; h < headers.length; h++) {
            const hd = headers[h];
            const segStart = hd.start;
            const segEnd = h + 1 < headers.length ? headers[h + 1].start : text.length;
            const seg = text.slice(segStart, segEnd);
            // 类型词：先看头后短尾（「1#~3#是倍福PLC风机」），再看头前 40 字
            //（「我们要接入10台逆变器，编号A01~A10」）
            const tail = seg.slice(0, 40);
            const before = text.slice(Math.max(0, hd.start - 40), hd.start);
            const tw =
                tail.match(GROUP_TYPE_WORD_RE)?.[1] ??
                before.match(GROUP_TYPE_WORD_RE)?.[1] ??
                "";
            const headDesc =
                pretty_head(tail, tw) !== tw
                    ? pretty_head(tail, tw)
                    : pretty_head(before, tw);
            push_group(tw, headDesc, hd.rawNums as string[], seg);
        }
        return groups.length > 0 ? groups : null;
    }

    // 无区间头：数量词 + 逐台列举/批量语汇 → 单组（编号 1..N）
    const cm = text.match(GROUP_COUNT_RE);
    if (!cm) return null;
    const count = Number(cm[1]);
    if (!Number.isInteger(count) || count < 2 || count > 200) return null;
    const tail = text.slice(cm.index ?? 0, (cm.index ?? 0) + 48);
    const tw = tail.match(GROUP_TYPE_WORD_RE)?.[1] ?? "";
    const headDesc = tail.split(/[，,。；\n]/)[0] ?? "";
    const rawNums: string[] = [];
    for (let n = 1; n <= count; n++) rawNums.push(String(n));
    push_group(tw, headDesc, rawNums, text);
    return groups.length > 0 ? groups : null;
}

/** 组成员前缀（§2.11 协议无关条）：类型缩写 + 编号**原样**拼入（1 → wt1、A01 → nbA01），
 *  前缀不含下划线。类型词未命中映射表 → null（交调用方按组级缺口处理，不猜）。 */
export function group_member_prefix(typeWord: string, rawNum: string): string | null {
    const pre = device_type_prefix_of(typeWord);
    if (pre === null) return null;
    return `${pre}${rawNum}`;
}

/** 组会话合并键（同键 = 同一组，重解析时单调覆盖） */
export function group_key(g: GroupSpec): string {
    return `${g.typeWord}|${g.rawNums.join(",")}`;
}

/** 组级缺口应答的连接参数解析（与消息解析同词表；应答只含参数、可逐台可规则——
 *  §2.11「组级缺口应答由组参数解析器消费，不走 §2.6 裸值兜底绑定」） */
export function parse_conn_answer(
    seg: string,
    rawNums: string[],
): { conns: GroupMemberConn[]; listenShared: boolean } {
    return parse_group_conns(seg, rawNums);
}

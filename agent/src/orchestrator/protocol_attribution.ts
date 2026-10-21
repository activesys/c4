// c4/agent/src/orchestrator/protocol_attribution.ts — 协议归属纯函数层
// 2026-10-17 生产野测裁定（华锐Bachmann/景观SCADA 接入事故）：接入侧协议快路的
// 子句级转发排除无法识别跨子句语境——「转发到2区，使用Asfp2协议」中转发标记与
// 协议声明被逗号拆开，别名快路把转发声明误绑接入侧；modbus 声明子句（句尾带
// 「转发」）反被排除，下游点表管道整个走错协议（isMb=false 一票否决弃 412 点）。
// 弃权语义（abstain, not guess）——快路只在强证据下绑定：
//   ① 协议 token 与强侧别关键词（采集|接入|接收|数据源|设备|装置）同子句；
//   ② 双侧同声明（BOTH_SIDES，2026-09-29 用例11 口径不变）；
//   ③ 弱标记子句（仅 采用|协议|规约 等触发词）看前邻子句语境：前邻为转发子句
//      → 视为转发侧声明弃绑（事故句式）；前邻为强接入/来向子句（转来|送来|
//      转过来）→ 随前句绑定；其余弃权交 L2 protocol_prompt 裁决；
//   ④ 协议缺口应答语境（pendingGap === "recv.protocol"）弱标记子句即绑定。
// 纠错通道：带侧别标记子句中的协议 token 构成显式重声明——未锁定期（点表未
// 落槽）覆盖既有绑定，错绑可口头纠正而非「取消」重来（生产实例四条绑定入口
// 全挂 !state.recv.protocol 守卫，纠正被静默无视）。
// 证据出示：每次绑定携带依据子句，摘要/方案层随协议名出示（evidence_tag），
// 归因错误用户一眼可见。
// 注册表知识不进本模块：canonical 匹配由调用方以 ProtocolMatcher 闭包注入。

// ── 子句切分与标记正则（与 orchestrator 既有口径一致）────────

const CLAUSE_SPLIT_RE = /[，,。；;;\n]+/;

/** 子句级转发标记（orchestrator FORWARD_CLAUSE_RE 同口径） */
export const FORWARD_CLAUSE_RE = /转发|入库|写入|推送|发送|目标|服务器/;

/** 双侧同声明（orchestrator BOTH_SIDES_RE 同口径）：命中子句不被转发排除 */
export const BOTH_SIDES_RE =
    /(?:(?:接收|采集|接入|数据源)[^\d]{0,6}(?:和|与|及|都|均|同时)[^\d]{0,6}(?:转发|发送|上传|入库|推送))|(?:(?:转发|发送|上传|入库|推送)[^\d]{0,6}(?:和|与|及|都|均|同时)[^\d]{0,6}(?:接收|采集|接入|数据源))|都是用|均用/;

/** 强接入侧标记：子句内出现即构成接入侧归属证据（弱标记 采用|协议|规约 不算——
 *  任何协议声明子句都含「协议」，以它归属等于没有归属） */
export const STRONG_RECV_RE = /采集|接入|接收|数据源|设备|装置/;

/** 接入侧来向标记（前邻子句语境）：数据被送到我方——协议声明随前句归接入侧
 *  （MSG4「第三方厂家转过来的，asfp2协议」形态，弃权语义下仍走确定性快路） */
export const RECV_DIRECTION_RE = /转来|送来|发来|传给|转过来|给我们/;

/** 接入侧子句触发词（orchestrator 阶段2 子句筛选同口径，含「规约」） */
const RECV_TRIGGER_RE = /采集|接入|接收|采用|数据源|上传|设备|协议|规约/;

/** 显式协议 token 提取（declared_protocol_token 两个形态） */
const TOKEN_SUFFIX_RE = /([A-Za-z][A-Za-z0-9]{2,15})\s*(?:协议|规约)/;
const TOKEN_PREFIX_RE = /协议[:：=\s]*([A-Za-z][A-Za-z0-9-]{2,15})/;

export interface ProtocolMatcher {
    /** 正则提取的协议 token → canonical（不在支持列表返回 null） */
    tokenCanonical(token: string): string | null;
    /** 子句别名包含匹配 → canonical（无命中 null） */
    clauseCanonical(clause: string): string | null;
}

export interface ProtocolBind {
    /** canonical 协议名（小写归一） */
    canonical: string;
    /** 依据子句（证据出示原文，超长截断） */
    clause: string;
}

export interface RecvScanResult {
    /** 绑定候选（null=弃权，交 L2 提示词裁决） */
    bind: ProtocolBind | null;
    /** 不支持协议声明（「abc」形态——缺口层报「X 不支持」，同原 declared 语义） */
    raw: string | null;
}

export interface RedeclScanResult {
    recv: ProtocolBind | null;
    fwd: ProtocolBind | null;
}

export function split_clauses(text: string): string[] {
    return text
        .split(CLAUSE_SPLIT_RE)
        .map((c) => c.trim())
        .filter((c) => c.length > 0);
}

function clause_token(clause: string): string | null {
    const m = clause.match(TOKEN_SUFFIX_RE) ?? clause.match(TOKEN_PREFIX_RE);
    return m === null ? null : m[1]!;
}

/** 转发标记是否先于协议 token（「转发采用asfp2协议」→ true；事故子句
 *  「…使用modbus协议转发」标记在 token 之后 → false，不构成转发侧声明） */
function forward_leads_token(clause: string): boolean {
    const latin = clause.search(/[A-Za-z]/);
    const marker = clause.search(FORWARD_CLAUSE_RE);
    return marker !== -1 && (latin === -1 || marker < latin);
}

/** 依据子句截断（摘要/方案展示 ≤40 字） */
export function evidence_clause(clause: string): string {
    const t = clause.trim();
    return t.length > 40 ? `${t.slice(0, 40)}…` : t;
}

/** 证据出示尾注（统一形态「（依据：…）」，无证据返回空串；展示边界截断 ≤40 字） */
export function evidence_tag(evidence: string | null | undefined): string {
    if (evidence === null || evidence === undefined || evidence.trim() === "") {
        return "";
    }
    return `（依据：${evidence_clause(evidence)}）`;
}

/**
 * 接入侧协议快路扫描（弃权语义）。raw 语义与原 declared_protocol_token 一致
 * （首个不支持的正则 token，供缺口层报「X 不支持」）；绑定收紧为强证据。
 */
export function recv_protocol_scan(
    text: string,
    m: ProtocolMatcher,
    answerContext: boolean,
): RecvScanResult {
    const clauses = split_clauses(text);
    let raw: string | null = null;
    for (let i = 0; i < clauses.length; i++) {
        const clause = clauses[i]!;
        if (!RECV_TRIGGER_RE.test(clause)) continue;
        const both = BOTH_SIDES_RE.test(clause);
        if (!both && FORWARD_CLAUSE_RE.test(clause)) continue;
        const prev = i > 0 ? clauses[i - 1]! : null;
        if (!STRONG_RECV_RE.test(clause)) {
            // 弱标记子句：跨子句语境裁决——前邻转发子句 → 转发侧声明（事故句式）；
            // 前邻强接入/来向子句 → 随前句归属；协议缺口应答 → 语境锁定；否则弃权
            if (prev !== null && FORWARD_CLAUSE_RE.test(prev)) continue;
            const prevRecv =
                prev !== null &&
                (STRONG_RECV_RE.test(prev) || RECV_DIRECTION_RE.test(prev));
            if (!answerContext && !prevRecv) continue;
        }
        const tok = clause_token(clause);
        if (tok !== null) {
            const canon = m.tokenCanonical(tok);
            if (canon !== null) {
                return {
                    bind: { canonical: canon, clause: evidence_clause(clause) },
                    raw,
                };
            }
            if (raw === null) raw = tok;
            // raw 记录后继续扫后续子句（原实现 alias 步仍会跑的语义：有效声明
            // 必须能顶掉无效 raw）
            continue;
        }
        const aliasCanon = m.clauseCanonical(clause);
        if (aliasCanon !== null) {
            return {
                bind: { canonical: aliasCanon, clause: evidence_clause(clause) },
                raw,
            };
        }
    }
    return { bind: null, raw };
}

/**
 * 显式侧别重声明扫描（纠错通道）：带侧别标记子句中的协议 token 构成重声明——
 * 强接入子句 → recv；转发子句 → fwd；双侧子句 → 两侧。弱标记子句不构成显式
 * 重声明（纠错必须带侧别语境，歧义交归属层/缺口层，不猜）。
 */
export function side_redeclaration_scan(
    text: string,
    m: ProtocolMatcher,
): RedeclScanResult {
    const out: RedeclScanResult = { recv: null, fwd: null };
    for (const clause of split_clauses(text)) {
        if (!RECV_TRIGGER_RE.test(clause) && !FORWARD_CLAUSE_RE.test(clause)) {
            continue;
        }
        const tok = clause_token(clause);
        const canonical =
            tok !== null ? m.tokenCanonical(tok) : m.clauseCanonical(clause);
        if (canonical === null) continue;
        const bind: ProtocolBind = { canonical, clause: evidence_clause(clause) };
        if (BOTH_SIDES_RE.test(clause)) {
            if (out.recv === null) out.recv = bind;
            if (out.fwd === null) out.fwd = bind;
        } else if (FORWARD_CLAUSE_RE.test(clause) && forward_leads_token(clause)) {
            if (out.fwd === null) out.fwd = bind;
        } else if (STRONG_RECV_RE.test(clause)) {
            if (out.recv === null) out.recv = bind;
        }
    }
    return out;
}

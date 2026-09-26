// c4/agent/src/orchestrator/gap_question.ts — 单缺口顺序提问文本生成（agent.md §2.6）
// 设计原则（C4H2：聚合提问 → 单缺口顺序提问）：
//   - 每回合只提问依赖序最靠前的一个缺口，问题文本由 registry schema 生成；
//   - 要求 = 字段完备性（schema 必填字段清单，缺了走字段级缺口追问）；
//   - 格式 = 示例（"写法不限"，解析宽容不变，不引入书写格式校验器）；
//   - 点表大时提示可上传 xlsx/csv（doc_parsers 通道）；
//   - 裸值兜底绑定：提问后用户以纯值片段（端口/IP/地址范围）作答时，
//     绑定给 pending 缺口——仅接受"整条消息就是一个值"的形态，宁可放过不可错绑。

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
};

const CONN_FIELD_EXAMPLES: Record<string, string> = {
    ip: "192.168.1.5",
    port: "9001",
    uid: "1",
};

/** 提问文本中的字段展示名：已知字段用短标签，未知字段回退 schema 描述截断/原名 */
function field_label(name: string, description?: string): string {
    if (CONN_FIELD_LABELS[name]) return CONN_FIELD_LABELS[name];
    if (description) {
        // 描述常带"必填：必须由用户显式指定…"等元指令，截取首个分隔符前的主体
        return description.split(/[，,；;：:]/)[0];
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
    if ((m = t.match(/^(?:从)?\s*(\d{2,7})\s*(?:号|开始|起)?$/))) {
        bare.number = Number(m[1]);
    }
    return Object.keys(bare).length > 0 ? bare : null;
}

/** 绑定结果：由 orchestrator 落到对应槽位 */
export type GapBind =
    | { kind: "conn"; side: "recv" | "fwd"; ip?: string; port?: number }
    | { kind: "points"; side: "fwd"; addrs: number[] }
    | null;

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
        if (bare.number !== undefined && pointCount !== null && pointCount > 0) {
            const start = bare.number;
            const addrs = Array.from({ length: pointCount }, (_, i) => start + i);
            return { kind: "points", side: "fwd", addrs };
        }
    }
    return null;
}

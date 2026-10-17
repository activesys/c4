// c4/agent/src/orchestrator/point_pipeline_modbus.ts — modbus 域完成管道
// func_test_case_real_points.md modbus 域规则（2026-10-11 用户裁定）的确定性执行层：
// 类型同义词映射与地址跨度互证（规则 2）、modscan/modbus 地址双轨换算（规则 1）、
// 字序声明解析（规则 3）、控制组/可写排除（规则 4）、多设备展开（规则 5）、
// 列裁剪（规则 6/8——未映射列天然不携带）、脏行剔除与同内容副本不产点（规则 8）、
// 重复地址清单（MB-05 拒收口径）。
// 本模块是 modbus 协议的「完成层」：point_pipeline 两遍法提取的原始行
// （addr/name/_device/_aux_* + 识别层声明袋）在此补齐 uid/fun/type/swap。
// point_pipeline.ts 的协议无关红线（不出现协议专属字样）由模块拆分承载——
// 协议知识集中于本文件；逐「块」调用（声明与展开作用域 = 数据块）。

import type { ExcludedGroup, RawPoint } from "./point_pipeline.js";
import type { ParsedPointBlock } from "../subagents/tools/doc_parsers.js";

// ── ASFP2 类型枚举（c4_modbus_client.md §4.6.1）──────────────
export const MB_TYPE = {
    BOOLEAN: 0,
    INT16: 3,
    UINT16: 4,
    INT32: 5,
    UINT32: 6,
    INT64: 7,
    UINT64: 8,
    FLOAT32: 10,
    FLOAT64: 11,
} as const;

interface TypeSyn {
    type: number;
    span: number;
}

// 类型同义词基线（域规则 2；键 = 归一化小写原文）。singed→signed 拼写容错在
// norm_type_word 先行修正（规则 2：拼写错误不得拒收）
const TYPE_SYNONYMS: Record<string, TypeSyn> = {
    int: { type: MB_TYPE.INT16, span: 1 },
    int16: { type: MB_TYPE.INT16, span: 1 },
    i16: { type: MB_TYPE.INT16, span: 1 },
    i32: { type: MB_TYPE.INT32, span: 2 },
    i64: { type: MB_TYPE.INT64, span: 4 },
    u16: { type: MB_TYPE.UINT16, span: 1 },
    u32: { type: MB_TYPE.UINT32, span: 2 },
    short: { type: MB_TYPE.INT16, span: 1 },
    "signed short": { type: MB_TYPE.INT16, span: 1 },
    sint: { type: MB_TYPE.INT16, span: 1 },
    uint: { type: MB_TYPE.UINT16, span: 1 },
    uint16: { type: MB_TYPE.UINT16, span: 1 },
    word: { type: MB_TYPE.UINT16, span: 1 },
    int32: { type: MB_TYPE.INT32, span: 2 },
    dint: { type: MB_TYPE.INT32, span: 2 },
    long: { type: MB_TYPE.INT32, span: 2 },
    "signed long": { type: MB_TYPE.INT32, span: 2 },
    uint32: { type: MB_TYPE.UINT32, span: 2 },
    dword: { type: MB_TYPE.UINT32, span: 2 },
    int64: { type: MB_TYPE.INT64, span: 4 },
    uint64: { type: MB_TYPE.UINT64, span: 4 },
    f32: { type: MB_TYPE.FLOAT32, span: 2 },
    float: { type: MB_TYPE.FLOAT32, span: 2 },
    real: { type: MB_TYPE.FLOAT32, span: 2 },
    vt_r4: { type: MB_TYPE.FLOAT32, span: 2 },
    f64: { type: MB_TYPE.FLOAT64, span: 4 },
    double: { type: MB_TYPE.FLOAT64, span: 4 },
    vt_r8: { type: MB_TYPE.FLOAT64, span: 4 },
    bool: { type: MB_TYPE.BOOLEAN, span: 1 },
    vt_bool: { type: MB_TYPE.BOOLEAN, span: 1 },
    bit: { type: MB_TYPE.BOOLEAN, span: 1 },
    // 中文写法
    浮点数: { type: MB_TYPE.FLOAT32, span: 2 },
    浮点型: { type: MB_TYPE.FLOAT32, span: 2 },
    单精度浮点: { type: MB_TYPE.FLOAT32, span: 2 },
    双精度浮点: { type: MB_TYPE.FLOAT64, span: 4 },
    布尔: { type: MB_TYPE.BOOLEAN, span: 1 },
    布尔量: { type: MB_TYPE.BOOLEAN, span: 1 },
    开关量: { type: MB_TYPE.BOOLEAN, span: 1 },
    "16位有符号的整型": { type: MB_TYPE.INT16, span: 1 },
    "16位无符号的整型": { type: MB_TYPE.UINT16, span: 1 },
    "32位有符号的整型": { type: MB_TYPE.INT32, span: 2 },
    "32位无符号的整型": { type: MB_TYPE.UINT32, span: 2 },
};

const TYPE_LOOKUP = new Map<string, TypeSyn & { label: string }>();
for (const [w, s] of Object.entries(TYPE_SYNONYMS)) {
    TYPE_LOOKUP.set(w, { ...s, label: w });
}

/** 类型词归一化：小写、去空白、singed→signed 容错（规则 2） */
function norm_type_word(raw: string): string {
    return String(raw ?? "")
        .trim()
        .toLowerCase()
        .replace(/\s+/g, " ")
        .replace(/singed/g, "signed");
}

function lookup_type(raw: string): (TypeSyn & { label: string }) | null {
    const w = norm_type_word(raw);
    return TYPE_LOOKUP.get(w) ?? null;
}

/** 类型词判定（控制组整组排除时统计表内显式点数用） */
/** 文字式起始地址 sheet 判定（域规则 8「无地址列的 sheet 是码表/说明页」，
 *  RP-MB-07 高力板遥信标本，2026-10-11 用户裁定：整 sheet 丢弃）——
 *  地址语义只以「X起始地址：N」整格文字出现（无算术公式单元格、表头无
 *  地址列）→ 无具体地址列，整 sheet 不产点、不计对账。 */
export function mb_is_textual_addr_sheet(
    header: Array<string> | null,
    rows: Array<Array<unknown> | null | undefined>,
): boolean {
    for (const h of header ?? []) {
        const s = String(h ?? "").trim();
        if (s !== "" && /地址/.test(s) && !/公式|说明/.test(s)) {
            return false; // 有具体地址列表头
        }
    }
    let textual = 0;
    let formula = 0;
    for (const r of rows ?? []) {
        for (const c of r ?? []) {
            const s = String(c ?? "").trim();
            if (s === "" || s.length > 40) continue;
            if (/^[\u4e00-\u9fa5A-Za-z]{1,8}起始地址[：:]\s*\d+$/.test(s)) textual++;
            else if (/起始地址/.test(s) && /[+×*]/.test(s)) formula++;
        }
    }
    return textual >= 2 && formula === 0;
}

// ── 纯数值 dump 块判定（MB-10 华创 Sheet2 形态，2026-10-17 实测）──────────
// 无表头文字、数据行所有非空格均为数值/逗号串（原始内存 dump：col0 裸浮点、
// 「508001001, 0, 0, 800000」四元组格、800000+i 序号列）——是数据导出残片
// 非点表，照识别会产出全占位名垃圾点并污染全表对账（3926=1915+40+1971 实测）。
// 判定：表头无文字 且 抽样数据行无任何文字单元格（≥10 行全数值）。
export function mb_is_numeric_dump_sheet(
    header: Array<string> | null,
    rows: Array<Array<unknown> | null | undefined>,
): boolean {
    const TEXTY = /[A-Za-z\u4e00-\u9fff]/;
    for (const h of header ?? []) {
        if (TEXTY.test(String(h ?? ""))) {
            return false; // 表头有文字 → 有列语义，不是 dump
        }
    }
    let dataRows = 0;
    let textyRows = 0;
    for (const r of (rows ?? []).slice(0, 50)) {
        const cells = (r ?? []).map((c) => String(c ?? "").trim());
        if (cells.every((c) => c === "")) continue;
        dataRows++;
        if (cells.some((c) => TEXTY.test(c))) textyRows++;
    }
    return dataRows >= 10 && textyRows === 0;
}

// ── Variable 声明式分表救提取（RP-MB-09 2026-10-12 四表口径）──────────
// 上海电气「遥测」形态：头部若干行 Variable 键值声明（VariableName/Desc/
// Offset/Scale + 功能码），其后成段普通数据行（风机号/描述/缩放/偏移地址）。
// 识别层见 Variable 声明易整页判 lookup（注册 hint 将「Variable 键值声明页」
// 列为 lookup 形态——该口径指整页仅声明行的纯声明页）——此处确定性兜底：
// 声明行只作 decls 来源（功能码权威声明），普通行按「末个纯整数单元格 =
// 寄存器地址、首个中文单元格 = 点名、行首小整数单元格 = 风机号语境」转录，
// 交 mb_finalize 走常规管线（与主表段同址同名 → 跨块同点合并计对账）。
export function mb_rescue_variable_sheet(
    blk: ParsedPointBlock,
): { rows: RawPoint[]; decls: Record<string, unknown> } | null {
    const DECL_RE = /Variable\w*\s*=\s*"|<Variable/i;
    let declRows = 0;
    const plain: string[][] = [];
    for (const r of blk.rows) {
        const cells = (r ?? []).map((c) => String(c ?? "").trim());
        if (cells.some((c) => DECL_RE.test(c))) declRows++;
        else plain.push(cells);
    }
    if (declRows < 1 || plain.length < 50 || blk.rows.length < 100) return null;
    const rows: RawPoint[] = [];
    for (const cells of plain) {
        // 地址 = 末个纯整数单元格（≤5 位，寄存器编址形态）；风机号语境 =
        // 行首小整数（≤200，台别列在首列——与派生段号数值对齐）
        let addr = "";
        for (let c = cells.length - 1; c >= 0; c--) {
            if (/^\d{1,5}$/.test(cells[c] ?? "")) {
                addr = cells[c]!;
                break;
            }
        }
        let name = "";
        for (const s of cells) {
            if (/\p{Script=Han}/u.test(s)) {
                name = s;
                break;
            }
        }
        if (addr === "" || name === "") continue;
        const p: RawPoint = { _src: blk.title, name, addr };
        const first = cells.find((s) => /^\d{1,3}$/.test(s));
        if (first !== undefined && first !== addr && Number(first) <= 200) {
            p["_device"] = String(Number(first));
        }
        rows.push(p);
    }
    if (rows.length < 50) return null;
    const decls: Record<string, unknown> = {};
    for (const r of blk.rows) {
        const text = (r ?? []).map((c) => String(c ?? "")).join(" ");
        if (!DECL_RE.test(text)) continue;
        const mFun = text.match(/功能码\s*[：:]?\s*0?([1-4])(?![0-9])/);
        if (mFun !== null && decls["fun_default"] === undefined) {
            decls["fun_default"] = Number(mFun[1]);
        }
    }
    return { rows, decls };
}

export function is_type_word(raw: string): boolean {
    return lookup_type(raw) !== null;
}

// ── 功能码值解析（数字 / 0X03 / FC04 / F02 / 2(读) / 寄存器类型词）──
export function parse_fun_value(raw: string): number | null {
    const s = String(raw ?? "").trim();
    if (s === "") return null;
    const low = s.toLowerCase();
    // 1~4 读功能码；5/6 写功能码（0X05/0X06/05——仅作可写排除标记，不落 fun）
    const mNum = low.match(/^(?:0x)?0?([1-6])(?:\s*[(（]读[)）])?$/);
    if (mNum) return Number(mNum[1]);
    const mFc = low.match(/^(?:fc|f)\s*0?([1-6])$/);
    if (mFc) return Number(mFc[1]);
    if (low.includes("功能码")) {
        const m = low.match(/功能码\s*(?:0x)?0?([1-6])/);
        if (m) return Number(m[1]);
    }
    if (low.includes("holding") || low.includes("保持寄存器")) return 3;
    if (/input_register|输入寄存器/.test(low.replace(/\s/g, "")) && !low.includes("整型")) return 4;
    if (low.includes("coil") || low.includes("线圈")) return 1;
    if (low.includes("discrete") || low.includes("离散输入")) return 2;
    return null;
}

// ── 单元格分类（功能码列被类型文字污染的表——域规则按值形态重判）──
export function classify_mb_cell(
    raw: string,
): { kind: "fun"; fun: number } | { kind: "type"; syn: TypeSyn & { label: string } } | null {
    const s = String(raw ?? "").trim();
    if (s === "") return null;
    const fun = parse_fun_value(s);
    if (fun !== null) return { kind: "fun", fun };
    const syn = lookup_type(s);
    if (syn !== null) return { kind: "type", syn };
    return null;
}

// ── modscan/modbus 双轨（域规则 1）────────────────────────
// modscan 编号域：线圈 00001 起（FC1）、离散输入 10001 起（FC2）、
// 输入寄存器 30001 起（FC4）、保持寄存器 40001 起（FC3）。
// 换算门控（防 0 基大表误转换——京城 遥信 0 基线圈可达 3 万）：识别层声明
// addr_mode=modscan，或代码全表特征判定（≥80% 已知 fun 行的地址整体落在
// modscan 编号域且最小地址 ≥ 10001）。逐行换算 = 编号 − 空间基。
const MODSCAN_BASE: Record<number, number> = { 1: 1, 2: 10001, 4: 30001, 3: 40001 };
const MODSCAN_LABEL: Record<number, string> = {
    1: "线圈",
    2: "离散输入",
    4: "输入寄存器",
    3: "保持寄存器",
};

function in_modscan_domain(fun: number, addr: number): boolean {
    const base = MODSCAN_BASE[fun];
    return base !== undefined && base > 1 && addr >= base && addr <= base + 9998;
}

// ── 字序映射（域规则 3，小端部署机基线）──────────────────
// ABCD → hton1/swap2；BADC → hton0/swap2；CDAB → hton1/swap0；DCBA → hton0/swap0
export function word_order_of(raw: string): { key: string; swap: number } | null {
    const s = String(raw ?? "");
    if (/ABCD/i.test(s)) return { key: "ABCD", swap: 2 };
    if (/BADC/i.test(s)) return { key: "BADC", swap: 2 };
    if (/CDAB/i.test(s)) return { key: "CDAB", swap: 0 };
    if (/DCBA/i.test(s)) return { key: "DCBA", swap: 0 };
    if (s.includes("字间先低后高")) return { key: "CDAB（字间先低后高，低字在前）", swap: 0 };
    if (s.includes("字间先高后低")) return { key: "ABCD（字间先高后低，高字在前）", swap: 2 };
    if (/交换/.test(s)) return { key: "ambiguous", swap: -1 };
    return null;
}

// ── 声明袋解释 ───────────────────────────────────────────
export interface MbDecls {
    ip?: string;
    port?: number;
    uid?: number;
    /** 块级功能码声明（非数据行「功能码：04」→ 4——无功能码列表的兜底） */
    fun_default?: number;
    word_order?: string;
    addr_mode?: string;
    offset?: number;
    turbine_count?: number;
    turbine_label?: string;
    formula?: string;
    addr_base?: number;
    writable_seqs?: string;
    device_note?: string;
}

function decl_num(v: unknown): number | null {
    if (typeof v === "number" && Number.isFinite(v)) return v;
    if (typeof v === "string") {
        const m = v.match(/-?\d+(?:\.\d+)?/);
        if (m) return Number(m[0]);
    }
    return null;
}

export function interpret_decls(declsList: Array<Record<string, unknown> | undefined>): MbDecls {
    const out: MbDecls = {};
    for (const d of declsList) {
        if (!d) continue;
        if (out.ip === undefined) {
            const ip = d["ip"] ?? d["conn_ip"];
            if (typeof ip === "string") {
                const m = ip.match(/\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}/);
                if (m) out.ip = m[0];
            }
        }
        if (out.port === undefined && decl_num(d["port"]) !== null) {
            out.port = decl_num(d["port"])!;
        }
        if (out.uid === undefined && decl_num(d["uid"]) !== null) {
            out.uid = decl_num(d["uid"])!;
        }
        if (out.fun_default === undefined && decl_num(d["fun_default"]) !== null) {
            const fv = decl_num(d["fun_default"])!;
            if (fv >= 1 && fv <= 4) out.fun_default = fv;
        }
        if (out.word_order === undefined && typeof d["word_order"] === "string") {
            out.word_order = d["word_order"];
        }
        if (out.addr_mode === undefined && typeof d["addr_mode"] === "string") {
            out.addr_mode = d["addr_mode"];
        }
        if (out.offset === undefined && decl_num(d["offset"]) !== null) {
            out.offset = decl_num(d["offset"])!;
        }
        if (out.turbine_count === undefined && decl_num(d["turbine_count"]) !== null) {
            out.turbine_count = decl_num(d["turbine_count"])!;
        }
        if (out.turbine_label === undefined && typeof d["turbine_label"] === "string") {
            out.turbine_label = d["turbine_label"];
        }
        if (out.formula === undefined && typeof d["formula"] === "string") {
            out.formula = d["formula"];
        }
        if (out.addr_base === undefined && decl_num(d["addr_base"]) !== null) {
            out.addr_base = decl_num(d["addr_base"])!;
        }
        if (out.writable_seqs === undefined && typeof d["writable_seqs"] === "string") {
            out.writable_seqs = d["writable_seqs"];
        }
        if (out.device_note === undefined && typeof d["device_note"] === "string") {
            out.device_note = d["device_note"];
        }
    }
    return out;
}

/** 公式解析：含（风机号-1）×N 项 → 步长 N（展开）；否则 null（地址原样——
 *  「起始地址+序号」且序号已含基址的形态不叠加，2026-10-11 用户裁定） */
export function formula_stride(formula: string | undefined): number | null {
    if (!formula) return null;
    const m = formula.replace(/\s/g, "").match(/[（(]?\s*风机号\s*-\s*1\s*[）)]?\s*[×*]\s*(\d+)/);
    return m ? Number(m[1]) : null;
}

/** 可写序号声明解析："168-172" → 序号集合（有界） */
function writable_set(spec: string | undefined): Set<number> | null {
    if (!spec) return null;
    const out = new Set<number>();
    for (const part of spec.split(/[、,，;；\s]+/)) {
        const m = part.match(/^(\d+)\s*[-~—]\s*(\d+)$/);
        if (m) {
            const a = Number(m[1]);
            const b = Number(m[2]);
            if (b >= a && b - a <= 10000) for (let x = a; x <= b; x++) out.add(x);
        } else if (/^\d+$/.test(part)) {
            out.add(Number(part));
        }
    }
    return out.size > 0 ? out : null;
}

// ── 完成结果 ─────────────────────────────────────────────
export interface MbFinalResult {
    points: RawPoint[];
    excluded: ExcludedGroup[];
    /** 解析面明示行（换算/容错/推断/缺省假定/副本/脏行——随对话可见） */
    notes: string[];
    /** 类型分布（原文标签 → 行数） */
    typeDist: Array<[string, number]>;
    /** 连接声明（表头/非数据行） */
    conn: { ip?: string; port?: number; uid?: number };
    /** 存在缺功能码的点（走功能码缺口追问） */
    needFun: boolean;
    /** 台数缺口（展开步长已知而台数表内不可确定——规则 5 追问） */
    needTurbines: boolean;
    /** 展开上下文（台数应答后由编排器调 expand_template 完成展开；src = 模板块
     *  标题——展开只作用于模板块，同文件其他块（如场级调度点）不随台数复制） */
    expand: { stride: number; label: string; src: string } | null;
    /** 同 identity 重复（(uid,fun,addr)——重复地址拒收清单） */
    dups: Array<{ key: string; names: string[]; addrs: number[] }>;
    /** 本块对账行数 = 产点行 + 数据性排除行（控制组/可写点）；脏行/声明行
     *  不计（RP-MB-09 四表口径：主表 3075 = 3074 产点 + 1 可写排除） */
    dataRows: number;
}

/** 展开模板（台数应答后的确定性展开；模板行 addr 为组内局部地址） */
export function expand_template(
    rows: RawPoint[],
    stride: number,
    label: string,
    count: number,
    base = 0,
    addrFromLocal = true,
): RawPoint[] {
    const out: RawPoint[] = [];
    for (let k = 1; k <= count; k++) {
        const dev = label.replace(/\{n\}/g, String(k));
        const off = (k - 1) * stride;
        for (const r of rows) {
            const p: RawPoint = { ...r };
            if (addrFromLocal) {
                const local = Number(r["addr"]);
                p["addr"] = Number.isFinite(local) ? base + off + local : r["addr"];
            }
            p["_device"] = dev;
            delete p["id"];
            out.push(p);
        }
    }
    return out;
}

// ── 主入口：逐块完成（提取后、id 赋配前调用）────────────────
export function mb_finalize_block(
    points: RawPoint[],
    excluded: ExcludedGroup[],
    decls: Record<string, unknown> | undefined,
    opts: { lookupTitle?: string; turbineCount?: number } = {},
): MbFinalResult {
    const d = interpret_decls([decls]);
    const notes: string[] = [];
    const typeDistMap = new Map<string, number>();
    let modscanCount = 0;
    const modscanSeen = new Map<number, number>();
    let inferred16 = 0;
    let inferred32 = 0;
    let wordOrderSwap: number | null = null;
    let wordOrderKey = "";
    let wordOrderAmbiguous = false;
    if (d.word_order) {
        const wo = word_order_of(d.word_order);
        if (wo) {
            if (wo.swap < 0) {
                wordOrderAmbiguous = true;
            } else {
                wordOrderSwap = wo.swap;
                wordOrderKey = wo.key;
            }
        }
    }
    const dirtyRows: string[] = [];
    const exclExtra = new Map<string, number>();

    // 控制组行级排除（规则 4：点类型语境 遥控/遥调/控制；写功能码 05/06 = 可写
    // 点——读写分区/写单寄存器形态，采集不接入）先行分流
    const work: RawPoint[] = [];
    for (const p of points) {
        const ctx = String(p["_aux_type_ctx"] ?? "").trim();
        if (ctx !== "" && p["point_type"] === undefined) p["point_type"] = ctx;
        const pt = String(p["point_type"] ?? "").trim();
        if (/遥控|遥调|控制/.test(pt) && pt !== "") {
            exclExtra.set(pt, (exclExtra.get(pt) ?? 0) + 1);
            continue;
        }
        // 写功能码（5 写单线圈 / 6 写单寄存器，含 0X05/0X06/05 记法）→ 可写点排除
        let preFun: number | null = null;
        for (const key of ["_aux_fun", "_aux_type", "_aux_regtype"]) {
            const c = classify_mb_cell(String(p[key] ?? ""));
            if (c !== null && c.kind === "fun") {
                preFun = c.fun;
                break;
            }
        }
        if (preFun === 5 || preFun === 6) {
            exclExtra.set("可写点（写功能码）", (exclExtra.get("可写点（写功能码）") ?? 0) + 1);
            continue;
        }
        work.push(p);
    }

    // 副本剔除（域规则 8：同内容人类可读副本不产点不计对账）——块内无对照，
    // 由调用方跨块执行（mb_dedupe_copies）；此处仅剔脏行
    const pre: RawPoint[] = [];
    for (const p of work) {
        const addrRaw = String(p["addr"] ?? "").trim();
        const name = String(p["name"] ?? "").trim();
        const addr = Number(addrRaw);
        if (addrRaw === "" || Number.isNaN(addr)) {
            // 脏行标签：点名优先；点名列为空时回退原始行末个非空格（歌美飒
            // 尾随空列形态——「通讯状态」在行尾独立列，点名列无值）
            let label = name;
            if (label === "") {
                const rawCells = (p["_raw"] as string[] | undefined) ?? [];
                for (let c = rawCells.length - 1; c >= 0; c--) {
                    if (String(rawCells[c] ?? "").trim() !== "") {
                        label = String(rawCells[c]).trim();
                        break;
                    }
                }
            }
            if (label !== "") dirtyRows.push(label);
            continue;
        }
        pre.push(p);
    }

    // modscan 判定：识别层声明优先；否则全表特征（≥80% 已知 fun 行落在编号域
    // 且最小地址 ≥ 10001——0 基表不可能整体满足）
    const cellFun = (p: RawPoint): number | null => {
        for (const key of ["_aux_fun", "_aux_type", "_aux_regtype"]) {
            const c = classify_mb_cell(String(p[key] ?? ""));
            if (c !== null && c.kind === "fun") return c.fun;
        }
        return null;
    };
    const funs = pre.map((p) => cellFun(p));
    // hex 功能码记法计数（RP-MB-08①：FC04/F02/0x05 形态）——识别须在解析面
    // 明示（域规则 1 同款「禁止静默」口径）
    let hexFunCount = 0;
    const hexFunSamples = new Map<number, string>();
    for (const p of pre) {
        let raw = "";
        for (const key of ["_aux_fun", "_aux_type", "_aux_regtype"]) {
            const s = String(p[key] ?? "").trim();
            if (s !== "") {
                raw = s;
                break;
            }
        }
        if (/^(?:fc|f)\s*0?[1-6]$|^0x0?[1-6]$/i.test(raw)) {
            const f = cellFun(p);
            if (f !== null) {
                hexFunCount++;
                if (!hexFunSamples.has(f)) hexFunSamples.set(f, raw.toUpperCase());
            }
        }
    }
    // 寄存器类型列映射明示（RP-MB-11：ABB/Bachmann「寄存器类型」列值为
    // Holding_register/Input_register——映射为功能码须解析面可见，禁止静默）
    {
        const regLabels = new Map<string, { n: number; fun: number }>();
        let regHit = false;
        for (const p of pre) {
            for (const key of ["_aux_fun", "_aux_regtype"]) {
                const s = String(p[key] ?? "").trim();
                if (s === "" || !/[A-Za-z]/.test(s)) continue;
                const f = parse_fun_value(s);
                if (f === null || f < 1 || f > 4) continue;
                if (/register|寄存器/i.test(s)) regHit = true;
                const cur = regLabels.get(s) ?? { n: 0, fun: f };
                cur.n++;
                regLabels.set(s, cur);
                break;
            }
        }
        const regTotal = [...regLabels.values()].reduce((a, b) => a + b.n, 0);
        if (regHit && regLabels.size > 0 && regTotal >= pre.length * 0.5) {
            const parts = [...regLabels.entries()]
                .map(([label, v]) => `${label}→功能码0${v.fun}（${v.n} 点）`)
                .join("、");
            notes.push(`寄存器类型列映射：${parts}`);
        }
    }
    let modscanMode = (d.addr_mode ?? "").toLowerCase().includes("modscan");
    if (!modscanMode) {
        const known = pre
            .map((p, i) => ({ a: Number(p["addr"]), f: funs[i] }))
            .filter((x) => x.f !== null);
        if (known.length >= 10) {
            const inDomain = known.filter((x) => in_modscan_domain(x.f!, x.a)).length;
            const minA = Math.min(...known.map((x) => x.a));
            if (inDomain >= known.length * 0.8 && minA >= 10001) modscanMode = true;
        }
    }

    // 可写序号排除（规则 4：读写分区脚注声明）
    const writable = writable_set(d.writable_seqs);

    // 第一遍：类型同义词收集（含 singed 容错统计）
    let hadSinged = false;
    const syns: Array<(TypeSyn & { label: string }) | null> = [];
    const qtys: Array<number | null> = [];
    for (let i = 0; i < pre.length; i++) {
        const p = pre[i]!;
        for (const key of ["_aux_type", "_aux_fun", "_aux_regtype"]) {
            if (/singed/i.test(String(p[key] ?? ""))) hadSinged = true;
        }
        const ctx = String(p["_aux_type_ctx"] ?? "").trim();
        if (ctx !== "" && p["point_type"] === undefined) p["point_type"] = ctx;
        const seq = decl_num(p["_aux_seq"]);
        if (writable !== null && seq !== null && writable.has(seq)) {
            exclExtra.set("可写点", (exclExtra.get("可写点") ?? 0) + 1);
            syns.push(null);
            qtys.push(null);
            continue;
        }
        let syn: (TypeSyn & { label: string }) | null = null;
        for (const key of ["_aux_type", "_aux_regtype"]) {
            const c = classify_mb_cell(String(p[key] ?? ""));
            if (c !== null && c.kind === "type") {
                syn = c.syn;
                break;
            }
        }
        const qty = decl_num(p["_aux_qty"]);
        if (syn !== null) {
            typeDistMap.set(syn.label, (typeDistMap.get(syn.label) ?? 0) + 1);
            // 类型标签随点携带：跨块副本剔除后按存留点重算类型分布（MB-10
            // bool 80→40 口径——分布须与存留点集一致，禁止计入已剔除副本）
            p["_type_label"] = syn.label;
        }
        syns.push(syn);
        qtys.push(qty === 1 || qty === 2 || qty === 4 ? qty : null);
    }

    // 第二遍：跨度互证/推断 + fun/type/swap 落定 + modscan 换算
    const kept: RawPoint[] = [];
    for (let i = 0; i < pre.length; i++) {
        const p = pre[i]!;
        const seq = decl_num(p["_aux_seq"]);
        if (writable !== null && seq !== null && writable.has(seq)) continue;
        const a = Number(p["addr"]);
        const fun = funs[i];
        const syn = syns[i];
        const qtySpan = qtys[i];
        let type: number | null = syn?.type ?? null;
        let span: number | null = syn?.span ?? qtySpan;
        // 位空间（fun 1/2）→ BOOLEAN，跨度推断不适用
        if (fun === 1 || fun === 2) {
            type = MB_TYPE.BOOLEAN;
            span = 1;
        }
        if (type === null) {
            // 无类型列 → 地址跨度推导（文件序相邻差分；段跳沿用前一点跨度，规则 2）
            let delta: number | null = null;
            for (let j = i + 1; j < pre.length && delta === null; j++) {
                const dj = Number(pre[j]!["addr"]) - a;
                if (dj === 1 || dj === 2 || dj === 4) delta = dj;
            }
            if (delta === null) {
                for (let j = i - 1; j >= 0 && delta === null; j--) {
                    const dj = a - Number(pre[j]!["addr"]);
                    if (dj === 1 || dj === 2 || dj === 4) delta = dj;
                }
            }
            if (delta === 1) {
                type = MB_TYPE.INT16;
                span = 1;
                inferred16++;
            } else if (delta === 2) {
                type = MB_TYPE.INT32;
                span = 2;
                inferred32++;
            } else if (delta === 4) {
                type = MB_TYPE.INT64;
                span = 4;
            } else {
                type = MB_TYPE.INT16;
                span = 1;
            }
        }
        // 类型列与数量列矛盾 → 以跨度为准并回报（规则 2）
        if (syn !== null && qtySpan !== null && syn.span !== qtySpan) {
            notes.push(`类型与寄存器数量矛盾（addr ${a}）：以跨度 ${qtySpan} 为准`);
        }
        // modscan 双轨换算（规则 1）
        let addrOut = a;
        if (modscanMode && fun !== null && in_modscan_domain(fun, a)) {
            addrOut = a - MODSCAN_BASE[fun]!;
            modscanCount++;
            if (!modscanSeen.has(fun)) modscanSeen.set(fun, a);
        }
        // 块级功能码声明兜底（非数据行「功能码：04」——无功能码列表的表）
        let funOut = fun;
        if (funOut === null && d.fun_default !== undefined) funOut = d.fun_default;
        if (funOut === 1 || funOut === 2) {
            // 位空间（线圈/离散输入）：类型恒 BOOLEAN、swap 恒 0
            type = MB_TYPE.BOOLEAN;
            span = 1;
        }
        // swap：16 位/位类型恒 0；多寄存器有声明按声明，无声明缺省 ABCD（swap2）
        let swap = 0;
        if (span !== 1 && funOut !== 1 && funOut !== 2) {
            if (wordOrderSwap !== null) swap = wordOrderSwap;
            else swap = 2;
        }
        const uidRaw = decl_num(p["_aux_uid"]);
        const out: RawPoint = {
            name: p["name"],
            addr: addrOut,
            _src: p["_src"],
            _row: p["_row"],
        };
        const dev = String(p["_device"] ?? "").trim();
        if (dev !== "") out["_device"] = dev;
        // 内部戳记随点携带：类型标签（跨块副本剔除后重算类型分布——MB-10
        // bool 80→40 口径）与段号语境标记（解析面明示消解来源）
        if (p["_type_label"] !== undefined) out["_type_label"] = p["_type_label"];
        if (p["_seg_dev"] === 1) out["_seg_dev"] = 1;
        const pt = String(p["point_type"] ?? "").trim();
        if (pt !== "") out["point_type"] = pt;
        if (funOut !== null) out["fun"] = funOut;
        if (uidRaw !== null) out["uid"] = uidRaw;
        else if (d.uid !== undefined) out["uid"] = d.uid;
        out["type"] = type;
        out["swap"] = swap;
        kept.push(out);
    }

    // 同 identity 重复清单（(uid,fun,addr)——重复地址拒收口径，MB-05）。
    // fun 未绑定时以点类型作空间代理（MB-01 实测：分段抖动使遥测/遥信同居
    // 一块时，fun 全空塌键把跨空间同址误判为重复——遥测@2 与遥信@2 是不同
    // 寄存器空间；同类型同址仍照常报重）
    const spaceOf = (p: RawPoint): string =>
        p["fun"] !== undefined ? String(p["fun"]) : `t:${String(p["point_type"] ?? "").trim()}`;
    const seenId = new Map<string, { names: string[]; addrs: number[] }>();
    for (const p of kept) {
        const key = `${p["uid"] ?? ""}|${spaceOf(p)}|${p["addr"]}`;
        const cur = seenId.get(key) ?? { names: [], addrs: [] };
        cur.names.push(String(p["name"] ?? ""));
        cur.addrs.push(Number(p["addr"]));
        seenId.set(key, cur);
    }
    const dups = [...seenId.entries()]
        .filter(([, v]) => v.addrs.length > 1)
        .map(([key, v]) => ({ key, names: v.names, addrs: v.addrs }));

    // 解析面明示
    if (d.device_note) notes.push(d.device_note);
    if (modscanCount > 0) {
        const parts = [...modscanSeen.entries()].map(
            ([f, sample]) => `${sample} → ${MODSCAN_LABEL[f] ?? `FC${f}`} 0`,
        );
        notes.push(
            `modscan 编号已换算为 modbus 地址（${parts.join("、")}，共 ${modscanCount} 点）`,
        );
    }
    if (hadSinged) notes.push("类型拼写 singed 按 signed 容错映射，不影响接入");
    if (inferred16 + inferred32 > 0) {
        notes.push(
            `无类型列，按地址跨度推导（推断）：差 1 → 16 位 ${inferred16} 处、差 2 → 32 位 ${inferred32} 处`,
        );
    }
    if (wordOrderAmbiguous) {
        notes.push(`字序声明表述含糊（${d.word_order ?? ""}）——需澄清`);
    } else if (wordOrderSwap !== null) {
        notes.push(`字序采纳：${wordOrderKey}（swap=${wordOrderSwap}）`);
    } else if (kept.some((p) => p["swap"] === 2)) {
        notes.push("字序无声明：32/64 位点按缺省 ABCD（swap=2）假定");
    }
    notes.push("标定/系数列原值接入，未应用标定（工程量换算属 calculatord 职责）");
    if (hexFunCount > 0) {
        const parts = [...hexFunSamples.entries()]
            .sort(([a], [b]) => a - b)
            .map(([f, raw]) => `${raw}→${f}`)
            .join("、");
        notes.push(`功能码十六进制记法已映射（${parts}，共 ${hexFunCount} 点）`);
    }
    if (opts.lookupTitle) {
        notes.push(`${opts.lookupTitle}：码表/说明页（无具体地址列），不产点、不计入对账`);
    }
    if (dirtyRows.length > 0) {
        notes.push(
            `脏行（无地址/无类型）不产点：${dirtyRows.slice(0, 3).join("、")}${dirtyRows.length > 3 ? "等" : ""}`,
        );
    }

    // 多设备展开（规则 5）：公式含（风机号−1）×N 项，或横向列组+偏移量声明。
    // 台数已知 → 就地展开；台数未知 → 模板保留 + needTurbines 追问
    const strideF = formula_stride(d.formula);
    let result = kept;
    let needTurbines = false;
    let expand: MbFinalResult["expand"] = null;
    if (strideF !== null && d.turbine_count !== undefined && d.turbine_count > 1) {
        const label = d.turbine_label ?? "风机{n}";
        result = expand_template(
            kept,
            strideF,
            label,
            d.turbine_count,
            d.addr_base ?? 0,
            true,
        );
        notes.push(
            `地址公式展开：${d.turbine_count} 台 × ${kept.length} 点（步长 ${strideF}，基址 ${d.addr_base ?? 0}，设备标签 ${label.replace(/\{n\}/g, "N")}）`,
        );
    } else if (d.offset !== undefined && d.turbine_count === undefined && opts.turbineCount === undefined && kept.length > 0) {
        // 横向列组 + 偏移量声明、台数表内不可确定（「……」不算声明）——模板
        // 保留（首列组），追问台数后按偏移展开（规则 5）
        needTurbines = true;
        expand = {
            stride: d.offset,
            label: d.turbine_label ?? "{n}# 风机",
            src: String(kept[0]?._src ?? ""),
        };
    } else if (d.offset !== undefined && opts.turbineCount !== undefined && opts.turbineCount > 1) {
        const label = d.turbine_label ?? "{n}# 风机";
        result = expand_template(
            kept,
            d.offset,
            label,
            opts.turbineCount,
            0,
            true,
        );
        notes.push(
            `按台数 ${opts.turbineCount} 展开：每台 ${kept.length} 点，偏移量 ${d.offset}，设备标签 ${label.replace(/\{n\}/g, "N")}`,
        );
    }

    const needFun = result.some((p) => p["fun"] === undefined);
    return {
        points: result,
        excluded: [...excluded, ...[...exclExtra.entries()].map(([name, count]) => ({ name, count }))],
        notes,
        typeDist: [...typeDistMap.entries()],
        conn: { ip: d.ip, port: d.port, uid: d.uid },
        needFun,
        needTurbines,
        expand,
        dups,
        dataRows: result.length + [...exclExtra.values()].reduce((a, b) => a + b, 0),
    };
}

// ── 跨块副本剔除与跨表同点合并（域规则 8 + RP-MB-09/10 裁定）──────────
// 两级判定：
//  A. 整块副本（既有口径）：后提取块 ≥80% 点与先前块同名点同址或整体 ±1
//     （京城 Sheet1 无表头副本 / MB-08 Sheet1 ±1 偏移副本）→ 命中行剔除，
//     不产点、不计入对账。
//  B. 跨块同点合并（2026-10-12/13 裁定）：同点名、地址相同或整体 ±1 的
//     跨块点对——配对占比高的少数侧为分表/副本侧，剔出点集：
//     · 分表承载主表缺失的功能码/类型声明（≥50% 配对可补全保留侧缺口）
//       → 合并为同点、计入对账（上海电气四表 4500：主表 3075 + 分表
//       1200/150/75，分表头部 Variable 声明兼作主表功能码/类型补全来源）；
//     · 纯人类可读视图（无增量信息）→ 不产点、不计入对账（华创 sheet1，
//       维持 Sheet3 1971 口径；含 AGC允许 @0~39 vs 主表 @1~40 的 −1 偏移
//       异址副本——提入将与主表构成同设备同名 ×2 撞名噪声）。
// 对账口径（recon）：total = Σ 各块 dataRows − 纯视图剔除行；merged = B 级
// 计入对账的合并行数。调用方以「全表对账 N 行」随解析面展示。
export interface MbDedupeResult {
    points: RawPoint[];
    notes: string[];
    recon: { total: number; merged: number; pureView: number } | null;
}

export function mb_dedupe_copies(
    points: RawPoint[],
    dataRowsBySrc?: ReadonlyMap<string, number>,
): MbDedupeResult {
    const srcOf = (p: RawPoint): string => String(p["_src"] ?? "?");
    // 区间地址（华创 sheet1 视图列「1254-1255」形态）：以区间起点参与 ±1 配对
    // ——Number("1254-1255")=NaN 会让两级剔除全体失明（MB-10 实测 3926=副本
    // 全量并入）
    const addrOf = (p: RawPoint): number => {
        const v = Number(p["addr"]);
        if (!Number.isNaN(v)) return v;
        const m = /^(\d+)\s*-\s*\d+$/.exec(String(p["addr"] ?? "").trim());
        return m !== null ? Number(m[1]!) : NaN;
    };
    const bySrc = new Map<string, RawPoint[]>();
    const order: string[] = [];
    for (const p of points) {
        const src = srcOf(p);
        if (!bySrc.has(src)) {
            bySrc.set(src, []);
            order.push(src);
        }
        bySrc.get(src)!.push(p);
    }
    const dropRows = new Set<RawPoint>();
    const notes: string[] = [];
    let pureView = 0;
    let merged = 0;

    // ── A 级：整块副本（既有口径，后块对前块）──
    for (let i = 1; i < order.length; i++) {
        const laterSrc = order[i]!;
        const later = bySrc.get(laterSrc)!.filter((p) => !dropRows.has(p));
        const buildRef = (withDev: boolean): Map<string, Array<[number, number | undefined]>> => {
            const ref = new Map<string, Array<[number, number | undefined]>>();
            for (let j = 0; j < i; j++) {
                for (const q of bySrc.get(order[j]!) ?? []) {
                    if (dropRows.has(q)) continue;
                    const key = withDev
                        ? `${String(q["_device"] ?? "")}|${String(q["name"] ?? "")}`
                        : String(q["name"] ?? "");
                    const a = addrOf(q);
                    if (!Number.isNaN(a)) {
                        const arr = ref.get(key);
                        const fun = q["fun"] === undefined ? undefined : Number(q["fun"]);
                        if (arr !== undefined) arr.push([a, fun]);
                        else ref.set(key, [[a, fun]]);
                    }
                }
            }
            return ref;
        };
        const buildMapOf = (rows: RawPoint[], withDev: boolean): Map<string, Array<[number, number | undefined]>> => {
            const ref = new Map<string, Array<[number, number | undefined]>>();
            for (const q of rows) {
                const key = withDev
                    ? `${String(q["_device"] ?? "")}|${String(q["name"] ?? "")}`
                    : String(q["name"] ?? "");
                const a = addrOf(q);
                if (Number.isNaN(a)) continue;
                const fun = q["fun"] === undefined ? undefined : Number(q["fun"]);
                const arr = ref.get(key);
                if (arr !== undefined) arr.push([a, fun]);
                else ref.set(key, [[a, fun]]);
            }
            return ref;
        };
        const refHits = (ref: Map<string, Array<[number, number | undefined]>>, probe: Map<string, Array<[number, number | undefined]>>): [number, number] => {
            let meaningful = 0;
            let matched = 0;
            for (const [key, refs] of ref) {
                for (const [b, f] of refs) {
                    meaningful++;
                    const qs = probe.get(key);
                    if (qs !== undefined && qs.some(([a, f2]) => Math.abs(a - b) <= 1 && funOk(f2, f))) {
                        matched++;
                    }
                }
            }
            return [meaningful, matched];
        };
        const funOk = (x: number | undefined, y: number | undefined): boolean =>
            x === undefined || y === undefined || x === y;
        const tryPass = (withDev: boolean): RawPoint[] | null => {
            const ref = buildRef(withDev);
            const drops: RawPoint[] = [];
            let meaningful = 0;
            for (const q of later) {
                const a = addrOf(q);
                if (Number.isNaN(a) && String(q["name"] ?? "") === "") continue;
                meaningful++;
                if (Number.isNaN(a)) continue;
                const key = withDev
                    ? `${String(q["_device"] ?? "")}|${String(q["name"] ?? "")}`
                    : String(q["name"] ?? "");
                const refs = ref.get(key);
                if (
                    refs !== undefined &&
                    refs.some(([b, f]) => Math.abs(b - a) <= 1 && funOk(f, q["fun"] === undefined ? undefined : Number(q["fun"])))
                ) {
                    drops.push(q);
                }
            }
            if (meaningful >= 10 && drops.length >= meaningful * 0.8) {
                // 镜像守卫（MB-10 华创形态）：副本块先于主表出现时，后块（主表）
                // 对前块（副本）≥80% 命中且前块对后块同样 ≥80% 命中——双侧互为
                // 全量镜像，A 级「后块对前块」会误剔主表。此形态交 B 级按配对
                // 占比 + 视图侧裁定；A 级只处理「后块 ⊆ 前块」的真子集副本
                // （京城 Sheet1/MB-08 ±1 副本在后的形态不受影响）
                const [refMeaningful, refMatched] = refHits(ref, buildMapOf(later, withDev));
                if (refMeaningful >= 10 && refMatched >= refMeaningful * 0.8) {
                    return null;
                }
                return drops;
            }
            return null;
        };
        const strict = tryPass(true);
        const loose = strict !== null ? strict : tryPass(false);
        if (loose !== null) {
            for (const q of loose) dropRows.add(q);
            pureView += loose.length;
            notes.push(
                `${laterSrc}：与先前数据块同内容副本（含整体 ±1 偏移），${loose.length} 行不产点、不计入对账`,
            );
        }
    }

    // ── B 级：跨块同点配对（双向——副本块可能先于主表块出现）──
    const survivorsBySrc = new Map<string, RawPoint[]>();
    for (const src of order) {
        survivorsBySrc.set(src, bySrc.get(src)!.filter((p) => !dropRows.has(p)));
    }
    const nameMap = new Map<string, RawPoint[]>();
    for (const src of order) {
        for (const p of survivorsBySrc.get(src)!) {
            const nm = String(p["name"] ?? "").trim();
            if (nm === "") continue;
            const arr = nameMap.get(nm);
            if (arr === undefined) nameMap.set(nm, [p]);
            else arr.push(p);
        }
    }
    const paired = new Set<RawPoint>();
    const pairs: Array<[RawPoint, RawPoint]> = [];
    for (const arr of nameMap.values()) {
        if (arr.length < 2) continue;
        const bySrcName = new Map<string, RawPoint[]>();
        for (const p of arr) {
            const src = srcOf(p);
            const list = bySrcName.get(src);
            if (list === undefined) bySrcName.set(src, [p]);
            else list.push(p);
        }
        if (bySrcName.size < 2) continue;
        // 逐 src 对双指针匹配（±1 偏移副本的地址在排序下交错——贪心相邻配对
        // 会漏配；双指针保证偏移副本的完美匹配）。异 fun = 不同寄存器空间，
        // 不得配对（MB-10 addr1 双空间共存口径）
        const srcsN = [...bySrcName.keys()].sort();
        for (let x = 0; x < srcsN.length; x++) {
            for (let y = x + 1; y < srcsN.length; y++) {
                const sideA = [...bySrcName.get(srcsN[x]!)!].sort((a, b) => addrOf(a) - addrOf(b));
                const sideB = [...bySrcName.get(srcsN[y]!)!].sort((a, b) => addrOf(a) - addrOf(b));
                let j = 0;
                for (const a of sideA) {
                    if (paired.has(a)) continue;
                    const aa = addrOf(a);
                    while (j < sideB.length && addrOf(sideB[j]!) < aa - 1) j++;
                    for (let k = j; k < sideB.length && addrOf(sideB[k]!) <= aa + 1; k++) {
                        const b = sideB[k]!;
                        if (paired.has(b)) continue;
                        // 异 fun = 不同寄存器空间，不得配对（MB-10 addr1 双空间
                        // 共存口径）；fun 未绑定时以点类型作空间代理（MB-01）
                        const fa = a["fun"] === undefined ? undefined : Number(a["fun"]);
                        const fb = b["fun"] === undefined ? undefined : Number(b["fun"]);
                        if (fa !== undefined && fb !== undefined && fa !== fb) continue;
                        const ta = String(a["point_type"] ?? "").trim();
                        const tb = String(b["point_type"] ?? "").trim();
                        if (fa === undefined && fb === undefined && ta !== "" && tb !== "" && ta !== tb) continue;
                        paired.add(a);
                        paired.add(b);
                        pairs.push([a, b]);
                        break;
                    }
                }
            }
        }
    }
    if (pairs.length >= 20) {
        const perSrcTotal = new Map<string, number>();
        const perSrcPaired = new Map<string, number>();
        for (const src of order) perSrcTotal.set(src, survivorsBySrc.get(src)!.length);
        for (const p of paired) {
            const src = srcOf(p);
            perSrcPaired.set(src, (perSrcPaired.get(src) ?? 0) + 1);
        }
        // 配对占比高的一侧为分表/副本侧（主表块大部分点无跨块对应）；
        // 占比打平（双侧互为全量镜像，华创 sheet1 视图在前形态）时按
        // 「区间地址占比」裁定——人类可读视图以「1254-1255」区间列展示
        // 跨度，主表用标量地址（Sheet3 mb_addr）
        const sideScore = (p: RawPoint): number => {
            const src = srcOf(p);
            return (perSrcPaired.get(src) ?? 0) / Math.max(1, perSrcTotal.get(src) ?? 1);
        };
        const viewish = (p: RawPoint): number => {
            const alive = (bySrc.get(srcOf(p)) ?? []).filter((q) => !dropRows.has(q));
            if (alive.length === 0) return 0;
            const rng = alive.filter((q) =>
                /^(\d+)\s*-\s*\d+$/.test(String(q["addr"] ?? "").trim()),
            ).length;
            return rng / alive.length;
        };
        const dropSides = new Map<string, Array<[RawPoint, RawPoint]>>();
        for (const [a, b] of pairs) {
            const dropSide =
                Math.abs(sideScore(a) - sideScore(b)) <= 0.05
                    ? (viewish(a) >= viewish(b) ? a : b)
                    : (sideScore(a) >= sideScore(b) ? a : b);
            const keepSide = dropSide === a ? b : a;
            const src = srcOf(dropSide);
            const arr = dropSides.get(src);
            if (arr === undefined) dropSides.set(src, [[dropSide, keepSide]]);
            else arr.push([dropSide, keepSide]);
        }
        for (const [src, plist] of dropSides) {
            const total = perSrcTotal.get(src) ?? plist.length;
            if (plist.length < 20 || plist.length < total * 0.6) continue;
            // 增量信息判定（2026-10-13 适用边界裁定）：≥50% 配对可补全保留侧
            // 功能码/类型缺口 → 分表声明权威（合并计入对账）；否则纯视图
            let filled = 0;
            for (const [d, k] of plist) {
                let hit = false;
                for (const key of ["fun", "point_type"] as const) {
                    const dv = d[key];
                    const dvS = String(dv ?? "").trim();
                    if (dv !== undefined && dvS !== "" && String(k[key] ?? "").trim() === "") {
                        k[key] = dv;
                        hit = true;
                    }
                }
                if (hit) filled++;
            }
            for (const [d] of plist) dropRows.add(d);
            if (filled * 2 >= plist.length) {
                merged += plist.length;
                notes.push(
                    `${src}：跨表同址同名合并为同点（分表声明补全功能码/类型），${plist.length} 行并入对账`,
                );
            } else {
                pureView += plist.length;
                notes.push(
                    `${src}：人类可读副本视图（同/邻址同名、无增量声明），${plist.length} 行不产点、不计入对账`,
                );
            }
        }
    }

    if (dropRows.size === 0) return { points, notes, recon: null };
    let reconTotal: number | null = null;
    if (dataRowsBySrc !== undefined) {
        reconTotal = -pureView;
        for (const [, n] of dataRowsBySrc) reconTotal += n;
    }
    return {
        points: points.filter((p) => !dropRows.has(p)),
        notes,
        recon: reconTotal !== null ? { total: reconTotal, merged, pureView } : null,
    };
}

// ── 功能码应答绑定（缺口追问后的确定性绑定）────────────────
// 应答形态：「遥测全部是输入寄存器（功能码 4），遥信是离散输入（功能码 2）」
// 「全是保持寄存器功能码 3」「遥测全是功能码 4，遥信功能码 2」——按点类型语境
// 绑定；「全部/都是」形态绑定全部缺 fun 点。
export function mb_bind_fun_answer(points: RawPoint[], userText: string): number {
    let bound = 0;
    const txt = String(userText ?? "");
    const ctxFun = new Map<string, number>();
    const ctxLabels = ["遥测", "遥信", "遥脉", "遥控", "遥调"];
    for (const label of ctxLabels) {
        const m = txt.match(
            new RegExp(`${label}[^。；\\n]{0,24}?(?:功能码|FC|输入寄存器|离散输入|保持寄存器|线圈)[^0-9]{0,6}([1-4])`, "u"),
        );
        if (m) {
            ctxFun.set(label, Number(m[1]));
            continue;
        }
        // 「遥测…输入寄存器（功能码 4）」括注形态
        const m2 = txt.match(
            new RegExp(`${label}[^。；\\n]{0,30}?功能码\\s*([1-4])`, "u"),
        );
        if (m2) ctxFun.set(label, Number(m2[1]));
        else {
            const m3 = txt.match(
                new RegExp(`${label}[^。；\\n]{0,20}(输入寄存器|离散输入|保持寄存器|线圈)`, "u"),
            );
            if (m3) {
                const word = m3[1];
                ctxFun.set(label, word === "输入寄存器" ? 4 : word === "保持寄存器" ? 3 : word === "离散输入" ? 2 : 1);
            }
        }
    }
    const mAll = txt.match(/(?:全部|都是|全为|均为)[^。；\n]{0,20}?(?:功能码|FC)\s*([1-4])/);
    const allFun = mAll ? Number(mAll[1]) : null;
    // 裸值兜底（MB-07 实测）：无类型语境也无「全部」限定时的扁平应答「功能码 3」
    // ——用户显式给出的全局默认，采纳不算猜；仅在无任何语境形态时生效
    const mBare = txt.match(/(?:功能码|FC)\s*0?([1-4])(?![0-9])/);
    const bareFun = ctxFun.size === 0 && allFun === null && mBare ? Number(mBare[1]) : null;
    for (const p of points) {
        if (p["fun"] !== undefined) continue;
        const pt = String(p["point_type"] ?? "").trim();
        const f = ctxFun.get(pt) ?? allFun ?? bareFun;
        if (f !== null && f !== undefined) {
            p["fun"] = f;
            if (f === 1 || f === 2) {
                p["type"] = MB_TYPE.BOOLEAN;
                p["swap"] = 0;
            }
            bound++;
        }
    }
    return bound;
}

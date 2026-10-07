// c4/agent/src/orchestrator/point_pipeline.ts — 点表提取与归一化管道
// agent.md §2.13（2026-10-07）：分块提取与合并、序列重构与重置检测、全记录一致去重、
// 确定性整段归一化（point_normalization 规格驱动）、excluded 跨块聚合。
// 协议无关红线（§2.13.1）：本模块不出现任何协议专属字样/分区数字——规则与数字全部
// 来自注册信息规格或 LLM 识别结果；数值换算由代码机制性执行（归一化执行主体是管道）。

import type { ParsedPointBlock, ParsedPointFile } from "../subagents/tools/doc_parsers.js";

// ── 类型 ──────────────────────────────────────────────────

/** 归一化规格（注册信息 point_normalization，loader 透传的机读数据） */
export interface NormSpec {
    type_field: string;
    per_type: Record<string, { start: number; hex_start_alias?: number }>;
    on_first_value?: Record<string, string>;
    otherwise?: string;
    hex_markers?: string[];
}

export interface ExcludedGroup {
    name: string;
    count: number;
}

export interface RawPoint {
    [k: string]: unknown;
}

export interface ResetInfo {
    /** 序列键（类型 × 文件） */
    seqKey: string;
    /** 回落位置（1 起序内序号）与前后值 */
    at: number;
    from: string;
    to: string;
    /** 回落总数 */
    drops: number;
    /** 伴生问题摘要（重复地址/缺地址行） */
    extra: string[];
    /** 该序列首值命中 hex 起点别名（如 4001H = 16385）——hex 形态判定随回落
     *  追问可见（func_test_case_real_points.md RP-06） */
    alias?: { hex: number; dec: number };
    /** 触发判定时的块签名（同签名不重跑提取） */
    sig?: string;
}

export interface PipelineResult {
    /** 归一化后的点（含 _src/_row 内部字段；addr 为换算后数值，未覆盖形态原样） */
    points: RawPoint[];
    excluded: ExcludedGroup[];
    /** 未做归一化换算（原样转写）的点数 */
    asisCount: number;
    /** 序列回落（非 null = 不接入，追问） */
    reset: ResetInfo | null;
}

/** LLM 调用回调（编排器注入 llm_json；测试注入桩） */
export type LlmCall = (
    promptFile: string,
    params: Record<string, string>,
    input: string,
) => Promise<Record<string, unknown> | null>;

// ── 常量（agent.md §2.13.5：行块上限常量可调）──────────────

/** 行块字符上限 */
const CHUNK_CHARS = 12000;
/** 行块行数上限（输出 token 预算内：~150 点 × ~60 token） */
const CHUNK_ROWS = 150;
/** 超过该行数的逻辑块走两遍法（识别 + 确定性转录），避免逐行 LLM 转写 */
export const TWO_PASS_ROWS = 400;
/** 批量英文标识翻译的每批名字数：长数组输出可靠性随长度劣化（RP-02 实测
 *  近 400 名单批返回 13 个空串），64 以内模型逐项翻译质量稳定 */
const ID_BATCH = 64;

// ── 数值解析 ──────────────────────────────────────────────

/** 十进制解析；失败按 16 进制解释（含 0x 前缀/H 后缀/裸 hex 字符）；仍失败返回 null */
export function parse_addr_value(raw: string): number | null {
    const s = String(raw ?? "").trim();
    if (s === "") return null;
    if (/^0x[0-9a-f]+$/i.test(s)) return parseInt(s.slice(2), 16);
    if (/^[0-9a-f]+$/i.test(s)) {
        // 纯数字按十进制；含 A~F 字符按 16 进制
        if (/^\d+$/.test(s)) {
            const n = Number(s);
            return Number.isSafeInteger(n) ? n : null;
        }
        const n = parseInt(s, 16);
        return Number.isNaN(n) ? null : n;
    }
    const n = Number(s); // 科学计数法等数值形态（Excel 存储值字符串化）
    return Number.isSafeInteger(n) ? n : null;
}

function has_hex_marker(raw: string, markers: string[]): boolean {
    const s = String(raw ?? "").trim();
    const up = s.toUpperCase();
    return markers.some((m) => {
        const mu = m.toUpperCase();
        return mu === "0X" ? up.startsWith("0X") : up.endsWith(mu);
    });
}

function strip_hex_marker(raw: string, markers: string[]): string {
    const s = String(raw ?? "").trim();
    for (const m of markers) {
        const mu = m.toUpperCase();
        if (mu === "0X" && s.toUpperCase().startsWith("0X")) return s.slice(2);
        if (mu !== "0X" && s.toUpperCase().endsWith(mu)) return s.slice(0, -m.length);
    }
    return s;
}

/** 序列解读模式（agent.md §2.13.2(b)：显式标记优先；首值恰为 hex_start_alias → 整段
 *  16 进制）。回落检测与归一化必须用同一解读——否则 hex 表中的纯数字成员（6400~6409）
 *  按十进制读会制造假回落（2026-10-07 RP-07 实测：640F→6410 假下降） */
function seq_decode_mode(
    seq: SeqPoint[],
    conf: { start: number; hex_start_alias?: number } | undefined,
    markers: string[],
): (raw: string) => number | null {
    let hexMode = seq.some((s) => has_hex_marker(s.raw, markers));
    if (!hexMode && conf?.hex_start_alias !== undefined && seq.length > 0) {
        const first = parse_addr_value(seq[0]!.raw);
        if (first === conf.hex_start_alias) hexMode = true;
    }
    return (raw: string): number | null => {
        if (hexMode) {
            const s = strip_hex_marker(raw, markers);
            const n = parseInt(s, 16);
            return Number.isNaN(n) ? null : n;
        }
        return parse_addr_value(raw);
    };
}

// ── 序列重构 / 回落检测 / 去重 ────────────────────────────

interface SeqPoint {
    p: RawPoint;
    raw: string;
    num: number | null;
}

/** 按（type_field 值 × 来源文件）重构有序序列（文档序，保序由解析与分块保证） */
function rebuild_sequences(
    points: RawPoint[],
    typeField: string,
): Map<string, SeqPoint[]> {
    const seqs = new Map<string, SeqPoint[]>();
    for (const p of points) {
        const t = String(p[typeField] ?? "");
        const src = String(p["_src"] ?? "text");
        const key = `${t}×${src}`;
        const list = seqs.get(key) ?? [];
        const raw = String(p["addr"] ?? "");
        list.push({ p, raw, num: parse_addr_value(raw) });
        seqs.set(key, list);
    }
    return seqs;
}

/** 回落 = 严格下降（相等值不判回落，交去重/L1）；数值化：十进制失败按 16 进制 */
function first_drop(seq: SeqPoint[]): { at: number; from: string; to: string; drops: number } | null {
    let drops = 0;
    let last: number | null = null;
    let at = -1;
    let from = "";
    let to = "";
    for (let i = 0; i < seq.length; i++) {
        const n = seq[i]!.num;
        if (n === null) continue; // 非数值（缺地址等）不参与单调性
        if (last !== null && n < last) {
            drops++;
            if (at === -1) {
                at = i + 1;
                from = seq[i - 1]!.raw;
                to = seq[i]!.raw;
            }
        }
        last = n;
    }
    return drops > 0 ? { at, from, to, drops } : null;
}

/** 去重判据 = 全记录一致（各提取字段不含 reason/内部字段、逐项相同），仅在单序列内 */
function dedupe_seq(seq: SeqPoint[]): SeqPoint[] {
    const seen = new Set<string>();
    const out: SeqPoint[] = [];
    for (const sp of seq) {
        const p = sp.p;
        const key = Object.keys(p)
            .filter((k) => !k.startsWith("_") && k !== "reason")
            .sort()
            .map((k) => `${k}=${String(p[k])}`)
            .join("|");
        if (seen.has(key)) continue;
        seen.add(key);
        out.push(sp);
    }
    return out;
}

// ── 确定性归一化 ──────────────────────────────────────────

function norm_points_of_seq(
    seqKey: string,
    seq: SeqPoint[],
    spec: NormSpec,
): { points: RawPoint[]; asis: number } {
    const t = seqKey.split("×")[0] ?? "";
    const conf = spec.per_type[t];
    const markers = spec.hex_markers ?? ["0x", "H"];
    const out: RawPoint[] = [];
    let asis = 0;
    if (!conf || typeof conf.start !== "number") {
        // 规格未覆盖该类型 → 原样转写
        for (const { p, raw } of seq) {
            out.push({ ...p, addr: raw, _norm_reason: `类型 ${t} 无归一化规格，原样转写` });
            asis++;
        }
        return { points: out, asis };
    }
    const start = conf.start;
    const decode = seq_decode_mode(seq, conf, markers);
    // 首值判定（首值 = 该连续段文档序第一个有数值的点）
    let first: number | null = null;
    for (const s of seq) {
        const n = decode(s.raw);
        if (n !== null) {
            first = n;
            break;
        }
    }
    let mode: "asis" | "add" | "add1" | "raw_asis";
    if (first === start) mode = "asis";
    else if (first === 0) mode = "add";
    else if (first === 1) mode = "add1";
    else mode = "raw_asis";
    const shift = mode === "add" ? start : mode === "add1" ? start - 1 : 0;
    for (const s of seq) {
        const n = decode(s.raw);
        const p = { ...s.p };
        if (n === null) {
            // 缺地址/非数值：原样保留（addr 缺省交 L1 必填追问）
            if (String(s.raw) !== "") p["addr"] = s.raw;
            out.push(p);
            continue;
        }
        if (mode === "raw_asis" || mode === "asis") {
            p["addr"] = n;
            if (mode === "raw_asis") {
                p["_norm_reason"] = `序列首值 ${first} 不在起点/0/1 形态内，整段原样转写`;
                asis++;
            }
        } else {
            p["addr"] = n + shift;
        }
        out.push(p);
    }
    return { points: out, asis };
}

// ── excluded 聚合 ─────────────────────────────────────────

function aggregate_excluded(
    perBlock: Array<ExcludedGroup[] | null>,
): ExcludedGroup[] {
    const map = new Map<string, number>();
    for (const groups of perBlock) {
        if (!groups) continue;
        for (const g of groups) {
            map.set(g.name, (map.get(g.name) ?? 0) + g.count);
        }
    }
    return [...map.entries()].map(([name, count]) => ({ name, count }));
}

// ── 主入口：合并 + 重置检测 + 去重 + 归一化 ───────────────

export function merge_and_normalize(
    rawPoints: RawPoint[],
    perBlockExcluded: Array<ExcludedGroup[] | null>,
    spec: NormSpec | null | undefined,
): PipelineResult {
    const excluded = aggregate_excluded(perBlockExcluded);
    const typeField = spec?.type_field ?? "point_type";
    if (rawPoints.length === 0) {
        return { points: [], excluded, asisCount: 0, reset: null };
    }
    const seqs = rebuild_sequences(rawPoints, typeField);
    // 1) 回落检测（任一序列回落 → 全体不接入，追问）；解读模式与归一化一致
    for (const [key, seq] of seqs) {
        const t = key.split("×")[0] ?? "";
        const decode = seq_decode_mode(seq, spec?.per_type[t], spec?.hex_markers ?? ["0x", "H"]);
        const numerified = seq.map((sp) => ({ ...sp, num: decode(sp.raw) }));
        const drop = first_drop(numerified);
        if (drop !== null) {
            const dupAddrs = new Map<string, number>();
            let missing = 0;
            for (const { raw } of seq) {
                if (String(raw).trim() === "") missing++;
                else dupAddrs.set(raw, (dupAddrs.get(raw) ?? 0) + 1);
            }
            const dups = [...dupAddrs.entries()].filter(([, c]) => c > 1);
            const extra: string[] = [];
            if (dups.length > 0) {
                extra.push(
                    `重复地址值 ${dups.length} 个（如 ${dups[0]![0]}×${dups[0]![1]}）`,
                );
            }
            if (missing > 0) extra.push(`无地址行 ${missing} 行`);
            const confT = spec?.per_type[t];
            const alias =
                confT?.hex_start_alias !== undefined &&
                typeof confT.start === "number" &&
                seq.length > 0 &&
                parse_addr_value(seq[0]!.raw) === confT.hex_start_alias
                    ? { hex: confT.hex_start_alias, dec: confT.start }
                    : undefined;
            return {
                points: [],
                excluded,
                asisCount: 0,
                reset: {
                    seqKey: key,
                    at: drop.at,
                    from: drop.from,
                    to: drop.to,
                    drops: drop.drops,
                    extra,
                    alias,
                },
            };
        }
    }
    // 2) 去重（序列内全记录一致）+ 3) 确定性归一化
    const points: RawPoint[] = [];
    let asisCount = 0;
    for (const [key, seq] of seqs) {
        const deduped = dedupe_seq(seq);
        if (!spec) {
            points.push(...deduped.map((sp) => sp.p));
            continue;
        }
        const r = norm_points_of_seq(key, deduped, spec);
        points.push(...r.points);
        asisCount += r.asis;
    }
    return { points, excluded, asisCount, reset: null };
}

// ── 分块提取（小块：LLM 逐行块忠实转写）─────────────────

interface Chunk {
    /** 语境行（文件/sheet/段标题） */
    context: string;
    rows: string[][];
    /** 本块首行在逻辑块内的行号（1 起，回落定位用） */
    startRow: number;
}

function chunk_block(block: ParsedPointBlock): Chunk[] {
    if (block.kind === "text") {
        // 自由文本：按字符切块（无行语义）
        const chunks: Chunk[] = [];
        let buf: string[][] = [];
        let size = 0;
        for (const line of block.rows) {
            buf.push(line);
            size += line.join(",").length + 1;
            if (size >= CHUNK_CHARS) {
                chunks.push({ context: "", rows: buf, startRow: 1 });
                buf = [];
                size = 0;
            }
        }
        if (buf.length > 0 || chunks.length === 0) {
            chunks.push({ context: "", rows: buf, startRow: 1 });
        }
        return chunks;
    }
    // 非数据行锚点：row n 表示第 n 个数据行之前的段标题
    const titleAt = new Map<number, string>();
    for (const nd of block.nonData) titleAt.set(nd.row, nd.text);
    const chunks: Chunk[] = [];
    let cur: string[][] = [];
    let curChars = 0;
    let curStart = 1;
    let lastTitle: string | null = null;
    for (let i = 0; i < block.rows.length; i++) {
        const title = titleAt.get(i + 1);
        if (title !== undefined) lastTitle = title;
        const needContext = cur.length === 0;
        cur.push(block.rows[i]!);
        curChars += block.rows[i]!.join(",").length + 1;
        const flush =
            cur.length >= CHUNK_ROWS || curChars >= CHUNK_CHARS || i === block.rows.length - 1;
        if (flush) {
            const ctxLines: string[] = [];
            if (lastTitle !== null) ctxLines.push(`# 段: ${lastTitle}`);
            chunks.push({ context: ctxLines.join("\n"), rows: cur, startRow: curStart });
            cur = [];
            curChars = 0;
            curStart = i + 2;
        } else if (needContext) {
            curStart = i + 1;
        }
    }
    return chunks;
}

/** 小块提取的 LLM 输入文本：文件/sheet 语境行 + 段标题 + 原样行 */
function render_chunk(file: ParsedPointFile, block: ParsedPointBlock, chunk: Chunk): string {
    const lines: string[] = [`# 文件: ${file.name}`, `# sheet: ${block.title}`];
    if (chunk.context !== "") lines.push(chunk.context);
    for (const r of chunk.rows) lines.push(r.join(","));
    return lines.join("\n");
}

// ── 两遍法（大块：识别 + 确定性转录）────────────────────

export interface BlockIdent {
    addr_col: number;
    name_col: number;
    point_type?: string;
    declared_count?: number | null;
    segments?: Array<{ row: number; point_type?: string; excluded?: boolean }>;
    notes?: string;
}

/** 两遍法第一遍：结构识别（列语义/类型/分段/排除），输入仅语境+样例 */
export async function identify_block(
    file: ParsedPointFile,
    block: ParsedPointBlock,
    params: Record<string, string>,
    userText: string,
    call: LlmCall,
): Promise<BlockIdent> {
    const sample: string[] = [];
    if (block.header) sample.push(block.header.join(","));
    const head = block.rows.slice(0, 15);
    head.forEach((r, i) => sample.push(`${i + 1}: ${r.join(",")}`));
    if (block.rows.length > 18) {
        sample.push("…");
        block.rows.slice(-3).forEach((r, i) => {
            sample.push(`${block.rows.length - 3 + i + 1}: ${r.join(",")}`);
        });
    }
    const ndLines = block.nonData
        .slice(0, 50)
        .map((nd) => `行${nd.row} 之前: ${nd.text}`)
        .join("\n");
    const context = [
        `文件: ${file.name}`,
        `数据块: ${block.title}（共 ${block.rows.length} 个数据行）`,
        block.header ? `表头: ${block.header.join(",")}` : "表头: 无（首行即数据）",
        ndLines !== "" ? `非数据行（段标题）:\n${ndLines}` : "非数据行: 无",
    ].join("\n");
    const r = await call("point_identify_prompt.txt", params, `${context}\n\n<user_input>\n${userText}\n</user_input>\n\n样例（行号: 原始行）:\n${sample.join("\n")}`);
    const addrCol = r && Number.isInteger(Number(r["addr_col"])) ? Number(r["addr_col"]) : -1;
    if (addrCol < 0) {
        throw new Error(
            `无法识别「${file.name}/${block.title}」的地址列——请检查点表或补充说明文字`,
        );
    }
    return {
        addr_col: addrCol,
        name_col: r && Number.isInteger(Number(r["name_col"])) ? Number(r["name_col"]) : -1,
        point_type: typeof r?.["point_type"] === "string" ? (r["point_type"] as string) : undefined,
        declared_count:
            typeof r?.["declared_count"] === "number" ? (r["declared_count"] as number) : null,
        segments: Array.isArray(r?.["segments"])
            ? (r["segments"] as BlockIdent["segments"])
            : undefined,
        notes: typeof r?.["notes"] === "string" ? (r["notes"] as string) : undefined,
    };
}

/** 两遍法第二遍：确定性逐行转录（忠实转写原值，排除段计数不提取） */
export function transcribe_block(
    block: ParsedPointBlock,
    ident: BlockIdent,
    typeName: string,
): { rows: RawPoint[]; excluded: ExcludedGroup[] } {
    const segs = [...(ident.segments ?? [])].sort((a, b) => a.row - b.row);
    const excluded: ExcludedGroup[] = [];
    const rows: RawPoint[] = [];
    // 段上下文向前携带：segments 只锚定段首行，中间行沿用当前段（RP-04 实测：
    // 只在边界行取段类型时中段行全部落空 ident.point_type，跨段序列被误判回落）
    type SegmentIdent = NonNullable<BlockIdent["segments"]>[number];
    let cur: SegmentIdent | undefined;
    for (let i = 0; i < block.rows.length; i++) {
        const seg = segs.find((s) => s.row === i + 1);
        if (seg !== undefined) cur = seg;
        if (cur?.excluded) {
            // 排除段：计点到下一个段边界（end 为本段末行的 1 基行号；i 置
            // end-1 使 i++ 后恰好落在下一段边界行——RP-04：遥控段后紧跟遥调段，
            // 原实现 i=end 会吞掉下一段边界行）
            const next = segs.find((s) => s.row > i + 1);
            const end = next ? next.row - 1 : block.rows.length;
            const count = end - (i + 1) + 1;
            excluded.push({ name: segTitle(block, cur.row), count });
            i = end - 1; // for 循环再 +1
            cur = undefined; // 下一段须由边界行重新锚定
            continue;
        }
        const raw = block.rows[i]!;
        const type = cur?.point_type ?? ident.point_type ?? "";
        const p: RawPoint = {
            _src: block.title,
            _row: i + 1,
        };
        if (type !== "") p[typeName] = type;
        p["addr"] = String(raw[ident.addr_col] ?? "").trim();
        const name = ident.name_col >= 0 ? String(raw[ident.name_col] ?? "").trim() : "";
        p["name"] = name;
        rows.push(p);
    }
    return { rows, excluded };
}

function segTitle(block: ParsedPointBlock, row: number): string {
    const nd = block.nonData.find((n) => n.row === row);
    if (nd) return nd.text;
    // 无段锚定标题的回退：块首段（row 1）即整个数据块 → 块标题（sheet 名，
    // 如整表排除的 YK sheet）；中段无锚定 → 行号占位
    return row === 1 ? block.title : `段@行${row}`;
}

// ── 批量英文标识翻译 ─────────────────────────────────────

export function is_compliant_id(name: string): boolean {
    return /^[a-zA-Z][a-zA-Z0-9_]*$/.test(name) && name.length <= 64;
}

/** 代码侧确定性修复（LLM 对禁令不可靠，RP-02 三轮实测：187/493 数字开头、
 * 63/493 超长、13/493 夹带中文）：先剔除非 [a-zA-Z0-9_] 字符并收拢下划线，
 * 数字开头补 p 前缀（identifier 要求字母开头），超 62 截断（留撞名顺延 _2
 * 的余量，段边界优先保语义完整）。全空 → 空串（交 failed 计数走 fail-visible） */
function fixup_raw_id(id: string): string {
    let s = id
        .trim()
        .replace(/[^a-zA-Z0-9_]+/g, "_")
        .replace(/_+/g, "_")
        .replace(/^_+|_+$/g, "");
    if (s === "") return "";
    if (/^[0-9]/.test(s)) s = `p${s}`;
    if (s.length > 62) {
        const cut = s.slice(0, 62);
        const at = cut.lastIndexOf("_");
        s = at >= 40 ? cut.slice(0, at) : cut;
    }
    return s;
}

/** 厂家占位点名的闭集确定性翻译：真实点表普遍以「空」「备用」整词标记未使用
 * 备用点（高力板镇 13/256、晨光 57、11号光伏区 21），语义即 spare——模型对
 * 这类无信息量名确定性拒译（RP-03 实测空串重试穷尽仍空），故走代码侧闭集
 * 映射而非生成；仅整名精确匹配，撞名由顺延兜底 */
const PLACEHOLDER_ID: ReadonlyMap<string, string> = new Map([
    ["空", "spare"],
    ["备用", "spare"],
]);

/** 批量翻译：仅翻译不合规英文标识的点名；代码侧确定性去重（撞名追加序号） */
export async function translate_ids(
    points: RawPoint[],
    call: LlmCall,
    existing: ReadonlySet<string> = new Set(),
): Promise<{ filled: number; failed: number }> {
    const used = new Set<string>(existing);
    const targets: number[] = [];
    for (let i = 0; i < points.length; i++) {
        const name = String(points[i]!["name"] ?? "").trim();
        const id = String(points[i]!["id"] ?? "").trim();
        if (id !== "") {
            // 提取层给出的 id 需修复：数字开头补前缀、超长截断（确定性规则统一）
            points[i]!["id"] = fixup_raw_id(id);
            used.add(String(points[i]!["id"]).toLowerCase());
            continue;
        }
        if (name !== "" && is_compliant_id(name)) {
            let cand = name.toLowerCase();
            let n = 2;
            while (used.has(cand)) cand = `${name.toLowerCase()}_${n++}`;
            points[i]!["id"] = cand;
            used.add(cand);
            continue;
        }
        const ph = PLACEHOLDER_ID.get(name);
        if (ph !== undefined) {
            let cand = ph;
            let n = 2;
            while (used.has(cand)) cand = `${ph}_${n++}`;
            points[i]!["id"] = cand;
            used.add(cand);
            continue;
        }
        if (name !== "") targets.push(i);
    }
    let filled = points.length - targets.length;
    // 批内失败项逐轮收窄重试（整批 → 16 → 单条）：LLM 长数组偶发空串/不合规
    // 是可靠性问题而非名字不可译（RP-02 实测 13/493），重试仍是 LLM 翻译
    // （§3.2.1.3b 翻译非确定可接受），不是代码侧生成；穷尽仍失败才走 fail-visible
    let pending = targets.slice();
    for (const size of [ID_BATCH, 16, 1]) {
        if (pending.length === 0) break;
        const next: number[] = [];
        for (let start = 0; start < pending.length; start += size) {
            const idx = pending.slice(start, start + size);
            const names = idx.map((i) => String(points[i]!["name"]));
            const r = await call(
                "id_translate_batch_prompt.txt",
                {},
                JSON.stringify(names),
            );
            const ids = r && Array.isArray(r["ids"]) ? (r["ids"] as unknown[]) : null;
            if (!ids || ids.length !== names.length) {
                next.push(...idx);
                continue;
            }
            for (let j = 0; j < idx.length; j++) {
                const id = fixup_raw_id(String(ids[j] ?? ""));
                if (id === "" || !is_compliant_id(id)) {
                    next.push(idx[j]!);
                    continue;
                }
                let cand = id.toLowerCase();
                let n = 2;
                while (used.has(cand)) cand = `${id.toLowerCase()}_${n++}`;
                points[idx[j]!]!["id"] = cand;
                used.add(cand);
                filled++;
            }
        }
        pending = next;
    }
    return { filled, failed: pending.length };
}

/** 类型值规范化：LLM 可能回中文枚举值（遥信），映射回枚举键（yx）——与
 *  point_normalization.per_type 键对齐；未知值原样（交 raw_asis 兜底） */
export function canonicalize_types(
    points: RawPoint[],
    typeField: string,
    enumMap: Record<string, string> | null | undefined,
): void {
    if (!enumMap) return;
    const reverse = new Map<string, string>();
    for (const [k, v] of Object.entries(enumMap)) {
        reverse.set(String(v).trim(), k);
        reverse.set(String(k).trim(), k);
    }
    for (const p of points) {
        const v = p[typeField];
        if (typeof v !== "string" || v === "") continue;
        p[typeField] = reverse.get(v.trim()) ?? v.trim();
    }
}

// ── 编排器入口：单文件块提取（自动选小块/两遍法）────────

export async function extract_file_block(
    file: ParsedPointFile,
    block: ParsedPointBlock,
    params: Record<string, string>,
    userText: string,
    call: LlmCall,
): Promise<{ rows: RawPoint[]; excluded: ExcludedGroup[]; declared: number | null }> {
    if (block.kind === "text") {
        // 自由文本块：按字符块 LLM 提取（表格块一律两遍法——2026-10-07 RP 实测：
        // 逐块 LLM 转写对 excluded 锚定（用户文字数量被照抄进每块）与 JSON 合法性
        // （hex 地址裸 token）不可靠；识别与转写分离后 LLM 只做语义识别，
        // 转写由代码确保忠实，见 agent.md §2.13.5 实现注记）
        const chunks = chunk_block(block);
        const rows: RawPoint[] = [];
        const excluded: ExcludedGroup[] = [];
        let declared: number | null = null;
        for (const chunk of chunks) {
            const input =
                block.kind === "text"
                    ? `<file_data>\n${chunk.rows.join("\n").slice(0, CHUNK_CHARS)}\n</file_data>\n${userText}`
                    : `<file_data>\n${render_chunk(file, block, chunk).slice(0, CHUNK_CHARS + 2000)}\n</file_data>\n${userText}`;
            const r = await call("point_prompt.txt", params, input);
            if (!r) continue;
            const pts = Array.isArray(r["points"]) ? (r["points"] as RawPoint[]) : [];
            for (const p of pts) p["_src"] = block.title;
            rows.push(...pts);
            if (Array.isArray(r["excluded"])) {
                excluded.push(
                    ...(r["excluded"] as Array<{ name?: unknown; count?: unknown }>)
                        .filter((g) => typeof g["name"] === "string")
                        .map((g) => ({
                            name: String(g["name"]),
                            count: typeof g["count"] === "number" ? g["count"] : 0,
                        })),
                );
            }
            if (declared === null && typeof r["declared_count"] === "number") {
                declared = r["declared_count"] as number;
            }
        }
        return { rows, excluded, declared };
    }
    // 表格块：两遍法（识别 1 次 + 确定性转录，块数不再决定 LLM 调用量）
    const ident = await identify_block(file, block, params, userText, call);
    const t = transcribe_block(block, ident, "point_type");
    // 声明守卫：declared_count 只能来自用户文字（识别层偶发把文件行数/地址
    // 计数当声明——RP-03 实测幻觉 256＝遥信行数，触发虚假对账追问）。数字未
    // 在用户文字中独立出现（前后无其他数字）即视为未声明
    const dc = ident.declared_count;
    const declared =
        typeof dc === "number" && new RegExp(`(?<![0-9])${dc}(?![0-9])`).test(userText)
            ? dc
            : null;
    return {
        rows: t.rows,
        excluded: t.excluded,
        declared,
    };
}

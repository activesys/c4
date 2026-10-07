// c4/agent/src/subagents/tools/doc_parsers.ts — 文档解析工具
// 纯格式提取，不做语义推断。语义推断由 LLM + responseFormat 完成

import { tool } from "langchain";
import { z } from "zod";
import * as fs from "node:fs";
import { createRequire } from "node:module";

const require_ = createRequire(import.meta.url);

// ── 纯 tabular data ───────────────────────────────────────

interface TabularData {
    headers: string[];
    rows: string[][];
    rowCount: number;
}

function parse_csv_raw(content: string): TabularData {
    const lines = content.split(/\r?\n/).filter((l) => l.trim().length > 0);
    if (lines.length === 0) return { headers: [], rows: [], rowCount: 0 };
    const first = lines[0]!.split(",").map((h) => h.trim());
    // 无表头检测（2026-09-29 用例11）：点表文件常无表头行，首行即数据（如
    // 「1000,风速」）——被当表头会吞掉第一个点（10 点解析成 9 点）。首格为纯数字
    // （地址/序号值，列名几乎不可能是纯数字）时判定为无表头，整表按数据行处理，
    // 表头置中性列名 col1..colN（列语义由消费方按值推断）
    if (/^\d+$/.test(first[0] ?? "")) {
        const rows: string[][] = [];
        for (const line of lines) {
            const cols = line.split(",").map((c) => c.trim());
            if (cols.length === 0 || cols.every((c) => c.length === 0)) continue;
            rows.push(cols);
        }
        return {
            headers: (rows[0] ?? []).map((_, i) => `col${i + 1}`),
            rows,
            rowCount: rows.length,
        };
    }
    const headers = first;
    const rows: string[][] = [];
    for (let i = 1; i < lines.length; i++) {
        const cols = lines[i]!.split(",").map((c) => c.trim());
        if (cols.length === 0 || cols.every((c) => c.length === 0)) continue;
        rows.push(cols);
    }
    return { headers, rows, rowCount: rows.length };
}

function parse_xlsx_raw(buf: Buffer): TabularData {
    try {
        const XLSX = require_("xlsx");
        const wb = XLSX.read(buf, { type: "buffer" });
        const sheet = wb.Sheets[wb.SheetNames[0]!];
        if (!sheet) return { headers: [], rows: [], rowCount: 0 };
        return parse_csv_raw(XLSX.utils.sheet_to_csv(sheet));
    } catch {
        // eslint-disable-next-line no-control-regex -- 清洗目标就是控制字符：二进制兜底转可打印 ASCII（保留换行）
        const text = buf.toString("utf-8").replace(/[^\x20-\x7E\x0A\x0D]/g, "");
        const lines = text.split(/\r?\n/).filter((l) => l.trim().length > 0);
        if (lines.length > 0) return parse_csv_raw(lines.join("\n"));
        return { headers: [], rows: [], rowCount: 0 };
    }
}

function format_tabular(t: TabularData): string {
    if (t.rowCount === 0) return "（文件为空或无法读取）";
    const lines = [`表头 (${t.headers.length} 列):`];
    lines.push(t.headers.map((h, i) => `  [${i}] ${h}`).join("\n"));
    lines.push(`\n数据行 (共 ${t.rowCount} 行):`);
    const max = Math.min(t.rowCount, 50);
    for (let i = 0; i < max; i++) {
        const row = t.rows[i]!;
        lines.push(`  [${i + 1}] ${row.map((c, j) => `${t.headers[j] || `col${j}`}=${c}`).join(", ")}`);
    }
    if (t.rowCount > max) lines.push(`  ... (还有 ${t.rowCount - max} 行未显示)`);
    return lines.join("\n");
}

// ── 工具 ──────────────────────────────────────────────────

export const xlsxParserTool = tool(
    async ({ filePath }: { filePath: string }) => {
        let buf: Buffer;
        try { buf = fs.readFileSync(filePath); } catch {
            return JSON.stringify({ success: false, error: `文件不存在: ${filePath}` });
        }
        const tabular = parse_xlsx_raw(buf);
        return JSON.stringify({
            success: tabular.rowCount > 0,
            tabular,
            formatted: tabular.rowCount > 0 ? format_tabular(tabular) : "",
        });
    },
    {
        name: "xlsx_parser",
        description: "读取 Excel 文件内容，返回表头和数据行（纯格式提取）。" +
            "拿到 raw data 后，分析列含义，系统会要求你输出结构化设备信息。",
        schema: z.object({ filePath: z.string().describe("xlsx 文件绝对路径") }),
    },
);

export const csvParserTool = tool(
    async ({ filePath }: { filePath: string }) => {
        let content: string;
        try { content = fs.readFileSync(filePath, "utf-8"); } catch {
            return JSON.stringify({ success: false, error: `文件不存在: ${filePath}` });
        }
        const tabular = parse_csv_raw(content);
        return JSON.stringify({
            success: tabular.rowCount > 0,
            tabular,
            formatted: tabular.rowCount > 0 ? format_tabular(tabular) : "",
        });
    },
    {
        name: "csv_parser",
        description: "读取 CSV 文件内容，返回表头和数据行（纯格式提取）。" +
            "拿到 raw data 后，分析列含义，系统会要求你输出结构化设备信息。",
        schema: z.object({ filePath: z.string().describe("CSV 文件绝对路径") }),
    },
);

export const txtParserTool = tool(
    async ({ filePath }: { filePath: string }) => {
        let content: string;
        try { content = fs.readFileSync(filePath, "utf-8"); } catch {
            return JSON.stringify({ success: false, error: `文件不存在: ${filePath}` });
        }
        if (content.trim().length === 0) {
            return JSON.stringify({ success: false, error: "文件内容为空" });
        }
        return JSON.stringify({ success: true, content });
    },
    {
        name: "txt_parser",
        description: "读取纯文本文件内容（.txt）。" +
            "拿到 raw data 后，分析内容，系统会要求你输出结构化设备信息。",
        schema: z.object({ filePath: z.string().describe("txt 文件绝对路径") }),
    },
);

// 编排器统一入口（agent.md §3.2.0 阶段 3/6 文件注入）：按扩展名分发解析，返回文本化表格
export function parse_any_file(filePath: string): string {
    if (filePath.endsWith(".xlsx") || filePath.endsWith(".xls")) {
        const r = parse_xlsx_raw(fs.readFileSync(filePath));
        return JSON.stringify(r);
    }
    if (filePath.endsWith(".csv")) {
        const r = parse_csv_raw(fs.readFileSync(filePath, "utf-8"));
        return JSON.stringify(r);
    }
    return fs.readFileSync(filePath, "utf-8");
}

// ── 点表文件结构化解析（agent.md §2.13.3，2026-10-07）────────
// 面向真实厂家点表：多 sheet 全量解析、GBK 解码、数值单元格按存储值转写、
// 文档序保序、非数据行（段标题）识别。产出结构化文件块供分块提取与两遍法转录。

export interface ParsedPointBlock {
    /** 逻辑块标题：sheet 名 / "csv" / "text" */
    title: string;
    kind: "table" | "text";
    /** 表头行（无表头为 null——首行即数据） */
    header: string[] | null;
    /** 非数据行（段标题等）：row = 该行之后首个数据行的行号（1 起） */
    nonData: Array<{ row: number; text: string }>;
    /** 数据行（保序；单元格为存储值字符串化，空单元格为 ""） */
    rows: string[][];
}

export interface ParsedPointFile {
    /** 原始文件名（上传原名，语境行与同名替换判定用） */
    name: string;
    blocks: ParsedPointBlock[];
}

function decode_bytes(buf: Buffer): string {
    // 按字节探测：utf-8 严格解码失败（GBK 厂家文件）→ 回退 GB18030（超集兼容 GBK）
    try {
        return new TextDecoder("utf-8", { fatal: true }).decode(buf);
    } catch {
        try {
            return new TextDecoder("gb18030").decode(buf);
        } catch {
            return buf.toString("latin1");
        }
    }
}

function is_pure_int(s: string): boolean {
    return /^\d+$/.test(s);
}

/** 表头判定：前 min(3, 宽度) 个单元格都不是纯整数 → 表头（点号/序号列值几乎必为数字） */
function detect_header(cells: string[]): boolean {
    const width = Math.min(3, cells.length);
    for (let i = 0; i < width; i++) {
        if (cells[i] !== "" && is_pure_int(cells[i]!)) return false;
    }
    return cells.some((c) => c !== "");
}

/** 单元格存储值 → 字符串（数值 String 化：0.00E+00 显示串的存储值 0 → "0"） */
function cell_str(v: unknown): string {
    if (v === undefined || v === null) return "";
    if (typeof v === "number") return Number.isInteger(v) ? String(v) : String(v);
    if (typeof v === "boolean") return v ? "1" : "0";
    return String(v).trim();
}

function rows_from_matrix(matrix: unknown[][]): {
    header: string[] | null;
    nonData: Array<{ row: number; text: string }>;
    rows: string[][];
} {
    const nonData: Array<{ row: number; text: string }> = [];
    const rows: string[][] = [];
    let header: string[] | null = null;
    // 单有效单元格行（表头前后均可能：文档标题/段标题）→ 非数据行，挂到其后
    // 首个数据行；首个多列行按 detect_header 判表头；其余为数据行，保序
    let headerDone = false;
    let pendingTitle: string | null = null;
    for (const raw of matrix) {
        const cells = (raw ?? []).map(cell_str);
        const filled = cells.filter((c) => c !== "");
        if (filled.length === 0) continue;
        if (filled.length === 1) {
            pendingTitle = filled[0]!;
            continue;
        }
        if (!headerDone) {
            if (detect_header(cells)) {
                header = cells;
                headerDone = true;
                continue;
            }
            headerDone = true;
        }
        rows.push(cells);
        if (pendingTitle !== null) {
            nonData.push({ row: rows.length, text: pendingTitle });
            pendingTitle = null;
        }
    }
    return { header, nonData, rows };
}

export function parse_point_file(filePath: string, originalName: string): ParsedPointFile {
    const buf = fs.readFileSync(filePath);
    if (filePath.endsWith(".xlsx") || filePath.endsWith(".xls")) {
        const XLSX = require_("xlsx");
        const wb = XLSX.read(buf, { type: "buffer" });
        const blocks: ParsedPointBlock[] = [];
        for (const title of wb.SheetNames) {
            const matrix = XLSX.utils.sheet_to_json(wb.Sheets[title], {
                header: 1,
                raw: true,
            }) as unknown[][];
            const { header, nonData, rows } = rows_from_matrix(matrix);
            if (rows.length === 0) continue; // 空 sheet 跳过
            blocks.push({ title, kind: "table", header, nonData, rows });
        }
        return { name: originalName, blocks };
    }
    if (filePath.endsWith(".csv")) {
        const text = decode_bytes(buf);
        const lines = text.split(/\r?\n/).filter((l) => l.trim().length > 0);
        const matrix = lines.map((l) => l.split(",").map((c) => c));
        const { header, nonData, rows } = rows_from_matrix(matrix);
        return {
            name: originalName,
            blocks: [{ title: "csv", kind: "table", header, nonData, rows }],
        };
    }
    // txt 及其他：自由文本块（走 LLM 文本通道，不强行结构化）
    return {
        name: originalName,
        blocks: [
            {
                title: "text",
                kind: "text",
                header: null,
                nonData: [],
                rows: decode_bytes(buf)
                    .split(/\r?\n/)
                    .filter((l) => l.trim().length > 0)
                    .map((l) => [l]),
            },
        ],
    };
}

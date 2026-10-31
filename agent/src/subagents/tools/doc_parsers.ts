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
    // RFC4180 行切分（2026-10-14 RP-IF 实测修复）：引号字段内的逗号/换行/双引号
    // 转义（""）不得分裂——AZGF 入库点表第 5 列（"device,plant_id=…"，内嵌逗号
    // 的 tags 串）被裸 split(",") 拆成 7 列，列语义与逐点映射全废
    const lines = split_csv_lines(content);
    if (lines.length === 0) return { headers: [], rows: [], rowCount: 0 };
    const first = split_csv_line(lines[0]!);
    // 无表头检测（2026-09-29 用例11）：点表文件常无表头行，首行即数据（如
    // 「1000,风速」）——被当表头会吞掉第一个点（10 点解析成 9 点）。首格为纯数字
    // （地址/序号值，列名几乎不可能是纯数字）时判定为无表头，整表按数据行处理，
    // 表头置中性列名 col1..colN（列语义由消费方按值推断）
    if (/^\d+$/.test(first[0] ?? "")) {
        const rows: string[][] = [];
        for (const line of lines) {
            const cols = split_csv_line(line);
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
        const cols = split_csv_line(lines[i]!);
        if (cols.length === 0 || cols.every((c) => c.length === 0)) continue;
        rows.push(cols);
    }
    return { headers, rows, rowCount: rows.length };
}

/** RFC4180 内容级行切分：引号字段内的逗号/换行/"" 转义不分裂行（引号保留在
 *  返回行内，由 split_csv_line 剥除并解转义） */
function split_csv_lines(content: string): string[] {
    const rows: string[] = [];
    let cur = "";
    let inQ = false;
    for (let i = 0; i < content.length; i++) {
        const ch = content[i]!;
        if (inQ) {
            cur += ch;
            if (ch === '"') {
                if (content[i + 1] === '"') {
                    cur += '"';
                    i++;
                } else {
                    inQ = false;
                }
            }
        } else if (ch === '"') {
            inQ = true;
            cur += ch;
        } else if (ch === "\n" || ch === "\r") {
            if (ch === "\r" && content[i + 1] === "\n") i++;
            rows.push(cur);
            cur = "";
        } else {
            cur += ch;
        }
    }
    if (cur.trim().length > 0) rows.push(cur);
    return rows.filter((l) => l.trim().length > 0);
}

/** 单行 CSV 分列（RFC4180）：双引号字段内逗号不分裂、"" 解转义为 "；无引号行
 *  与裸 split(",")+trim 等价 */
function split_csv_line(line: string): string[] {
    const cols: string[] = [];
    let cur = "";
    let inQ = false;
    for (let i = 0; i < line.length; i++) {
        const ch = line[i]!;
        if (inQ) {
            if (ch === '"') {
                if (line[i + 1] === '"') {
                    cur += '"';
                    i++;
                } else {
                    inQ = false;
                }
            } else {
                cur += ch;
            }
        } else if (ch === '"') {
            inQ = true;
        } else if (ch === ",") {
            cols.push(cur.trim());
            cur = "";
        } else {
            cur += ch;
        }
    }
    cols.push(cur.trim());
    return cols;
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

// 类型定义自 2026-10-29（M1）收口到共享包 @c4/stages（stages/src/point_file_types.ts，
// c4_asset_learning.md §2.2 识别流水线训练/运行同构）——此处转出保持既有相对导入兼容
import type { ParsedPointBlock, ParsedPointFile } from "@c4/stages";
export type { ParsedPointBlock, ParsedPointFile };

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
    // 连续单格行累积（2026-10-11 modbus 通辽实测：表头前「通讯协议：…」「服务端：
    // …」「设备地址：1 端口号：503」三行连续——单变量覆盖只留末行，连接声明
    // 丢失）。全部挂到其后首个数据行
    let pendingTitles: string[] = [];
    for (const raw of matrix) {
        const cells = (raw ?? []).map(cell_str);
        const filled = cells.filter((c) => c !== "");
        if (filled.length === 0) continue;
        if (filled.length === 1) {
            pendingTitles.push(filled[0]!);
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
        if (pendingTitles.length > 0) {
            for (const t of pendingTitles) {
                nonData.push({ row: rows.length, text: t });
            }
            pendingTitles = [];
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
        // RFC4180 切分（2026-10-14 RP-IF 实测修复）：引号字段内逗号不分裂——
        // AZGF 入库点表第 5 列 tags 串被裸 split(",") 拆碎，逐点映射全废
        const lines = split_csv_lines(text);
        const matrix = lines.map((l) => split_csv_line(l));
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

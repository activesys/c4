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
    const headers = lines[0]!.split(",").map((h) => h.trim());
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
        // eslint-disable-next-line @typescript-eslint/no-require-imports
        const XLSX = require_("xlsx");
        const wb = XLSX.read(buf, { type: "buffer" });
        const sheet = wb.Sheets[wb.SheetNames[0]!];
        if (!sheet) return { headers: [], rows: [], rowCount: 0 };
        return parse_csv_raw(XLSX.utils.sheet_to_csv(sheet));
    } catch {
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

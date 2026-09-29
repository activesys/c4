// test/tools/doc_parsers.test.ts — parse_any_file 无表头检测回归（2026-09-29 用例11：
// 无表头点表首行被当表头吞掉，10 点解析成 9 点）

import { describe, it, expect } from "vitest";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { parse_any_file } from "../../src/subagents/tools/doc_parsers.js";

function write_temp_csv(content: string): string {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), "c4_doc_parsers_"));
    const file = path.join(dir, "points.csv");
    fs.writeFileSync(file, content, "utf-8");
    return file;
}

describe("parse_any_file 无表头检测", () => {
    it("首格为纯数字的 CSV 按无表头处理，首行保留为数据行", () => {
        const file = write_temp_csv("1000,风速\n1001,功率\n1002,风向\n");
        const td = JSON.parse(parse_any_file(file)) as {
            headers: string[];
            rows: string[][];
            rowCount: number;
        };
        expect(td.rowCount).toBe(3);
        expect(td.rows[0]).toEqual(["1000", "风速"]);
        expect(td.rows[2]).toEqual(["1002", "风向"]);
        expect(td.headers).toEqual(["col1", "col2"]);
    });

    it("带表头的 CSV 行为不变", () => {
        const file = write_temp_csv("addr,点名\n1000,风速\n1001,功率\n");
        const td = JSON.parse(parse_any_file(file)) as {
            headers: string[];
            rows: string[][];
            rowCount: number;
        };
        expect(td.headers).toEqual(["addr", "点名"]);
        expect(td.rowCount).toBe(2);
        expect(td.rows[0]).toEqual(["1000", "风速"]);
    });
});

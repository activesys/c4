// point_pipeline_modbus 单测（RP-MB-09/10/11 2026-10-12/13 裁定的确定性层）：
// 编码段号设备语境派生、Variable 声明式分表救提取、跨块同点合并对账口径。
// 断言基准为真实点表文件形态（上海电气/华创），不依赖 LLM。
import { describe, it, expect } from "vitest";
import {
    derive_seg_device_col,
    transcribe_block,
    type BlockIdent,
    type ParsedPointBlock,
    type RawPoint,
} from "../../src/orchestrator/point_pipeline.js";
import {
    mb_rescue_variable_sheet,
    mb_dedupe_copies,
    mb_finalize_block,
} from "../../src/orchestrator/point_pipeline_modbus.js";

function blk(title: string, header: string[] | null, rows: string[][]): ParsedPointBlock {
    return { title, kind: "table", header, nonData: [], rows };
}

describe("derive_seg_device_col（RP-MB-09/10 edos 编码段号派生设备语境）", () => {
    const rowOf = (code: string, name: string): string[] => [code, name, "100"];
    it("上海电气形态：前缀一致 + 3 位段号 + 段内流水 → 派生段号列", () => {
        const rows: string[][] = [];
        for (let seg = 34; seg <= 58; seg++) {
            for (let k = 1; k <= 10; k++) {
                rows.push(rowOf(`DT_JCFJU_FJ${String(seg).padStart(3, "0")}${String(k).padStart(5, "0")}`, `点${k}`));
            }
        }
        // 点名跨段重复（每段同 10 个点名）、(段,点名) 唯一 → 正交
        expect(derive_seg_device_col(blk("接入点表", ["edos点表", "描述", "缩放"], rows), 2, 1)).toBe(0);
    });
    it("华创形态：段号 000~033（场级段 000）→ 派生段号列", () => {
        const rows: string[][] = [];
        for (let seg = 0; seg <= 33; seg++) {
            for (let k = 1; k <= 10; k++) {
                rows.push(rowOf(`DT_JCFJU_FJ${String(seg).padStart(3, "0")}${String(k).padStart(5, "0")}`, `有功功率${k}`));
            }
        }
        expect(derive_seg_device_col(blk("Sheet3", ["edos点表", "描述", "系数"], rows), 2, 1)).toBe(0);
    });
    it("点号列形态（段号恒定）→ 不派生（RP-02 护栏：撞名判定不得失效）", () => {
        const rows: string[][] = [];
        for (let k = 1; k <= 100; k++) {
            rows.push(rowOf(`DT_CGDQU_DQDI${String(k).padStart(5, "0")}`, `点名${k % 10}`));
        }
        expect(derive_seg_device_col(blk("t", null, rows), 2, 1)).toBeUndefined();
    });
    it("纯数字列（地址形态）→ 不派生", () => {
        const rows: string[][] = [];
        for (let k = 1; k <= 100; k++) {
            rows.push(rowOf(String(30000 + k), `点名${k % 10}`));
        }
        expect(derive_seg_device_col(blk("t", null, rows), 2, 1)).toBeUndefined();
    });
    it("段号与点名不正交（(段,点名) 大量碰撞）→ 不派生", () => {
        const rows: string[][] = [];
        for (let seg = 1; seg <= 4; seg++) {
            for (let k = 1; k <= 100; k++) {
                rows.push(rowOf(`DEV${String(seg).padStart(3, "0")}${String(k).padStart(5, "0")}`, `共享点名${k % 5}`));
            }
        }
        expect(derive_seg_device_col(blk("t", null, rows), 2, 1)).toBeUndefined();
    });
});

describe("transcribe_block device_seg_col（段号语境转录）", () => {
    it("段号数值作 _device（034 → \"34\"，与分表风机号对齐）", () => {
        const block = blk("t", null, [
            ["DT_JCFJU_FJ03400001", "机舱振动传感器X", "324"],
            ["DT_JCFJU_FJ05800123", "Error62\"", "4674"],
        ]);
        const ident: BlockIdent = { addr_col: 2, name_col: 1, device_seg_col: 0 };
        const { rows } = transcribe_block(block, ident, "point_type");
        expect(rows[0]!["_device"]).toBe("34");
        expect(rows[1]!["_device"]).toBe("58");
        expect(rows[0]!["name"]).toBe("机舱振动传感器X");
    });
});

describe("mb_rescue_variable_sheet（RP-MB-09 遥测分表形态）", () => {
    const variableRow = (desc: string, name: string, off: string): string[] => [
        `<Variable VariableDesc="${desc}" VariableName="${name}"`,
        `VariableOffset="${off}"`,
        'type="short" VariableScale="10000" />',
    ];
    const plainRow = (dev: string, name: string, scale: string, off: string): string[] => [
        dev,
        name,
        scale,
        off,
    ];
    it("声明行 + 成段普通数据行 → 救提取（声明行不产点，风机号作语境）", () => {
        const rows: string[][] = [variableRow("机舱振动传感器X", "1_Nace_Vib_X", "324")];
        for (let dev = 34; dev <= 58; dev++) {
            for (let k = 0; k < 10; k++) {
                rows.push(plainRow(String(dev), `测点${k}`, "100", String(324 + k)));
            }
        }
        const got = mb_rescue_variable_sheet(blk("遥测", ["<Variable …功能码04"], rows));
        expect(got).not.toBeNull();
        expect(got!.rows.length).toBe(250); // 声明行不计入点
        expect(got!.rows[0]!["name"]).toBe("测点0");
        expect(got!.rows[0]!["addr"]).toBe("324");
        expect(got!.rows[0]!["_device"]).toBe("34");
        expect(got!.decls["fun_default"]).toBeUndefined();
    });
    it("纯声明页（无成段数据行）→ 不救（交 lookup 口径）", () => {
        const rows: string[][] = [];
        for (let k = 0; k < 60; k++) {
            rows.push(variableRow(`点${k}`, `1_P${k}`, String(300 + k)));
        }
        expect(mb_rescue_variable_sheet(blk("遥测", null, rows))).toBeNull();
    });
});

describe("mb_dedupe_copies B 级：跨块同点合并（RP-MB-09/10 裁定）", () => {
    const src = (p: RawPoint): string => String(p["_src"] ?? "?");

    it("分表声明补全缺口 → 合并计对账（上海电气四表口径）", () => {
        const main: RawPoint[] = [];
        const sub: RawPoint[] = [];
        for (let i = 0; i < 40; i++) {
            main.push({ _src: "接入点表", name: `遥信${i % 20}`, addr: String(i) });
            sub.push({ _src: "遥信点表", name: `遥信${i % 20}`, addr: String(i), fun: 2 });
        }
        // 主表唯有点（防 A 级整块副本判定——真实主表大部分点无跨块对应）
        for (let i = 0; i < 30; i++) {
            main.push({ _src: "接入点表", name: `主表独有${i}`, addr: String(1000 + i) });
        }
        const got = mb_dedupe_copies([...sub, ...main], new Map([["接入点表", 70], ["遥信点表", 40]]));
        expect(got.points.length).toBe(70);
        expect(got.points.every((p) => src(p) === "接入点表")).toBe(true);
        expect(got.points[0]!["fun"]).toBe(2); // 分表声明补全保留侧缺口
        expect(got.recon).toEqual({ total: 110, merged: 40, pureView: 0 });
        expect(got.notes.join("")).toContain("并入对账");
    });

    it("纯人类可读视图（±1 偏移、无增量声明）→ 不计对账（华创 sheet1 口径）", () => {
        const copy: RawPoint[] = [];
        const main: RawPoint[] = [];
        for (let i = 0; i < 40; i++) {
            copy.push({ _src: "sheet1", name: "遥信风场AGC允许", addr: String(i), fun: 2, point_type: "bool" });
        }
        for (let i = 0; i < 40; i++) {
            main.push({ _src: "Sheet3", name: "遥信风场AGC允许", addr: String(i + 1), fun: 2, point_type: "bool" });
        }
        for (let i = 0; i < 30; i++) {
            main.push({ _src: "Sheet3", name: `独有点${i}`, addr: String(1000 + i), fun: 4, point_type: "float" });
        }
        const got = mb_dedupe_copies([...copy, ...main], new Map([["sheet1", 40], ["Sheet3", 70]]));
        expect(got.points.length).toBe(70);
        expect(got.points.every((p) => src(p) === "Sheet3")).toBe(true);
        expect(got.recon).toEqual({ total: 70, merged: 0, pureView: 40 });
        expect(got.notes.join("")).toContain("sheet1");
        expect(got.notes.join("")).toContain("不计入对账");
    });

    it("异 fun 同址同名 = 不同寄存器空间 → 不配对（MB-10 addr1 双空间口径）", () => {
        const a: RawPoint[] = [];
        const b: RawPoint[] = [];
        for (let i = 0; i < 25; i++) {
            a.push({ _src: "甲", name: `点${i}`, addr: String(i), fun: 2 });
            b.push({ _src: "乙", name: `点${i}`, addr: String(i), fun: 4 });
        }
        const got = mb_dedupe_copies([...a, ...b], new Map([["甲", 25], ["乙", 25]]));
        expect(got.points.length).toBe(50);
        expect(got.recon).toBeNull();
    });

    it("同块内同名邻址（段内残余撞名）→ 不配对、照常交撞名拒收", () => {
        const pts: RawPoint[] = [];
        for (let i = 0; i < 30; i++) {
            pts.push({ _src: "主表", name: "发电机冷却水温度", addr: String(i * 6) });
        }
        const got = mb_dedupe_copies(pts);
        expect(got.points.length).toBe(30);
        expect(got.recon).toBeNull();
    });
});

describe("mb_finalize_block 重复地址键（MB-01 跨空间塌键回归）", () => {
    it("fun 未绑定：同址跨点类型（遥测/遥信）→ 不报重", () => {
        const got = mb_finalize_block(
            [
                { _src: "点表对应关系", name: "风场有功总和", addr: "0", point_type: "遥测" },
                { _src: "点表对应关系", name: "AGC允许", addr: "0", point_type: "遥信" },
            ],
            [],
            {},
        );
        expect(got.dups).toEqual([]);
    });
    it("fun 未绑定：同址同点类型 → 照常报重（MB-05 口径）", () => {
        const got = mb_finalize_block(
            [
                { _src: "s", name: "点A", addr: "109", point_type: "遥测" },
                { _src: "s", name: "点A", addr: "109", point_type: "遥测" },
            ],
            [],
            {},
        );
        expect(got.dups.length).toBe(1);
    });
    it("fun 已绑定：跨 fun 同址 → 不报重；同 fun 同址 → 报重", () => {
        const got = mb_finalize_block(
            [
                { _src: "s", name: "点A", addr: "1", _aux_fun: "4" },
                { _src: "s", name: "点B", addr: "1", _aux_fun: "2" },
                { _src: "s", name: "点C", addr: "3", _aux_fun: "4" },
                { _src: "s", name: "点C", addr: "3", _aux_fun: "4" },
            ],
            [],
            {},
        );
        expect(got.dups.length).toBe(1);
        expect(got.dups[0]!.names).toEqual(["点C", "点C"]);
    });
});

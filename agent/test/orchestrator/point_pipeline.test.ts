// point_pipeline 单测（agent.md §2.13 管道机制；断言基准为点表文件事实与
// 设计归一化规则，不依赖 LLM）
import { describe, it, expect } from "vitest";
import {
    assign_point_ids,
    merge_and_normalize,
    parse_addr_value,
    transcribe_block,
    extract_file_block,
    DEFAULT_PLACEHOLDER_NAMES,
    type NormSpec,
    type RawPoint,
} from "../../src/orchestrator/point_pipeline.js";
import { is_safe_point_key, normalize_point_name } from "../../src/executor/point_key.js";
import { parse_point_file } from "../../src/subagents/tools/doc_parsers.js";
import { zh_start_address } from "../../src/orchestrator/zh_numeral.js";

const SPEC: NormSpec = {
    type_field: "point_type",
    per_type: {
        yx: { start: 1 },
        yc: { start: 16385, hex_start_alias: 4001 },
        ym: { start: 25601, hex_start_alias: 6401 },
    },
    on_first_value: { start: "asis", "0": "+start", "1": "+start-1" },
    otherwise: "asis_with_reason",
    hex_markers: ["0x", "H"],
};

function pt(type: string, addr: string | number, name = ""): RawPoint {
    return { point_type: type, addr: String(addr), name, _src: "f" };
}

describe("parse_addr_value", () => {
    it("十进制/科学计数法/16 进制形态", () => {
        expect(parse_addr_value("16385")).toBe(16385);
        expect(parse_addr_value("0.00E+00")).toBe(0);
        expect(parse_addr_value("4.00E+10")).toBe(40000000000);
        expect(parse_addr_value("0x4001")).toBe(0x4001);
        expect(parse_addr_value("40DF")).toBe(0x40df); // 含 A~F → 16 进制
        expect(parse_addr_value("")).toBeNull();
    });
});

describe("merge_and_normalize 归一化（首值判定）", () => {
    it("0 起整段 +start", () => {
        const r = merge_and_normalize([pt("yx", "0"), pt("yx", "1"), pt("yx", "2")], [], SPEC);
        expect(r.points.map((p) => p["addr"])).toEqual([1, 2, 3]);
    });
    it("1 起整段 +start-1（ym 落分区起点）", () => {
        const r = merge_and_normalize(
            Array.from({ length: 260 }, (_, i) => pt("ym", String(i + 1))),
            [],
            SPEC,
        );
        expect(r.points[0]!["addr"]).toBe(25601);
        expect(r.points[259]!["addr"]).toBe(25860);
    });
    it("绝对地址原样（yx 起点 1 / yc 起点 16385）", () => {
        const r = merge_and_normalize([pt("yx", "1"), pt("yx", "2")], [], SPEC);
        expect(r.points.map((p) => p["addr"])).toEqual([1, 2]);
        const r2 = merge_and_normalize([pt("yc", "16385"), pt("yc", "16857")], [], SPEC);
        expect(r2.points.map((p) => p["addr"])).toEqual([16385, 16857]);
        expect(r2.asisCount).toBe(0);
    });
    it("hex 别名：首值 4001 → 整段 16 进制（0x6401 → 25601）", () => {
        const r = merge_and_normalize(
            [pt("ym", "6401"), pt("ym", "6402"), pt("ym", "6430")],
            [],
            SPEC,
        );
        expect(r.points.map((p) => p["addr"])).toEqual([25601, 25602, 25648]);
    });
    it("显式 0x 标记优先", () => {
        const r = merge_and_normalize([pt("yc", "0x4001"), pt("yc", "0x4002")], [], SPEC);
        expect(r.points.map((p) => p["addr"])).toEqual([16385, 16386]);
    });
    it("未覆盖形态原样转写并写 reason（asis_with_reason）", () => {
        const r = merge_and_normalize([pt("yx", "1000"), pt("yx", "1002")], [], SPEC);
        expect(r.points.map((p) => p["addr"])).toEqual([1000, 1002]);
        expect(r.asisCount).toBe(2);
        expect(String(r.points[0]!["_norm_reason"])).toContain("原样转写");
    });
    it("不同类型独立换算（11号光伏区形态：三类型各自 0 起）", () => {
        const r = merge_and_normalize(
            [pt("yc", "0"), pt("yc", "363"), pt("yx", "0"), pt("yx", "201"), pt("ym", "0"), pt("ym", "31")],
            [],
            SPEC,
        );
        expect(r.points.map((p) => p["addr"])).toEqual([16385, 16748, 1, 202, 25601, 25632]);
    });
});

describe("merge_and_normalize 序列回落检测", () => {
    it("严格下降触发回落且不接入", () => {
        const r = merge_and_normalize([pt("yx", "1"), pt("yx", "2"), pt("yx", "9"), pt("yx", "3")], [], SPEC);
        expect(r.reset).not.toBeNull();
        expect(r.reset!.drops).toBe(1);
        expect(r.points).toEqual([]);
    });
    it("相等值不判回落（交去重/L1）", () => {
        const r = merge_and_normalize([pt("yx", "1"), pt("yx", "1"), pt("yx", "2")], [], SPEC);
        expect(r.reset).toBeNull();
    });
    it("回落时伴生重复/缺地址摘要", () => {
        const r = merge_and_normalize(
            [pt("yx", "0"), pt("yx", "0"), pt("yx", ""), pt("yx", "5"), pt("yx", "1")],
            [],
            SPEC,
        );
        expect(r.reset).not.toBeNull();
        expect(r.reset!.extra.join("；")).toContain("重复地址值");
        expect(r.reset!.extra.join("；")).toContain("无地址行");
    });
    it("跨文件序列互不影响（回落只看同类型×同文件）", () => {
        const a = pt("yx", "1");
        const b = { ...pt("yx", "9"), _src: "g" };
        const b2 = { ...pt("yx", "2"), _src: "g" };
        const r = merge_and_normalize([a, b, b2], [], SPEC);
        expect(r.reset).not.toBeNull();
        expect(r.reset!.seqKey).toBe("yx×g");
    });
    it("hex 别名序列回落携带进制判定事实（RP-06：遥测21 吞址 40DF→40）", () => {
        // 首值 4001 命中 yc hex 别名 → 整段 16 进制；0x40DF 后被吞址数值打断
        const seq = [pt("yc", "4001"), pt("yc", "4002"), pt("yc", "40DF"), pt("yc", "40"), pt("yc", "400000000000")];
        const r = merge_and_normalize(seq, [], SPEC);
        expect(r.reset).not.toBeNull();
        expect(r.reset!.from).toBe("40DF");
        expect(r.reset!.alias).toEqual({ hex: 4001, dec: 16385 });
    });
    it("十进制序列回落无别名事实（RP-10：多设备分段）", () => {
        const r = merge_and_normalize([pt("yx", "1"), pt("yx", "2"), pt("yx", "1")], [], SPEC);
        expect(r.reset).not.toBeNull();
        expect(r.reset!.alias).toBeUndefined();
    });
});

describe("merge_and_normalize 去重与 excluded 聚合", () => {
    it("全记录一致去重（不含 reason/内部字段）；同址异名保留交 L1", () => {
        const r = merge_and_normalize(
            [pt("yx", "1", "α"), { ...pt("yx", "1", "α") }, { ...pt("yx", "1", "β") }],
            [],
            SPEC,
        );
        expect(r.points).toHaveLength(2);
    });
    it("excluded 跨块按组名聚合", () => {
        const r = merge_and_normalize(
            [pt("yx", "1")],
            [[{ name: "遥控", count: 3 }], [{ name: "遥控", count: 5 }, { name: "YK", count: 17 }]],
            SPEC,
        );
        expect(r.excluded).toEqual([
            { name: "遥控", count: 8 },
            { name: "YK", count: 17 },
        ]);
    });
});

describe("transcribe_block（两遍法转录）", () => {
    const block = {
        title: "104遥测",
        kind: "table" as const,
        header: ["序号", "点号", "名称"],
        nonData: [
            { row: 1, text: "遥测数据定义" },
            { row: 4, text: "遥控数据定义" },
        ],
        rows: [
            ["1", "16385", "IA"],
            ["2", "16386", "IB"],
            ["3", "16387", "IC"],
            ["1", "24577", "遥控1"],
        ],
    };
    it("按识别列转录，排除段计数不提取", () => {
        const r = transcribe_block(
            block,
            { addr_col: 1, name_col: 2, point_type: "yc", segments: [{ row: 4, excluded: true }] },
            "point_type",
        );
        expect(r.rows).toHaveLength(3);
        expect(r.rows[0]!["addr"]).toBe("16385");
        expect(r.rows[0]!["point_type"]).toBe("yc");
        expect(r.excluded).toEqual([{ name: "遥控数据定义", count: 1 }]);
    });
    it("整块排除（segments row=1）无段锚定时组名回退块标题（RP-02：YK sheet）", () => {
        // 模拟 YK sheet：首行即表头（不产生 nonData 锚定），整表排除
        const ykBlock = {
            title: "YK",
            kind: "table" as const,
            header: ["点号", "描述", "四方控点名"],
            nonData: [],
            rows: [
                ["24577", "遥控1", "YK1"],
                ["24578", "遥控2", "YK2"],
            ],
        };
        const r = transcribe_block(
            ykBlock,
            { addr_col: 0, name_col: 1, point_type: null, segments: [{ row: 1, excluded: true }] },
            "point_type",
        );
        expect(r.rows).toHaveLength(0);
        expect(r.excluded).toEqual([{ name: "YK", count: 2 }]);
    });
    it("段类型向前携带 + 排除段边界不互吞（RP-04：单 sheet 五段，遥控段后紧跟遥调段）", () => {
        // 11号光伏区形态：yc 4 行 → yx 3 行 → 遥控排除 2 行 → 遥调排除 2 行
        const seg = {
            title: "新建 XLS 工作表",
            kind: "table" as const,
            header: ["序号", "名称"],
            nonData: [
                { row: 1, text: "遥测数据定义" },
                { row: 5, text: "遥信数据定义" },
                { row: 8, text: "遥控数据定义" },
                { row: 10, text: "遥调数据定义" },
            ],
            rows: [
                ["0", "IA"], ["1", "IB"], ["2", "IC"], ["3", "Ua"],
                ["0", "合位"], ["1", "合位"], ["2", "合位"],
                ["0", "遥控1"], ["1", "遥控2"],
                ["0", "遥调1"], ["1", "遥调2"],
            ],
        };
        const r = transcribe_block(
            seg,
            {
                addr_col: 0,
                name_col: 1,
                point_type: null,
                segments: [
                    { row: 1, point_type: "yc" },
                    { row: 5, point_type: "yx" },
                    { row: 8, excluded: true },
                    { row: 10, excluded: true },
                ],
            },
            "point_type",
        );
        // 中间行沿用当前段类型（此前只在边界行取到，中段行全部无类型）
        expect(r.rows.filter((p) => p["point_type"] === "yc").map((p) => p["addr"])).toEqual([
            "0", "1", "2", "3",
        ]);
        expect(r.rows.filter((p) => p["point_type"] === "yx").map((p) => p["addr"])).toEqual([
            "0", "1", "2",
        ]);
        expect(r.rows).toHaveLength(7);
        // 相邻排除段各自计数，边界行不被上一段吞掉
        expect(r.excluded).toEqual([
            { name: "遥控数据定义", count: 2 },
            { name: "遥调数据定义", count: 2 },
        ]);
    });
    it("declared 守卫：识别层幻觉文件行数不充当用户声明（RP-03：遥信 256 行）", async () => {
        const file = {
            name: "高力板镇104点表.xlsx",
            blocks: [
                {
                    title: "104遥信",
                    kind: "table" as const,
                    header: ["点号", "名称"],
                    nonData: [],
                    rows: [
                        ["1", "轻瓦斯报警"],
                        ["2", "重瓦斯跳闸"],
                    ],
                },
            ],
        };
        const call = async () => ({
            addr_col: 0,
            name_col: 1,
            point_type: "yx",
            // 模拟识别层幻觉：把本段 2 行（实测场景 256 行）当用户声明
            declared_count: 2,
        });
        const without = await extract_file_block(file, file.blocks[0]!, {}, "请解析此文件", call);
        expect(without.declared).toBeNull();
        const withDecl = await extract_file_block(
            file,
            file.blocks[0]!,
            {},
            "点表共 2 点，请解析",
            call,
        );
        expect(withDecl.declared).toBe(2);
    });
});

describe("assign_point_ids（id 赋配：归一化 + 顺延/拒收）", () => {
    const ph = [...DEFAULT_PLACEHOLDER_NAMES];

    it("中文名/英文名归一化作裸 id；ASCII 小写、忽略提取层 id", () => {
        const pts: RawPoint[] = [
            { name: "1#风机风速", addr: "1", id: "llm_invented" },
            { name: "Windspeed", addr: "2" },
        ];
        const r = assign_point_ids(pts);
        expect(r.failed).toBe(0);
        expect(r.conflicts).toEqual([]);
        expect(pts[0]!["id"]).toBe("1#风机风速");
        expect(pts[1]!["id"]).toBe("windspeed");
    });

    it("设备语境并入：裸 id = 设备_点名，跨设备同名不撞", () => {
        const pts: RawPoint[] = [
            { name: "有功功率", addr: "1", _device: "1#逆变器" },
            { name: "有功功率", addr: "2", _device: "2#逆变器" },
        ];
        const r = assign_point_ids(pts);
        expect(r.conflicts).toEqual([]);
        expect(pts[0]!["id"]).toBe("1#逆变器_有功功率");
        expect(pts[1]!["id"]).toBe("2#逆变器_有功功率");
    });

    it("占位白名单撞名顺延：最小未占用后缀 + existing 种子", () => {
        const pts: RawPoint[] = [
            { name: "备用", addr: "10" },
            { name: "备用", addr: "11" },
            { name: "备用", addr: "12" },
        ];
        const r = assign_point_ids(pts, { existing: new Set(["备用", "备用_2"]), placeholderNames: ph });
        expect(r.filled).toBe(3);
        expect(pts[0]!["id"]).toBe("备用_3");
        expect(pts[1]!["id"]).toBe("备用_4");
        expect(pts[2]!["id"]).toBe("备用_5");
    });

    it("业务点名撞名 → conflicts（拒收路径），首点占位、余点不赋 id", () => {
        const pts: RawPoint[] = [
            { name: "A相功率因数", addr: "100" },
            { name: "A相功率因数", addr: "101" },
        ];
        const r = assign_point_ids(pts);
        expect(r.conflicts).toEqual([
            { name: "A相功率因数", addrs: ["100", "101"] },
        ]);
        expect(pts[1]!["id"]).toBeUndefined();
    });

    it("设备语境化解跨设备同名后，同装置内同名仍判 conflicts", () => {
        const pts: RawPoint[] = [
            { name: "高压熔断器A", addr: "6", _device: "ZRR300AOLD" },
            { name: "高压熔断器A", addr: "49", _device: "ZRR300AOLD" },
        ];
        const r = assign_point_ids(pts);
        expect(r.conflicts).toHaveLength(1);
        expect(r.conflicts[0]!.addrs).toEqual(["6", "49"]);
    });

    it("空名/纯符号 → failed（追问路径）", () => {
        const pts: RawPoint[] = [
            { name: "", addr: "1" },
            { name: "·。·", addr: "2" },
        ];
        const r = assign_point_ids(pts);
        expect(r.failed).toBe(2);
        for (const p of pts) expect(p["id"]).toBeUndefined();
    });
});

describe("normalize_point_name / is_safe_point_key（字符白名单制）", () => {
    it("全半角折叠、危险字符替换、收拢修剪、小写", () => {
        expect(normalize_point_name("１＃风机风速")).toBe("1#风机风速");
        expect(normalize_point_name("电流.1")).toBe("电流_1");
        expect(normalize_point_name("  U A B 电压 ")).toBe("u_a_b_电压");
        expect(normalize_point_name("功率（kW）")).toBe("功率(kw)");
    });
    it("纯符号/空白 → 空串；纯数字保留", () => {
        expect(normalize_point_name("·。·")).toBe("");
        expect(normalize_point_name("123")).toBe("123");
    });
    it("安全判定：等于自身归一化；点号/空白不安全", () => {
        expect(is_safe_point_key("1#风机风速")).toBe(true);
        expect(is_safe_point_key("a.b")).toBe(false);
        expect(is_safe_point_key("a b")).toBe(false);
        expect(is_safe_point_key("")).toBe(false);
    });
});

// ── 解析层：真实文件抽查（基准见 func_test_case_real_points.md 附录 A）──
const PTS = "/home/wangbo/work/activesys/points/iec104";

describe("parse_point_file 真实文件", () => {
    it("高力板镇：4 sheet、段标题 nonData、行数 256/473/4/4", () => {
        const f = parse_point_file(`${PTS}/高力板镇104点表-光伏区-新.xlsx`, "高力板镇104点表-光伏区-新.xlsx");
        expect(f.blocks.map((b) => b.title)).toEqual(["104遥信", "104遥测", "104遥调", "104遥控"]);
        expect(f.blocks.map((b) => b.rows.length)).toEqual([256, 473, 4, 4]);
        expect(f.blocks[0]!.nonData.map((n) => n.text)).toContain("遥信0001H(0001开始)");
        expect(f.blocks[1]!.rows[0]![1]).toBe("16385"); // 点号列十进制绝对
    });
    it("遥测21：282 行，吞址行为存储值字符串（40/400/…）", () => {
        const f = parse_point_file(`${PTS}/遥测21.xls`, "遥测21.xls");
        expect(f.blocks).toHaveLength(1); // 空 sheet 跳过
        expect(f.blocks[0]!.rows).toHaveLength(282);
        const addrs = f.blocks[0]!.rows.map((r) => r[7]);
        expect(addrs[0]).toBe("4001");
        expect(addrs).toContain("40000000000"); // 0x40E9 位置被 Excel 吞为 4×10^10
        expect(addrs[281]).toBe("411A");
    });
    it("GBK csv 解码与行数（yxTagTable 4688 行）", () => {
        const f = parse_point_file(`${PTS}/yxTagTable.csv`, "yxTagTable.csv");
        expect(f.blocks[0]!.header).toEqual(["序号", "地址", "设备", "描述"]);
        expect(f.blocks[0]!.rows).toHaveLength(4688);
        expect(f.blocks[0]!.rows[0]![1]).toBe("1");
    });
    it("11号光伏区：段标题识别（单文件多段）", () => {
        const f = parse_point_file(`${PTS}/11号光伏区1号箱变测控点表.xls`, "11号光伏区1号箱变测控点表.xls");
        expect(f.blocks).toHaveLength(1);
        const titles = f.blocks[0]!.nonData.map((n) => n.text);
        expect(titles).toContain("遥测数据定义");
        expect(titles).toContain("遥信数据定义");
        expect(titles).toContain("遥调数据定义");
        expect(f.blocks[0]!.rows).toHaveLength(612); // 364+202+32+8+6
    });
});

describe("zh_start_address 阿拉伯数字「号」容错", () => {
    it("「从 10000 号开始」与「从10000开始」均命中", () => {
        expect(zh_start_address("转发点表与采集侧一致，从 10000 号开始顺序编址")).toBe(10000);
        expect(zh_start_address("从10000开始")).toBe(10000);
        expect(zh_start_address("没有起始地址")).toBeNull();
    });
});

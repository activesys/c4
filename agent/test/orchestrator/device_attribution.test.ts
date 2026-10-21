// c4/agent/test/orchestrator/device_attribution.test.ts
// 设备名提取纯函数单测（2026-10-19 用户给定 13 类封闭设备类型词表；2026-10-18
// 生产野测「的应该是2区」误绑事故回归）。
// 运行：cd c4/agent && npx vitest run test/orchestrator/device_attribution.test.ts

import { describe, expect, it } from "vitest";
import {
    clean_device_candidate,
    device_type_of,
    numbered_device_of,
    strip_site_prefix,
    TYPE_ONLY_DEVICE_RE,
} from "../../src/orchestrator/device_attribution.js";

describe("strip_site_prefix 已绑场站名前缀剥离（用例 71①）", () => {
    it("场站语境短语不进设备名（2026-10-19 回归：曾出「国电河北ii区2号风机_风速」）", () => {
        expect(strip_site_prefix("国电河北II区2号风机", "国电河北II区")).toBe("2号风机");
        expect(strip_site_prefix("国电河北ii区2号风机", "国电河北II区")).toBe("2号风机");
    });

    it("71② 超集形态：全名剥离后继续走语汇引导剥离（①不早返回）", () => {
        expect(strip_site_prefix("国电河北II区风电场3号风机", "国电河北II区")).toBe("3号风机");
    });

    it("只剥全名：部分匹配（去品牌）不动（71③/④ 口径）", () => {
        // 国电河北 无场站语汇标记（场/站/区/…）→ 保守不剥
        expect(strip_site_prefix("国电河北2号风机", "国电河北II区")).toBe("国电河北2号风机");
    });

    it("非前缀候选与空场站", () => {
        expect(strip_site_prefix("2号风机", "国电河北II区")).toBe("2号风机");
        expect(strip_site_prefix("2号风机", null)).toBe("2号风机");
        // 规则②不依赖已绑场站：「(场站语汇引导)(编号类型词)」形态照剥
        expect(strip_site_prefix("国电河北II区2号风机", null)).toBe("2号风机");
        expect(strip_site_prefix("河北II区2号风机", "国电河北II区")).toBe("2号风机");
    });

    it("场站语汇引导 + 编号类型词尾段（extra 53：子集场站指称，全名匹配不可达）", () => {
        expect(strip_site_prefix("阿拉善风电场2号风机", "华能阿拉善")).toBe("2号风机");
        expect(strip_site_prefix("大唐辽宁小塔子风电场III区2号风机", "华能阿拉善"))
            .toBe("2号风机");
    });

    it("类型词链与非场站引导不误剥", () => {
        expect(strip_site_prefix("1号风机变桨控制器", "华能阿拉善")).toBe("1号风机变桨控制器");
        expect(strip_site_prefix("老张2号风机", "华能阿拉善")).toBe("老张2号风机");
        expect(strip_site_prefix("二十三区光伏", null)).toBe("二十三区光伏");
        expect(strip_site_prefix("14区1#逆变器", "华能阿拉善")).toBe("14区1#逆变器");
    });
});

describe("clean_device_candidate 开头垃圾剥离 + 词表锚定", () => {
    it("事故原句「的应该是2区」→ null 追问（2026-10-18 误绑回归）", () => {
        expect(clean_device_candidate("的应该是2区")).toBeNull();
    });

    it("「的应该是2区箱变」→ 剥离 + 类型锚定 → 2区箱变", () => {
        expect(clean_device_candidate("的应该是2区箱变")).toBe("2区箱变");
    });

    it("「是2区逆变器」→ 2区逆变器", () => {
        expect(clean_device_candidate("是2区逆变器")).toBe("2区逆变器");
    });

    it("「就是1号风机」→ 1号风机", () => {
        expect(clean_device_candidate("就是1号风机")).toBe("1号风机");
    });

    it("「使用风机」→ 剥离引导词后类型词保留（47① 由调用方 TYPE_ONLY 拦截）", () => {
        expect(clean_device_candidate("使用风机")).toBe("风机");
    });

    it("词表内全名不动：「1号风机变桨控制器」", () => {
        expect(clean_device_candidate("1号风机变桨控制器")).toBe("1号风机变桨控制器");
    });

    it("词表内全名不动：「14区1#逆变器」", () => {
        expect(clean_device_candidate("14区1#逆变器")).toBe("14区1#逆变器");
    });

    it("词表内全名不动：「2区箱变」「1号汇流箱」", () => {
        expect(clean_device_candidate("2区箱变")).toBe("2区箱变");
        expect(clean_device_candidate("1号汇流箱")).toBe("1号汇流箱");
    });

    it("词表外干净名原样保留：「海林华锐」（MB-01）/「power_forecast_1」（47D）", () => {
        expect(clean_device_candidate("海林华锐")).toBe("海林华锐");
        expect(clean_device_candidate("power_forecast_1")).toBe("power_forecast_1");
    });

    it("词表外干净名原样保留：「1号主变测控装置」（用例 34 形态）", () => {
        expect(clean_device_candidate("1号主变测控装置")).toBe("1号主变测控装置");
    });

    it("空串/纯垃圾 → null", () => {
        expect(clean_device_candidate("")).toBeNull();
        expect(clean_device_candidate("的")).toBeNull();
        expect(clean_device_candidate("应该")).toBeNull();
        expect(clean_device_candidate("  的是  ")).toBeNull();
    });

    it("剥过语法片段但无类型词 → null（宁可追问不可错绑）", () => {
        expect(clean_device_candidate("的设备")).toBeNull();
        expect(clean_device_candidate("是那个装置")).toBeNull();
    });
});

describe("device_type_of 封闭词表", () => {
    it("13 类全部可识别", () => {
        for (const word of DEVICE_TYPE_WORDS_EXPECTED) {
            expect(device_type_of(`1号${word}`)).toBe(word);
            expect(device_type_of(word)).toBe(word);
        }
    });

    it("词表外词 → null", () => {
        expect(device_type_of("1号主变测控装置")).toBeNull();
        expect(device_type_of("海林华锐")).toBeNull();
        expect(device_type_of("应该是2区")).toBeNull();
    });
});

describe("TYPE_ONLY_DEVICE_RE 47① 纯类型词口径", () => {
    it("裸类型词命中（词表新类全量）", () => {
        expect(TYPE_ONLY_DEVICE_RE.test("箱变")).toBe(true);
        expect(TYPE_ONLY_DEVICE_RE.test("逆变器")).toBe(true);
        expect(TYPE_ONLY_DEVICE_RE.test("汇流箱")).toBe(true);
        expect(TYPE_ONLY_DEVICE_RE.test("电计量")).toBe(true);
        expect(TYPE_ONLY_DEVICE_RE.test("计量小站")).toBe(true);
        expect(TYPE_ONLY_DEVICE_RE.test("风功率")).toBe(true);
        expect(TYPE_ONLY_DEVICE_RE.test("故障录波")).toBe(true);
        expect(TYPE_ONLY_DEVICE_RE.test("保信")).toBe(true);
        expect(TYPE_ONLY_DEVICE_RE.test("气象数据")).toBe(true);
        expect(TYPE_ONLY_DEVICE_RE.test("风机")).toBe(true);
        expect(TYPE_ONLY_DEVICE_RE.test("测风塔")).toBe(true);
        expect(TYPE_ONLY_DEVICE_RE.test("光伏")).toBe(true);
        expect(TYPE_ONLY_DEVICE_RE.test("主变")).toBe(true);
    });

    it("升压站例外不命中（单台无编号合法设备名）", () => {
        expect(TYPE_ONLY_DEVICE_RE.test("升压站")).toBe(false);
    });

    it("全名/带编号不命中（锚定 ^...$）", () => {
        expect(TYPE_ONLY_DEVICE_RE.test("1号风机")).toBe(false);
        expect(TYPE_ONLY_DEVICE_RE.test("1号风机变桨控制器")).toBe(false);
        expect(TYPE_ONLY_DEVICE_RE.test("2区箱变")).toBe(false);
        expect(TYPE_ONLY_DEVICE_RE.test("1号主变测控装置")).toBe(false);
        expect(TYPE_ONLY_DEVICE_RE.test("风电机组")).toBe(true);
    });
});

describe("numbered_device_of 编号设备指称（2026-10-19 用户口径：编号也是设备名）", () => {
    it("用户给定四形态全命中", () => {
        expect(numbered_device_of("现在接入1#风机的数据")).toBe("1#风机");
        expect(numbered_device_of("2号变压器接入")).toBe("2号变压器");
        expect(numbered_device_of("接入箱变123的数据")).toBe("箱变123");
        expect(numbered_device_of("二十三区光伏使用modbus")).toBe("二十三区光伏");
    });

    it("多段前缀叠合：「14区1#逆变器」「三号风机」", () => {
        expect(numbered_device_of("14区1#逆变器数据上传")).toBe("14区1#逆变器");
        expect(numbered_device_of("三号风机转发")).toBe("三号风机");
        expect(numbered_device_of("1号风机")).toBe("1号风机");
    });

    it("裸类型词不算编号设备（交 47① 追问）", () => {
        expect(numbered_device_of("接入逆变器的数据")).toBeNull();
        expect(numbered_device_of("升压站")).toBeNull();
        expect(numbered_device_of("无设备信息的一句话")).toBeNull();
    });

    it("后缀编号允许字母尾：「箱变12A」完整保留（不吞半截）", () => {
        expect(numbered_device_of("箱变12A数据上传")).toBe("箱变12A");
        expect(numbered_device_of("箱变123")).toBe("箱变123");
    });
});

const DEVICE_TYPE_WORDS_EXPECTED = [
    "风机",
    "升压站",
    "电计量",
    "计量小站",
    "风功率",
    "测风塔",
    "光伏",
    "箱变",
    "逆变器",
    "汇流箱",
    "故障录波",
    "保信",
    "气象数据",
] as const;

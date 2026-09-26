// c4/agent/test/orchestrator/gap_question.test.ts
// 单缺口顺序提问纯函数单测（agent.md §2.6 C4H2 修订：聚合提问 → 单缺口顺序提问）
//   - ask_* 确切提问文本生成：要求（字段清单）与示例（写法不限）分离
//   - parse_bare_value / bind_bare：裸值兜底绑定（宁可放过不可错绑）
// 运行：cd c4/agent && npm test

import { describe, expect, it } from "vitest";
import {
    ask_conn,
    ask_points,
    ask_protocol,
    bind_bare,
    parse_bare_value,
} from "../../src/orchestrator/gap_question.js";

describe("ask_protocol", () => {
    it("含支持协议清单与回复方式引导", () => {
        const s = ask_protocol("接入", ["modbus", "iec104", "asfp2"]);
        expect(s).toContain("接入协议");
        expect(s).toContain("modbus、iec104、asfp2");
        expect(s).toContain("回复协议名即可");
    });
});

describe("ask_conn", () => {
    it("asfp2 接收侧（仅端口必填）→ 端口单项提问带示例", () => {
        const s = ask_conn("接入", [
            { name: "port", description: "数据接收监听端口，必填：必须由用户显式指定" },
        ]);
        expect(s).toContain("接入连接信息");
        expect(s).toContain("端口");
        expect(s).toContain("9001");
    });

    it("modbus 连接（ip+端口）→ 双字段提问带示例", () => {
        const s = ask_conn("接入", [{ name: "ip" }, { name: "port" }]);
        expect(s).toContain("IP 地址");
        expect(s).toContain("端口");
        expect(s).toContain("192.168.1.5");
    });

    it("未知字段回退 schema 描述主体", () => {
        const s = ask_conn("转发", [
            { name: "bucket", description: "InfluxDB bucket 名称，必填：由用户提供" },
        ]);
        expect(s).toContain("InfluxDB bucket 名称");
    });
});

describe("ask_points", () => {
    it("asfp2 点表（地址+点名）→ 示例两列", () => {
        const s = ask_points("接入", [{ name: "name" }, { name: "addr" }]);
        expect(s).toContain("每个点需要 地址、点名");
        expect(s).toContain("3000:风速");
        expect(s).toContain("写法不限");
        expect(s).toContain("xlsx/csv");
    });

    it("modbus 点表 → 六字段要求 + 六值示例行", () => {
        const s = ask_points("接入", [
            { name: "name" },
            { name: "addr" },
            { name: "uid" },
            { name: "fun" },
            { name: "type" },
            { name: "swap" },
        ]);
        expect(s).toContain("从站号(uid)");
        expect(s).toContain("功能码(fun)");
        expect(s).toContain("数据类型(type)");
        expect(s).toContain("字节交换(swap");
        expect(s).toContain("3000:风速:1:3:4:0");
    });

    it("schema 未知字段保留原名入清单", () => {
        const s = ask_points("接入", [{ name: "addr" }, { name: "name" }, { name: "custom" }]);
        expect(s).toContain("custom");
    });
});

describe("parse_bare_value", () => {
    it("裸端口（用例 3/4 场景：回答「9001」）→ port + number 双解释", () => {
        expect(parse_bare_value("9001")).toEqual({ port: 9001, number: 9001 });
    });

    it("带前缀端口「端口9001」→ port", () => {
        expect(parse_bare_value("端口9001")).toMatchObject({ port: 9001 });
        expect(parse_bare_value("端口：9001")).toMatchObject({ port: 9001 });
    });

    it("地址范围三种分隔符 → range（转发点表裸回答场景）", () => {
        expect(parse_bare_value("2390-2399").range).toEqual({ start: 2390, end: 2399 });
        expect(parse_bare_value("2390~2399").range).toEqual({ start: 2390, end: 2399 });
        expect(parse_bare_value("2390到2399").range).toEqual({ start: 2390, end: 2399 });
    });

    it("ip:port → 双值；裸 IP → ip", () => {
        expect(parse_bare_value("192.168.1.5:502")).toEqual({ ip: "192.168.1.5", port: 502 });
        expect(parse_bare_value("192.168.1.5")).toEqual({ ip: "192.168.1.5" });
    });

    it("自然语言起始地址「从10000开始」→ number", () => {
        expect(parse_bare_value("从10000开始")).toMatchObject({ number: 10000 });
    });

    it("非裸值消息 → null（不绑，走正常提取）", () => {
        expect(parse_bare_value("3000:风速:3")).toBeNull();
        expect(parse_bare_value("3000 风速")).toBeNull();
        expect(parse_bare_value("你好")).toBeNull();
        expect(parse_bare_value("")).toBeNull();
        expect(parse_bare_value("使用端口9001接收")).toBeNull();
    });
});

describe("bind_bare", () => {
    it("连接缺口 + 裸端口 → 绑定端口（用例 4「9001」场景）", () => {
        expect(bind_bare("recv.conn", { port: 9001 }, null)).toEqual({
            kind: "conn",
            side: "recv",
            port: 9001,
        });
        expect(bind_bare("fwd.conn", { port: 9001 }, null)).toMatchObject({
            side: "fwd",
            port: 9001,
        });
    });

    it("连接缺口 + ip:port → 双值绑定", () => {
        expect(bind_bare("recv.conn", { ip: "192.168.1.5", port: 502 }, null)).toEqual({
            kind: "conn",
            side: "recv",
            ip: "192.168.1.5",
            port: 502,
        });
    });

    it("非法端口（0 / 超范围）→ 拒绝绑定", () => {
        expect(bind_bare("recv.conn", { port: 0 }, null)).toBeNull();
        expect(bind_bare("recv.conn", { port: 70000 }, null)).toBeNull();
    });

    it("转发点表缺口 + 范围 → 按采集点数截断展开（用例 4「2390-2399」场景）", () => {
        const r = bind_bare("fwd.points", { range: { start: 2390, end: 2399 } }, 10);
        expect(r).toMatchObject({ kind: "points", side: "fwd" });
        if (r?.kind === "points") {
            expect(r.addrs).toHaveLength(10);
            expect(r.addrs[0]).toBe(2390);
            expect(r.addrs[9]).toBe(2399);
        }
    });

    it("转发点表缺口 + 裸数字起始 → 按采集点数展开（用例 5「从一万开始」场景）", () => {
        const r = bind_bare("fwd.points", { number: 10000 }, 10);
        expect(r).toMatchObject({ kind: "points", side: "fwd" });
        if (r?.kind === "points") {
            expect(r.addrs).toHaveLength(10);
            expect(r.addrs).toEqual([10000, 10001, 10002, 10003, 10004, 10005, 10006, 10007, 10008, 10009]);
        }
    });

    it("未知采集点数时裸数字起始无法确定点数 → 拒绝绑定", () => {
        expect(bind_bare("fwd.points", { number: 10000 }, null)).toBeNull();
    });

    it("接入点表缺口不参与绑定（点名必需，宁可放过不可错绑）", () => {
        expect(bind_bare("recv.points", { range: { start: 1000, end: 1009 } }, 10)).toBeNull();
    });

    it("协议/场站缺口不参与绑定", () => {
        expect(bind_bare("recv.protocol", { port: 502 }, null)).toBeNull();
        expect(bind_bare("site", { port: 502 }, null)).toBeNull();
    });
});

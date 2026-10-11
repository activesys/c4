// c4/agent/test/orchestrator/gap_question.test.ts
// 单缺口顺序提问纯函数单测（agent.md §2.6 C4He 修订：聚合提问 → 单缺口顺序提问）
//   - ask_* 确切提问文本生成：要求（字段清单）与示例（写法不限）分离
//   - parse_bare_value / bind_bare：裸值兜底绑定（宁可放过不可错绑）
// 运行：cd c4/agent && npm test

import { describe, expect, it } from "vitest";
import {
    ask_conn,
    ask_points,
    ask_protocol,
    bind_bare,
    bind_change_answer,
    bind_device_answer,
    is_forward_mirror_answer,
    parse_bare_value,
    parse_receive_port,
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

    it("未知字段回退 schema 描述主体（bucket 已进短标签表——2026-10-03 用例 39）", () => {
        // 已知字段走短标签，不走描述回退
        const known = ask_conn("转发", [
            { name: "bucket", description: "InfluxDB bucket 名称，必填：由用户提供" },
        ]);
        expect(known).toContain("bucket 名");
        // 真正的未知字段才回退 schema 描述主体
        const s = ask_conn("转发", [
            { name: "compression", description: "压缩算法，必填：由用户提供" },
        ]);
        expect(s).toContain("压缩算法");
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

    it("起始地址带点数「从1000开始，10个点。」→ number + count（2026-09-26 用例4 实测答法）", () => {
        expect(parse_bare_value("从1000开始，10个点。")).toEqual({ number: 1000, count: 10 });
        expect(parse_bare_value("从10000开始，共10个点")).toEqual({ number: 10000, count: 10 });
        expect(parse_bare_value("1000开始，10个点")).toMatchObject({ number: 1000, count: 10 });
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

    it("未知采集点数时回退用户显式点数（「从1000开始，10个点」场景）", () => {
        const r = bind_bare("fwd.points", { number: 1000, count: 10 }, null);
        expect(r).toMatchObject({ kind: "points", side: "fwd" });
        if (r?.kind === "points") {
            expect(r.addrs).toEqual([1000, 1001, 1002, 1003, 1004, 1005, 1006, 1007, 1008, 1009]);
        }
    });

    it("采集点数已知时优先于用户附带点数（一一对应以采集侧为准）", () => {
        const r = bind_bare("fwd.points", { number: 1000, count: 15 }, 10);
        expect(r).toMatchObject({ kind: "points", side: "fwd" });
        if (r?.kind === "points") {
            expect(r.addrs).toHaveLength(10);
            expect(r.addrs[9]).toBe(1009);
        }
    });

    it("接入点表缺口不参与绑定（点名必需，宁可放过不可错绑）", () => {
        expect(bind_bare("recv.points", { range: { start: 1000, end: 1009 } }, 10)).toBeNull();
    });

    it("协议/场站缺口不参与绑定", () => {
        expect(bind_bare("recv.protocol", { port: 502 }, null)).toBeNull();
        expect(bind_bare("site", { port: 502 }, null)).toBeNull();
    });
});

describe("is_forward_mirror_answer", () => {
    it("2026-09-27 用例4 线上实际应答「与接入点表一致」→ 命中", () => {
        expect(is_forward_mirror_answer("与接入点表一致")).toBe(true);
        expect(is_forward_mirror_answer("与接收侧一致")).toBe(true);
        expect(is_forward_mirror_answer("点表与I区一致")).toBe(true);
        expect(is_forward_mirror_answer("跟采集一样")).toBe(true);
        expect(is_forward_mirror_answer("一致")).toBe(true);
    });

    it("具体地址/其他内容不命中（宁可放过不可错绑）", () => {
        expect(is_forward_mirror_answer("2390-2399")).toBe(false);
        expect(is_forward_mirror_answer("从1000开始，10个点")).toBe(false);
        expect(is_forward_mirror_answer("与I区不一致")).toBe(false);
        expect(is_forward_mirror_answer("")).toBe(false);
        expect(
            is_forward_mirror_answer("转发点表用2390到2399，与I区一致的那批点"),
        ).toBe(false);
    });
});

describe("bind_change_answer（变更流应答绑定，2026-09-27 用例10）", () => {
    it("点名应答：中文词 → 落 name 且落归一化 id（2026-10-07 裁定）", () => {
        const draft = [{ addr: 2000 }];
        expect(bind_change_answer("change.name", "风速2", draft)).toBe(true);
        expect(draft[0]["name"]).toBe("风速2");
        expect(draft[0]["id"]).toBe("风速2");
    });

    it("换名应答为自然语句（携带转发地址等补充信息）→ 取引导词后尾段（用例10）", () => {
        const draft = [{ addr: 2000, name: "风速", id: "风速" }];
        expect(
            bind_change_answer("change.name", "新点名叫转速，转发地址5011", draft),
        ).toBe(true);
        expect(draft[0]["name"]).toBe("转速");
        expect(draft[0]["id"]).toBe("转速");
    });

    it("点名应答：英文形态 → name 与 id 同时落（用户原文提供）", () => {
        const draft = [{ addr: 2000 }];
        expect(bind_change_answer("change.name", "vibration", draft)).toBe(true);
        expect(draft[0]["name"]).toBe("vibration");
        expect(draft[0]["id"]).toBe("vibration");
    });

    it("点名应答：撞名换名场景 → 覆盖已拒绝的旧名，旧 id 作废并以新名归一化为 id", () => {
        const draft = [{ addr: 1010, name: "功率", id: "power" }];
        expect(bind_change_answer("change.name", "角度", draft)).toBe(true);
        expect(draft[0]["name"]).toBe("角度");
        // 旧 id 残留会被 id 重复比对误判重名（「角度 vs 功率」死循环实测）——
        // 新 id = 新点名归一化（2026-10-07 裁定），同样不得残留旧 id
        expect(draft[0]["id"]).toBe("角度");
    });

    it("点名应答：换名为英文形态 → 旧 id 作废并以新名原文为 id", () => {
        const draft = [{ addr: 1010, name: "功率", id: "power" }];
        expect(bind_change_answer("change.name", "angle", draft)).toBe(true);
        expect(draft[0]["name"]).toBe("angle");
        expect(draft[0]["id"]).toBe("angle");
    });

    it("点名应答：纯数字/多词句子 → 拒绝绑定", () => {
        expect(bind_change_answer("change.name", "6000", [{ addr: 2000 }])).toBe(false);
        expect(bind_change_answer("change.name", "风速 风速2", [{ addr: 2000 }])).toBe(false);
    });

    it("英文标识应答：合规标识 → 落 id", () => {
        const draft = [{ addr: 2000, name: "功率" }];
        expect(bind_change_answer("change.id", "power_2", draft)).toBe(true);
        expect(draft[0]["id"]).toBe("power_2");
    });

    it("英文标识应答：含点号/空白等不安全形态 → 拒绝绑定（2026-10-08 安全 key 口径）", () => {
        expect(bind_change_answer("change.id", "a.b", [{ addr: 2000 }])).toBe(false);
        expect(bind_change_answer("change.id", "a b", [{ addr: 2000 }])).toBe(false);
        expect(bind_change_answer("change.id", "功率(备用)", [{ addr: 2000 }])).toBe(true);
    });

    it("转发地址应答：恰一点缺失 + 纯数字 → 落 forward_addr（用例10「6000」场景）", () => {
        const draft = [{ addr: 2000, name: "风速2", id: "wind_speed_2" }];
        expect(bind_change_answer("change.forward_addr", "6000", draft)).toBe(true);
        expect(draft[0]["forward_addr"]).toBe(6000);
    });

    it("转发地址应答：多点缺失无法对应 → 拒绝绑定", () => {
        const draft = [{ addr: 2000 }, { addr: 2001 }];
        expect(bind_change_answer("change.forward_addr", "6000", draft)).toBe(false);
    });

    it("转发地址应答：范围/IP 形态不是转发地址 → 拒绝绑定", () => {
        const draft = [{ addr: 2000 }];
        expect(
            bind_change_answer("change.forward_addr", "2390-2399", draft),
        ).toBe(false);
        expect(
            bind_change_answer("change.forward_addr", "192.168.1.5:502", draft),
        ).toBe(false);
    });
});

describe("bind_change_answer：change.addr（先点名后补地址，2026-09-27「反向有功」）", () => {
    it("无址条目 + 纯数字应答 → 落 addr", () => {
        const draft = [{ name: "反向有功", id: "reverse_active_power" }];
        expect(bind_change_answer("change.addr", "2800", draft)).toBe(true);
        expect(draft[0]["addr"]).toBe(2800);
    });

    it("多点缺地址无法对应 → 拒绝绑定", () => {
        const draft = [{ name: "a" }, { name: "b" }];
        expect(bind_change_answer("change.addr", "2800", draft)).toBe(false);
    });

    it("范围/IP 形态不是点位地址 → 拒绝绑定", () => {
        const draft = [{ name: "a" }];
        expect(bind_change_answer("change.addr", "2390-2399", draft)).toBe(false);
        expect(bind_change_answer("change.addr", "192.168.1.5", draft)).toBe(false);
    });
});

describe("parse_receive_port（接收端口确定性捕获，2026-09-28 用例7）", () => {
    it("后缀形式「监听9001端口」→ 9001（曾漏捕）", () => {
        expect(parse_receive_port("监听9001端口")).toBe(9001);
        expect(
            parse_receive_port("监听9001端口，我们需要将这些数据转发到II区服务器上"),
        ).toBe(9001);
    });

    it("前缀形式照常捕获", () => {
        expect(parse_receive_port("接收端口使用7867")).toBe(7867);
        expect(parse_receive_port("端口：9001")).toBe(9001);
        expect(parse_receive_port("监听 9001")).toBe(9001);
    });

    it("转发语境子句排除（转发端口不是接收端口）", () => {
        expect(parse_receive_port("转发目标端口9999")).toBeNull();
        expect(
            parse_receive_port("转发到127.0.0.1:9900，转发端口9999"),
        ).toBeNull();
    });

    it("无端口表述 → null", () => {
        expect(parse_receive_port("接入1号风机的数据")).toBeNull();
        expect(parse_receive_port("")).toBeNull();
    });
});

// ── 设备缺口应答绑定（recv.device，agent.md §3.2.1.3c）──────

describe("bind_device_answer（§3.2.1.3c 三层识别 L0+追问应答）", () => {
    it("全名原样接受", () => {
        expect(bind_device_answer("2号风机", [])).toBe("2号风机");
        expect(bind_device_answer("升压站", [])).toBe("升压站");
        expect(bind_device_answer("3#主变", [])).toBe("3#主变");
    });

    it("裸数字仅在累积文本存在设备类型词时绑定", () => {
        expect(bind_device_answer("2", ["接入1号风机"])).toBe("2号风机");
        expect(bind_device_answer("2", ["升压站数据接入"])).toBe("2号升压站");
    });

    it("类型词缺失 → 不猜，返回 null 追问全名（宁可放过不可错绑）", () => {
        expect(bind_device_answer("2", ["你好"])).toBeNull();
        expect(bind_device_answer("2", [])).toBeNull();
    });

    it("中文数字应答（三号）→ 3号X", () => {
        expect(bind_device_answer("三号", ["风机的数据"])).toBe("3号风机");
    });

    it("裸「N号」「N#」形态受类型词守卫约束（不是名称）", () => {
        expect(bind_device_answer("3号", ["接入风机的数据"])).toBe("3号风机");
        expect(bind_device_answer("3号", ["你好"])).toBeNull();
        expect(bind_device_answer("3#", ["升压站数据"])).toBe("3号升压站");
    });

    it("端口/地址量级数字（100 以上）不作设备编号解释", () => {
        expect(bind_device_answer("9001", ["风机的数据"])).toBeNull();
        expect(bind_device_answer("100", ["风机的数据"])).toBeNull();
    });
});

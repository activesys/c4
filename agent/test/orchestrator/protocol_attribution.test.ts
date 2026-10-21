// protocol_attribution 单测（2026-10-17 生产野测裁定的弃权语义 + 纠错通道）：
// 跨子句语境弃绑（事故句式逐字语料）、强证据绑定、协议缺口应答语境、
// 来向前邻绑定、显式侧别重声明。断言基准为生产真实措辞，不依赖 LLM。
import { describe, it, expect } from "vitest";
import {
    recv_protocol_scan,
    side_redeclaration_scan,
    split_clauses,
    evidence_tag,
    type ProtocolMatcher,
} from "../../src/orchestrator/protocol_attribution.js";

// 注册表替身：modbus/asfp2/iec104 为接入侧支持协议，influxdb 仅转发侧；
// clauseCanonical 按别名包含（alias_match_protocol 同口径）
const SUPPORTED = ["modbus", "asfp2", "iec104"];
function makeMatcher(readerOnly: string[] = []): ProtocolMatcher {
    const all = [...SUPPORTED, ...readerOnly];
    return {
        tokenCanonical: (tok) => {
            const t = tok.toLowerCase();
            return all.find((p) => p === t) ?? null;
        },
        clauseCanonical: (clause) => {
            const lc = clause.toLowerCase();
            return all.find((p) => lc.includes(p)) ?? null;
        },
    };
}

describe("split_clauses / evidence_tag", () => {
    it("中英文标点混切", () => {
        expect(split_clauses("a，b,c。d；e;f\ng")).toEqual([
            "a", "b", "c", "d", "e", "f", "g",
        ]);
    });
    it("证据尾注：有/无/超长截断", () => {
        expect(evidence_tag(null)).toBe("");
        expect(evidence_tag("采集用modbus协议")).toBe("（依据：采集用modbus协议）");
        const long = "x".repeat(45);
        expect(evidence_tag(long)).toBe(`（依据：${"x".repeat(40)}…）`);
    });
});

describe("recv_protocol_scan 弃权语义", () => {
    const m = makeMatcher();

    it("生产事故句式逐字语料：跨子句 asfp2 不得误绑接入侧（弃权交 L2）", () => {
        const text =
            "我要接入一区风机数据，风机scada使用modbus协议转发，ip:127.0.0.1, " +
            "port:502，转发到2区，使用Asfp2协议，127.0.0.1:9907。";
        const r = recv_protocol_scan(text, m, false);
        expect(r.bind).toBeNull();
        expect(r.raw).toBeNull();
    });

    it("最小事故句式：「转发到2区，使用Asfp2协议」前邻转发 → 弃绑", () => {
        expect(recv_protocol_scan("转发到2区，使用Asfp2协议", m, false).bind).toBeNull();
    });

    it("强侧别子句照旧绑定：采集/接入/设备/装置", () => {
        expect(recv_protocol_scan("采集用modbus协议", m, false).bind?.canonical).toBe("modbus");
        expect(recv_protocol_scan("接入设备采用IEC104规约", m, false).bind?.canonical).toBe("iec104");
        expect(recv_protocol_scan("装置是IEC104规约", m, false).bind?.canonical).toBe("iec104");
    });

    it("弱标记子句：无协议缺口语境 → 弃权；应答语境 → 绑定", () => {
        expect(recv_protocol_scan("使用modbus协议", m, false).bind).toBeNull();
        expect(recv_protocol_scan("使用modbus协议", m, true).bind?.canonical).toBe("modbus");
    });

    it("前邻来向子句：MSG4 形态（转过来的）随前句绑定", () => {
        const text = "第三方厂家转过来的，asfp2协议，2390:电网频率";
        expect(recv_protocol_scan(text, m, false).bind?.canonical).toBe("asfp2");
    });

    it("前邻强接入子句：MSG44/53 形态照旧绑定", () => {
        const text44 = "现在需要接入1号风机齿轮箱油泵控制器的数据，modbus协议，设备IP是192.168.110.52";
        expect(recv_protocol_scan(text44, m, false).bind?.canonical).toBe("modbus");
        const text53 = "现在需要接入阿拉善风电场2号风机的数据，第三方厂家通过asfp2协议给我们转来2号风机数据";
        expect(recv_protocol_scan(text53, m, false).bind?.canonical).toBe("asfp2");
    });

    it("双侧同声明（用例11）：BOTH_SIDES 子句不被转发排除", () => {
        expect(
            recv_protocol_scan("接收和转发都是用asfp2协议", m, false).bind?.canonical,
        ).toBe("asfp2");
    });

    it("别名包含绑定：MSG45 形态（modbus采集口，无协议后缀）", () => {
        const text = "厂家在变桨控制器上开放了modbus采集口";
        expect(recv_protocol_scan(text, m, false).bind?.canonical).toBe("modbus");
    });

    it("不支持协议 raw：13B 形态记 raw 不绑定", () => {
        const r = recv_protocol_scan("使用abc协议接入1号风机的数据", m, false);
        expect(r.bind).toBeNull();
        expect(r.raw).toBe("abc");
    });

    it("转发标记子句照旧排除：单句混写交 L2 裁决", () => {
        expect(recv_protocol_scan("使用modbus协议转发到2区", m, false).bind).toBeNull();
    });
});

describe("side_redeclaration_scan 纠错通道", () => {
    const m = makeMatcher(["influxdb"]);

    it("生产纠错句式：「接入侧是modbus」（别名包含、无协议后缀）", () => {
        const text = "不对，接入侧是modbus，设备ip:192.168.110.54，端口502";
        const r = side_redeclaration_scan(text, m);
        expect(r.recv?.canonical).toBe("modbus");
        expect(r.recv?.clause).toBe("接入侧是modbus");
        expect(r.fwd).toBeNull();
    });

    it("「接入协议是modbus」强子句重声明", () => {
        expect(side_redeclaration_scan("接入协议是modbus", m).recv?.canonical).toBe("modbus");
    });

    it("转发侧重声明：「转发改成influxdb写入」", () => {
        const r = side_redeclaration_scan("转发改成influxdb写入", m);
        expect(r.fwd?.canonical).toBe("influxdb");
        expect(r.recv).toBeNull();
    });

    it("弱标记子句不构成重声明（纠错必须带侧别语境）", () => {
        const r = side_redeclaration_scan("就用asfp2吧", m);
        expect(r.recv).toBeNull();
        expect(r.fwd).toBeNull();
    });

    it("双侧同声明构成两侧重声明", () => {
        const r = side_redeclaration_scan("接收和转发都是用asfp2协议", m);
        expect(r.recv?.canonical).toBe("asfp2");
        expect(r.fwd?.canonical).toBe("asfp2");
    });

    it("事故句式整体不构成重声明（协议子句弱标记）", () => {
        const text =
            "我要接入一区风机数据，风机scada使用modbus协议转发，转发到2区，使用Asfp2协议";
        const r = side_redeclaration_scan(text, m);
        expect(r.recv).toBeNull();
        expect(r.fwd).toBeNull();
    });
});

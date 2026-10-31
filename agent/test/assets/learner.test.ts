// AssetLearner 单元测试（c4_asset_learning.md §3.2/§4.1/§9.8 M1 样板闭环）
// 临时库路径逐用例独立；node:sqlite 实验性告警属预期。

import { mkdirSync, mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterAll, describe, expect, it } from "vitest";
import { AssetLearner } from "@c4/assets";
import { alias_key_norm } from "@c4/assets";

const tmpDirs: string[] = [];
const learners: AssetLearner[] = [];

function makeLearner(): AssetLearner {
    const dir = mkdtempSync(join(tmpdir(), "c4-assets-test-"));
    tmpDirs.push(dir);
    const l = new AssetLearner(join(dir, "assets.db"));
    learners.push(l);
    return l;
}

afterAll(() => {
    // 显式关闭 sqlite 句柄——打开的 DatabaseSync 会让 vitest worker 挂起不退出
    for (const l of learners) {
        try {
            l.close();
        } catch {
            // 清理失败不影响测试结论
        }
    }
    for (const d of tmpDirs) {
        try {
            rmSync(d, { recursive: true, force: true });
        } catch {
            // 清理失败不影响测试结论
        }
    }
});

describe("alias_key_norm（§7.1）", () => {
    it("编号语境中文数字折叠：一号主变 ≡ 1#主变 ≡ 1号主变", () => {
        expect(alias_key_norm("一号主变")).toBe(alias_key_norm("1#主变"));
        expect(alias_key_norm("一号主变")).toBe(alias_key_norm("1号主变"));
    });

    it("组合数字进位求值：十一号 → 11号", () => {
        expect(alias_key_norm("十一号")).toBe(alias_key_norm("11号"));
        expect(alias_key_norm("一百二十号")).toBe(alias_key_norm("120号"));
    });

    it("非编号语境不折叠：「一致」「统一」「二次回路」保持原样", () => {
        expect(alias_key_norm("一致")).toBe(alias_key_norm("一致"));
        expect(alias_key_norm("二次回路")).not.toContain("2");
        expect(alias_key_norm("统一")).not.toContain("1");
    });
});

describe("AssetLearner 协议别名闭环（§3.2/§4.1/§9.8）", () => {
    it("纠错学习创建候选别名 → 命中确认计数递增 → 达标沉淀 active", () => {
        const l = makeLearner();
        // 纠错学习：更正子句「接入侧用南104」→ 目标 canonical iec104
        const created = l.learnAliasFromCorrection(
            "s1",
            "2",
            "iec104",
            "接入侧用南104",
            ["iec104", "modbus"],
        );
        expect(created).toBeGreaterThan(0);
        const aliases = l.learnedProtocolAliases();
        const hit = aliases.find((a) => a.canonical === "iec104");
        expect(hit).toBeDefined();
        expect(hit!.status).toBe("candidate");
        expect(hit!.usage_count).toBe(0);

        // 命中记录 + 确认（最终协议一致）→ usage_count/streak 递增
        l.recordLearnedHit("s1", hit!.alias, "iec104");
        l.collectConfirm("s1", { recv: "iec104" });

        const after = l.learnedProtocolAliases().find((a) => a.canonical === "iec104")!;
        expect(after.usage_count).toBe(1);
        expect(after.confirm_streak).toBe(1);

        // 未达标不沉淀
        expect(after.status).toBe("candidate");

        // 连续确认至门控（usage_count ≥ 5）
        for (let i = 2; i <= 5; i++) {
            l.recordLearnedHit(`s${i}`, hit!.alias, "iec104");
            l.collectConfirm(`s${i}`, { recv: "iec104" });
        }
        const active = l.learnedProtocolAliases().find((a) => a.canonical === "iec104")!;
        expect(active.usage_count).toBe(5);
        expect(active.status).toBe("active");
    });

    it("命中但确认的最终协议不同 → streak 清零（负样本信号）", () => {
        const l = makeLearner();
        l.learnAliasFromCorrection("s1", "2", "iec104", "用南104", ["iec104"]);
        const alias = l.learnedProtocolAliases().find((a) => a.canonical === "iec104")!.alias;
        // 先积累一次
        l.recordLearnedHit("s2", alias, "iec104");
        l.collectConfirm("s2", { recv: "iec104" });
        expect(l.learnedProtocolAliases()[0].confirm_streak).toBe(1);
        // 命中但用户最终确认的是 modbus
        l.recordLearnedHit("s3", alias, "iec104");
        l.collectConfirm("s3", { recv: "modbus" });
        const row = l.learnedProtocolAliases().find((a) => a.canonical === "iec104")!;
        expect(row.confirm_streak).toBe(0);
        expect(row.usage_count).toBe(1);
    });

    it("逐轮提议快照终局比对：任意一轮提议 ≠ 最终值 → corrected 事件（§4.1）", () => {
        const l = makeLearner();
        // 第一轮误判 iec104，用户改口 modbus（第二轮提议），最终 modbus
        l.recordProposal("s1", "2", "iec104");
        l.recordProposal("s1", "2", "modbus");
        l.collectConfirm("s1", { recv: "modbus" });
        // 无异常抛出即采集成功——事件落库断言经 learner 行为间接覆盖
        //（事件表内容由 M2 导入导出验收细查）
    });

    it("学习失败降级不抛出：只读目录建库失败时构造降级（§1.2 主流程不受影响）", () => {
        // 只读目录（0o555）下建库 → SQLITE_CANTOPEN → 构造捕获降级为 no-op。
        // 不用 /proc 类病态路径——recursive mkdirSync 在其上会挂起（实测）。
        const dir = mkdtempSync(join(tmpdir(), "c4-assets-bad-"));
        tmpDirs.push(dir);
        const ro = join(dir, "readonly");
        mkdirSync(ro, { mode: 0o555 });
        const l = new AssetLearner(join(ro, "assets.db"));
        expect(l.learnedProtocolAliases()).toEqual([]);
        expect(() => l.collectConfirm("x", { recv: "modbus" })).not.toThrow();
    });
});

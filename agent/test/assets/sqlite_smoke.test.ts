import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import { AssetLearner } from "@c4/assets";

describe("hit + confirm loop", () => {
    it("bumps counters", () => {
        const dir = mkdtempSync(join(tmpdir(), "sqlite-smoke-"));
        const l = new AssetLearner(join(dir, "assets.db"));
        l.learnAliasFromCorrection("s1", "2", "iec104", "用南104", ["iec104"]);
        const alias = l.learnedProtocolAliases()[0].alias;
        l.recordLearnedHit("s2", alias, "iec104");
        l.collectConfirm("s2", { recv: "iec104" });
        const row = l.learnedProtocolAliases()[0];
        console.log("row:", row);
        expect(row.usage_count).toBe(1);
        l.close();
    });
});

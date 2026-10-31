// gap_answer 单测（2026-10-17 生产收摊实测的词形对齐）：字段缺口的追问文案
// 教用户的词是「uid」，绑定器必须认识它 + 常见口语变体。纯确定性层，全词形
// 断言不依赖 LLM。
import { describe, it, expect } from "vitest";
import {
    fwd_file_gate,
    uid_answer_of,
} from "@c4/stages";

describe("uid_answer_of 词形对齐", () => {
    it("从站词形（2026-10-02 链步31 原口径，不得退化）", () => {
        expect(uid_answer_of("从站号都是1")).toBe(1);
        expect(uid_answer_of("从站号是1")).toBe(1);
        expect(uid_answer_of("从站号:1")).toBe(1);
        expect(uid_answer_of("从站号：1")).toBe(1);
        expect(uid_answer_of("从站是2")).toBe(2);
        expect(uid_answer_of("从站号均为3")).toBe(3);
        expect(uid_answer_of("从站号都是1，端口502")).toBe(1);
    });

    it("uid 词形（生产收摊三连中前两连，系统教的词必须认）", () => {
        expect(uid_answer_of("uid是1")).toBe(1);
        expect(uid_answer_of("uid:1")).toBe(1);
        expect(uid_answer_of("uid为2")).toBe(2);
        expect(uid_answer_of("UID 都是 1")).toBe(1);
        expect(uid_answer_of("Uid都是1")).toBe(1);
        expect(uid_answer_of("uid都是1")).toBe(1);
    });

    it("裸值兜底（生产收摊三连中的第三连；提问即语境锁定）", () => {
        expect(uid_answer_of("1")).toBe(1);
        expect(uid_answer_of(" 1 ")).toBe(1);
        expect(uid_answer_of("都是1")).toBe(1);
        expect(uid_answer_of("全是2")).toBe(2);
        expect(uid_answer_of("均为3")).toBe(3);
        expect(uid_answer_of("247")).toBe(247);
    });

    it("非 uid 应答不得误绑", () => {
        expect(uid_answer_of("")).toBeNull();
        expect(uid_answer_of("风速是1")).toBeNull();
        expect(uid_answer_of("1100")).toBeNull();
        expect(uid_answer_of("转发点从1000开始")).toBeNull();
        expect(uid_answer_of("继续")).toBeNull();
        expect(uid_answer_of("从站号")).toBeNull();
        expect(uid_answer_of("3000:风速")).toBeNull();
    });
});

describe("fwd_file_gate 转发文件通道门控", () => {
    const UPLOAD_MSG =
        "用户上传了文件: path=/tmp/c4_upload_x.xlsx, name=景观中科机组SCADA集控点表V1.5.xlsx" +
        "\n用户消息: 请解析此文件中的设备信息（展示要求：全程使用中文回复）";

    it("生产事故形态：应「接入点表」之问上传文件 → 不得注入转发提取", () => {
        // 上传时刻 pendingGap 是接入点表缺口（12:56 野测实值）
        expect(fwd_file_gate("recv.points", UPLOAD_MSG)).toBe(false);
        expect(fwd_file_gate(null, UPLOAD_MSG)).toBe(false);
    });

    it("设计场景①：正在问转发点表缺口时上传 → 放行", () => {
        expect(fwd_file_gate("fwd.points", UPLOAD_MSG)).toBe(true);
        expect(fwd_file_gate("fwd.points", "")).toBe(true);
    });

    it("设计场景②：本轮消息明确说了转发点表/转发地址 → 放行", () => {
        expect(
            fwd_file_gate("recv.points", "转发点表用这个文件\n用户上传了文件: path=/tmp/a.xlsx"),
        ).toBe(true);
        expect(fwd_file_gate(null, "转发地址见文件")).toBe(true);
    });

    it("其他缺口语境一律不放行（歧义交给追问，不猜）", () => {
        expect(fwd_file_gate("fwd.required", UPLOAD_MSG)).toBe(false);
        expect(fwd_file_gate("recv.device", "用户上传了文件: path=/tmp/a.xlsx")).toBe(false);
        expect(fwd_file_gate(null, "转发到2区，使用Asfp2协议")).toBe(false);
    });
});

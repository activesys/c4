// c4/agent/test/orchestrator/zh_numeral.test.ts
// 自然语言起始地址提取单测（func_test_case 用例 5：「转发地址使用从一万开始的地址」
// → 10000；同时必须不误伤「一一对应」类普通词汇）

import { describe, expect, it } from "vitest";
import { zh_start_address } from "../../src/orchestrator/zh_numeral.js";

describe("zh_start_address", () => {
  it("用例 5 原句：从一万开始 → 10000", () => {
    expect(
      zh_start_address("接收端口使用9001，转发地址使用从一万开始的地址。"),
    ).toBe(10000);
  });

  it("两万 → 20000", () => {
    expect(zh_start_address("转发地址从两万开始")).toBe(20000);
  });

  it("一万二 → 12000（短尾按千位补齐）", () => {
    expect(zh_start_address("从一万二开始")).toBe(12000);
  });

  it("一万零九 → 10009（零位补齐）", () => {
    expect(zh_start_address("从一万零九开始")).toBe(10009);
  });

  it("阿拉伯数字起始：从3500开始 → 3500", () => {
    expect(zh_start_address("转发地址从3500开始")).toBe(3500);
  });

  it("「一一对应」等普通词汇不得误判为起始地址", () => {
    expect(zh_start_address("转发点表与接收点表一一对应，2390到2399。")).toBeNull();
  });

  it("无起始地址表述 → null", () => {
    expect(zh_start_address("现在需要接入1号风机的数据，asfp2协议")).toBeNull();
    expect(zh_start_address("端口：9001")).toBeNull();
  });
});

// c4/test/web/unit/confirm_detect.test.ts
// L1 unit tests for confirm-phrase detection — web.md §3.1.3, §3.1.3 匹配健壮性.
//
// matchConfirmPhrase operates on the *accumulated* agent bubble text (not
// individual tokens). It must match full phrase patterns, never bare keywords.
//
// Button payloads are structured envelopes (web.md §3.1.3): the bracketed
// prefix is the ONLY confirmation channel recognized by the backend
// (super_worker.ts「执行闸门」). Free-typed text is never treated as
// confirmation.
//
// Frontend must NOT match the bare keywords — the design (web.md §3.1.3) says
// words like 执行/好的/开始 appear in normal sentences and would cause false
// positives. Only *confirm phrases* ("是否确认", "确认执行", etc.) trigger
// the buttons.
//
// 3.2.4 and 3.2.5 (button payloads) are exercised in chat_stream.test.tsx via
// the full ChatView hook, since they require the stream + send pipeline.

import { describe, it, expect } from "vitest";
import {
  matchConfirmPhrase,
  CONFIRM_KEYWORD,
  CANCEL_KEYWORD,
  buttonDisplayLabel,
} from "@frontend/hooks/useConfirmDetect";

describe("matchConfirmPhrase — full-phrase matching (web.md §3.1.3)", () => {
  it("3.2.1 matches a complete confirm phrase '是否确认执行' on accumulated text", () => {
    expect(matchConfirmPhrase("…是否确认执行？")).toBe(true);
  });

  it("3.2.2 still matches after the phrase is split across two text events (累积匹配)", () => {
    // 「是否」 arrives in event 1, 「确认」 arrives in event 2.
    // The hook operates on the *accumulated* buffer, so after concatenation
    // the full phrase is present and must match.
    const accumulated = "是否" + "确认";
    expect(matchConfirmPhrase(accumulated)).toBe(true);
  });

  it("3.2.3 does NOT match bare keywords used in normal sentences (拒绝误触发)", () => {
    expect(matchConfirmPhrase("好的，我明白了")).toBe(false);
    expect(matchConfirmPhrase("请执行下一步")).toBe(false);
    expect(matchConfirmPhrase("我们开始讨论吧")).toBe(false);
  });

  it("3.2.6 does NOT match non-plan text (chitchat)", () => {
    expect(matchConfirmPhrase("请问今天天气如何")).toBe(false);
    expect(matchConfirmPhrase("好的，我记下来了")).toBe(false);
  });
});

describe("useConfirmDetect — exported constants (web.md §3.1.3)", () => {
  it("CONFIRM_KEYWORD carries the structured button envelope prefix", () => {
    expect(CONFIRM_KEYWORD).toBe("[C4_BUTTON_CONFIRM] 确认");
    expect(CONFIRM_KEYWORD.startsWith("[C4_BUTTON_CONFIRM]")).toBe(true);
  });

  it("CANCEL_KEYWORD carries the structured cancel envelope prefix", () => {
    expect(CANCEL_KEYWORD).toBe("[C4_BUTTON_CANCEL] 取消，不执行");
    expect(CANCEL_KEYWORD.startsWith("[C4_BUTTON_CANCEL]")).toBe(true);
  });

  it("free-typed text never equals the structured button payload (web.md §3.1.3 唯一通道)", () => {
    expect("确认").not.toBe(CONFIRM_KEYWORD);
    expect("从一万开始").not.toContain("[C4_BUTTON_CONFIRM]");
  });
});

describe("buttonDisplayLabel — bubble display mapping (web.md §3.1.3)", () => {
  it("maps the confirm envelope to its friendly label", () => {
    expect(buttonDisplayLabel(CONFIRM_KEYWORD)).toBe("确认");
  });

  it("maps the cancel envelope to its friendly label", () => {
    expect(buttonDisplayLabel(CANCEL_KEYWORD)).toBe("取消，不执行");
  });

  it("returns null for free-typed text (raw content renders as-is)", () => {
    expect(buttonDisplayLabel("确认")).toBeNull();
    expect(buttonDisplayLabel("从一万开始")).toBeNull();
    expect(buttonDisplayLabel("")).toBeNull();
  });
});

describe("matchConfirmPhrase — phrase coverage", () => {
  // Cover the full set of confirm phrases the design calls out, plus negative cases.
  const positive = [
    "是否确认",
    "是否确认执行",
    "确认执行",
    "是否确认执行？",
    "请确认接入方案",
    "请确认是否按方案执行",
  ];
  const negative = [
    "",
    "开始",
    "好的",
    "执行",
    "按方案",
    "请帮我看看这个方案",
    "请问是否需要确认一下",
  ];

  it.each(positive)("matches: %s", (text) => {
    expect(matchConfirmPhrase(text)).toBe(true);
  });
  it.each(negative)("does not match: %s", (text) => {
    expect(matchConfirmPhrase(text)).toBe(false);
  });
});

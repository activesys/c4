// c4/test/web/unit/confirm_detect.test.ts
// L1 unit tests for confirm-phrase detection — web.md §3.1.3, §3.1.3 匹配健壮性.
//
// matchConfirmPhrase operates on the *accumulated* agent bubble text (not
// individual tokens). It must match full phrase patterns, never bare keywords.
//
// Backend regex (informational, NOT used here):
//   /确认|好的|执行|按方案|开始/                → confirm
//   /取消|拒绝|放弃|停止|算了|不执行|不要执行|不确认/  → reject (defensive)
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
  it("CONFIRM_KEYWORD is the literal string '确认'", () => {
    expect(CONFIRM_KEYWORD).toBe("确认");
  });

  it("CANCEL_KEYWORD is the literal string '取消，不执行'", () => {
    expect(CANCEL_KEYWORD).toBe("取消，不执行");
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

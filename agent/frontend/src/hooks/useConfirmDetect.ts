// c4/agent/frontend/src/hooks/useConfirmDetect.ts
// Confirm-phrase detection on the *accumulated* agent bubble — web.md §3.1.3.
//
// The backend's keyword regex (/确认|好的|执行|按方案|开始/) is too loose: words
// like 「好的」, 「执行」, 「开始」 appear in normal sentences and would cause
// false positives. The frontend therefore matches *phrases* that explicitly
// indicate the agent has finished presenting a plan and is asking for
// confirmation:
//   - "是否确认"
//   - "是否确认执行"
//   - "确认执行"
//   - "请确认"
//   - "请确认是否按方案执行"
//


/** Literal text sent as a plain chat message when the user clicks 「确认」.
 *
 * Structured envelope: the bracketed prefix is the ONLY confirmation channel
 * recognized by the backend (super_worker.ts「执行闸门」). Free-typed text is
 * never treated as confirmation — see agent.md「执行闸门」. Keep the prefix in
 * sync with the backend constant CONFIRM_BUTTON_PREFIX.
 */
export const CONFIRM_KEYWORD = "[C4_BUTTON_CONFIRM] 确认";

/** Literal text sent as a plain chat message when the user clicks 「取消」.
 *
 * Structured envelope mirroring CONFIRM_KEYWORD; keep the prefix in sync with
 * the backend constant CANCEL_BUTTON_PREFIX.
 */
export const CANCEL_KEYWORD = "[C4_BUTTON_CANCEL] 取消，不执行";

/**
 * Phrases that mean "the agent has just presented a plan and is asking for
 * confirmation". Match on the *accumulated* agent bubble text. Bare keywords
 * (确认/好的/执行/按方案/开始) are intentionally NOT in this list — see web.md
 * §3.1.3 匹配健壮性.
 */
const CONFIRM_PHRASES = [
  "是否确认",
  "确认执行",
  "请确认",
];

/**
 * Pure function — given the accumulated assistant text, return whether the
 * confirm/cancel buttons should be visible. Operates on the buffer, not on
 * individual tokens, so it is robust to phrase splits across text events.
 */
export function matchConfirmPhrase(accumulatedText: string): boolean {
  if (!accumulatedText) return false;
  return CONFIRM_PHRASES.some((phrase) => accumulatedText.includes(phrase));
}

/** Friendly label for button-originated messages, or null for plain text.
 * Rendered in the chat bubble; the raw envelope still travels on the wire.
 */
export function buttonDisplayLabel(text: string): string | null {
  if (text.startsWith(CONFIRM_KEYWORD)) return "确认";
  if (text.startsWith(CANCEL_KEYWORD)) return "取消，不执行";
  return null;
}

// c4/agent/frontend/src/hooks/useDoubleEscCancel.ts
// 双 ESC 全局取消手势 — web.md §3.7.6（2026-10-24 用户裁定）。
// 连续两次 Esc（判定窗口 1500ms）→ POST /api/cancel（阻塞态中断：显示会话终止 /
// 在途回合中止；非阻塞态后端空操作）。首次按键 toast「再按一次 Esc 取消当前操作」
// （1500ms 自灭）；模态框（role=dialog）打开期间 Esc 归模态框——不计数。
// 手势名统一「双击 ESC」（用户可见文案标准形；「双 ESC」为文档简写）。

import { useCallback, useEffect, useRef, useState } from "react";
import { cancelBlocked } from "@frontend/api/display";

const ESC_WINDOW_MS = 1500;

export function useDoubleEscCancel(): {
  /** 首次按键后的提示文案（窗口内非空；超时/触发后清空） */
  hint: string;
} {
  const [hint, setHint] = useState("");
  const lastEscRef = useRef(0);
  const hintTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const clearHint = useCallback(() => {
    if (hintTimerRef.current !== null) {
      clearTimeout(hintTimerRef.current);
      hintTimerRef.current = null;
    }
    setHint("");
  }, []);

  useEffect(() => {
    const onKeydown = (e: KeyboardEvent) => {
      if (e.key !== "Escape") return;
      // 模态框优先（web.md §3.7.6）：site-edit / mcp-detail 等打开期间 Esc 关闭
      // 对话框，不计数、不触发取消手势
      if (document.querySelector('[role="dialog"]') !== null) return;

      const now = Date.now();
      if (now - lastEscRef.current <= ESC_WINDOW_MS) {
        // 双击成立 → 结构化取消（阻塞态路由在后端）；失败静默（网络抖动下次再按）
        lastEscRef.current = 0;
        clearHint();
        void cancelBlocked().catch(() => undefined);
        return;
      }
      lastEscRef.current = now;
      setHint("再按一次 Esc 取消当前操作");
      if (hintTimerRef.current !== null) clearTimeout(hintTimerRef.current);
      hintTimerRef.current = setTimeout(() => {
        hintTimerRef.current = null;
        setHint("");
      }, ESC_WINDOW_MS);
    };

    window.addEventListener("keydown", onKeydown);
    return () => {
      window.removeEventListener("keydown", onKeydown);
      if (hintTimerRef.current !== null) clearTimeout(hintTimerRef.current);
    };
  }, [clearHint]);

  return { hint };
}

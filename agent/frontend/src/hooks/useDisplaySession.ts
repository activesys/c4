// c4/agent/frontend/src/hooks/useDisplaySession.ts
// /api/display 页面级常驻轮询 — web.md §3.5.3/§3.5.6。
// 1s 探测（与 /api/state 轮询同层）：探测到活跃会话后改按其 intervalMs 轮询数据；
// 页面刷新后同样经常驻探测恢复展示与提交门控态。门控（active）与表格渲染同源。

import { useEffect, useRef, useState } from "react";
import { fetchDisplay, type DisplayPayload } from "@frontend/api/display";

export function useDisplaySession(): {
  /** 最近一次 /api/display 载荷（null = 首轮未返回） */
  payload: DisplayPayload | null;
  /** 活跃显示会话是否存在（提交门控判定源，web.md §3.5.6） */
  active: boolean;
} {
  const [payload, setPayload] = useState<DisplayPayload | null>(null);
  // 轮询周期随会话 intervalMs 收紧；无会话时 1s 探测
  const intervalRef = useRef(1000);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const stoppedRef = useRef(false);

  useEffect(() => {
    stoppedRef.current = false;

    const poll = async () => {
      try {
        const next = await fetchDisplay();
        if (!stoppedRef.current) setPayload(next);
        intervalRef.current = next.active
          ? Math.max(250, next.session?.intervalMs ?? 1000)
          : 1000;
      } catch {
        // 读取失败保持上次值（不杜撰）；周期回落探测档
        intervalRef.current = 1000;
      }
      if (!stoppedRef.current) {
        timerRef.current = setTimeout(() => void poll(), intervalRef.current);
      }
    };

    void poll();
    return () => {
      stoppedRef.current = true;
      if (timerRef.current !== null) clearTimeout(timerRef.current);
    };
  }, []);

  return { payload, active: payload?.active === true };
}

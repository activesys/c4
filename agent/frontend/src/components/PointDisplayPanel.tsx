// c4/agent/frontend/src/components/PointDisplayPanel.tsx
// 对点核验显示卡片（web.md §3.5）——轮询 /api/display，随活跃会话存在。

import { useCallback, useEffect, useRef, useState } from "react";

interface PointRecord {
    tick: number;
    t: number;
    v: number | string;
}

interface PointSnapshot {
    key: string;
    name?: string;
    value: number | string | null;
    timestampMs: number | null;
    state: "ok" | "no_data" | "stale";
    staleForMs?: number;
    freq: { count: number; intervalMs: number };
    lastError?: string;
    records?: PointRecord[];
}

interface DisplayPayload {
    active: boolean;
    session?: {
        sessionId: string;
        intervalMs: number;
        mode: "realtime" | "cumulative";
        tick: number;
        terminateRemaining?: string;
        degraded?: boolean;
        points: PointSnapshot[];
    };
    lastSession?: { endedReason: string; finalTick: number };
}

const STATE_LABEL: Record<PointSnapshot["state"], { text: string; cls: string }> = {
    ok: { text: "正常", cls: "pdp__badge pdp__badge--ok" },
    no_data: { text: "暂无数据", cls: "pdp__badge pdp__badge--nodata" },
    stale: { text: "已停止刷新", cls: "pdp__badge pdp__badge--stale" },
};

function fmtAge(ts: number | null): string {
    if (ts === null) return "—";
    const s = Math.max(0, Math.round((Date.now() - ts) / 1000));
    return s < 60 ? `${s} 秒前` : `${Math.floor(s / 60)} 分钟前`;
}

function fmtTime(ts: number | null): string {
    if (ts === null) return "—";
    const d = new Date(ts);
    const p2 = (n: number) => String(n).padStart(2, "0");
    return `${p2(d.getHours())}:${p2(d.getMinutes())}:${p2(d.getSeconds())}`;
}

export function PointDisplayPanel(): JSX.Element | null {
    const [payload, setPayload] = useState<DisplayPayload | null>(null);
    const [prevSessionId, setPrevSessionId] = useState<string | null>(null);
    const cursorRef = useRef<number | null>(null);
    const lastParamsRef = useRef<{ pointKeys: string[]; mode: string } | null>(null);

    const poll = useCallback(async () => {
        try {
            const q =
                payload?.session?.mode === "cumulative" && cursorRef.current !== null
                    ? `?since=${cursorRef.current}`
                    : "";
            const res = await fetch(`/api/display${q}`);
            const data = (await res.json()) as DisplayPayload;
            if (data.session && data.session.sessionId !== prevSessionId) {
                cursorRef.current = null; // 新会话：弃旧游标
                if (prevSessionId !== null) {
                    lastParamsRef.current = null; // 跨会话参数不保留
                }
            }
            setPrevSessionId(data.session ? data.session.sessionId : prevSessionId);
            if (data.session) {
                lastParamsRef.current = {
                    pointKeys: data.session.points.map((p) => p.key),
                    mode: data.session.mode,
                };
            }
            setPayload(data);
        } catch {
            setPayload((p) => p ?? { active: false });
        }
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [payload?.session?.mode, prevSessionId]);

    useEffect(() => {
        const interval = payload?.session?.intervalMs ?? 1000;
        const t = setInterval(() => void poll(), interval);
        void poll();
        return () => clearInterval(t);
    }, [poll, payload?.session?.intervalMs]);

    const stop = useCallback(
        async (pointKeys?: string[]) => {
            await fetch("/api/display/stop", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ pointKeys }),
            });
            void poll();
        },
        // eslint-disable-next-line react-hooks/exhaustive-deps
        [],
    );

    const resubscribe = useCallback(async () => {
        const params = lastParamsRef.current;
        if (!params) return;
        await fetch("/api/display", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ pointKeys: params.pointKeys, mode: params.mode }),
        });
        void poll();
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, []);

    if (!payload) return null;

    if (!payload.active || !payload.session) {
        if (!payload.lastSession) return null;
        const ls = payload.lastSession;
        const done = ls.endedReason === "completed_count" || ls.endedReason === "completed_duration";
        return (
            <div className="pdp pdp--ended" data-testid="point-display-ended">
                {done
                    ? `显示已完成（共刷新 ${ls.finalTick} 次）`
                    : "显示已中止，数据管道未受影响"}
                {lastParamsRef.current && (
                    <button className="pdp__resubscribe" onClick={() => void resubscribe()}>
                        重新订阅
                    </button>
                )}
            </div>
        );
    }

    const s = payload.session;
    return (
        <div className="pdp" data-testid="point-display-panel">
            <div className="pdp__head">
                <span>
                    显示中 · {s.mode === "cumulative" ? "累积模式" : "实时值模式"}
                    {s.terminateRemaining ? ` · ${s.terminateRemaining}` : ""}
                    {s.degraded ? " · 读取通道异常" : ""}
                </span>
                <button className="pdp__stop" onClick={() => void stop()} data-testid="pdp-stop">
                    ✕ 停止
                </button>
            </div>
            {s.points.map((p) => {
                const badge = STATE_LABEL[p.state];
                return (
                    <div className="pdp__point" key={p.key}>
                        <div className="pdp__point-head">
                            <span className="pdp__name">
                                {p.name ?? p.key}
                                {p.name && <span className="pdp__key-sub">{p.key}</span>}
                            </span>
                            <span className={badge.cls}>
                                {p.state === "stale" && p.staleForMs !== undefined
                                    ? `已停止刷新 ${Math.round(p.staleForMs / 1000)} 秒`
                                    : badge.text}
                            </span>
                            {s.points.length > 1 && (
                                <button
                                    className="pdp__point-stop"
                                    onClick={() => void stop([p.key])}
                                    aria-label={`停止 ${p.name ?? p.key}`}
                                >
                                    ✕
                                </button>
                            )}
                        </div>
                        <div className="pdp__value">
                            {p.state === "no_data" ? "—" : String(p.value ?? "—")}
                        </div>
                        <div className="pdp__meta">
                            数据时间 {fmtTime(p.timestampMs)}（{fmtAge(p.timestampMs)}） · 近 60s
                            刷新 {p.freq.count} 次
                            {p.freq.intervalMs > 0
                                ? ` · 平均 ${(p.freq.intervalMs / 1000).toFixed(2)}s`
                                : ""}
                        </div>
                        {s.mode === "cumulative" && p.records && p.records.length > 0 && (
                            <table className="pdp__records">
                                <tbody>
                                    {[...p.records].reverse().map((r) => (
                                        <tr key={r.tick}>
                                            <td>{new Date(r.t).toLocaleTimeString()}</td>
                                            <td>{String(r.v)}</td>
                                        </tr>
                                    ))}
                                </tbody>
                            </table>
                        )}
                    </div>
                );
            })}
        </div>
    );
}

// c4/agent/frontend/src/components/PointDisplayTable.tsx
// 点位显示表格 — web.md §3.5（2026-10-23 呈现范式重构：卡片范式废除）。
// 页面一体的朴素三列表格「设备名 | 点名 | 值」（无边框阴影浮层），表格上方
// 展示信息区（粘性）；数据在表格内原地刷新（逐字段替换非整表重绘）。
//   - 值单元格两级结构：主行原始值 / 异常态状态文案替代数值（C4_RS_00054——
//     标注旁的陈旧值仍易被误用，宁可信息降级不可误读）；次行「时间 · 近 60s
//     刷新 N 次 · 平均 X.Xs」（C4_RS_00055 两项频率指标齐全）。
//   - 累积模式：值格次行已刷计数，点击行展开序列明细（手风琴单开）。
//   - 终止：双击 ESC（唯一手动通道，web.md §3.7.6——无停止按钮）；会话结束后
//     信息区原位转终止摘要（2026-10-28 用户裁定：重新订阅按钮废除——重开显示由
//     用户重新发起对话完成）。
// 轮询由 ChatView 的 useDisplaySession 页面级常驻承载（门控与渲染同源）。

import { useState } from "react";
import {
  type DisplayPayload,
  type DisplayPointSnapshot,
} from "@frontend/api/display";

export interface PointDisplayTableProps {
  payload: DisplayPayload | null;
}

function clockOf(ts: number | null): string {
  if (ts === null) return "—";
  const d = new Date(ts);
  const p = (n: number): string => String(n).padStart(2, "0");
  return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

/** 裸点名：去掉 channel{N}. 实例句柄（key 句柄属豁免场景，仅悬浮 tooltip 展示） */
function bareName(key: string): string {
  const idx = key.indexOf(".");
  return idx >= 0 ? key.slice(idx + 1) : key;
}

function staleMinutes(p: DisplayPointSnapshot): number {
  return Math.max(1, Math.round((p.staleForMs ?? 0) / 60_000));
}

function endedSummary(
  reason: NonNullable<DisplayPayload["lastSession"]>["endedReason"],
  finalTick: number,
): string {
  switch (reason) {
    case "completed_count":
    case "completed_duration":
      return `显示已完成（共刷新 ${finalTick} 次），数据管道未受影响`;
    case "replaced":
      return "显示已切换（新显示已建立）";
    case "error":
      return "显示已中止，数据管道未受影响";
    case "stopped":
    default:
      return "显示已停止，数据管道未受影响";
  }
}

export function PointDisplayTable({
  payload,
}: PointDisplayTableProps): JSX.Element | null {
  // 累积模式行展开（手风琴单开，web.md §3.5.2——防批量下 DOM 爆炸）
  const [expandedKey, setExpandedKey] = useState<string | null>(null);

  const session = payload?.session;
  const lastSession = payload?.lastSession ?? null;

  if (payload === null) return null;

  if (!payload.active || session === undefined) {
    if (lastSession === null) return null;
    return (
      <div className="pdt pdt--ended" data-testid="point-display-ended">
        <div className="pdt__ended-row">
          <span data-testid="point-display-ended-text">
            {endedSummary(lastSession.endedReason, lastSession.finalTick)}
          </span>
        </div>
      </div>
    );
  }

  // 行序确定性（web.md §3.5.5）：device 字典序 → key 字典序；任意规模同一平铺形态
  const rows = [...session.points].sort((a, b) =>
    a.device === b.device
      ? a.key.localeCompare(b.key)
      : a.device.localeCompare(b.device),
  );
  const deviceCount = new Set(rows.map((p) => p.device)).size;
  const intervalText = String(session.intervalMs / 1000).replace(/\.0$/, "");
  const remaining =
    session.terminateRemaining !== undefined
      ? session.terminateRemaining.includes("/")
        ? `已刷 ${session.terminateRemaining}`
        : `剩余 ${session.terminateRemaining.replace(/s$/, " 秒")}`
      : "持续显示";

  return (
    <div className="pdt" data-testid="point-display-table">
      <div className="pdt__info" data-testid="point-display-info">
        <span className="pdt__info-main">
          显示中 · {session.mode === "cumulative" ? "累积模式" : "实时值模式"} · 每{" "}
          {intervalText} 秒刷新 · {remaining} · 共 {rows.length} 点 · {deviceCount}{" "}
          台设备
        </span>
        <span className="pdt__esc-hint">双击 ESC 停止显示</span>
      </div>
      {session.degraded === true ? (
        <div className="pdt__degraded" role="alert">
          读取通道异常，数据管道不受影响
        </div>
      ) : null}
      <div className="pdt__scroll">
        <table className="pdt__table" data-testid="point-display-rows">
          <thead>
            <tr>
              <th>设备名</th>
              <th>点名</th>
              <th>值</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((p) => {
              const isCumulative = session.mode === "cumulative";
              const expanded = isCumulative && expandedKey === p.key;
              const name = p.name !== undefined && p.name !== "" ? p.name : bareName(p.key);
              return (
                <RowGroup
                  key={p.key}
                  point={p}
                  cumulative={isCumulative}
                  expanded={expanded}
                  onToggle={() => setExpandedKey(expanded ? null : p.key)}
                />
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function RowGroup({
  point,
  cumulative,
  expanded,
  onToggle,
}: {
  point: DisplayPointSnapshot;
  cumulative: boolean;
  expanded: boolean;
  onToggle: () => void;
}): JSX.Element {
  const name =
    point.name !== undefined && point.name !== "" ? point.name : bareName(point.key);
  return (
    <>
      <tr
        className={cumulative ? "pdt__row--expandable" : undefined}
        onClick={cumulative ? onToggle : undefined}
        data-testid="point-display-row"
        title={point.key}
      >
        <td className="pdt__device">{point.device}</td>
        <td className="pdt__name">{name}</td>
        <td className="pdt__value">
          <span
            className={`pdt__main pdt__main--${point.state}`}
            data-testid={`point-value-${point.state}`}
          >
            {point.state === "ok" && point.value !== null
              ? String(point.value)
              : point.state === "no_data"
                ? "暂无数据"
                : `已停止刷新 ${staleMinutes(point)} 分钟`}
          </span>
        </td>
      </tr>
      {expanded && point.records !== undefined ? (
        <tr className="pdt__expand">
          <td colSpan={3}>
            <ul className="pdt__records">
              {[...point.records].reverse().map((r) => (
                <li key={r.tick}>
                  <span>{clockOf(r.t)}</span>
                  <span>{String(r.v)}</span>
                </li>
              ))}
            </ul>
          </td>
        </tr>
      ) : null}
    </>
  );
}

// ── 结束会话的沉淀快照表（2026-10-28 用户裁定：刷新停止/双 ESC 停止后表格
//    保留在页面中，每次显示在下方重新生成）——只读三列表格（最后值/最后状态）
//    + 结束原因行 ─────────────────────────────

export interface DisplaySnapshot {
  /** 会话结束前的最后活跃载荷（session.points 含最后值/最后状态） */
  payload: DisplayPayload & { session: NonNullable<DisplayPayload["session"]> };
  endedReason: NonNullable<DisplayPayload["lastSession"]>["endedReason"];
  finalTick: number;
}

export function DisplaySnapshotTable({
  snapshot,
}: {
  snapshot: DisplaySnapshot;
}): JSX.Element {
  const session = snapshot.payload.session;
  const rows = [...session.points].sort((a, b) =>
    a.device === b.device
      ? a.key.localeCompare(b.key)
      : a.device.localeCompare(b.device),
  );
  const intervalText = String(session.intervalMs / 1000).replace(/\.0$/, "");

  return (
    <div className="pdt pdt--ended" data-testid="point-display-snapshot">
      <div className="pdt__ended-row">
        <span data-testid="point-display-ended-text">
          {endedSummary(snapshot.endedReason, snapshot.finalTick)} · 每{" "}
          {intervalText} 秒刷新
        </span>
      </div>
      <div className="pdt__scroll">
        <table className="pdt__table">
          <thead>
            <tr>
              <th>设备名</th>
              <th>点名</th>
              <th>值</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((p) => {
              const name =
                p.name !== undefined && p.name !== ""
                  ? p.name
                  : bareName(p.key);
              return (
                <tr key={p.key} title={p.key}>
                  <td className="pdt__device">{p.device}</td>
                  <td className="pdt__name">{name}</td>
                  <td className="pdt__value">
                    <span
                      className={`pdt__main pdt__main--${p.state}`}
                    >
                      {p.state === "ok" && p.value !== null
                        ? String(p.value)
                        : p.state === "no_data"
                          ? "暂无数据"
                          : `已停止刷新 ${staleMinutes(p)} 分钟`}
                    </span>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}

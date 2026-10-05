// c4/agent/frontend/src/components/ToolCallCard.tsx
// Tool-call progress card — web.md §3.1.1, §3.1.2.
//
// Shows the tool name + status, result line expanded by default (2026-10-05
// 用户指令：思考步骤默认打开)；args are intentionally not rendered (backend
// always sends args={} and non-technical users should not see protocol-level
// fields by default). 无结果内容的步骤不渲染详情区。

import { useState } from "react";

export type ToolCardStatus = "running" | "done";

export interface ToolCallCardProps {
  name: string;
  status: ToolCardStatus;
  result?: string;
}

export function ToolCallCard({
  name,
  status,
  result,
}: ToolCallCardProps): JSX.Element {
  const [expanded, setExpanded] = useState(true);

  const statusText = status === "running" ? "执行中" : "完成";

  return (
    <div data-testid="tool-card" data-status={status} className="tool-card">
      <button
        type="button"
        className="tool-card__header"
        aria-expanded={expanded}
        onClick={() => setExpanded((v) => !v)}
      >
        <span className="tool-card__name">{name}</span>
        <span className={`tool-card__status tool-card__status--${status}`}>
          {statusText}
        </span>
        <span aria-hidden="true" className="tool-card__toggle">
          {expanded ? "▾" : "▸"}
        </span>
      </button>
      {expanded && result !== undefined && result !== "" && (
        <div data-testid="tool-card-details" className="tool-card__details">
          {result}
        </div>
      )}
    </div>
  );
}

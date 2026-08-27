// c4/agent/frontend/src/components/PhaseBadge.tsx
// Working-phase badge — web.md §3.4.2.
//
// Maps each AgentPhase to a Chinese label + color token. The color tokens are
// CSS classes consumed by the top-level stylesheet (gray/blue/green/orange).
// PHASE_META is exported as plain data so other modules (and tests) can read
// labels without rendering — and so an unknown phase has a clear fallback
// path: we render the raw string instead of crashing (§3.3.6).

import type { AgentPhase, AgentState } from "@frontend/api/state";

export interface PhaseMeta {
  label: string;
  color: "gray" | "blue" | "green" | "orange";
}

export const PHASE_META: Record<AgentPhase, PhaseMeta> = {
  idle: { label: "空闲", color: "gray" },
  collecting: { label: "收集信息中", color: "blue" },
  planning: { label: "生成方案中", color: "blue" },
  confirmed: { label: "已确认", color: "green" },
  executing: { label: "执行中", color: "orange" },
};

export interface PhaseBadgeProps {
  phase: AgentPhase | string;
}

/**
 * Pure rendering component. Unknown phases fall back to a gray badge showing
 * the raw string — defensive against forward-compatible phase additions.
 */
export function PhaseBadge({ phase }: PhaseBadgeProps): JSX.Element {
  const known = (PHASE_META as Record<string, PhaseMeta | undefined>)[phase];
  const meta: PhaseMeta = known ?? { label: String(phase), color: "gray" };

  return (
    <span
      data-testid="phase-badge"
      data-phase={phase}
      data-color={meta.color}
      className={`phase-badge phase-badge--${meta.color}`}
      role="status"
      aria-live="polite"
    >
      {meta.label}
    </span>
  );
}

/** Convenience type re-export so consumers can import from one place. */
export type { AgentState };

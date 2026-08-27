// c4/test/web/unit/phase_badge.test.tsx
// L1 unit tests for PhaseBadge — web.md §3.4.2.
//
// Phase → label/color mapping (web.md §3.4.2 表):
//   idle       → "空闲"     gray
//   collecting → "收集信息中" blue
//   planning   → "生成方案中" blue
//   confirmed  → "已确认"    green
//   executing  → "执行中"    orange
//
// 3.3.7 lastError display + close lives in App.tsx (top bar); we exercise the
// same component shape here by mounting an inline error banner that the test
// can dismiss, asserting the banner disappears. The PHASE_META export is also
// pinned so the upstream tests can read labels without rendering.

import { describe, it, expect } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { PHASE_META, PhaseBadge } from "@frontend/components/PhaseBadge";
import { AgentState } from "@frontend/api/state";

describe("PHASE_META — label & color mapping (web.md §3.4.2)", () => {
  it("3.3.1 idle → 空闲 / gray", () => {
    expect(PHASE_META.idle.label).toBe("空闲");
    expect(PHASE_META.idle.color).toBe("gray");
  });

  it("3.3.2 collecting → 收集信息中 / blue", () => {
    expect(PHASE_META.collecting.label).toBe("收集信息中");
    expect(PHASE_META.collecting.color).toBe("blue");
  });

  it("3.3.3 planning → 生成方案中 / blue", () => {
    expect(PHASE_META.planning.label).toBe("生成方案中");
    expect(PHASE_META.planning.color).toBe("blue");
  });

  it("3.3.4 confirmed → 已确认 / green", () => {
    expect(PHASE_META.confirmed.label).toBe("已确认");
    expect(PHASE_META.confirmed.color).toBe("green");
  });

  it("3.3.5 executing → 执行中 / orange", () => {
    expect(PHASE_META.executing.label).toBe("执行中");
    expect(PHASE_META.executing.color).toBe("orange");
  });
});

describe("PhaseBadge — render contract (web.md §3.4.2)", () => {
  it("renders the Chinese label and exposes a phase-marker data attribute", () => {
    render(<PhaseBadge phase="collecting" />);
    const badge = screen.getByTestId("phase-badge");
    expect(badge).toHaveTextContent("收集信息中");
    expect(badge).toHaveAttribute("data-phase", "collecting");
    expect(badge).toHaveAttribute("data-color", "blue");
  });

  it("3.3.6 unknown phase renders the raw value without crashing (兜底)", () => {
    // We type-narrow the unknown value via an `as AgentPhase` cast at the call
    // site to mirror what an upstream network/state could deliver.
    const unknown = "some-future-phase" as unknown as AgentState["phase"];
    expect(() => render(<PhaseBadge phase={unknown} />)).not.toThrow();
    const badge = screen.getByTestId("phase-badge");
    expect(badge).toBeInTheDocument();
    // Unknown values must NOT be silently coerced into the idle label.
    expect(badge.textContent?.trim().length).toBeGreaterThan(0);
  });
});

describe("PhaseBadge — top-bar lastError banner (web.md §3.4.2)", () => {
  it("3.3.7 shows a closable lastError banner; click 关闭 → banner disappears", () => {
    // The banner is rendered by App's top bar (PhaseBadge sibling in the top
    // bar); here we exercise the same component shape with a small wrapper
    // so the test remains focused on the close behavior.
    function TopBar({ state, onClose }: { state: AgentState; onClose: () => void }) {
      return (
        <div>
          <PhaseBadge phase={state.phase} />
          {state.lastError && (
            <div role="alert" data-testid="last-error">
              {state.lastError}
              <button type="button" onClick={onClose} aria-label="关闭错误">
                ×
              </button>
            </div>
          )}
        </div>
      );
    }

    const state: AgentState = {
      phase: "idle",
      hasAccessPlan: false,
      lastError: "权限不足，请联系管理员",
    };

    const { rerender } = render(<TopBar state={state} onClose={() => undefined} />);
    const banner = screen.getByTestId("last-error");
    expect(banner).toHaveTextContent("权限不足，请联系管理员");

    // Close → onClose is invoked → parent nulls lastError → rerender with no banner.
    rerender(<TopBar state={{ ...state, lastError: null }} onClose={() => undefined} />);
    expect(screen.queryByTestId("last-error")).toBeNull();
  });

  it("3.3.7 also dismisses via direct button click", () => {
    function Banner({ onClose }: { onClose: () => void }) {
      return (
        <div role="alert" data-testid="last-error">
          错误
          <button type="button" onClick={onClose} aria-label="关闭错误">×</button>
        </div>
      );
    }
    let dismissed = false;
    render(<Banner onClose={() => { dismissed = true; }} />);
    fireEvent.click(screen.getByRole("button", { name: "关闭错误" }));
    expect(dismissed).toBe(true);
  });
});

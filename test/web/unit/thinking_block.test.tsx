// c4/test/web/unit/thinking_block.test.tsx
// L1 unit tests for ThinkingBlock — 思考过程折叠块（chat.deepseek.com「已思考」式）。
//
// Covered:
//   无步骤且已结束 → 不渲染
//   进行中 → 标题「思考中…（已用时 N 秒）」且默认展开显示步骤
//   结束有步骤 → 「已思考（用时 N 秒）」默认展开（2026-10-05 用户指令），
//   点击标题可折叠

import { describe, it, expect } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { ThinkingBlock } from "@frontend/components/ThinkingBlock";

const steps = [
  { name: "output_device_info", status: "done" as const, result: "已注册" },
  { name: "registry_write", status: "running" as const },
];

describe("ThinkingBlock", () => {
  it("无步骤且回合已结束 → 不渲染", () => {
    const { container } = render(
      <ThinkingBlock steps={[]} active={false} elapsedSec={3} />,
    );
    expect(container.querySelector('[data-testid=thinking-block]')).toBeNull();
  });

  it("进行中 → 「思考中…（已用时 N 秒）」且默认展开显示步骤", () => {
    render(<ThinkingBlock steps={steps} active={true} elapsedSec={4} />);
    const block = screen.getByTestId("thinking-block");
    expect(block.getAttribute("data-active")).toBe("true");
    expect(screen.getByText("思考中…（已用时 4 秒）")).toBeInTheDocument();
    // 默认展开：步骤可见
    expect(screen.getAllByTestId("tool-card").length).toBe(2);
  });

  it("结束有步骤 → 「已思考（用时 N 秒）」默认展开，点击折叠", () => {
    render(<ThinkingBlock steps={steps} active={false} elapsedSec={7} />);
    const block = screen.getByTestId("thinking-block");
    expect(block.getAttribute("data-active")).toBe("false");
    expect(screen.getByText("已思考（用时 7 秒）")).toBeInTheDocument();
    // 默认展开（2026-10-05 用户指令）：步骤直接可见
    expect(screen.getAllByTestId("tool-card").length).toBe(2);

    // 点击标题 → 折叠
    fireEvent.click(screen.getByText("已思考（用时 7 秒）"));
    expect(screen.queryByTestId("tool-card")).toBeNull();
  });
});

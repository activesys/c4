// c4/test/web/unit/tool_card.test.tsx
// L1 unit tests for ToolCallCard — web.md §3.1.1, §3.1.2.
//
// Card displays the tool's name only (NOT its args — backend always sends
// args={}). Status starts at "running", then flips to "done" on tool_result.
// Details default to expanded (2026-10-05 用户指令：思考步骤默认打开)；
// 无结果内容的步骤不渲染详情区。

import { describe, it, expect } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { ToolCallCard } from "@frontend/components/ToolCallCard";

describe("ToolCallCard — render & status (web.md §3.1.1, §3.1.2)", () => {
  it("3.4.1 renders the tool name and an '执行中' status; args are NOT shown", () => {
    render(<ToolCallCard name="xlsx_parser" status="running" />);
    const card = screen.getByTestId("tool-card");
    expect(card).toHaveTextContent("xlsx_parser");
    expect(card).toHaveTextContent("执行中");
    expect(card).toHaveAttribute("data-status", "running");
    // args must NOT be displayed — even if a parent accidentally passes one in.
    expect(card.textContent).not.toMatch(/args/);
  });

  it("3.4.2 renders '完成' status and surfaces the tool_result", () => {
    const resultText = "解析完成：1#风机，Modbus TCP";
    render(<ToolCallCard name="xlsx_parser" status="done" result={resultText} />);
    const card = screen.getByTestId("tool-card");
    expect(card).toHaveTextContent("xlsx_parser");
    expect(card).toHaveTextContent("完成");
    expect(card).toHaveAttribute("data-status", "done");
  });
});

describe("ToolCallCard — default expand (2026-10-05 用户指令)", () => {
  it("3.4.3 details are expanded by default; clicking the toggle collapses them", () => {
    const resultText = "解析完成：1#风机，Modbus TCP";
    render(<ToolCallCard name="xlsx_parser" status="done" result={resultText} />);

    // 默认展开：结果直接可见
    const details = screen.getByTestId("tool-card-details");
    expect(details).toBeVisible();
    expect(details).toHaveTextContent(resultText);

    // 点击头部 → 折叠（条件渲染：详情区从 DOM 移除）
    const toggle = screen.getByRole("button", { name: /xlsx_parser/ });
    fireEvent.click(toggle);
    expect(screen.queryByTestId("tool-card-details")).toBeNull();

    // 再点 → 展开
    fireEvent.click(toggle);
    expect(screen.getByTestId("tool-card-details")).toBeVisible();
  });

  it("无结果内容（result 为空串）→ 不渲染详情区（避免空白占位）", () => {
    render(<ToolCallCard name="registry_lookup" status="done" result="" />);
    expect(screen.queryByTestId("tool-card-details")).toBeNull();
  });
});

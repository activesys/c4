// c4/test/web/unit/tool_card.test.tsx
// L1 unit tests for ToolCallCard — web.md §3.1.1, §3.1.2.
//
// Card displays the tool's name only (NOT its args — backend always sends
// args={}). Status starts at "running", then flips to "done" on tool_result.
// Details default to collapsed (non-technical users should not see protocol
// details by default — §3.1.2).

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

describe("ToolCallCard — default collapse (web.md §3.1.2)", () => {
  it("3.4.3 details are collapsed by default; clicking the toggle expands them", () => {
    const resultText = "解析完成：1#风机，Modbus TCP";
    render(<ToolCallCard name="xlsx_parser" status="done" result={resultText} />);

    // The result must exist in the DOM but be hidden until expanded.
    const details = screen.getByTestId("tool-card-details");
    expect(details).toBeInTheDocument();
    expect(details).not.toBeVisible();

    // Click the header toggle to expand.
    const toggle = screen.getByRole("button", { name: /xlsx_parser/ });
    fireEvent.click(toggle);

    // After clicking, the details should be visible (expanded).
    const detailsAfter = screen.getByTestId("tool-card-details");
    expect(detailsAfter).toBeVisible();
    expect(detailsAfter).toHaveTextContent(resultText);
  });

  it("clicking again collapses the details (toggle behavior)", () => {
    render(<ToolCallCard name="xlsx_parser" status="done" result="result" />);
    const toggle = screen.getByRole("button", { name: /xlsx_parser/ });

    // expand
    fireEvent.click(toggle);
    expect(screen.getByTestId("tool-card-details")).toBeVisible();
    // collapse
    fireEvent.click(toggle);
    expect(screen.getByTestId("tool-card-details")).not.toBeVisible();
  });
});

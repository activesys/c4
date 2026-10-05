// c4/test/web/unit/site_setup.test.tsx
// L1 unit tests for SiteSetupGate/SiteEditDialog — 场站初始化引导层（不可跳过）
// 与顶栏编辑对话框（2026-10-05 用户指令）。

import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { SiteEditDialog, SiteSetupGate } from "@frontend/components/SiteSetupGate";

function mockFetchOnce(status: number, body: unknown): void {
  globalThis.fetch = vi.fn().mockResolvedValue({
    ok: status < 400,
    status,
    json: async () => body,
  } as unknown as typeof fetch);
}

beforeEach(() => {
  vi.restoreAllMocks();
});

describe("SiteSetupGate — 首次启动引导层", () => {
  it("仅保留大字号主文案；无图标/标题/缩写输入/跳过入口", () => {
    render(<SiteSetupGate onBound={() => {}} />);
    expect(screen.getByText("初次见面，告诉我你在哪里")).toBeInTheDocument();
    expect(screen.getByTestId("site-setup-name")).toBeInTheDocument();
    expect(screen.getByTestId("site-setup-submit")).toBeInTheDocument();
    // 图标与「初始化场站信息」标题已删除（2026-10-05 用户指令）
    expect(screen.queryByText("初始化场站信息")).toBeNull();
    expect(screen.queryByRole("img")).toBeNull();
    // 引导层不收集缩写（后端自动生成）
    expect(screen.queryByTestId("site-setup-abbr")).toBeNull();
    // 不可跳过：不存在「跳过/暂不」类入口
    expect(screen.queryByText(/跳过|暂不/)).toBeNull();
  });

  it("名称占位提示为「例如：华能阿拉善一区」", () => {
    render(<SiteSetupGate onBound={() => {}} />);
    expect(
      screen.getByPlaceholderText("例如：华能阿拉善一区"),
    ).toBeInTheDocument();
  });

  it("名称为空 → 提交按钮禁用（不发起请求）", () => {
    const fetchSpy = vi.fn();
    globalThis.fetch = fetchSpy as unknown as typeof fetch;
    render(<SiteSetupGate onBound={() => {}} />);
    const submit = screen.getByTestId(
      "site-setup-submit",
    ) as HTMLButtonElement;
    expect(submit.disabled).toBe(true);
    fireEvent.click(submit);
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it("填写名称提交 → POST /api/site，成功回调 onBound", async () => {
    mockFetchOnce(200, {
      success: true,
      site: { name: "华能阿拉善", abbr: "hnals" },
    });
    const onBound = vi.fn();
    render(<SiteSetupGate onBound={onBound} />);
    fireEvent.change(screen.getByTestId("site-setup-name"), {
      target: { value: "华能阿拉善" },
    });
    fireEvent.click(screen.getByTestId("site-setup-submit"));
    await waitFor(() => expect(onBound).toHaveBeenCalledTimes(1));
    const call = (globalThis.fetch as ReturnType<typeof vi.fn>).mock
      .calls[0] as [string, RequestInit];
    expect(call[0]).toBe("/api/site");
    expect(JSON.parse(String(call[1]?.body))).toEqual({
      name: "华能阿拉善",
      abbr: "",
    });
  });

  it("后端校验失败 → 展示错误消息，不回调 onBound", async () => {
    mockFetchOnce(400, { success: false, error: "场站名称需为 2~20 个字符（不含空格、逗号、句号）" });
    const onBound = vi.fn();
    render(<SiteSetupGate onBound={onBound} />);
    fireEvent.change(screen.getByTestId("site-setup-name"), {
      target: { value: "长" },
    });
    fireEvent.click(screen.getByTestId("site-setup-submit"));
    expect(
      await screen.findByTestId("site-setup-error"),
    ).toHaveTextContent(/2~20/);
    expect(onBound).not.toHaveBeenCalled();
  });

  it("缩写自动生成失败（422）→ 引导层动态露出缩写输入", async () => {
    const responses = [
      { status: 422, body: { success: false, error: "无法自动生成场站缩写，请手动填写（2~12 位字母/数字）" } },
      { status: 200, body: { success: true, site: { name: "华能阿拉善一区", abbr: "hnalsyq" } } },
    ];
    globalThis.fetch = vi
      .fn()
      .mockImplementation(async () => {
        const r = responses.shift() ?? responses[0];
        return {
          ok: r.status < 400,
          status: r.status,
          json: async () => r.body,
        } as unknown as Response;
      }) as unknown as typeof fetch;
    const onBound = vi.fn();
    render(<SiteSetupGate onBound={onBound} />);
    fireEvent.change(screen.getByTestId("site-setup-name"), {
      target: { value: "华能阿拉善一区" },
    });
    fireEvent.click(screen.getByTestId("site-setup-submit"));
    // 422 → 缩写输入动态出现
    const abbrInput = await screen.findByTestId("site-setup-abbr");
    expect(abbrInput).toBeInTheDocument();
    // 手填缩写后再提交 → 成功
    fireEvent.change(abbrInput, { target: { value: "hnalsyq" } });
    fireEvent.click(screen.getByTestId("site-setup-submit"));
    await waitFor(() => expect(onBound).toHaveBeenCalledTimes(1));
  });
});

describe("SiteEditDialog — 顶栏编辑对话框", () => {
  it("挂载即拉取当前场站并预填；无缩写输入，保存原值回传", async () => {
    const fetchSpy = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({
        success: true,
        site: { name: "华能阿拉善", abbr: "hnals" },
      }),
    } as unknown as Response);
    globalThis.fetch = fetchSpy as unknown as typeof fetch;
    const onSaved = vi.fn();
    render(<SiteEditDialog onSaved={onSaved} onClose={() => {}} />);
    // 主文案与引导层同风格（大字号 lead 由 CSS 承担）；说明文字已删除
    expect(screen.getByText("场站有变？数据不搬家")).toBeInTheDocument();
    expect(
      screen.queryByText(/改名仅影响数据归属判定/),
    ).toBeNull();
    const nameInput = (await screen.findByTestId(
      "site-setup-name",
    )) as HTMLInputElement;
    await waitFor(() => expect(nameInput.value).toBe("华能阿拉善"));
    // 无缩写输入（2026-10-05 用户指令）
    expect(screen.queryByTestId("site-setup-abbr")).toBeNull();
    // 保存：缩写输入框不展示，但原缩写随表单原值回传（不触发重新生成）
    fireEvent.change(nameInput, { target: { value: "华能阿拉善盟" } });
    fireEvent.click(screen.getByTestId("site-setup-submit"));
    await waitFor(() => expect(onSaved).toHaveBeenCalledTimes(1));
    const call = (fetchSpy as ReturnType<typeof vi.fn>).mock
      .calls[1] as [string, RequestInit];
    expect(call[0]).toBe("/api/site");
    expect(JSON.parse(String(call[1]?.body))).toEqual({
      name: "华能阿拉善盟",
      abbr: "hnals",
    });
  });

  it("Esc 关闭触发 onClose", () => {
    const onClose = vi.fn();
    render(<SiteEditDialog onSaved={() => {}} onClose={onClose} />);
    fireEvent.keyDown(window, { key: "Escape" });
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("父组件重渲染（新回调引用）不重复拉取，用户编辑不被冲回", async () => {
    // 真实场景：useAgentState 每秒轮询 → App 重渲染 → onClose/onSaved 每次都是
    // 新引用。旧实现 effect([onClose]) 会反复 fetch → initial 新对象 → 表单
    // 同步 effect 把用户输入覆盖回服务器值（删字即被填回）
    const fetchSpy = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({
        success: true,
        site: { name: "华能阿拉善", abbr: "hnals" },
      }),
    } as unknown as Response);
    globalThis.fetch = fetchSpy as unknown as typeof fetch;
    const view = render(
      <SiteEditDialog onSaved={() => {}} onClose={() => {}} />,
    );
    const nameInput = (await screen.findByTestId(
      "site-setup-name",
    )) as HTMLInputElement;
    await waitFor(() => expect(nameInput.value).toBe("华能阿拉善"));
    expect(fetchSpy).toHaveBeenCalledTimes(1);

    // 用户编辑后，父组件以全新回调引用重渲染多次
    fireEvent.change(nameInput, { target: { value: "华能阿拉善盟" } });
    view.rerender(<SiteEditDialog onSaved={() => {}} onClose={() => {}} />);
    view.rerender(<SiteEditDialog onSaved={() => {}} onClose={() => {}} />);
    expect((screen.getByTestId("site-setup-name") as HTMLInputElement).value).toBe(
      "华能阿拉善盟",
    );
    // 只拉取过一次
    expect(fetchSpy).toHaveBeenCalledTimes(1);
  });
});

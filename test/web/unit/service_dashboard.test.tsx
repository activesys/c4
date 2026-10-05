// c4/test/web/unit/service_dashboard.test.tsx
// L1 unit tests for ServiceDashboard — web.md §3.3.
//
// Covered (§3.5):
//   3.5.1 5 cards render with display_name (title) + service_type (subtitle)
//   3.5.2 分组：writer → 「采集」组，reader → 「转发」组
//   3.5.3 unknown role → 独立分组（标题=原始值），不崩溃（兜底）
//   3.5.4 描述渲染：有协议时取协议摘要
//   3.5.5 503 → show "Agent 启动中，请稍候" + retry button
//   3.5.6 loading → skeleton placeholder until resolved
//   3.5.7 点击卡片 → 详情弹窗（协议/点表字段/接入配置），可关闭
//   3.5.8 图标：引用注册的 icon；未注册 → service_type 派生缩写 + 默认深色徽标
//
// We use msw to mock the GET /api/services response so the test is a true
// "frontend ↔ contract" exercise without a live backend.

import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { setupServer } from "msw/node";
import { ServiceDashboard } from "@frontend/components/ServiceDashboard";
import type { ServiceCatalogEntry } from "@frontend/api/services";

const server = setupServer();

const sampleServices: ServiceCatalogEntry[] = [
  {
    service_type: "c4_modbus_client",
    display_name: "Modbus 数据采集",
    role: "writer",
    protocols: [
      {
        protocol: "Modbus TCP",
        description: "Modbus TCP 协议",
        selection_rules: [
          { condition: "局域网可达", description: "与设备同网段" },
        ],
      },
    ],
    point_fields: [
      { name: "ip", type: "string", description: "设备 IP" },
    ],
    plan_fields: [
      { name: "ip", type: "string", required: true, default: null, description: "设备 IP" },
      { name: "port", type: "number", required: false, default: 502, description: "端口" },
    ],
  },
  {
    service_type: "c4_iec104_client",
    display_name: "IEC104 数据采集",
    role: "writer",
    protocols: [
      { protocol: "IEC104", description: "电力 104 协议", selection_rules: [] },
    ],
    point_fields: [],
    plan_fields: [
      { name: "common_address", type: "number", required: true, default: null, description: "公共地址" },
    ],
  },
  {
    service_type: "c4_asfp2_client",
    display_name: "ASFP2 数据采集",
    role: "writer",
    protocols: [],
    point_fields: [],
    plan_fields: [],
  },
  {
    service_type: "c4_asfp2_server",
    display_name: "ASFP2 服务端",
    role: "reader",
    protocols: [
      { protocol: "ASFP2", description: "内部转发协议", selection_rules: [] },
    ],
    point_fields: [],
    plan_fields: [],
  },
  {
    service_type: "c4_influxdb_client",
    display_name: "InfluxDB 写入",
    role: "reader",
    protocols: [],
    point_fields: [],
    plan_fields: [],
  },
];

beforeEach(() => {
  server.resetHandlers();
  server.listen({ onUnhandledRequest: "error" });
});
afterEach(() => {
  server.resetHandlers();
  server.close();
  vi.restoreAllMocks();
});

describe("ServiceDashboard — render happy-path (web.md §3.3.2)", () => {
  it("3.5.1 renders 5 cards with display_name as title and service_type as subtitle", async () => {
    server.use(
      http.get("/api/services", () =>
        HttpResponse.json({ success: true, services: sampleServices, count: sampleServices.length }),
      ),
    );

    render(<ServiceDashboard />);
    await waitFor(() => {
      expect(screen.getAllByTestId("service-card")).toHaveLength(5);
    });

    for (const svc of sampleServices) {
      expect(screen.getByText(svc.display_name)).toBeInTheDocument();
      expect(screen.getByText(svc.service_type)).toBeInTheDocument();
    }
  });

  it("3.5.2 分组：writer → 「采集」组，reader → 「转发」组", async () => {
    server.use(
      http.get("/api/services", () =>
        HttpResponse.json({ success: true, services: sampleServices, count: sampleServices.length }),
      ),
    );

    render(<ServiceDashboard />);
    await waitFor(() => {
      expect(screen.getAllByTestId("service-card")).toHaveLength(5);
    });

    const groups = screen.getAllByTestId("mcp-group");
    expect(groups).toHaveLength(2);
    // 第一组「采集」收全部 writer（3 项），第二组「转发」收全部 reader（2 项）
    expect(groups[0].textContent).toContain("采集");
    expect(
      groups[0].querySelectorAll('[data-testid="service-card"]'),
    ).toHaveLength(3);
    expect(groups[0].textContent).toContain("Modbus 数据采集");
    expect(groups[1].textContent).toContain("转发");
    expect(
      groups[1].querySelectorAll('[data-testid="service-card"]'),
    ).toHaveLength(2);
    expect(groups[1].textContent).toContain("ASFP2 服务端");
  });

  it("3.5.3 未知角色 → 独立分组（标题=原始值），不崩溃（兜底）", async () => {
    const weird: ServiceCatalogEntry = {
      ...sampleServices[0],
      service_type: "c4_experimental",
      display_name: "Experimental",
      role: "some_future_role",
    };
    server.use(
      http.get("/api/services", () =>
        HttpResponse.json({ success: true, services: [weird], count: 1 }),
      ),
    );

    expect(() => render(<ServiceDashboard />)).not.toThrow();
    await waitFor(() => {
      expect(screen.getByTestId("service-card")).toBeInTheDocument();
    });
    // 未知角色不进「采集/转发」组，而是独立成组，标题渲染原始值。
    const groups = screen.getAllByTestId("mcp-group");
    expect(groups).toHaveLength(1);
    expect(groups[0].textContent).toContain("some_future_role");
  });

  it("3.5.4 描述渲染：有协议时取协议摘要", async () => {
    server.use(
      http.get("/api/services", () =>
        HttpResponse.json({
          success: true,
          services: [sampleServices[0]], // protocols: [{ protocol:"Modbus TCP", ... }]
          count: 1,
        }),
      ),
    );

    render(<ServiceDashboard />);
    await waitFor(() => {
      expect(screen.getByTestId("service-card")).toBeInTheDocument();
    });

    expect(
      screen.getByText("Modbus TCP — Modbus TCP 协议"),
    ).toBeInTheDocument();
  });

  it("3.5.7 点击卡片弹出详情弹窗，可关闭回到列表", async () => {
    server.use(
      http.get("/api/services", () =>
        HttpResponse.json({
          success: true,
          services: [sampleServices[0]], // 含点表字段 + 接入配置
          count: 1,
        }),
      ),
    );

    render(<ServiceDashboard />);
    await waitFor(() => {
      expect(screen.getByTestId("service-card")).toBeInTheDocument();
    });

    // 点击行 → 弹窗出现，展示完整注册信息
    fireEvent.click(screen.getByTestId("service-card"));
    const dialog = screen.getByTestId("mcp-dialog");
    expect(dialog).toHaveAttribute("role", "dialog");
    expect(screen.getByText("通信协议")).toBeInTheDocument();
    expect(screen.getByText("点表字段")).toBeInTheDocument();
    expect(screen.getByText("接入配置")).toBeInTheDocument();
    // 详情里恢复展示必填/可选标签（表头与标签都含「必填」，用 getAllByText）
    expect(screen.getAllByText("必填").length).toBeGreaterThan(0);
    expect(screen.getByText("可选")).toBeInTheDocument();

    // 关闭（×）→ 弹窗消失
    fireEvent.click(screen.getByTestId("mcp-dialog-close"));
    expect(screen.queryByTestId("mcp-dialog")).toBeNull();
    expect(screen.getByTestId("service-card")).toBeInTheDocument();
  });

  it("3.5.8 图标（协议无关架构）：icon 为文件 URL 时 <img> 引用；未注册/加载失败 → 动态生成默认深色徽标", async () => {
    const withIcon: ServiceCatalogEntry = {
      ...sampleServices[0],
      service_type: "c4_asfp2_server",
      icon: "/api/services/icons/c4_asfp2_server.svg",
    };
    const withoutIcon: ServiceCatalogEntry = {
      ...sampleServices[1], // c4_iec104_client，无 icon 字段
    };
    server.use(
      http.get("/api/services", () =>
        HttpResponse.json({
          success: true,
          services: [withIcon, withoutIcon],
          count: 2,
        }),
      ),
    );

    render(<ServiceDashboard />);
    await waitFor(() => {
      expect(screen.getAllByTestId("service-card")).toHaveLength(2);
    });

    // 注册提供的图标：以 <img> 引用 Agent 解析出的 URL（alt="" → presentation 角色，按类名查）
    const img = document.querySelector(
      "img.mcp-icon--img",
    ) as HTMLImageElement;
    expect(img).not.toBeNull();
    expect(img.getAttribute("src")).toBe("/api/services/icons/c4_asfp2_server.svg");

    // 未注册图标 → 动态生成默认徽标（service_type 派生缩写 iec104 → IE，
    // 深色底按 service_type 的 FNV-1a 哈希从调色板选取）
    const badges = screen.getAllByTestId("mcp-icon");
    expect(badges).toHaveLength(1);
    expect(badges[0].textContent).toBe("IE");
    const paletteRgb = [
      "rgb(61, 67, 81)",  // #3d4351
      "rgb(49, 67, 107)", // #31436b
      "rgb(74, 58, 99)",  // #4a3a63
      "rgb(31, 78, 95)",  // #1f4e5f
      "rgb(90, 58, 58)",  // #5a3a3a
      "rgb(63, 90, 54)",  // #3f5a36
    ];
    expect(paletteRgb).toContain(badges[0].style.background);
    // c4_iec104_client 的 FNV-1a 哈希稳定落在 #3f5a36
    expect(badges[0].style.background).toBe("rgb(63, 90, 54)");

    // 加载失败（onError）→ 回退动态生成的默认徽标（asfp2_server → AS）
    fireEvent.error(img);
    const fallback = screen.getAllByTestId("mcp-icon");
    expect(fallback).toHaveLength(2);
    expect(fallback[0].textContent).toBe("AS");
  });
});

describe("ServiceDashboard — error & loading (web.md §3.3.2)", () => {
  it("3.5.5 503 shows 'Agent 启动中，请稍候' and a retry button; clicking retry re-fetches", async () => {
    let requestCount = 0;
    server.use(
      http.get("/api/services", () => {
        requestCount++;
        if (requestCount === 1) {
          return HttpResponse.json(
            { success: false, error: "MCP Service Registry 尚未加载" },
            { status: 503 },
          );
        }
        return HttpResponse.json({
          success: true,
          services: [sampleServices[0]],
          count: 1,
        });
      }),
    );

    render(<ServiceDashboard />);
    // Error banner appears with the friendly message + retry button.
    await waitFor(() => {
      expect(screen.getByTestId("services-error")).toBeInTheDocument();
    });
    expect(screen.getByTestId("services-error")).toHaveTextContent("Agent 启动中，请稍候");
    const retryBtn = screen.getByTestId("services-retry");
    fireEvent.click(retryBtn);

    await waitFor(() => {
      expect(screen.getByTestId("service-card")).toBeInTheDocument();
    });
    expect(requestCount).toBe(2);
  });

  it("3.5.6 shows a skeleton placeholder while the request is pending", async () => {
    // We delay the response indefinitely and assert the skeleton is present
    // before resolving.
    let resolveFn: ((v: Response) => void) | null = null;
    const pending = new Promise<Response>((resolve) => {
      resolveFn = resolve;
    });

    server.use(
      http.get("/api/services", async () => {
        return pending;
      }),
    );

    render(<ServiceDashboard />);
    // The skeleton element must be visible during the pending request.
    expect(screen.getByTestId("services-skeleton")).toBeInTheDocument();

    // Now resolve with real data and verify the skeleton is replaced.
    resolveFn!(
      HttpResponse.json({ success: true, services: sampleServices, count: sampleServices.length }),
    );
    await waitFor(() => {
      expect(screen.queryByTestId("services-skeleton")).toBeNull();
      expect(screen.getAllByTestId("service-card")).toHaveLength(5);
    });
  });
});

// c4/test/web/unit/service_dashboard.test.tsx
// L1 unit tests for ServiceDashboard — web.md §3.3.
//
// Covered (§3.5):
//   3.5.1 5 cards render with display_name (title) + service_type (subtitle)
//   3.5.2 role badges: writer → 采集, reader → 转发
//   3.5.3 unknown role → render the raw value without crashing (兜底)
//   3.5.4 plan_fields required → "必填", optional → "可选"
//   3.5.5 503 → show "Agent 启动中，请稍候" + retry button
//   3.5.6 loading → skeleton placeholder until resolved
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

  it("3.5.2 role badges: writer → 采集, reader → 转发", async () => {
    server.use(
      http.get("/api/services", () =>
        HttpResponse.json({ success: true, services: sampleServices, count: sampleServices.length }),
      ),
    );

    render(<ServiceDashboard />);
    await waitFor(() => {
      expect(screen.getAllByTestId("service-card")).toHaveLength(5);
    });

    // writer cards → 采集
    const collectBadges = screen.getAllByTestId("role-badge-采集");
    expect(collectBadges.length).toBe(3);
    // reader cards → 转发
    const forwardBadges = screen.getAllByTestId("role-badge-转发");
    expect(forwardBadges.length).toBe(2);
  });

  it("3.5.3 unknown role renders the raw value without crashing (兜底)", async () => {
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
    // The raw role value is rendered verbatim.
    expect(screen.getByTestId("role-badge-raw")).toHaveTextContent("some_future_role");
  });

  it("3.5.4 plan_fields: required=true → 必填, required=false → 可选", async () => {
    server.use(
      http.get("/api/services", () =>
        HttpResponse.json({
          success: true,
          services: [sampleServices[0]], // has one required, one optional
          count: 1,
        }),
      ),
    );

    render(<ServiceDashboard />);
    await waitFor(() => {
      expect(screen.getByTestId("service-card")).toBeInTheDocument();
    });

    expect(screen.getByText("必填")).toBeInTheDocument();
    expect(screen.getByText("可选")).toBeInTheDocument();
    // `ip` and `port` also appear in point_fields; getAllByText avoids ambiguity.
    expect(screen.getAllByText("ip").length).toBeGreaterThan(0);
    expect(screen.getAllByText("port").length).toBeGreaterThan(0);
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

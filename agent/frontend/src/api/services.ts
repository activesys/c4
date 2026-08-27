// c4/agent/frontend/src/api/services.ts
// GET /api/services — web.md §3.3.1
//
// 200: { success: true, services: ServiceCatalogEntry[], count: number }
// 503: { success: false, error: string }   (registry not loaded yet)

export type ServiceRole = "writer" | "reader" | string;

export interface ProtocolSelectionRule {
  condition: string;
  description: string;
}

export interface ProtocolSummary {
  protocol: string;
  description: string;
  selection_rules: ProtocolSelectionRule[];
}

export interface PointField {
  name: string;
  type: string;
  description: string;
}

export interface PlanField {
  name: string;
  type: string;
  required: boolean;
  default: unknown;
  description: string;
}

export interface ServiceCatalogEntry {
  service_type: string;
  display_name: string;
  role: ServiceRole;
  protocols: ProtocolSummary[];
  point_fields: PointField[];
  plan_fields: PlanField[];
}

interface ServicesResponseOk {
  success: true;
  services: ServiceCatalogEntry[];
  count: number;
}
interface ServicesResponseErr {
  success: false;
  error: string;
}
type ServicesResponse = ServicesResponseOk | ServicesResponseErr;

export async function fetchServices(): Promise<ServiceCatalogEntry[]> {
  const res = await fetch("/api/services", { method: "GET" });
  const body = (await res.json()) as ServicesResponse;
  if (!res.ok || body.success === false) {
    const msg =
      "success" in body && body.success === false
        ? body.error
        : `服务目录查询失败: HTTP ${res.status}`;
    throw new Error(msg);
  }
  return body.services;
}

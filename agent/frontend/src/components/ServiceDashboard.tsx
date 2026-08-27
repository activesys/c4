// c4/agent/frontend/src/components/ServiceDashboard.tsx
// MCP service catalog — web.md §3.3.
//
// Fetches GET /api/services on mount and renders a card per service. Shows a
// skeleton placeholder while loading, a friendly retry banner on 503
// (registry not yet loaded), and individual cards once the data arrives.
//
// role mapping (§3.3.2):
//   "writer" → "采集"
//   "reader" → "转发"
//   any other value → render the raw string verbatim (§3.5.3 兜底)

import { useCallback, useEffect, useState } from "react";
import {
  fetchServices,
  type ServiceCatalogEntry,
} from "@frontend/api/services";

export function ServiceDashboard(): JSX.Element {
  const [services, setServices] = useState<ServiceCatalogEntry[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const list = await fetchServices();
      setServices(list);
    } catch (err) {
      // The backend uses 503 for "registry not loaded yet" — surface as a
      // user-friendly retry banner rather than a crash.
      setError("Agent 启动中，请稍候");
      // Keep the raw error in dev for diagnostics.
      if (err instanceof Error && err.message) {
        // eslint-disable-next-line no-console
        console.warn("[ServiceDashboard] fetch failed:", err.message);
      }
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  if (loading) {
    return (
      <div data-testid="services-skeleton" className="services-skeleton" role="status" aria-busy="true">
        <div className="services-skeleton__card" />
        <div className="services-skeleton__card" />
        <div className="services-skeleton__card" />
      </div>
    );
  }

  if (error) {
    return (
      <div data-testid="services-error" className="services-error" role="alert">
        <span>{error}</span>
        <button
          type="button"
          data-testid="services-retry"
          onClick={() => void load()}
        >
          重试
        </button>
      </div>
    );
  }

  return (
    <div className="service-dashboard" data-testid="service-dashboard">
      {services?.map((svc) => (
        <ServiceCard key={svc.service_type} service={svc} />
      ))}
    </div>
  );
}

function ServiceCard({ service }: { service: ServiceCatalogEntry }): JSX.Element {
  return (
    <article data-testid="service-card" className="service-card">
      <header className="service-card__header">
        <h3 className="service-card__title">{service.display_name}</h3>
        <RoleBadge role={service.role} />
      </header>
      <p className="service-card__subtitle">{service.service_type}</p>

      {service.protocols.length > 0 && (
        <section className="service-card__section">
          <h4>支持协议</h4>
          <ul>
            {service.protocols.map((p) => (
              <li key={p.protocol}>
                <strong>{p.protocol}</strong>
                {p.description ? ` — ${p.description}` : null}
              </li>
            ))}
          </ul>
        </section>
      )}

      {service.point_fields.length > 0 && (
        <section className="service-card__section">
          <h4>点表字段</h4>
          <ul>
            {service.point_fields.map((f) => (
              <li key={f.name}>
                <code>{f.name}</code> ({f.type}) — {f.description}
              </li>
            ))}
          </ul>
        </section>
      )}

      {service.plan_fields.length > 0 && (
        <section className="service-card__section">
          <h4>接入配置</h4>
          <ul>
            {service.plan_fields.map((f) => (
              <li key={f.name}>
                <code>{f.name}</code> ({f.type})
                <span className={f.required ? "tag tag--required" : "tag tag--optional"}>
                  {f.required ? "必填" : "可选"}
                </span>
                {f.description ? ` — ${f.description}` : null}
              </li>
            ))}
          </ul>
        </section>
      )}
    </article>
  );
}

function RoleBadge({ role }: { role: string }): JSX.Element {
  if (role === "writer") {
    return (
      <span data-testid="role-badge-采集" className="role-badge role-badge--writer">
        采集
      </span>
    );
  }
  if (role === "reader") {
    return (
      <span data-testid="role-badge-转发" className="role-badge role-badge--reader">
        转发
      </span>
    );
  }
  // Unknown role — surface the raw value so the user can still read it.
  return (
    <span data-testid="role-badge-raw" className="role-badge role-badge--unknown">
      {role}
    </span>
  );
}

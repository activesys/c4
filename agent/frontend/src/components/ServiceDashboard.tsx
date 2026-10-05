// c4/agent/frontend/src/components/ServiceDashboard.tsx
// MCP 目录 — web.md §3.3。
//
// 展示样式参考 ZCode 子智能体设置页：按角色分组（采集 / 转发 / 其他注册角色），
// 每组标题带「N 项」计数，组内每个 MCP 一行——图标 + 名称 + service_type
// 副标题 + 描述。点击任意一行弹出详情弹窗，展示注册表完整信息
// （通信协议/选择规则、点表字段、接入配置）。
//
// 图标（协议无关架构）：icon 为注册 JSON 提供的图标文件路径（Agent 已解析为
// URL，前端以 <img> 引用）；未注册或加载失败时前端动态生成默认徽标——
// 深色圆角矩形 + 浅色英文缩写（从 service_type 派生）。
//
// role mapping (§3.3.2)：
//   "writer" → 分组「采集」
//   "reader" → 分组「转发」
//   any other value → 独立分组（标题=原始值）(§3.5.3 兜底)

import { useCallback, useEffect, useRef, useState } from "react";
import {
  fetchServices,
  type ServiceCatalogEntry,
} from "@frontend/api/services";

interface ServiceGroup {
  /** 分组标题：采集 / 转发 / 未知角色的原始值 */
  title: string;
  items: ServiceCatalogEntry[];
}

function groupServices(services: ServiceCatalogEntry[]): ServiceGroup[] {
  const groups: ServiceGroup[] = [
    { title: "采集", items: [] },
    { title: "转发", items: [] },
  ];
  const rawGroups = new Map<string, ServiceGroup>();
  for (const svc of services) {
    if (svc.role === "writer") {
      groups[0].items.push(svc);
    } else if (svc.role === "reader") {
      groups[1].items.push(svc);
    } else {
      let g = rawGroups.get(svc.role);
      if (!g) {
        g = { title: svc.role, items: [] };
        rawGroups.set(svc.role, g);
      }
      g.items.push(svc);
    }
  }
  return [...groups.filter((g) => g.items.length > 0), ...rawGroups.values()];
}

/** 描述：优先取各协议摘要；无协议时按角色给一句话说明。 */
function describeService(svc: ServiceCatalogEntry): string {
  if (svc.protocols.length > 0) {
    return svc.protocols
      .map((p) => `${p.protocol} — ${p.description}`)
      .join("；");
  }
  if (svc.role === "writer") {
    return "从现场设备采集数据，写入共享内存供下游使用";
  }
  if (svc.role === "reader") {
    return "从共享内存读取数据，转发到下游系统";
  }
  return "已注册的 MCP 服务";
}

export function ServiceDashboard(): JSX.Element {
  const [services, setServices] = useState<ServiceCatalogEntry[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  // 当前弹出详情的 MCP（null = 详情弹窗关闭）
  const [selected, setSelected] = useState<ServiceCatalogEntry | null>(null);

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

  const groups = groupServices(services ?? []);
  return (
    <div className="service-dashboard" data-testid="service-dashboard">
      {groups.map((group) => (
        <section key={group.title} className="mcp-group" data-testid="mcp-group">
          <h3 className="mcp-group__title">
            {group.title}
            <span className="mcp-group__count">{group.items.length} 项</span>
          </h3>
          <div className="mcp-group__list">
            {group.items.map((svc) => (
              <ServiceCard
                key={svc.service_type}
                service={svc}
                onOpen={() => setSelected(svc)}
              />
            ))}
          </div>
        </section>
      ))}
      {selected && (
        <McpDetailDialog service={selected} onClose={() => setSelected(null)} />
      )}
    </div>
  );
}

function ServiceCard({
  service,
  onOpen,
}: {
  service: ServiceCatalogEntry;
  onOpen: () => void;
}): JSX.Element {
  return (
    <button
      type="button"
      data-testid="service-card"
      className="mcp-card"
      onClick={onOpen}
      aria-haspopup="dialog"
    >
      <McpIcon
        iconUrl={service.icon}
        fallbackLabel={deriveAbbr(service.service_type)}
        fallbackColor={darkColorFor(service.service_type)}
      />
      <div className="mcp-card__body">
        <div className="mcp-card__head">
          <span className="mcp-card__name">{service.display_name}</span>
        </div>
        <p className="mcp-card__type">{service.service_type}</p>
        <p className="mcp-card__desc">{describeService(service)}</p>
      </div>
    </button>
  );
}

/** 行图标（协议无关架构）：引用注册提供的图标文件 URL（<img>）；
 *  未注册（iconUrl 为空）或加载失败（onError）时，动态生成默认徽标——
 *  随机深色圆角矩形（按 service_type 哈希取色）+ 浅色英文缩写。 */
function McpIcon({
  iconUrl,
  fallbackLabel,
  fallbackColor,
}: {
  iconUrl?: string;
  fallbackLabel: string;
  fallbackColor: string;
}): JSX.Element {
  const [broken, setBroken] = useState(false);

  // 图标 URL 变化（切换服务/重新注册）时重置加载失败状态
  useEffect(() => {
    setBroken(false);
  }, [iconUrl]);

  if (iconUrl && !broken) {
    return (
      <img
        src={iconUrl}
        alt=""
        aria-hidden="true"
        className="mcp-icon mcp-icon--img"
        onError={() => setBroken(true)}
      />
    );
  }
  return (
    <span
      className="mcp-icon mcp-icon--proto"
      style={{ background: fallbackColor }}
      data-testid="mcp-icon"
      aria-hidden="true"
    >
      {fallbackLabel}
    </span>
  );
}

const ROLE_LABEL: Record<string, string> = {
  writer: "采集",
  reader: "转发",
};

/** 默认徽标（未注册图标时前端动态生成）：随机深色圆角矩形 + 浅色英文缩写。
 *  颜色按 service_type 哈希从深色调色板选取——同一服务稳定不变，不同服务呈现差异。 */
const DARK_ICON_PALETTE = [
  "#3d4351", // 石板灰
  "#31436b", // 深蓝
  "#4a3a63", // 深紫
  "#1f4e5f", // 深青
  "#5a3a3a", // 深棕红
  "#3f5a36", // 深橄榄
];

function darkColorFor(serviceType: string): string {
  // FNV-1a：比乘法哈希分布更均匀，避免近似的服务名撞色
  let hash = 0x811c9dc5;
  for (let i = 0; i < serviceType.length; i++) {
    hash ^= serviceType.charCodeAt(i);
    hash = Math.imul(hash, 0x01000193) >>> 0;
  }
  return DARK_ICON_PALETTE[hash % DARK_ICON_PALETTE.length];
}

/** 从 service_type 派生英文缩写（去 c4_ 前缀后取前两个字母，如 asfp2→AS） */
function deriveAbbr(serviceType: string): string {
  const letters = serviceType.replace(/^c4_/, "").replace(/[^a-z]/gi, "");
  return letters.toUpperCase().slice(0, 2) || "M";
}

function formatDefault(v: unknown): string {
  if (v === null || v === undefined) return "—";
  if (typeof v === "string") return v;
  try {
    return JSON.stringify(v);
  } catch {
    return String(v);
  }
}

/** MCP 详情弹窗：展示注册表中的完整信息（协议/点表字段/接入配置）。
 *  关闭方式：右上 ×、点击遮罩、Esc。打开时锁定页面滚动。 */
function McpDetailDialog({
  service,
  onClose,
}: {
  service: ServiceCatalogEntry;
  onClose: () => void;
}): JSX.Element {
  const closeRef = useRef<HTMLButtonElement | null>(null);

  useEffect(() => {
    closeRef.current?.focus();
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    document.addEventListener("keydown", onKey);
    const prevOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.removeEventListener("keydown", onKey);
      document.body.style.overflow = prevOverflow;
    };
  }, [onClose]);

  return (
    <div className="mcp-backdrop" onClick={onClose}>
      <div
        data-testid="mcp-dialog"
        role="dialog"
        aria-modal="true"
        aria-label={`${service.display_name} 详情`}
        className="mcp-dialog"
        onClick={(e) => e.stopPropagation()}
      >
        <header className="mcp-dialog__header">
          <div className="mcp-dialog__heading">
            <h3 className="mcp-dialog__title">{service.display_name}</h3>
            <p className="mcp-dialog__type">{service.service_type}</p>
          </div>
          <span className="mcp-dialog__role">
            {ROLE_LABEL[service.role] ?? service.role}
          </span>
          <button
            ref={closeRef}
            type="button"
            data-testid="mcp-dialog-close"
            className="mcp-dialog__close"
            aria-label="关闭详情"
            onClick={onClose}
          >
            ×
          </button>
        </header>

        <div className="mcp-dialog__body">
          {service.protocols.length > 0 && (
            <section className="mcp-dialog__section">
              <h4>通信协议</h4>
              {service.protocols.map((p) => (
                <div key={p.protocol} className="mcp-dialog__proto">
                  <p className="mcp-dialog__proto-name">{p.protocol}</p>
                  <p className="mcp-dialog__proto-desc">{p.description}</p>
                  {p.selection_rules.length > 0 && (
                    <ul className="mcp-dialog__rules">
                      {p.selection_rules.map((r) => (
                        <li key={r.condition}>
                          当 {r.condition}：{r.description}
                        </li>
                      ))}
                    </ul>
                  )}
                </div>
              ))}
            </section>
          )}

          {service.point_fields.length > 0 && (
            <section className="mcp-dialog__section">
              <h4>点表字段</h4>
              <table className="mcp-dialog__table">
                <thead>
                  <tr>
                    <th>名称</th>
                    <th>类型</th>
                    <th>说明</th>
                  </tr>
                </thead>
                <tbody>
                  {service.point_fields.map((f) => (
                    <tr key={f.name}>
                      <td><code>{f.name}</code></td>
                      <td>{f.type}</td>
                      <td>{f.description}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </section>
          )}

          {service.plan_fields.length > 0 && (
            <section className="mcp-dialog__section">
              <h4>接入配置</h4>
              <table className="mcp-dialog__table">
                <thead>
                  <tr>
                    <th>名称</th>
                    <th>类型</th>
                    <th>必填</th>
                    <th>默认值</th>
                    <th>说明</th>
                  </tr>
                </thead>
                <tbody>
                  {service.plan_fields.map((f) => (
                    <tr key={f.name}>
                      <td><code>{f.name}</code></td>
                      <td>{f.type}</td>
                      <td>
                        <span
                          className={
                            f.required
                              ? "mcp-dialog__req mcp-dialog__req--yes"
                              : "mcp-dialog__req"
                          }
                        >
                          {f.required ? "必填" : "可选"}
                        </span>
                      </td>
                      <td>{formatDefault(f.default)}</td>
                      <td>{f.description}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </section>
          )}
        </div>
      </div>
    </div>
  );
}

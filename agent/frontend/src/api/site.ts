// c4/agent/frontend/src/api/site.ts
// GET/POST /api/site — 场站初始化向导与顶栏编辑（web.md §3.4 扩展）。

export interface SiteConfig {
  name: string;
  abbr: string;
}

interface SiteOk {
  success: true;
  site: SiteConfig | null;
}
interface SiteBoundOk {
  success: true;
  site: SiteConfig;
}
interface SiteErr {
  success: false;
  error: string;
}
type SiteResponse = SiteOk | SiteErr;
type SiteBindResponse = SiteBoundOk | SiteErr;

async function parse<T extends SiteResponse>(res: Response): Promise<T> {
  return (await res.json()) as T;
}

/** 当前绑定场站；未绑定返回 null */
export async function fetchSite(): Promise<SiteConfig | null> {
  const res = await fetch("/api/site", { method: "GET" });
  const body = await parse(res);
  if (!res.ok || body.success === false) {
    const msg = body.success === false ? body.error : `HTTP ${res.status}`;
    throw new Error(`场站信息读取失败: ${msg}`);
  }
  return body.site;
}

/** 绑定场站（初始化向导/顶栏编辑共用）；校验或写入失败抛出后端错误消息 */
export async function bindSite(
  name: string,
  abbr: string,
): Promise<SiteConfig> {
  const res = await fetch("/api/site", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name, abbr }),
  });
  const body = await parse<SiteBindResponse>(res);
  if (!res.ok || body.success === false) {
    const msg = body.success === false ? body.error : `HTTP ${res.status}`;
    throw new Error(msg);
  }
  return body.site;
}

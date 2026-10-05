// c4/agent/src/site_config.ts
// 场站配置读写（agent.json site 字段，§3.2.1.3a 权威配置）。
// orchestrator（对话内绑定 persist_site）与 /api/site 路由（初始化向导/顶栏
// 编辑）共用同一实现——读改写逻辑单处维护，避免双实现漂移。

import { readFileSync, writeFileSync } from "node:fs";

export interface SiteInfo {
    name: string;
    abbr: string;
}

/** 场站名称口径与对话内绑定一致：2~20 个非空白/非分隔字符 */
const SITE_NAME_RE = /^[^\s，,。]{2,20}$/;

/** 缩写用作点 key/写入标识（如 InfluxDB measurement）：2~12 位字母数字 */
const SITE_ABBR_RE = /^[a-z0-9]{2,12}$/i;

/** 校验场站名称；合法返回 null，否则返回用户可读错误 */
export function validate_site_name(name: string): string | null {
    if (!SITE_NAME_RE.test(name.trim())) {
        return "场站名称需为 2~20 个字符（不含空格、逗号、句号）";
    }
    return null;
}

/** 校验场站缩写；合法返回 null，否则返回用户可读错误 */
export function validate_site_abbr(abbr: string): string | null {
    if (!SITE_ABBR_RE.test(abbr.trim())) {
        return "场站缩写需为 2~12 位字母或数字";
    }
    return null;
}

/**
 * 名称派生缩写的确定性兜底：取首个 2~12 位 ASCII 字母数字序列（小写）。
 * 纯中文名称派生不出 → 返回 ""（调用方再走 LLM 生成或要求手填）。
 */
export function derive_abbr_from_name(name: string): string {
    const runs = name.trim().match(/[a-zA-Z0-9]{2,12}/g);
    return runs === null ? "" : runs[0].toLowerCase();
}

/** 读取 agent.json 的 site 字段；未绑定/文件不可读/字段残缺 → null */
export function read_site_config(agentConfigPath: string): SiteInfo | null {
    try {
        const obj = JSON.parse(
            readFileSync(agentConfigPath, "utf-8"),
        ) as Record<string, unknown>;
        const site = obj["site"] as Partial<SiteInfo> | undefined;
        if (
            site !== undefined &&
            typeof site["name"] === "string" &&
            typeof site["abbr"] === "string" &&
            site["name"] !== ""
        ) {
            return { name: site["name"], abbr: site["abbr"] };
        }
        return null;
    } catch {
        return null;
    }
}

/** 读改写 agent.json 的 site 字段（保留其余字段）；写入失败返回 false（不抛出） */
export function write_site_config(
    agentConfigPath: string,
    site: SiteInfo,
): boolean {
    try {
        const obj = JSON.parse(
            readFileSync(agentConfigPath, "utf-8"),
        ) as Record<string, unknown>;
        obj["site"] = site;
        writeFileSync(agentConfigPath, JSON.stringify(obj, null, 4) + "\n");
        return true;
    } catch {
        return false;
    }
}

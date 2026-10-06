// c4/agent/src/server/routes/site.ts — GET/POST /api/site
// 场站初始化向导与顶栏编辑的数据源（2026-10-05 用户指令：首次启动须由用户
// 提供场站信息，引导层不可跳过）。
//   GET  → 当前绑定（未绑定为 null）
//   POST → 校验 + 落盘 agent.json + 状态推送（顶栏实时更新）
// 缩写缺省时依次尝试：注入的 LLM 生成回调 → 名称 ASCII 派生；两者皆败 →
// 422 请用户手填（LLM 只影响便利性，不阻塞初始化主流程）。

import { Router, type Request, type Response } from "express";
import {
    derive_abbr_from_name,
    read_site_config,
    validate_site_abbr,
    validate_site_name,
    write_site_config,
    type SiteInfo,
} from "../../site_config.js";
import type { AgentStateWriter } from "../types.js";

export interface SiteRouterOptions {
    /** agent.json 权威配置路径（§3.2.1.3a） */
    agentConfigPath: string;
    /** 状态写入器：绑定成功后推送 siteName（顶栏 1s 轮询内生效） */
    stateWriter: AgentStateWriter;
    /** LLM 缩写生成回调（index.ts 注入，内部自控超时；失败/缺省走派生兜底） */
    generateSiteAbbr?: (name: string) => Promise<string>;
    /** 场站重绑定回调（2026-10-06 用户指令：修改后立即生效无需重启）——写入成功后
     *  回灌运行中编排器（绑定基准与全部活草稿），归属判定立即使用新场站 */
    rebindSite?: (site: SiteInfo) => void;
}

export function createSiteRouter(options: SiteRouterOptions): Router {
    const router = Router();

    router.get("/", (_req: Request, res: Response) => {
        res.status(200).json({
            success: true,
            site: read_site_config(options.agentConfigPath),
        });
    });

    router.post("/", async (req: Request, res: Response) => {
        const body = (req.body ?? {}) as Record<string, unknown>;
        const name = typeof body["name"] === "string" ? body["name"].trim() : "";
        const abbrRaw =
            typeof body["abbr"] === "string" ? body["abbr"].trim() : "";

        const nameErr = validate_site_name(name);
        if (nameErr !== null) {
            res.status(400).json({ success: false, error: nameErr });
            return;
        }
        if (abbrRaw !== "") {
            const abbrErr = validate_site_abbr(abbrRaw);
            if (abbrErr !== null) {
                res.status(400).json({ success: false, error: abbrErr });
                return;
            }
        }

        let abbr = abbrRaw.toLowerCase();
        if (abbr === "") {
            if (options.generateSiteAbbr !== undefined) {
                try {
                    const generated = await options.generateSiteAbbr(name);
                    if (generated !== "") abbr = generated;
                } catch {
                    // LLM 失败 → 走确定性派生兜底
                }
            }
            if (abbr === "") abbr = derive_abbr_from_name(name);
            if (abbr === "") {
                res.status(422).json({
                    success: false,
                    error: "无法自动生成场站缩写，请手动填写（2~12 位字母/数字）",
                });
                return;
            }
        }

        const site: SiteInfo = { name, abbr };
        if (!write_site_config(options.agentConfigPath, site)) {
            res.status(500).json({
                success: false,
                error: "场站配置写入失败（agent.json 不可写）",
            });
            return;
        }
        options.stateWriter.setSiteName(site.name);
        options.rebindSite?.(site); // 回灌运行中编排器（2026-10-06 用户指令：即时生效）
        res.status(200).json({ success: true, site });
    });

    return router;
}

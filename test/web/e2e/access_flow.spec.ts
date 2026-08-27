// c4/test/web/e2e/access_flow.spec.ts
// L3 端到端测试（Playwright，真实浏览器 + 真实后端）— README §5.1。
//
// 三个场景共享一个后端实例（Agent 跨轮状态在内存中，web.md §3.1.2）：
//   5.1.1 上传 + 对话接入 + 确认执行 — 确认按钮为关键词驱动（累积文本匹配
//         「是否确认/确认执行/请确认」），点击后是普通 POST，无 interrupt/resume
//   5.1.2 服务目录浏览 — 卡片列表渲染，无 503
//   5.1.3 顶栏工作阶段徽标 — 存在、合法且随对话更新（允许 1s 轮询滞后）
//
// LLM 驱动部分以 DEEPSEEK_API_KEY 门控（README §1.4：无 key 时 skip）。

import { expect, test, type Page } from "@playwright/test";
import { fileURLToPath } from "node:url";

const fixturesDir = fileURLToPath(new URL("./fixtures", import.meta.url));
const TURBINE1_CSV = `${fixturesDir}/1#风机点表.csv`;
const TURBINE2_CSV = `${fixturesDir}/2#风机点表.csv`;

const NO_LLM_KEY = "DEEPSEEK_API_KEY 未设置 — 跳过 LLM 驱动的 E2E 场景";
const VALID_PHASES = ["idle", "collecting", "planning", "confirmed", "executing"];
// 后端执行成功的流式文本（super_worker：接入方案已执行，配置已写入。 / 服务已重启: ...）
const CONFIG_WRITTEN = /接入方案已执行|配置已写入|接入完成|服务已重启/;

const input = (page: Page) => page.getByTestId("chat-input");
const confirmButtons = (page: Page) => page.getByTestId("confirm-buttons");

// ── Helpers ─────────────────────────────────────────────

async function gotoChat(page: Page): Promise<void> {
    await page.goto("/");
    await expect(page.getByTestId("chat-view")).toBeVisible();
}

/** 等待当前对话流结束：输入框重新可用（useChatStream.status 回到 idle）。 */
async function waitForIdle(page: Page, timeout = 300_000): Promise<void> {
    await expect(input(page)).toBeEnabled({ timeout });
}

/** 发送一条对话消息，并等待该轮流结束（输入框重新可用）。 */
async function sendMessage(page: Page, text: string): Promise<void> {
    await waitForIdle(page);
    await input(page).fill(text);
    await page.getByRole("button", { name: "发送", exact: true }).click();
    await waitForIdle(page);
}

/**
 * 上传点表文件并等待上传 SSE 流结束 + 前端消息转发（ChatView.handleFileUpload
 * 会把解析结果逐段转发为对话消息）全部落定。
 */
async function uploadFixture(page: Page, csvPath: string): Promise<void> {
    const uploadDone = page.waitForResponse(
        (res) => res.url().includes("/api/upload") && res.request().method() === "POST",
        { timeout: 300_000 },
    );
    await page.getByTestId("file-upload-input").setInputFiles(csvPath);
    await uploadDone;
    await waitForIdle(page);
    await page.waitForTimeout(3000);
}

/**
 * 等待「确认/取消」按钮出现。按钮由累积文本匹配确认句式触发（web.md §3.1.3）；
 * 若当前轮未出现，补发「生成接入方案」触发方案轮（web.md §4.1 轮 2）。
 */
async function ensureConfirmButtons(page: Page): Promise<void> {
    const buttons = confirmButtons(page);
    if (await buttons.isVisible().catch(() => false)) {
        return;
    }
    for (let attempt = 0; attempt < 3; attempt += 1) {
        await sendMessage(page, "请生成接入方案，并转发到中心侧");
        try {
            await buttons.waitFor({ state: "visible", timeout: 240_000 });
            return;
        } catch {
            // 累积文本未匹配到确认句式 — 再试一轮
        }
    }
    throw new Error("累积文本未匹配到确认句式，确认按钮未出现");
}

// ── 5.1.1 上传 + 对话接入 + 确认执行 ─────────────────────

test("5.1.1 上传点表 + 对话接入 + 确认执行（关键词驱动确认，无 interrupt/resume）", async ({
    page,
}) => {
    test.skip(!process.env.DEEPSEEK_API_KEY, NO_LLM_KEY);
    await gotoChat(page);

    // ── 轮 1a：上传可解析点表（web.md §3.2）──
    await uploadFixture(page, TURBINE1_CSV);
    // 解析结果以单个气泡纯文本回显（含设备 IP）
    await expect(
        page.getByTestId("user-bubble").filter({ hasText: "192.168.110.10" }).first(),
    ).toBeVisible({ timeout: 120_000 });

    // ── 轮 1b：提交接入需求 ──
    await sendMessage(
        page,
        "接入 1#风机，设备 IP 192.168.110.10，Modbus TCP 端口 502，采集数据并转发到中心侧",
    );

    // ── 轮 1c：补场站信息（后端确定性拦截：已记录场站…）──
    await sendMessage(page, "场站名称：华能阿拉善，缩写：hnals");

    // ── 轮 2/3：累积文本命中确认句式 → 确认按钮出现（不依赖 interrupt）──
    await ensureConfirmButtons(page);

    // ── 拦截确认点击发起的 POST /api/chat，断言是普通 POST ──
    const confirmPost = page.waitForRequest(
        (req) => req.url().includes("/api/chat") && req.method() === "POST",
        { timeout: 60_000 },
    );
    const confirmButton = confirmButtons(page).getByRole("button", {
        name: "确认",
        exact: true,
    });
    await expect(confirmButton).toBeVisible();
    await confirmButton.click();

    const req = await confirmPost;
    const body = req.postDataJSON() as Record<string, unknown>;
    // web.md §3.1.3：确认是普通 POST { message:"确认", history }，不含 resume/interruptId
    expect(body.message).toBe("确认");
    expect(body.resume).toBeUndefined();
    expect(body.interruptId).toBeUndefined();

    // ── 流式输出执行结果（接入方案已执行，配置已写入。/ 服务已重启: ...）──
    await expect(page.getByTestId("agent-bubble").last()).toContainText(CONFIG_WRITTEN, {
        timeout: 240_000,
    });
});

// ── 5.1.2 服务目录浏览 ──────────────────────────────────

test("5.1.2 服务目录浏览（卡片列表渲染，无 503）", async ({ page }) => {
    await gotoChat(page);
    await page.getByTestId("nav-services").click();

    await expect(page.getByTestId("service-dashboard")).toBeVisible({ timeout: 30_000 });
    // 无 503 错误条（services-error 只在 registry 未加载时出现）
    await expect(page.getByTestId("services-error")).toHaveCount(0);
    const cardCount = await page.getByTestId("service-card").count();
    expect(cardCount).toBeGreaterThanOrEqual(1);
});

// ── 5.1.3 顶栏工作阶段徽标 ───────────────────────────────

test("5.1.3 顶栏工作阶段徽标：存在、合法且随对话更新（允许 1s 滞后）", async ({ page }) => {
    await gotoChat(page);
    const badge = page.getByTestId("phase-badge");
    await expect(badge).toBeVisible();

    const initialPhase = (await badge.getAttribute("data-phase")) ?? "";
    expect(VALID_PHASES).toContain(initialPhase);

    if (!process.env.DEEPSEEK_API_KEY) {
        // 无 LLM key 时仅断言徽标存在且 phase 合法（README §1.4）
        return;
    }

    // 1s 采样徽标，观察对话过程中的 phase 变化（前端轮询周期 1s，允许滞后）
    const seen = new Set<string>([initialPhase]);
    let sampling = true;
    const sampler = (async () => {
        while (sampling) {
            const phase = await badge.getAttribute("data-phase").catch(() => null);
            if (phase) {
                seen.add(phase);
                expect(VALID_PHASES).toContain(phase);
            }
            await page.waitForTimeout(1000);
        }
    })();

    try {
        // 上传 → output_device_info → phase: collecting（后端确定性写入）
        await uploadFixture(page, TURBINE2_CSV);
        await sendMessage(page, "接入 2#风机，设备 IP 192.168.110.11，转发到中心侧");
    } finally {
        sampling = false;
        await sampler;
    }

    // 对话过程中徽标出现过工作阶段（collecting/planning/...）— 徽标确实随 phase 更新
    const workPhases = [...seen].filter((phase) => phase !== "idle");
    expect(workPhases.length).toBeGreaterThan(0);

    // 徽标与后端 /api/state 最终一致（轮询滞后 ≤ 1s，容忍重试）
    await expect
        .poll(
            async () => {
                const state = await (
                    await page.request.get("http://localhost:5173/api/state")
                ).json();
                const badgePhase = await badge.getAttribute("data-phase");
                return badgePhase === state?.state?.phase;
            },
            { timeout: 10_000, intervals: [500, 1000] },
        )
        .toBe(true);
});

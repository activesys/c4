// c4/agent/frontend/src/App.tsx
// Top-level SPA shell — web.md §2.2.
//
// 双形态外壳（视觉基准：打开页面 = www.deepseek.com，交互后 = chat.deepseek.com）：
//   - 落地形态（view=chat 且未开始对话）：无侧栏，顶栏横排导航（logo 左、
//     PhaseBadge/MCP 右），主区整高交给 ChatView 的 hero 居中布局；
//   - 对话形态（首条消息/上传后）与 MCP 目录视图：chat.deepseek.com 式侧栏
//     （「开启新对话」+ 导航项）+ 顶栏。
//
// ChatView 以 key={chatEpoch} 重挂载实现「开启新对话」——卸载即清空会话状态，
// 回到落地形态；started 随之复位，与「每次打开页面呈现落地页」的体验一致。

import { useCallback, useEffect, useRef, useState } from "react";
import { ChatView } from "./components/ChatView";
import { ServiceDashboard } from "./components/ServiceDashboard";
import { PhaseBadge } from "./components/PhaseBadge";
import { SiteEditDialog, SiteSetupGate } from "./components/SiteSetupGate";
import { useAgentState } from "./hooks/useAgentState";
import { resetSessionState } from "@frontend/api/state";
import c4IconUrl from "./assets/c4-icon.svg";

type View = "chat" | "services";

function App(): JSX.Element {
  const [view, setView] = useState<View>("chat");
  const [started, setStarted] = useState(false);
  const [chatEpoch, setChatEpoch] = useState(0);
  const [errorDismissed, setErrorDismissed] = useState(false);
  const [siteEditOpen, setSiteEditOpen] = useState(false);
  // 侧栏折叠（DeepSeek 式，2026-10-05 用户指令）：进入对话页默认隐藏——
  // 顶栏呈 [C4图标][展开侧栏][接入对话][MCP][+新对话] 胶囊形态；会话内可
  // 随时展开/收起（2026-10-05 用户指令：进入对话页默认隐藏左侧栏）
  const [navHidden, setNavHidden] = useState(true);
  const mainRef = useRef<HTMLElement | null>(null);

  // 主页背景：7 张同形式（云雾+网格）变体；页面加载随机一张，「开启新对话」
  // 时重新摇取且避开当前张（连续两次必不相同）——styles.css
  // .app--landing.bg-0~6（2026-10-05 用户指令）
  const [bgIndex, setBgIndex] = useState(() => Math.floor(Math.random() * 7));
  const rollBg = useCallback((prev: number): number => {
    const next = Math.floor(Math.random() * 6);
    return next < prev ? next : next + 1; // 均匀分布于 0~6 中除 prev 外的 6 张
  }, []);

  const toggleNav = useCallback(() => {
    setNavHidden((prev) => !prev);
  }, []);

  const { phase, lastError, siteName, refresh } = useAgentState(1000);

  // MCP/对话切换返回对话视图时，滚动容器（app__main）钉到底部——会话常驻
  // 挂载后滚动位置会被 MCP 视图的滚动改写，返回时恢复到最新消息处
  //（2026-10-05 用户指令）
  useEffect(() => {
    if (view !== "chat") return;
    const main = mainRef.current;
    if (main) main.scrollTop = main.scrollHeight;
  }, [view]);

  // 首次启动引导（2026-10-05 用户指令：不可跳过）：部署后场站未绑定（agent.json
  // site 为空）时，所有视图之上强制初始化。phase==="unknown" 表示首轮 /api/state
  // 未返回，此时不渲染引导层，避免已配置部署闪现。
  const siteUnbound = phase !== "unknown" && siteName === null;

  // Reset the dismissal flag whenever a new error appears so the banner
  // re-shows on the next poll.
  const bannerVisible = !errorDismissed && Boolean(lastError);

  const dismissError = useCallback(() => setErrorDismissed(true), []);

  // 对话框回调稳定化：useAgentState 每秒轮询触发 App 重渲染，内联箭头函数
  // 会生成新引用，导致 SiteEditDialog 的 effect 反复重跑（拉取 → 表单被重置）
  const closeSiteEdit = useCallback(() => setSiteEditOpen(false), []);
  const handleSiteSaved = useCallback(() => {
    setSiteEditOpen(false);
    void refresh();
  }, [refresh]);

  // Force-refresh agent state whenever the user switches views — keeps the
  // badge reasonably fresh without waiting for the next 1s poll tick.
  // 切换仅做视图隐藏切换（ChatView 常驻挂载，2026-10-05 修复）：不再重置
  // started——「对话接入」是纯返回动作，进行中的会话原样保留，不跳落地页
  const switchView = useCallback(
    (next: View) => {
      setView(next);
      void refresh();
    },
    [refresh],
  );

  // 开启新对话：重挂载 ChatView 清空会话 + 回到落地形态（www.deepseek.com hero）；
  // 同时复位后端全局状态（phase → idle、撤销方案）——上一会话可能仍在收集信息/
  // 待确认，徽标应回「空闲」（2026-10-05 用户指令）
  const startNewChat = useCallback(() => {
    setChatEpoch((k) => k + 1);
    setStarted(false);
    setErrorDismissed(false);
    setView("chat");
    setBgIndex(rollBg); // 回落地页换一张背景（避开当前张）
    resetSessionState()
      .catch(() => undefined)
      .then(() => refresh());
  }, [refresh, rollBg]);

  // 落地形态仅限「对话视图且未开始对话」；MCP 目录始终走侧栏布局。
  const landing = view === "chat" && !started;
  const errorBanner = bannerVisible ? (
    <div role="alert" data-testid="last-error" className="app__error">
      <span>{lastError}</span>
      <button type="button" aria-label="关闭错误" onClick={dismissError}>
        ×
      </button>
    </div>
  ) : null;

  return (
    <div
      className={`app${landing ? ` app--landing bg-${bgIndex}` : ""}`}
      data-testid="app"
    >
      <header className="app__topbar" role="banner">
        {/* 左栏：品牌 + 收起按钮 + 侧栏折叠态胶囊组（DeepSeek 式）
            [C4图标][收起侧栏] / 折叠后 [C4图标][展开侧栏][接入对话][MCP][+新对话]；
            标语仅落地页显示（对话页/折叠态只留图标，2026-10-05 用户指令） */}
        <div className="app__topbar-left">
          <div className="app__brand">
            <img
              src={c4IconUrl}
              alt="C4"
              className="app__brand-icon"
            />
            {landing ? (
              <span className="app__brand-name">C4让数据接入更智能、更轻松。</span>
            ) : null}
          </div>
          {/* 收起按钮紧贴 C4 图标右侧（2026-10-05 用户指令） */}
          {!landing && !navHidden ? (
            <button
              type="button"
              data-testid="nav-toggle"
              className="app__icon-btn"
              onClick={toggleNav}
              aria-label="收起侧边栏"
              title="收起侧边栏"
            >
              <PanelIcon />
            </button>
          ) : null}
          {!landing && navHidden ? (
            <div className="app__topbar-pill" data-testid="nav-pill">
              <button
                type="button"
                data-testid="nav-toggle"
                className="app__icon-btn"
                onClick={toggleNav}
                aria-label="展开侧边栏"
                title="展开侧边栏"
              >
                <PanelIcon />
              </button>
              <button
                type="button"
                data-testid="pill-chat"
                className={`app__icon-btn ${view === "chat" ? "app__icon-btn--active" : ""}`}
                onClick={() => switchView("chat")}
                aria-label="对话接入"
                title="对话接入"
              >
                <ChatIcon />
              </button>
              <button
                type="button"
                data-testid="pill-services"
                className={`app__icon-btn app__icon-btn--text ${view === "services" ? "app__icon-btn--active" : ""}`}
                onClick={() => switchView("services")}
                aria-label="打开 MCP 目录"
                title="MCP"
              >
                MCP
              </button>
              <button
                type="button"
                data-testid="pill-new-chat"
                className="app__icon-btn"
                onClick={startNewChat}
                aria-label="开启新对话"
                title="开启新对话"
              >
                <PlusCircleIcon />
              </button>
            </div>
          ) : null}
        </div>
        {/* 顶栏中央：当前绑定场站（落地页/对话页均展示），点击可修改 */}
        <div className="app__topbar-center" data-testid="site-name">
          {siteName ? (
            <button
              type="button"
              className="app__site-name"
              onClick={() => setSiteEditOpen(true)}
              aria-label="修改场站信息"
              title="点击修改场站信息"
            >
              {siteName}
            </button>
          ) : null}
        </div>
        <div className="app__topbar-right">
          {errorBanner}
          <PhaseBadge phase={phase} />
          {landing || !navHidden ? (
            <button
              type="button"
              data-testid="nav-services"
              className="app__topbar-link"
              onClick={() => switchView("services")}
            >
              MCP
            </button>
          ) : null}
        </div>
      </header>

      <div className="app__body">
        {!landing && !navHidden && (
          <nav className="app__nav" aria-label="主导航">
            <button
              type="button"
              data-testid="nav-new-chat"
              className="app__nav-new"
              onClick={startNewChat}
            >
              <PlusCircleIcon />
              <span>开启新对话</span>
            </button>
            <button
              type="button"
              data-testid="nav-chat"
              className={`app__nav-item ${view === "chat" ? "is-active" : ""}`}
              onClick={() => switchView("chat")}
              aria-current={view === "chat" ? "page" : undefined}
            >
              对话接入
            </button>
            <button
              type="button"
              data-testid="nav-services"
              className={`app__nav-item ${view === "services" ? "is-active" : ""}`}
              onClick={() => switchView("services")}
              aria-current={view === "services" ? "page" : undefined}
            >
              MCP
            </button>
          </nav>
        )}

        <main ref={mainRef} className="app__main" role="main">
          {/* 双视图常驻挂载（hidden 切换显示）：切到 MCP 不卸载 ChatView，
              会话状态保留——「对话接入」返回时会话原样在（2026-10-05 修复：
              原条件渲染卸载 ChatView，返回即丢会话并闪回落地页）；包装层
              display:contents 不改变 .app__main 直排几何 */}
          <div className="app__view" hidden={view !== "chat"} data-testid="chat-view-shell">
            <ChatView
              key={chatEpoch}
              landing={landing}
              onStarted={() => setStarted(true)}
            />
          </div>
          <div className="app__view" hidden={view !== "services"}>
            <ServiceDashboard />
          </div>
        </main>
      </div>

      {/* 场站初始化引导层（不可跳过）：叠加在初始界面之上——先渲染落地页
          （云雾网格背景），引导卡片浮于其上（2026-10-05 用户指令） */}
      {siteUnbound ? <SiteSetupGate onBound={() => void refresh()} /> : null}

      {siteEditOpen ? (
        <SiteEditDialog onSaved={handleSiteSaved} onClose={closeSiteEdit} />
      ) : null}
    </div>
  );
}

export default App;

// ── 顶栏图标（DeepSeek 式侧栏折叠，2026-10-05 用户指令）──

/** 侧栏收起/展开：圆角方框 + 左侧 1/3 分隔线（面板切换图标） */
function PanelIcon(): JSX.Element {
  return (
    <svg
      viewBox="0 0 24 24"
      width="18"
      height="18"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.8"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <rect x="3" y="4.5" width="18" height="15" rx="3" />
      <path d="M9.5 4.5v15" />
    </svg>
  );
}

/** 开启新对话：圆圈加号（⊕） */
function PlusCircleIcon(): JSX.Element {
  return (
    <svg
      viewBox="0 0 24 24"
      width="18"
      height="18"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.8"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <circle cx="12" cy="12" r="9" />
      <path d="M12 8.5v7M8.5 12h7" />
    </svg>
  );
}

/** 对话接入：气泡（胶囊折叠态的对话视图入口） */
function ChatIcon(): JSX.Element {
  return (
    <svg
      viewBox="0 0 24 24"
      width="18"
      height="18"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.8"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d="M12 4c-4.6 0-8.2 3-8.2 6.9 0 1.7.7 3.2 1.9 4.4L5 20l4.1-1.6c1 .3 1.9.4 2.9.4 4.6 0 8.2-3 8.2-6.9S16.6 4 12 4z" />
    </svg>
  );
}

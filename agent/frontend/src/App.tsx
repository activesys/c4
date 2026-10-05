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

import { useCallback, useState } from "react";
import { ChatView } from "./components/ChatView";
import { ServiceDashboard } from "./components/ServiceDashboard";
import { PhaseBadge } from "./components/PhaseBadge";
import { useAgentState } from "./hooks/useAgentState";
import c4IconUrl from "./assets/c4-icon.svg";

type View = "chat" | "services";

function App(): JSX.Element {
  const [view, setView] = useState<View>("chat");
  const [started, setStarted] = useState(false);
  const [chatEpoch, setChatEpoch] = useState(0);
  const [errorDismissed, setErrorDismissed] = useState(false);

  const { phase, lastError, refresh } = useAgentState(1000);

  // Reset the dismissal flag whenever a new error appears so the banner
  // re-shows on the next poll.
  const bannerVisible = !errorDismissed && Boolean(lastError);

  const dismissError = useCallback(() => setErrorDismissed(true), []);

  // Force-refresh agent state whenever the user switches views — keeps the
  // badge reasonably fresh without waiting for the next 1s poll tick.
  // 切换视图会卸载 ChatView（会话状态随之清空，原实现即如此），因此返回
  // 对话视图时复位 started，让空会话重新呈现落地页而非空对话布局。
  const switchView = useCallback(
    (next: View) => {
      setView(next);
      if (next === "chat") {
        setStarted(false);
      }
      void refresh();
    },
    [refresh],
  );

  // 开启新对话：重挂载 ChatView 清空会话 + 回到落地形态（www.deepseek.com hero）。
  const startNewChat = useCallback(() => {
    setChatEpoch((k) => k + 1);
    setStarted(false);
    setErrorDismissed(false);
    setView("chat");
    void refresh();
  }, [refresh]);

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
    <div className={`app${landing ? " app--landing" : ""}`} data-testid="app">
      <header className="app__topbar" role="banner">
        <button
          type="button"
          className="app__brand"
          onClick={startNewChat}
          aria-label="回到首页"
        >
          <img
            src={c4IconUrl}
            alt="C4"
            className="app__brand-icon"
          />
          <span className="app__brand-name">C4让数据接入更智能、更轻松。</span>
        </button>
        <div className="app__topbar-right">
          {errorBanner}
          <PhaseBadge phase={phase} />
          {landing ? (
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
        {!landing && (
          <nav className="app__nav" aria-label="主导航">
            <button
              type="button"
              data-testid="nav-new-chat"
              className="app__nav-new"
              onClick={startNewChat}
            >
              ＋ 开启新对话
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

        <main className="app__main" role="main">
          {view === "chat" ? (
            <ChatView
              key={chatEpoch}
              landing={landing}
              onStarted={() => setStarted(true)}
            />
          ) : (
            <ServiceDashboard />
          )}
        </main>
      </div>
    </div>
  );
}

export default App;

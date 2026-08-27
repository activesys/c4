// c4/agent/frontend/src/App.tsx
// Top-level SPA shell — web.md §2.2.
//
// Layout: top bar (PhaseBadge + closable lastError banner) over a two-column
// body (left nav + main view). Default view is ChatView; nav switches to
// ServiceDashboard.

import { useCallback, useState } from "react";
import { ChatView } from "./components/ChatView";
import { ServiceDashboard } from "./components/ServiceDashboard";
import { PhaseBadge } from "./components/PhaseBadge";
import { useAgentState } from "./hooks/useAgentState";

type View = "chat" | "services";

function App(): JSX.Element {
  const [view, setView] = useState<View>("chat");
  const [errorDismissed, setErrorDismissed] = useState(false);

  const { phase, lastError, refresh } = useAgentState(1000);

  // Reset the dismissal flag whenever a new error appears so the banner
  // re-shows on the next poll.
  const bannerVisible = !errorDismissed && Boolean(lastError);

  const dismissError = useCallback(() => setErrorDismissed(true), []);

  // Force-refresh agent state whenever the user switches views — keeps the
  // badge reasonably fresh without waiting for the next 1s poll tick.
  const switchView = useCallback(
    (next: View) => {
      setView(next);
      void refresh();
    },
    [refresh],
  );

  return (
    <div className="app" data-testid="app">
      <header className="app__topbar" role="banner">
        <div className="app__title">C4 · 场站智能助手</div>
        <div className="app__topbar-right">
          <PhaseBadge phase={phase} />
          {bannerVisible && (
            <div
              role="alert"
              data-testid="last-error"
              className="app__error"
            >
              <span>{lastError}</span>
              <button
                type="button"
                aria-label="关闭错误"
                onClick={dismissError}
              >
                ×
              </button>
            </div>
          )}
        </div>
      </header>

      <div className="app__body">
        <nav className="app__nav" aria-label="主导航">
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
            服务目录
          </button>
        </nav>

        <main className="app__main" role="main">
          {view === "chat" ? <ChatView /> : <ServiceDashboard />}
        </main>
      </div>
    </div>
  );
}

export default App;
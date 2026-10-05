// c4/agent/frontend/src/components/LandingHero.tsx
// 落地页 hero 区块 — 风格基准 www.deepseek.com（eyebrow 大写小字 + 超大居中
// 标题，留白布局）。纯展示组件；输入框仍由 ChatView 渲染（testid 与发送逻辑
// 保持单一来源）。

export function LandingHero(): JSX.Element {
  return (
    <div className="hero" data-testid="landing-hero">
      <p className="hero__eyebrow">SITE ACCESS INTELLIGENCE</p>
      <h1 className="hero__title">场站接入，一问即达</h1>
    </div>
  );
}

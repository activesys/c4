// c4/agent/frontend/src/components/Markdown.tsx
// Agent 气泡的 Markdown 渲染（GFM：表格/删除线/任务列表）。
// react-markdown 默认转义原始 HTML——LLM 输出中的任何 HTML 片段一律按
// 纯文本展示，杜绝提示注入诱导的 XSS；用户气泡不经过此组件（保持原样）。

import { memo } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

export const Markdown = memo(function Markdown({ text }: { text: string }) {
  return (
    <div className="md">
      <ReactMarkdown remarkPlugins={[remarkGfm]}>{text}</ReactMarkdown>
    </div>
  );
});

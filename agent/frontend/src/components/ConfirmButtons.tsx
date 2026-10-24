// c4/agent/frontend/src/components/ConfirmButtons.tsx
// 方案确认按钮 — web.md §3.1.3（2026-10-24 修订：仅确认按钮）。
// 取消按钮已废除（用户裁定：取消不经文本、不设取消按钮——双 ESC →
// POST /api/cancel，web.md §3.7.6）。

import { CONFIRM_KEYWORD } from "@frontend/hooks/useConfirmDetect";

export interface ConfirmButtonsProps {
  visible: boolean;
  onConfirm: () => void;
}

export function ConfirmButtons({
  visible,
  onConfirm,
}: ConfirmButtonsProps): JSX.Element | null {
  if (!visible) return null;
  return (
    <div data-testid="confirm-buttons" className="confirm-buttons" role="group">
      <button
        type="button"
        className="confirm-buttons__confirm"
        onClick={onConfirm}
        data-confirm-keyword={CONFIRM_KEYWORD}
      >
        确认
      </button>
    </div>
  );
}

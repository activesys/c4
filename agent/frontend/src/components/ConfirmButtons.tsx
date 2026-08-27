// c4/agent/frontend/src/components/ConfirmButtons.tsx
// Plan-confirmation buttons — web.md §3.1.3, §3.2.4, §3.2.5.
//
// Plain POST `/api/chat` carrying the keyword as the `message`. NO
// resume/interruptId — the design explicitly forbids relying on interrupt
// (backend never emits it).

import {
  CONFIRM_KEYWORD,
  CANCEL_KEYWORD,
} from "@frontend/hooks/useConfirmDetect";

export interface ConfirmButtonsProps {
  visible: boolean;
  onConfirm: () => void;
  onCancel: () => void;
}

export function ConfirmButtons({
  visible,
  onConfirm,
  onCancel,
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
      <button
        type="button"
        className="confirm-buttons__cancel"
        onClick={onCancel}
        data-cancel-keyword={CANCEL_KEYWORD}
      >
        取消
      </button>
    </div>
  );
}

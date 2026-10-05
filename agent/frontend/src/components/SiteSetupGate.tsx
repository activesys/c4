// c4/agent/frontend/src/components/SiteSetupGate.tsx
// 场站初始化引导层 + 顶栏编辑对话框（2026-10-05 用户指令）。
//
// C4 部署后不知道场站信息（agent.json site 为空），首次启动时由用户补填：
//   - SiteSetupGate：全屏引导层，不可跳过——场站是归属判定的根基，初始化
//     只需一次（POST /api/site 落盘 agent.json 后 1s 轮询内消失）；
//   - SiteEditDialog：顶栏场站名点击后的编辑态（Esc/遮罩可关闭，改动影响面
//     在框内提示：仅影响归属判定基准与后续新设备缩写，已接入设备不迁移）。
// 两者共用 SiteForm；缩写可选——留空由后端自动生成（LLM→名称派生）。

import { useEffect, useRef, useState } from "react";
import { bindSite, fetchSite, type SiteConfig } from "@frontend/api/site";

export interface SiteFormProps {
  /** 编辑态预填（gate 模式为 null） */
  initial: SiteConfig | null;
  /** 是否展示缩写输入（引导层不展示——缩写由后端自动生成；编辑对话框展示） */
  showAbbr?: boolean;
  /** 绑定成功回调（App 触发 /api/state 立即刷新） */
  onDone: () => void;
  /** 编辑态取消（gate 模式无取消） */
  onCancel?: () => void;
}

function SiteForm({
  initial,
  showAbbr = true,
  onDone,
  onCancel,
}: SiteFormProps): JSX.Element {
  const [name, setName] = useState(initial?.name ?? "");
  const [abbr, setAbbr] = useState(initial?.abbr ?? "");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  // 渐进披露：缩写自动生成失败（后端 422）时才在引导层露出缩写输入
  const [needAbbr, setNeedAbbr] = useState(false);

  // 编辑态的 initial 由对话框异步拉取（挂载后到达）——只在首次到达时同步进
  // 受控输入。不得随 initial 引用变化反复覆盖：否则父组件（1s 轮询重渲染）
  // 每次传入新对象都会把用户正在输入的内容冲回服务器值（2026-10-05 实测）
  const syncedRef = useRef(false);
  useEffect(() => {
    if (initial !== null && !syncedRef.current) {
      syncedRef.current = true;
      setName(initial.name);
      setAbbr(initial.abbr);
    }
  }, [initial]);

  const submit = async (): Promise<void> => {
    if (busy) return;
    if (name.trim() === "") {
      setError("请填写场站名称");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      await bindSite(name.trim(), abbr.trim());
      onDone();
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      if (msg.includes("缩写")) setNeedAbbr(true);
      setError(msg);
      setBusy(false);
    }
  };

  return (
    <form
      className="site-form"
      data-testid="site-setup"
      autoComplete="off"
      onSubmit={(e) => {
        e.preventDefault();
        void submit();
      }}
    >
      <label className="site-form__label" htmlFor="site-setup-name">
        场站名称
      </label>
      <input
        id="site-setup-name"
        data-testid="site-setup-name"
        className="site-form__input"
        placeholder="例如：华能阿拉善一区"
        value={name}
        onChange={(e) => setName(e.target.value)}
        autoFocus
        maxLength={20}
        autoComplete="off"
        name="c4-site-name"
      />

      {(showAbbr || needAbbr) && (
        <>
          <label className="site-form__label" htmlFor="site-setup-abbr">
            场站缩写（可选）
          </label>
          <input
            id="site-setup-abbr"
            data-testid="site-setup-abbr"
            className="site-form__input"
            placeholder="2~12 位字母/数字，留空自动生成"
            value={abbr}
            onChange={(e) => setAbbr(e.target.value)}
            maxLength={12}
            autoComplete="off"
            name="c4-site-abbr"
          />
        </>
      )}

      {error !== null && (
        <p role="alert" data-testid="site-setup-error" className="site-form__error">
          {error}
        </p>
      )}

      <div className="site-form__actions">
        {onCancel !== undefined && (
          <button
            type="button"
            className="site-form__btn site-form__btn--ghost"
            onClick={onCancel}
            disabled={busy}
          >
            取消
          </button>
        )}
        <button
          type="submit"
          data-testid="site-setup-submit"
          className="site-form__btn site-form__btn--primary"
          disabled={busy || name.trim() === ""}
        >
          {busy ? "保存中…" : initial === null ? "完成初始化" : "保存"}
        </button>
      </div>
    </form>
  );
}

export interface SiteSetupGateProps {
  /** 绑定成功（App 立即刷新 /api/state，siteName 到位后卸载引导层） */
  onBound: () => void;
}

/** 首次启动全屏引导层——不可跳过 */
export function SiteSetupGate({ onBound }: SiteSetupGateProps): JSX.Element {
  return (
    <div className="site-gate" data-testid="site-gate">
      <div className="site-gate__card">
        <h1 className="site-gate__lead">初次见面，告诉我你在哪里</h1>
        <SiteForm initial={null} showAbbr={false} onDone={onBound} />
      </div>
    </div>
  );
}

export interface SiteEditDialogProps {
  onSaved: () => void;
  onClose: () => void;
}

/** 顶栏场站名点击后的编辑对话框（Esc/遮罩关闭）——风格与引导层一致 */
export function SiteEditDialog({
  onSaved,
  onClose,
}: SiteEditDialogProps): JSX.Element {
  const [initial, setInitial] = useState<SiteConfig | null>(null);
  const [loadErr, setLoadErr] = useState<string | null>(null);

  // onClose 经 ref 转发：父组件每次重渲染传入新函数引用时，拉取/按键监听
  // 只在挂载时执行一次，不随引用变化反复 fetch（fetch 会生成新 initial →
  // 表单被重置，2026-10-05 实测「删字被填回」根因）
  const onCloseRef = useRef(onClose);
  useEffect(() => {
    onCloseRef.current = onClose;
  });

  useEffect(() => {
    let mounted = true;
    fetchSite()
      .then((s) => {
        if (mounted) setInitial(s);
      })
      .catch((err: unknown) => {
        if (mounted) {
          setLoadErr(err instanceof Error ? err.message : String(err));
        }
      });
    const onKey = (e: KeyboardEvent): void => {
      if (e.key === "Escape") onCloseRef.current();
    };
    window.addEventListener("keydown", onKey);
    return () => {
      mounted = false;
      window.removeEventListener("keydown", onKey);
    };
  }, []);

  return (
    <div
      className="site-dialog__overlay"
      data-testid="site-edit-dialog"
      onClick={onClose}
    >
      <div
        className="site-dialog"
        role="dialog"
        aria-modal="true"
        aria-label="修改场站信息"
        onClick={(e) => e.stopPropagation()}
      >
        <h2 className="site-gate__lead">场站有变？数据不搬家</h2>
        {/* 缩写输入不展示：SiteForm 内部仍持有 initial.abbr，保存时原值回传
            ——改名不触发缩写重新生成（2026-10-05 用户指令） */}
        {loadErr !== null ? (
          <p role="alert" className="site-form__error">
            {loadErr}
          </p>
        ) : (
          <SiteForm
            initial={initial}
            showAbbr={false}
            onDone={onSaved}
            onCancel={onClose}
          />
        )}
      </div>
    </div>
  );
}

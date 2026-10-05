// c4/agent/frontend/src/components/FileUpload.tsx
// File upload widget — web.md §3.2.
//
// Two responsibilities:
//   1. Accept only files the backend can actually parse
//      (.xlsx/.csv/.xls/.txt — see classifyFileType). Other extensions that
//      multer allows (pdf/docx/images) get a "暂不支持解析" hint.
//   2. Hand the chosen file to the caller via onUpload.
//
// We deliberately do NOT auto-upload here — the caller decides when to
// trigger the network request (e.g. only after they click 发送, so the upload
// and the message stay coupled). The wrapper in ChatView owns the actual
// streamUpload call.

import { useId, useRef, useState } from "react";
import { classifyFileType } from "@frontend/api/upload";

const ACCEPT = ".xlsx,.csv,.xls,.txt";

export interface FileUploadProps {
  onUpload: (file: File, message?: string) => void;
}

export function FileUpload({ onUpload }: FileUploadProps): JSX.Element {
  const inputRef = useRef<HTMLInputElement | null>(null);
  const [warning, setWarning] = useState<string | null>(null);
  const inputId = useId();

  const handleChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    setWarning(null);
    const file = e.target.files?.[0];
    if (!file) return;

    if (classifyFileType(file.name) === "unsupported") {
      // We still let the user "try" if they really want — but warn first.
      setWarning(`暂不支持解析：${file.name}（支持 .xlsx/.csv/.xls/.txt）`);
    }
    onUpload(file);
    // Reset so the same file can be re-selected.
    if (inputRef.current) inputRef.current.value = "";
  };

  return (
    <div className="file-upload">
      <label htmlFor={inputId} className="file-upload__button" aria-label="上传文件">
        <svg
          className="file-upload__icon"
          viewBox="0 0 24 24"
          width="16"
          height="16"
          fill="none"
          stroke="currentColor"
          strokeWidth="2"
          strokeLinecap="round"
          strokeLinejoin="round"
          aria-hidden="true"
        >
          <path d="m21.44 11.05-9.19 9.19a6 6 0 0 1-8.49-8.49l8.57-8.57A4 4 0 1 1 18 8.84l-8.59 8.57a2 2 0 0 1-2.83-2.83l8.49-8.48" />
        </svg>
        上传
      </label>
      <input
        id={inputId}
        ref={inputRef}
        type="file"
        accept={ACCEPT}
        data-testid="file-upload-input"
        onChange={handleChange}
        style={{ display: "none" }}
      />
      {warning && (
        <span data-testid="file-upload-warning" className="file-upload__warning" role="status">
          {warning}
        </span>
      )}
    </div>
  );
}

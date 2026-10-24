import React from "react";
import type {
  OfficeCoCreationMode,
  OfficeSuite,
} from "./OfficeCoCreationContract";
import { OFFICE_CO_CREATION_PROTOCOL_VERSION } from "./OfficeCoCreationContract";

export interface OfficeCoCreationModeBarProps {
  readonly mode: OfficeCoCreationMode;
  readonly suite: OfficeSuite;
  readonly onChange: (mode: OfficeCoCreationMode) => void;
  readonly disabled?: boolean;
  readonly scopeLabel?: string;
}

const OPTIONS: readonly {
  readonly value: OfficeCoCreationMode;
  readonly label: string;
  readonly title: string;
}[] = [
  { value: "ask", label: "询问", title: "只读取和回答，不修改文件" },
  { value: "suggest", label: "建议", title: "生成可审阅建议，不写入文件" },
  { value: "execute", label: "执行", title: "允许本轮写入；必须验证，可回滚" },
];

export function OfficeCoCreationModeBar({
  mode,
  suite,
  onChange,
  disabled = false,
  scopeLabel,
}: OfficeCoCreationModeBarProps): React.JSX.Element {
  return (
    <div
      className="lt-office-co-creation-modebar"
      data-office-co-creation-protocol={OFFICE_CO_CREATION_PROTOCOL_VERSION}
      data-office-co-creation-suite={suite}
      data-office-co-creation-mode={mode}
      aria-label="Office 共创方式"
    >
      <div
        className="lt-office-co-creation-modes"
        role="group"
        aria-label="选择共创方式"
      >
        {OPTIONS.map((option) => (
          <button
            key={option.value}
            type="button"
            className={option.value === mode ? "active" : ""}
            aria-pressed={option.value === mode}
            title={option.title}
            disabled={disabled}
            onClick={() => onChange(option.value)}
          >
            {option.label}
          </button>
        ))}
      </div>
      <span className="lt-office-co-creation-policy">
        {mode === "ask" ? "只读" : mode === "suggest" ? "不写入" : "验证后提交"}
        {scopeLabel ? ` · ${scopeLabel}` : ""}
      </span>
    </div>
  );
}

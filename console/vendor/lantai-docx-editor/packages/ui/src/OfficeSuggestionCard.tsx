import { useEffect, useState, type ReactNode } from "react";
import {
  assertOfficeSuggestionTransition,
  type OfficeSuggestionStatus,
  type OfficeSuite,
} from "./OfficeCoCreationContract";

const STATUS_LABELS: Readonly<Record<OfficeSuggestionStatus, string>> = {
  draft: "生成中",
  refining: "继续完善",
  pending: "等待确认",
  accepted: "已确认修改",
  rejected: "已拒绝",
  applied: "已应用",
  verified: "已验证",
  failed: "执行失败",
  rolled_back: "已回滚",
};

export interface OfficeSuggestionCardProps {
  readonly id: string;
  readonly suite: OfficeSuite;
  readonly children: ReactNode;
  readonly title?: string;
  readonly scopeLabel?: string;
  readonly status?: OfficeSuggestionStatus;
  readonly disabled?: boolean;
  readonly onAccept?: () => void;
  readonly onRefine?: () => void;
  readonly onReject?: () => void;
}

/**
 * Reviewable Office proposal. Suggestions remain inert until the user explicitly
 * asks to execute them; accepting a card must start a fresh execute-mode run.
 */
export function OfficeSuggestionCard({
  id,
  suite,
  children,
  title = "沈行知的改写建议",
  scopeLabel,
  status = "pending",
  disabled = false,
  onAccept,
  onRefine,
  onReject,
}: OfficeSuggestionCardProps): React.JSX.Element {
  const [localStatus, setLocalStatus] =
    useState<OfficeSuggestionStatus>(status);

  useEffect(() => setLocalStatus(status), [status]);

  const transition = (next: OfficeSuggestionStatus): void => {
    assertOfficeSuggestionTransition(localStatus, next);
    setLocalStatus(next);
  };

  // A proposal is actionable exactly once. A refinement starts a new model
  // turn which will yield a new card; keeping this card clickable while that
  // turn runs would allow duplicate execute/reject transitions.
  const actionable = localStatus === "pending";

  return (
    <section
      className={`lt-office-suggestion lt-office-suggestion-${localStatus}`}
      data-office-suggestion-id={id}
      data-office-suggestion-status={localStatus}
      data-office-suite={suite}
      aria-label={title}
    >
      <header className="lt-office-suggestion-header">
        <div>
          <strong>{title}</strong>
          {scopeLabel && (
            <span className="lt-office-suggestion-scope">{scopeLabel}</span>
          )}
        </div>
        <span className="lt-office-suggestion-status" role="status">
          {STATUS_LABELS[localStatus]}
        </span>
      </header>
      <div className="lt-office-suggestion-body">{children}</div>
      {actionable && (
        <footer className="lt-office-suggestion-review">
          <p className="lt-office-suggestion-confirmation-note">
            {scopeLabel
              ? `确认后只会按上方建议修改${scopeLabel}。`
              : "确认后只会按上方写明的建议和目标范围修改文档。"}
          </p>
          <div className="lt-office-suggestion-actions">
            <button
              type="button"
              className="lt-office-suggestion-secondary"
              disabled={disabled}
              onClick={() => {
                transition("refining");
                onRefine?.();
              }}
            >
              继续完善建议
            </button>
            <button
              type="button"
              className="lt-office-suggestion-secondary"
              disabled={disabled}
              onClick={() => {
                transition("rejected");
                onReject?.();
              }}
            >
              拒绝
            </button>
            <button
              type="button"
              className="lt-office-suggestion-primary"
              disabled={disabled || !onAccept}
              onClick={() => {
                transition("accepted");
                onAccept?.();
              }}
            >
              确认并修改
            </button>
          </div>
        </footer>
      )}
    </section>
  );
}

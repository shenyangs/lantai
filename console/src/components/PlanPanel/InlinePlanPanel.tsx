import {
  CheckCircle2,
  ChevronDown,
  ChevronUp,
  Circle,
  ListChecks,
  Loader2,
  PanelRightOpen,
  RefreshCcw,
  ShieldCheck,
  XCircle,
} from "lucide-react";
import { Fragment, useId, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import type { SubTaskResponse } from "../../api/modules/plan";
import styles from "./index.module.less";
import PlanPanel from "./index";
import { usePlanState } from "./usePlanState";
import { getActiveTaskIndex, getPlanPreviewTasks } from "./viewModel";

interface InlinePlanPanelProps {
  active?: boolean;
  onRequestReplan?: () => void;
  onRequestValidation?: () => void;
}

function taskStateIcon(state: SubTaskResponse["state"]) {
  if (state === "done") return CheckCircle2;
  if (state === "in_progress") return Loader2;
  if (state === "abandoned") return XCircle;
  return Circle;
}

function trimLine(value?: string | null): string {
  return typeof value === "string" ? value.trim() : "";
}

export default function InlinePlanPanel({
  active = true,
  onRequestReplan,
  onRequestValidation,
}: InlinePlanPanelProps) {
  const { t } = useTranslation();
  const [expanded, setExpanded] = useState(false);
  const [detailsOpen, setDetailsOpen] = useState(false);
  const previewId = useId();
  const { plan, loading, doneCount, totalCount, percent } = usePlanState({
    active,
  });

  const activeIndex = useMemo(
    () => getActiveTaskIndex(plan?.subtasks ?? []),
    [plan?.subtasks],
  );
  const activeTask = plan?.subtasks[activeIndex] ?? null;
  const currentStep =
    totalCount > 0
      ? plan?.state === "done"
        ? totalCount
        : activeIndex + 1
      : 0;
  const previewTasks = useMemo(
    () => getPlanPreviewTasks(plan?.subtasks ?? [], activeIndex),
    [activeIndex, plan?.subtasks],
  );
  const hiddenTaskCount = Math.max(0, totalCount - previewTasks.length);
  const activeSummary =
    trimLine(activeTask?.name) ||
    trimLine(plan?.description) ||
    (loading ? t("plan.loading", "Loading plan") : "");
  const hasValidationStep = Boolean(
    plan?.subtasks.some((task) =>
      /验证|检查|验收|测试|校对|复核/i.test(
        `${task.name} ${task.description || ""}`,
      ),
    ),
  );

  // Fetching plan state is not itself evidence that a goal exists. Rendering
  // the loading-only shell creates a misleading 0/0 active goal that flashes
  // away as soon as the backend truthfully returns null.
  if (!active || !plan) return null;

  return (
    <Fragment>
      <section
        className={styles.inlinePanel}
        data-expanded={expanded ? "true" : "false"}
        data-state={plan.state}
        aria-label={t("plan.executionPlan", "Execution plan")}
      >
        <button
          type="button"
          className={styles.inlineHeader}
          onClick={() => setExpanded((value) => !value)}
          aria-expanded={expanded}
          aria-controls={previewId}
        >
          <span className={styles.inlineTitleGroup}>
            <span className={styles.inlineStatusDot} aria-hidden="true" />
            <ListChecks size={16} aria-hidden="true" />
            <strong>{t("plan.executionPlan", "Execution plan")}</strong>
          </span>
          <span className={styles.inlineSummary}>{activeSummary}</span>
          <span className={styles.inlineProgressMeta}>
            <span>
              {t("plan.completed", "Completed")} {doneCount}/{totalCount}
            </span>
            <span className={styles.inlineProgressDivider} aria-hidden="true">
              ·
            </span>
            <span>
              {plan.state === "done"
                ? t("plan.allStepsCompleted", "All steps complete")
                : plan.state === "abandoned"
                  ? t("plan.planAbandoned", "Plan ended")
                : `${t("plan.currentStepPrefix", "On step")} ${currentStep}${t(
                    "plan.currentStepSuffix",
                    "",
                  )}`}
            </span>
          </span>
          {expanded ? (
            <ChevronUp size={16} aria-hidden="true" />
          ) : (
            <ChevronDown size={16} aria-hidden="true" />
          )}
        </button>
        <div className={styles.inlineProgressTrack} aria-hidden="true">
          <span style={{ width: `${percent}%` }} />
        </div>

        {expanded ? (
          <div id={previewId} className={styles.inlinePreview}>
            <ol className={styles.inlineTaskList}>
              {previewTasks.map(({ task, index }) => {
                const Icon = taskStateIcon(task.state);
                const outcome = trimLine(task.outcome);
                const description = trimLine(task.description);
                const isActiveTask =
                  index === activeIndex &&
                  (task.state === "in_progress" || task.state === "todo");
                return (
                  <li
                    key={`${task.name}-${index}`}
                    className={styles.inlineTaskItem}
                    data-state={task.state}
                    data-active={isActiveTask ? "true" : "false"}
                  >
                    <Icon
                      className={
                        task.state === "in_progress"
                          ? styles.inlineTaskIconSpin
                          : undefined
                      }
                      size={15}
                      aria-hidden="true"
                    />
                    <span className={styles.inlineTaskBody}>
                      <span className={styles.inlineTaskMeta}>
                        <span>
                          {t("plan.step", "Step")} {index + 1}
                        </span>
                        <span>{t(`plan.state.${task.state}`, task.state)}</span>
                      </span>
                      <span className={styles.inlineTaskName}>{task.name}</span>
                      {description ? (
                        <span className={styles.inlineTaskDesc}>
                          {description}
                        </span>
                      ) : null}
                      {outcome ? (
                        <span className={styles.inlineTaskOutcome}>
                          {t("plan.outcomeArrow", "Result")}: {outcome}
                        </span>
                      ) : null}
                    </span>
                  </li>
                );
              })}
            </ol>
            {hiddenTaskCount > 0 ? (
              <div className={styles.inlineHiddenCount}>
                {t("plan.remainingStepsPrefix", "")}
                {hiddenTaskCount} {t("plan.remainingStepsSuffix", "more steps")}
              </div>
            ) : null}
            <div className={styles.inlinePlanActions}>
              <span data-warning={hasValidationStep ? "false" : "true"}>
                {hasValidationStep
                  ? t("plan.validationReady", "Validation step included")
                  : t("plan.validationMissing", "No explicit validation step")}
              </span>
              <div>
                <button
                  type="button"
                  className={styles.inlinePlanPrimaryAction}
                  onClick={() => setDetailsOpen(true)}
                >
                  <PanelRightOpen size={13} aria-hidden="true" />
                  {t("plan.viewFullPlan", "View full plan")}
                </button>
                {onRequestReplan ? (
                  <button type="button" onClick={onRequestReplan}>
                    <RefreshCcw size={13} aria-hidden="true" />
                    {t("plan.adjustPlan", "Adjust plan")}
                  </button>
                ) : null}
                {onRequestValidation && !hasValidationStep ? (
                  <button type="button" onClick={onRequestValidation}>
                    <ShieldCheck size={13} aria-hidden="true" />
                    {t("plan.addValidation", "Add validation")}
                  </button>
                ) : null}
              </div>
            </div>
          </div>
        ) : null}
      </section>
      <PlanPanel open={detailsOpen} onClose={() => setDetailsOpen(false)} />
    </Fragment>
  );
}

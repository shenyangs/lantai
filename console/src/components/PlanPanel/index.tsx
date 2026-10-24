import type { ReactNode } from "react";
import { Drawer, Progress, Spin } from "antd";
import {
  CheckOutlined,
  ClockCircleOutlined,
  MinusOutlined,
  ProfileOutlined,
  StopOutlined,
} from "@ant-design/icons";
import { IconButton } from "@/vendor/lantaiDesign";
import { SparkOperateRightLine } from "@/vendor/lantaiIcons";
import { useTranslation } from "react-i18next";
import styles from "./index.module.less";
import { usePlanState } from "./usePlanState";
import { getActiveTaskIndex } from "./viewModel";

interface PlanPanelProps {
  open: boolean;
  onClose: () => void;
}

const STATE_ICONS: Record<string, ReactNode> = {
  done: <CheckOutlined />,
  in_progress: <ClockCircleOutlined />,
  abandoned: <StopOutlined />,
  todo: <MinusOutlined />,
};

const STATE_CLASS: Record<string, string> = {
  todo: styles.stateTodo,
  in_progress: styles.stateInProgress,
  done: styles.stateDone,
  abandoned: styles.stateAbandoned,
};

export default function PlanPanel({ open, onClose }: PlanPanelProps) {
  const { t } = useTranslation();
  const { plan, loading, doneCount, totalCount, percent } = usePlanState({
    active: open,
  });
  const activeIndex = getActiveTaskIndex(plan?.subtasks ?? []);
  const currentStep =
    totalCount > 0
      ? plan?.state === "done"
        ? totalCount
        : activeIndex + 1
      : 0;

  return (
    <Drawer
      className={styles.drawer}
      open={open}
      onClose={onClose}
      placement="right"
      width={420}
      closable={false}
      title={null}
      styles={{ body: { padding: 0 } }}
    >
      <div className={styles.header}>
        <span className={styles.headerTitle}>
          {t("plan.executionPlan", "Execution plan")}
        </span>
        <IconButton
          bordered={false}
          icon={<SparkOperateRightLine />}
          onClick={onClose}
          aria-label={t("plan.close", "Close plan")}
        />
      </div>

      <div className={styles.content}>
        {loading && !plan ? (
          <div className={`${styles.emptyState} ${styles.loadingState}`}>
            <Spin />
          </div>
        ) : !plan ? (
          <div className={styles.emptyState}>
            <span className={styles.emptyIcon} aria-hidden="true">
              <ProfileOutlined />
            </span>
            <strong>{t("plan.noPlan", "No active plan")}</strong>
            <p>
              {t("plan.noPlanHint", "Use /plan <description> to create a plan")}
            </p>
          </div>
        ) : (
          <>
            <div className={styles.planInfo}>
              <div className={styles.planName}>
                {plan.name}
                <span
                  className={`${styles.planState} ${
                    STATE_CLASS[plan.state] || ""
                  }`}
                >
                  {t(`plan.state.${plan.state}`, plan.state)}
                </span>
              </div>
              <div className={styles.planDesc}>{plan.description}</div>
            </div>

            <div className={styles.progressSection}>
              <div className={styles.progressLabel}>
                <span>
                  {t("plan.completed", "Completed")} {doneCount}/{totalCount}
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
              </div>
              <Progress
                percent={percent}
                size="small"
                status={plan.state === "abandoned" ? "exception" : "active"}
                showInfo={false}
              />
            </div>

            <ul className={styles.subtaskList}>
              {plan.subtasks.map((subtask, idx) => (
                <li
                  key={`${subtask.name}-${idx}`}
                  className={styles.subtaskItem}
                  data-active={
                    idx === activeIndex &&
                    (subtask.state === "in_progress" ||
                      subtask.state === "todo")
                      ? "true"
                      : "false"
                  }
                  data-state={subtask.state}
                >
                  <span
                    className={styles.subtaskIcon}
                    data-state={subtask.state}
                    aria-hidden="true"
                  >
                    {STATE_ICONS[subtask.state] || <MinusOutlined />}
                  </span>
                  <div className={styles.subtaskBody}>
                    <div className={styles.subtaskMeta}>
                      <span>
                        {t("plan.step", "Step")} {idx + 1}
                      </span>
                      <span>
                        {t(`plan.state.${subtask.state}`, subtask.state)}
                      </span>
                    </div>
                    <div className={styles.subtaskName}>{subtask.name}</div>
                    {subtask.description ? (
                      <div className={styles.subtaskDesc}>
                        {subtask.description}
                      </div>
                    ) : null}
                    {subtask.outcome && (
                      <div className={styles.subtaskOutcome}>
                        ✓ {subtask.outcome}
                      </div>
                    )}
                  </div>
                </li>
              ))}
            </ul>

            {plan.outcome && (
              <div className={styles.planOutcome}>
                <strong>{t("plan.outcome", "Outcome")}:</strong> {plan.outcome}
              </div>
            )}
          </>
        )}
      </div>
    </Drawer>
  );
}

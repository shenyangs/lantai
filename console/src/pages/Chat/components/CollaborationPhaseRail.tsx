import type { CollaborationPhase } from "../../../api/types/chat";
import styles from "./CollaborationPhaseRail.module.less";

const STEPS = [
  { id: "staffing", label: "组队" },
  { id: "working", label: "协作" },
  { id: "reconciling", label: "汇总" },
  { id: "verifying", label: "核验" },
] as const;

function phaseIndex(phase?: CollaborationPhase | string): number {
  switch (phase) {
    case "preparing":
    case "staffing":
      return 0;
    case "working":
      return 1;
    case "reconciling":
      return 2;
    case "verifying":
    case "delivering":
    case "closing":
    case "closed":
      return 3;
    default:
      return 0;
  }
}

export default function CollaborationPhaseRail({
  phase,
  compact = false,
  failed = false,
}: {
  phase?: CollaborationPhase | string;
  compact?: boolean;
  failed?: boolean;
}) {
  const activeIndex = phaseIndex(phase);
  const closed = phase === "closed";

  return (
    <ol
      className={styles.rail}
      data-compact={compact ? "true" : "false"}
      data-closed={closed ? "true" : "false"}
      data-failed={failed ? "true" : "false"}
      aria-label="协作阶段"
    >
      {STEPS.map((step, index) => {
        const state = failed
          ? index < 1
            ? "complete"
            : index === 1
              ? "failed"
              : "upcoming"
          : closed || index < activeIndex
            ? "complete"
            : index === activeIndex
              ? "current"
              : "upcoming";
        return (
          <li
            key={step.id}
            data-state={state}
            aria-current={state === "current" ? "step" : undefined}
          >
            <span aria-hidden="true" />
            <em>{step.label}</em>
          </li>
        );
      })}
    </ol>
  );
}

import { useCallback, useMemo, useState, useSyncExternalStore } from "react";
import {
  AlertCircle,
  CheckCircle2,
  Circle,
  CircleStop,
  Clock3,
  Hand,
  PanelRightClose,
  PauseCircle,
  PlayCircle,
  Scale,
  Users,
} from "lucide-react";
import { App, Tooltip } from "antd";

import type {
  CollaborationBatchSnapshot,
  CollaborationMemberStatus,
} from "../../../api/types/chat";
import { runtimeApi } from "../../../api/modules/runtime";
import { BeverageAgentAvatar } from "../../../components/BeverageAgentAvatar";
import {
  collaborationRuntimeControlTarget,
  collaborationSnapshotForCall,
  subscribeCollaborationCall,
} from "../collaborationEventStore";
import CollaborationPhaseRail from "./CollaborationPhaseRail";
import styles from "./HostedCollaborationRoomPanel.module.less";

interface HostedCollaborationRoomPanelProps {
  callId: string;
  initialSnapshot: CollaborationBatchSnapshot;
  onClose: () => void;
  onTakeover?: () => void;
}

type RoomView = "process" | "conclusions" | "members";

const PHASE_LABELS: Record<string, string> = {
  preparing: "准备中",
  staffing: "正在组队",
  working: "协作中",
  reconciling: "统一结果",
  verifying: "验证中",
  delivering: "准备交付",
  closing: "正在关房",
  closed: "已关房",
};

const MEMBER_STATUS_LABELS: Record<CollaborationMemberStatus, string> = {
  queued: "等待中",
  running: "进行中",
  retrying: "重试中",
  waiting_resource: "等待独占资源",
  completed: "结果已回收",
  failed: "失败",
  cancelled: "已取消",
};

function phaseLabel(snapshot: CollaborationBatchSnapshot): string {
  if (snapshot.status === "failed") return "协作失败";
  if (snapshot.status === "partial") return "部分完成";
  if (snapshot.status === "cancelled") return "已取消";
  return PHASE_LABELS[String(snapshot.phase || "")] || "协作中";
}

function terminal(snapshot: CollaborationBatchSnapshot): boolean {
  return (
    snapshot.phase === "closed" ||
    ["completed", "partial", "failed", "cancelled"].includes(snapshot.status)
  );
}

function closureOutcomeLabel(snapshot: CollaborationBatchSnapshot): string {
  const closure = snapshot.closure;
  if (!closure) return "";
  if (closure.deliverable_ready) return "已交付";
  if (closure.synthesis_ready) return "待主智能体汇总";
  return (
    {
      partial: "部分完成",
      waiting_approval: "等待确认",
      failed: "协作失败",
      cancelled: "已取消",
    }[closure.outcome] || closure.outcome
  );
}

function statusTone(status: CollaborationMemberStatus): string {
  if (status === "completed") return styles.memberSuccess;
  if (status === "failed" || status === "cancelled") return styles.memberDanger;
  if (
    status === "running" ||
    status === "retrying" ||
    status === "waiting_resource"
  )
    return styles.memberActive;
  return "";
}

function memberProgressLabel(
  member: CollaborationBatchSnapshot["members"][number],
): string {
  if (member.status === "waiting_resource") return "等待独占资源";
  if (member.review_status === "running") return "正在交叉复核";
  if (member.review_status === "completed") return "已完成交叉复核";
  if (member.review_status === "unavailable") {
    return "首轮结果已保留 · 复核未完成";
  }
  if (member.status === "queued" && member.depends_on?.length) {
    return "等待上游交付";
  }
  return MEMBER_STATUS_LABELS[member.status];
}

export default function HostedCollaborationRoomPanel({
  callId,
  initialSnapshot,
  onClose,
  onTakeover,
}: HostedCollaborationRoomPanelProps) {
  const [view, setView] = useState<RoomView>("conclusions");
  const [pauseOverride, setPauseOverride] = useState<boolean | null>(null);
  const [controlBusy, setControlBusy] = useState(false);
  const { message } = App.useApp();
  const subscribe = useCallback(
    (listener: () => void) => subscribeCollaborationCall(callId, listener),
    [callId],
  );
  const getSnapshot = useCallback(
    () => collaborationSnapshotForCall(callId),
    [callId],
  );
  const liveSnapshot = useSyncExternalStore(
    subscribe,
    getSnapshot,
    getSnapshot,
  );
  const snapshot = liveSnapshot || initialSnapshot;
  const controlTarget = collaborationRuntimeControlTarget(snapshot);
  const paused = pauseOverride ?? false;
  const completeCount = snapshot.members.filter(
    (member) => member.status === "completed",
  ).length;
  const activeCount = snapshot.members.filter((member) =>
    ["queued", "running", "retrying", "waiting_resource"].includes(
      member.status,
    ),
  ).length;
  const activity = useMemo(
    () =>
      [
        ...(snapshot.host_decisions || []).map((item) => ({
          id: item.decision_id,
          actor: snapshot.host?.display_name || "沈行知",
          detail: item.safe_summary,
          kind: "decision",
        })),
        ...(snapshot.messages || []).map((item) => ({
          id: item.message_id,
          actor: item.display_name,
          detail: item.safe_summary,
          kind: item.kind,
        })),
      ].slice(-20),
    [snapshot],
  );
  const isTerminal = terminal(snapshot);
  const togglePause = async () => {
    if (!controlTarget || controlBusy) return;
    setControlBusy(true);
    try {
      if (paused) {
        await runtimeApi.resumeRun(controlTarget.runId, controlTarget.request);
        setPauseOverride(false);
        message.success("协作已继续");
      } else {
        await runtimeApi.pauseRun(controlTarget.runId, controlTarget.request);
        setPauseOverride(true);
        message.success("协作已暂停");
      }
    } catch (error) {
      console.error("Failed to update collaboration run state:", error);
      message.error(paused ? "继续协作失败" : "暂停协作失败");
    } finally {
      setControlBusy(false);
    }
  };
  const stopCollaboration = async () => {
    if (!controlTarget || controlBusy) return;
    setControlBusy(true);
    try {
      await runtimeApi.interruptRun(controlTarget.runId, controlTarget.request);
      message.success("已停止本次协作");
    } catch (error) {
      console.error("Failed to stop collaboration run:", error);
      message.error("停止本次协作失败");
    } finally {
      setControlBusy(false);
    }
  };
  return (
    <aside
      className={styles.panel}
      aria-label="协作室"
      data-testid="hosted-collaboration-room"
    >
      <header className={styles.header}>
        <span className={styles.hostAvatar} aria-hidden="true">
          <BeverageAgentAvatar
            agentId={snapshot.host?.agent_id || "default"}
            size={38}
          />
        </span>
        <span className={styles.identity}>
          <small>沈行知主持</small>
          <strong>协作室</strong>
          <em>
            {snapshot.members.length} 位协作者
            {activeCount ? ` · ${activeCount} 位进行中` : ""}
            {snapshot.protocol === "roundtable"
              ? ` · 圆桌第 ${snapshot.current_round || 1}/2 轮`
              : ""}
          </em>
        </span>
        <span
          className={styles.status}
          data-terminal={isTerminal ? "true" : "false"}
        >
          {snapshot.status === "failed" ? (
            <AlertCircle size={13} />
          ) : isTerminal ? (
            <CheckCircle2 size={13} />
          ) : (
            <Clock3 size={13} />
          )}
          {phaseLabel(snapshot)}
        </span>
        {!isTerminal &&
        controlTarget &&
        snapshot.controls?.can_pause !== false ? (
          <Tooltip title={paused ? "继续协作" : "暂停协作"}>
            <button
              type="button"
              className={styles.iconButton}
              aria-label={paused ? "继续协作" : "暂停协作"}
              disabled={controlBusy}
              onClick={togglePause}
            >
              {paused ? <PlayCircle size={16} /> : <PauseCircle size={16} />}
            </button>
          </Tooltip>
        ) : (
          <span />
        )}
        {!isTerminal &&
        controlTarget &&
        snapshot.controls?.can_stop !== false ? (
          <Tooltip title="停止本次协作">
            <button
              type="button"
              className={styles.iconButton}
              aria-label="停止本次协作"
              disabled={controlBusy}
              onClick={stopCollaboration}
            >
              <CircleStop size={16} />
            </button>
          </Tooltip>
        ) : (
          <span />
        )}
        <Tooltip title="收起协作室">
          <button
            type="button"
            className={styles.iconButton}
            aria-label="收起协作室"
            onClick={onClose}
          >
            <PanelRightClose size={17} />
          </button>
        </Tooltip>
      </header>

      <section className={styles.taskBlock}>
        <small>这个问题准备怎么回答</small>
        <p>{snapshot.goal || "完成本次多角色协作并形成统一交付"}</p>
        {snapshot.why_parallel ? <em>{snapshot.why_parallel}</em> : null}
        <span>{snapshot.quiet_summary || "沈行知正在主持本次协作。"}</span>
        {snapshot.budget && snapshot.budget_usage ? (
          <span>
            执行预算：尝试 {snapshot.budget_usage.member_attempts_started}/
            {snapshot.budget.max_member_attempts} · 已验收产物{" "}
            {snapshot.budget_usage.verified_artifact_bytes}/
            {snapshot.budget.max_artifact_bytes} 字节
          </span>
        ) : null}
        <div className={styles.phaseRail}>
          <CollaborationPhaseRail
            phase={snapshot.phase}
            failed={snapshot.status === "failed"}
          />
        </div>
      </section>

      <nav className={styles.tabs} aria-label="协作室视图">
        {(
          [
            ["conclusions", "结论与证据", completeCount],
            ["members", "成员", snapshot.members.length],
            ["process", "过程", activity.length],
          ] as const
        ).map(([id, label, count]) => (
          <button
            key={id}
            type="button"
            className={view === id ? styles.tabActive : ""}
            aria-current={view === id ? "page" : undefined}
            onClick={() => setView(id)}
          >
            {label}
            {count ? <span>{count}</span> : null}
          </button>
        ))}
      </nav>

      <div className={styles.body}>
        {view === "process" ? (
          <section className={styles.processView} aria-live="polite">
            <div className={styles.roomSummary}>
              <Users size={17} aria-hidden="true" />
              <span>
                <strong>{phaseLabel(snapshot)}</strong>
                <em>
                  {completeCount}/{snapshot.members.length} 位已完成
                </em>
              </span>
            </div>
            {activity.length ? (
              <div className={styles.timeline}>
                {activity.map((item) => (
                  <div className={styles.timelineItem} key={item.id}>
                    <span className={styles.timelineMarker}>
                      {item.kind === "decision" ? (
                        <Scale size={13} />
                      ) : (
                        <Circle size={11} />
                      )}
                    </span>
                    <span>
                      <strong>{item.actor}</strong>
                      <small>{item.detail}</small>
                    </span>
                  </div>
                ))}
              </div>
            ) : (
              <div className={styles.emptyState}>
                <Clock3 size={18} />
                <strong>正在建立协作记录</strong>
                <span>关键决策和成员提交会出现在这里。</span>
              </div>
            )}
          </section>
        ) : view === "conclusions" ? (
          <section className={styles.conclusionView}>
            {snapshot.closure ? (
              <div
                className={styles.closure}
                data-ready={
                  snapshot.closure.synthesis_ready ||
                  snapshot.closure.deliverable_ready
                    ? "true"
                    : "false"
                }
              >
                {snapshot.closure.synthesis_ready ||
                snapshot.closure.deliverable_ready ? (
                  <CheckCircle2 size={17} />
                ) : (
                  <AlertCircle size={17} />
                )}
                <span>
                  <strong>{snapshot.closure.summary}</strong>
                  <em>结局：{closureOutcomeLabel(snapshot)}</em>
                </span>
              </div>
            ) : null}
            {(snapshot.host_decisions || []).length ? (
              <div className={styles.section}>
                <strong>沈行知当前判断</strong>
                {(snapshot.host_decisions || []).slice(-3).map((decision) => (
                  <article key={decision.decision_id}>
                    <span>{decision.safe_summary}</span>
                    <em>{decision.reason}</em>
                  </article>
                ))}
              </div>
            ) : null}
            <div className={styles.section}>
              <strong>各席已发现</strong>
              {snapshot.members.map((member) => (
                <article key={member.member_id}>
                  <strong>
                    {member.display_name} · {member.label}
                  </strong>
                  <span>
                    {member.result ||
                      (member.status === "failed"
                        ? member.error_message || "本席未形成可验收结果。"
                        : memberProgressLabel(member))}
                  </span>
                  <em>{memberProgressLabel(member)}</em>
                </article>
              ))}
            </div>
            <div className={styles.section}>
              <strong>验收条件</strong>
              {(snapshot.acceptance_criteria || []).map((item) => {
                const met = snapshot.closure?.acceptance_met.includes(item);
                return (
                  <span key={item} data-met={met ? "true" : "false"}>
                    {met ? <CheckCircle2 size={13} /> : <Circle size={13} />}
                    {item}
                  </span>
                );
              })}
            </div>
            {(snapshot.conflicts || []).length ? (
              <div className={styles.section}>
                <strong>冲突与证据缺口</strong>
                {(snapshot.conflicts || []).map((conflict) => (
                  <article key={conflict.conflict_id}>
                    <span>{conflict.safe_summary}</span>
                    <em>
                      {conflict.status === "resolved"
                        ? "已解决"
                        : "待在交付中标明"}
                    </em>
                  </article>
                ))}
              </div>
            ) : (
              <div className={styles.emptyState}>
                <Scale size={18} />
                <strong>当前没有记录到开放冲突</strong>
                <span>
                  这不等于已证明所有成员达成一致；沈行知仍会核对各席依据后统一判断。
                </span>
              </div>
            )}
          </section>
        ) : (
          <section className={styles.membersView}>
            {snapshot.members.map((member) => (
              <article key={member.member_id}>
                <span className={styles.memberAvatar} aria-hidden="true">
                  <BeverageAgentAvatar agentId={member.agent_id} size={34} />
                </span>
                <span className={styles.memberIdentity}>
                  <strong>{member.display_name}</strong>
                  <em>{member.role || member.label}</em>
                  <small>{member.label}</small>
                  {member.execution_task_id ? (
                    <small>
                      任务 {member.execution_task_id}
                      {member.resource_claims?.length
                        ? ` · 资源 ${member.resource_claims.join("、")}`
                        : ""}
                    </small>
                  ) : null}
                </span>
                <span
                  className={`${styles.memberStatus} ${statusTone(member.status)}`}
                >
                  {memberProgressLabel(member)}
                </span>
              </article>
            ))}
          </section>
        )}
      </div>

      <footer className={styles.footer}>
        <span>
          {isTerminal
            ? snapshot.status === "failed"
              ? "协作未形成结果 · 可追溯"
              : "协作已闭环 · 可追溯"
            : "无需参会 · 直接发新消息可调整方向"}
        </span>
        {!isTerminal && onTakeover ? (
          <button
            type="button"
            className={styles.takeoverButton}
            onClick={onTakeover}
          >
            <Hand size={14} />
            调整方向
          </button>
        ) : null}
      </footer>
    </aside>
  );
}

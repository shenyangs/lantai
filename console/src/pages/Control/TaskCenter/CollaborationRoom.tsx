import { useMemo, useState } from "react";
import { Button } from "antd";
import { CheckCircle2, Circle, GitBranch, Scale, Users } from "lucide-react";

import type { CollaborationBatchSnapshot } from "../../../api/types/chat";
import type { RuntimeRunSummary } from "../../../api/types/runtime";
import { BeverageAgentAvatar } from "../../../components/BeverageAgentAvatar";
import { CapabilityChip } from "../../../components/CapabilityLayout";
import CollaborationPhaseRail from "../../Chat/components/CollaborationPhaseRail";
import type { CollaborationParticipant } from "./collaborationProjection";
import styles from "./CollaborationRoom.module.less";

type RoomTab = "process" | "conclusions" | "members";

function toneForStatus(status: string) {
  const normalized = status.toLowerCase();
  if (["completed", "succeeded"].includes(normalized))
    return "success" as const;
  if (["failed", "stalled", "interrupted", "cancelled"].includes(normalized)) {
    return "danger" as const;
  }
  if (
    ["running", "started", "retrying", "waiting_resource"].includes(normalized)
  )
    return "info" as const;
  return "muted" as const;
}

function labelForStatus(status: string): string {
  return (
    {
      queued: "等待中",
      running: "进行中",
      started: "进行中",
      retrying: "重试中",
      waiting_resource: "等待资源",
      completed: "已完成",
      succeeded: "已完成",
      failed: "失败",
      stalled: "卡住",
      interrupted: "中断",
      cancelled: "取消",
    }[status.toLowerCase()] ||
    status ||
    "已记录"
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

export interface CollaborationRoomProps {
  parentRun: RuntimeRunSummary;
  participants: CollaborationParticipant[];
  hostedSnapshot?: CollaborationBatchSnapshot;
  availableRuns: RuntimeRunSummary[];
  onOpenRun: (run: RuntimeRunSummary) => void;
  onShowBoard: () => void;
}

export default function CollaborationRoom({
  parentRun,
  participants,
  hostedSnapshot,
  availableRuns,
  onOpenRun,
  onShowBoard,
}: CollaborationRoomProps) {
  const [tab, setTab] = useState<RoomTab>("process");
  const availableById = new Map(availableRuns.map((run) => [run.id, run]));
  const participantByAgent = useMemo(
    () =>
      new Map(
        participants.map((participant) => [participant.agentName, participant]),
      ),
    [participants],
  );
  const members = useMemo(() => {
    if (!hostedSnapshot) return participants;
    return hostedSnapshot.members.map((member) => {
      const detail = participantByAgent.get(member.display_name);
      return {
        id: member.member_id,
        runId: detail?.runId || null,
        agentName: member.display_name,
        agentId: member.agent_id,
        role: member.role,
        delegationReason: member.label,
        permissionProfile: member.permission_profile || "read_only",
        executionMode: member.execution_mode || "",
        workspaceScope: member.workspace_scope || "",
        allowedTools: member.allowed_tools || [],
        executionTaskId: member.execution_task_id || "",
        resourceClaims: member.resource_claims || [],
        resourceLeases: member.resource_leases || [],
        artifacts: member.artifacts || [],
        contextSummary: detail?.contextSummary || "",
        currentTool: detail?.currentTool || "",
        status: member.status,
        resultSummary: detail?.resultSummary || "",
        failureReason: detail?.failureReason || member.error_code || "",
        eventCount: detail?.eventCount || 0,
        updatedAt: detail?.updatedAt || "",
      };
    });
  }, [hostedSnapshot, participants, participantByAgent]);
  const activeCount = members.filter((participant) =>
    ["running", "started", "retrying", "waiting_resource"].includes(
      participant.status.toLowerCase(),
    ),
  ).length;
  const hostName = hostedSnapshot?.host?.display_name || "沈行知";
  const activity = [
    ...(hostedSnapshot?.host_decisions || []).map((item) => ({
      id: item.decision_id,
      actor: hostName,
      summary: item.safe_summary,
      decision: true,
    })),
    ...(hostedSnapshot?.messages || []).map((item) => ({
      id: item.message_id,
      actor: item.display_name,
      summary: item.safe_summary,
      decision: false,
    })),
  ].slice(-16);

  return (
    <section className={styles.room} aria-label="协作室">
      <header className={styles.header}>
        <span className={styles.headerIcon}>
          <Users size={17} />
        </span>
        <span className={styles.headerCopy}>
          <strong>协作室 · {hostName}主持</strong>
          <em>
            {members.length} 位协作者
            {activeCount ? ` · ${activeCount} 位进行中` : ""}
            {hostedSnapshot?.unattended ? " · 无需参会" : ""}
          </em>
        </span>
        <Button size="small" type="text" onClick={onShowBoard}>
          查看任务看板
        </Button>
      </header>
      <div className={styles.parentLine}>
        <GitBranch size={14} />
        <span>主运行</span>
        <strong>{parentRun.title || parentRun.display_id}</strong>
        {hostedSnapshot?.quiet_summary ? (
          <em>{hostedSnapshot.quiet_summary}</em>
        ) : null}
      </div>
      {hostedSnapshot?.budget && hostedSnapshot.budget_usage ? (
        <div className={styles.parentLine}>
          <Scale size={14} />
          <span>团队预算</span>
          <strong>
            尝试 {hostedSnapshot.budget_usage.member_attempts_started}/
            {hostedSnapshot.budget.max_member_attempts}
          </strong>
          <em>
            已验收 {hostedSnapshot.budget_usage.verified_artifact_bytes} /{" "}
            {hostedSnapshot.budget.max_artifact_bytes} 字节
          </em>
        </div>
      ) : null}
      {hostedSnapshot ? (
        <div className={styles.phaseRail}>
          <CollaborationPhaseRail
            phase={hostedSnapshot.phase}
            compact
            failed={hostedSnapshot.status === "failed"}
          />
        </div>
      ) : null}

      <nav className={styles.tabs} aria-label="协作室详情">
        {(
          [
            ["process", "动态"],
            ["conclusions", "结论与证据"],
            ["members", "成员"],
          ] as const
        ).map(([id, label]) => (
          <button
            key={id}
            type="button"
            className={tab === id ? styles.tabActive : ""}
            aria-current={tab === id ? "page" : undefined}
            onClick={() => setTab(id)}
          >
            {label}
          </button>
        ))}
      </nav>

      {tab === "process" ? (
        <div className={styles.timeline}>
          {activity.length ? (
            activity.map((item) => (
              <article key={item.id}>
                <span>
                  {item.decision ? <Scale size={13} /> : <Circle size={11} />}
                </span>
                <div>
                  <strong>{item.actor}</strong>
                  <p>{item.summary}</p>
                </div>
              </article>
            ))
          ) : (
            <div className={styles.emptyLine}>
              协作过程会随 RuntimeEvent 继续投影到这里。
            </div>
          )}
        </div>
      ) : tab === "conclusions" ? (
        <div className={styles.conclusions}>
          {hostedSnapshot?.closure ? (
            <div
              className={styles.closure}
              data-ready={
                hostedSnapshot.closure.synthesis_ready ||
                hostedSnapshot.closure.deliverable_ready
                  ? "true"
                  : "false"
              }
            >
              <CheckCircle2 size={15} />
              <span>
                <strong>{hostedSnapshot.closure.summary}</strong>
                <em>{closureOutcomeLabel(hostedSnapshot)}</em>
              </span>
            </div>
          ) : null}
          <div className={styles.criteria}>
            <strong>验收条件</strong>
            {(hostedSnapshot?.acceptance_criteria || []).map((criterion) => (
              <span key={criterion}>
                {hostedSnapshot?.closure?.acceptance_met.includes(criterion) ? (
                  <CheckCircle2 size={13} />
                ) : (
                  <Circle size={13} />
                )}
                {criterion}
              </span>
            ))}
          </div>
          {(hostedSnapshot?.conflicts || []).map((conflict) => (
            <article className={styles.conflict} key={conflict.conflict_id}>
              <strong>
                {conflict.status === "resolved" ? "已解决" : "待标明"}
              </strong>
              <span>{conflict.safe_summary}</span>
            </article>
          ))}
        </div>
      ) : (
        <div className={styles.participants}>
          {members.map((participant) => {
            const childRun = participant.runId
              ? availableById.get(participant.runId)
              : undefined;
            const agentId =
              "agentId" in participant ? String(participant.agentId || "") : "";
            return (
              <article key={participant.id} className={styles.participant}>
                <div className={styles.participantHeader}>
                  <span className={styles.participantIdentity}>
                    {agentId ? (
                      <BeverageAgentAvatar agentId={agentId} size={30} />
                    ) : null}
                    <span>
                      <strong>{participant.agentName}</strong>
                      {participant.role ? <em>{participant.role}</em> : null}
                    </span>
                  </span>
                  <CapabilityChip tone={toneForStatus(participant.status)}>
                    {labelForStatus(participant.status)}
                  </CapabilityChip>
                </div>
                {participant.delegationReason ? (
                  <p>{participant.delegationReason}</p>
                ) : null}
                <div className={styles.participantMeta}>
                  {participant.currentTool ? (
                    <span>当前工具：{participant.currentTool}</span>
                  ) : null}
                  <span>
                    权限：{participant.permissionProfile || "read_only"}
                  </span>
                  {"workspaceScope" in participant &&
                  participant.workspaceScope ? (
                    <span>工作区：{participant.workspaceScope}</span>
                  ) : null}
                  {"allowedTools" in participant &&
                  participant.allowedTools?.length ? (
                    <span>工具：{participant.allowedTools.join("、")}</span>
                  ) : null}
                  {"executionTaskId" in participant &&
                  participant.executionTaskId ? (
                    <span>执行任务：{participant.executionTaskId}</span>
                  ) : null}
                  {"resourceClaims" in participant &&
                  participant.resourceClaims?.length ? (
                    <span>
                      独占资源：{participant.resourceClaims.join("、")}
                    </span>
                  ) : null}
                  {"resourceLeases" in participant &&
                  participant.resourceLeases?.length ? (
                    <span>
                      当前租约：
                      {participant.resourceLeases
                        .map((lease) => `${lease.resource}#${lease.generation}`)
                        .join("、")}
                    </span>
                  ) : null}
                  {"artifacts" in participant &&
                  participant.artifacts?.length ? (
                    <span>已验收产物：{participant.artifacts.length} 个</span>
                  ) : null}
                  {participant.eventCount ? (
                    <span>{participant.eventCount} 条事件</span>
                  ) : null}
                </div>
                {participant.failureReason ? (
                  <div className={styles.failure}>
                    失败：{participant.failureReason}
                  </div>
                ) : participant.resultSummary ? (
                  <div className={styles.result}>
                    结果：{participant.resultSummary}
                  </div>
                ) : null}
                <div className={styles.participantActions}>
                  {childRun ? (
                    <Button
                      size="small"
                      type="text"
                      onClick={() => onOpenRun(childRun)}
                    >
                      打开子运行
                    </Button>
                  ) : (
                    <span>
                      {"executionMode" in participant &&
                      participant.executionMode === "scoped_execute"
                        ? "隔离执行成员·由主持人统一验收交付"
                        : "只读成员·由主持人统一交付"}
                    </span>
                  )}
                </div>
              </article>
            );
          })}
        </div>
      )}
    </section>
  );
}

import {
  Alert,
  Button,
  Descriptions,
  Drawer,
  Empty,
  Popconfirm,
  Skeleton,
  Timeline,
} from "antd";
import {
  MessageSquareText,
  Play,
  RotateCcw,
  SlidersHorizontal,
  ShieldCheck,
  ShieldOff,
  Square,
} from "lucide-react";
import dayjs from "dayjs";
import { useEffect, useState } from "react";

import { runtimeApi } from "../../../api/modules/runtime";
import type {
  RuntimeAutonomyGrantRequest,
  RuntimeEvent,
  RuntimeTaskBoardItem,
} from "../../../api/types/runtime";
import AutonomyGrantModal from "./AutonomyGrantModal";
import {
  taskDeliveryLabel,
  taskSourceLabel,
  taskStatusDescription,
  taskStatusLabel,
} from "./taskBoardModel";
import styles from "./index.module.less";

interface TaskDetailsDrawerProps {
  item: RuntimeTaskBoardItem | null;
  busy: boolean;
  onClose: () => void;
  onStart: (item: RuntimeTaskBoardItem) => void;
  onInterrupt: (item: RuntimeTaskBoardItem) => void;
  onContinue: (item: RuntimeTaskBoardItem) => void;
  onOpenChat: (item: RuntimeTaskBoardItem) => void;
  onOpenAdvanced: (item: RuntimeTaskBoardItem) => void;
  onGrantAutonomy: (
    item: RuntimeTaskBoardItem,
    request: RuntimeAutonomyGrantRequest,
  ) => Promise<boolean>;
  onRevokeAutonomy: (item: RuntimeTaskBoardItem) => Promise<boolean>;
}

const TASK_EVENT_PAGE_SIZE = 160;
const MAX_TASK_EVENT_PAGES = 100;

export async function loadAllTaskEvents(
  item: Pick<RuntimeTaskBoardItem, "id" | "attempt_id">,
): Promise<RuntimeEvent[]> {
  const allEvents: RuntimeEvent[] = [];
  const seen = new Set<string>();
  let afterSeq = 0;

  for (let page = 0; page < MAX_TASK_EVENT_PAGES; page += 1) {
    const response = await runtimeApi.getRunEvents(item.id, afterSeq, {
      limit: TASK_EVENT_PAGE_SIZE,
      attemptId: item.attempt_id || undefined,
    });
    for (const event of response.events || []) {
      const key = `${event.seq ?? ""}:${event.id}`;
      if (seen.has(key)) continue;
      seen.add(key);
      allEvents.push(event);
    }
    if (!response.has_more) return allEvents;
    const nextAfterSeq = Number(response.next_after_seq || 0);
    if (!Number.isFinite(nextAfterSeq) || nextAfterSeq <= afterSeq) {
      throw new Error("运行记录分页位置没有前进，无法确认记录完整性。");
    }
    afterSeq = nextAfterSeq;
  }

  throw new Error("运行记录超过当前可安全读取的页数，尚未完整加载。");
}

function eventTitle(event: RuntimeEvent) {
  const payload = event.payload || {};
  return String(
    payload.trace_title || payload.summary || event.type || "运行事件",
  );
}

function reportTitle(kind: string) {
  if (kind === "deadline_snapshot") return "截至截止时间";
  if (kind === "terminal") return "任务报告";
  return "进度快照";
}

function formatMicroCny(value: number | null | undefined) {
  if (value === null || value === undefined) return "未设置";
  return `¥${(value / 1_000_000).toFixed(6)}`;
}

function formatDuration(value: number | null | undefined) {
  if (value === null || value === undefined) return "未知";
  if (value < 60) return `${Math.round(value)} 秒`;
  if (value < 3_600) return `${(value / 60).toFixed(1)} 分钟`;
  return `${(value / 3_600).toFixed(1)} 小时`;
}

export default function TaskDetailsDrawer({
  item,
  busy,
  onClose,
  onStart,
  onInterrupt,
  onContinue,
  onOpenChat,
  onOpenAdvanced,
  onGrantAutonomy,
  onRevokeAutonomy,
}: TaskDetailsDrawerProps) {
  const [events, setEvents] = useState<RuntimeEvent[]>([]);
  const [loadingEvents, setLoadingEvents] = useState(false);
  const [eventsError, setEventsError] = useState("");
  const [eventsReloadKey, setEventsReloadKey] = useState(0);
  const [grantOpen, setGrantOpen] = useState(false);

  const grant = item?.autonomy_grant;
  const grantActive = Boolean(
    grant &&
      grant.revocation_generation === 0 &&
      dayjs(grant.expires_at).isAfter(dayjs()),
  );
  const canManageAutonomy = Boolean(
    item?.source === "chat" &&
      item.chat_id &&
      item.workspace_dir &&
      item.authority_status !== "stop_requested",
  );

  useEffect(() => {
    if (!item) {
      setEvents([]);
      setEventsError("");
      return;
    }
    setEvents([]);
    setEventsError("");
    let cancelled = false;
    let timer = 0;
    const load = async () => {
      setLoadingEvents((current) => current || events.length === 0);
      try {
        const nextEvents = await loadAllTaskEvents(item);
        if (!cancelled) {
          setEvents(nextEvents);
          setEventsError("");
        }
      } catch (error) {
        if (!cancelled) {
          const reason = error instanceof Error ? error.message : "";
          setEventsError(
            reason.includes("完整")
              ? reason
              : "运行记录读取失败，任务本身没有被修改。",
          );
        }
      } finally {
        if (!cancelled) {
          setLoadingEvents(false);
          if (["active", "needs_user"].includes(item.lane)) {
            timer = window.setTimeout(load, 3_000);
          }
        }
      }
    };
    void load();
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
    // Events are reset and reloaded when the selected authoritative item changes.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [eventsReloadKey, item?.id, item?.attempt_id, item?.lane]);

  return (
    <Drawer
      title={item?.title || "任务详情"}
      width={620}
      open={Boolean(item)}
      onClose={onClose}
      extra={
        item ? (
          <div className={styles.drawerActions}>
            {item.can_start ? (
              <Button
                type="primary"
                icon={<Play size={15} />}
                loading={busy}
                onClick={() => onStart(item)}
              >
                开始
              </Button>
            ) : null}
            {item.can_interrupt &&
            item.authority_status !== "stop_requested" ? (
              <Button
                danger
                icon={<Square size={14} />}
                loading={busy}
                onClick={() => onInterrupt(item)}
              >
                停止
              </Button>
            ) : null}
            {item.can_resume || item.can_retry ? (
              <Button
                icon={<RotateCcw size={15} />}
                loading={busy}
                onClick={() => onContinue(item)}
              >
                继续
              </Button>
            ) : null}
            {item.can_open_chat ? (
              <Button
                icon={<MessageSquareText size={15} />}
                onClick={() => onOpenChat(item)}
              >
                打开会话
              </Button>
            ) : null}
            <Button
              icon={<SlidersHorizontal size={15} />}
              onClick={() => onOpenAdvanced(item)}
            >
              高级控制
            </Button>
          </div>
        ) : null
      }
    >
      {item ? (
        <div className={styles.drawerBody}>
          <section className={styles.taskOutcome} data-lane={item.lane}>
            <span>{taskStatusLabel(item)}</span>
            <strong>{taskStatusDescription(item)}</strong>
            <small>{taskDeliveryLabel(item)}</small>
          </section>
          {item.next_action ? (
            <section className={styles.actionCallout}>
              <span>建议下一步</span>
              <strong>{item.next_action}</strong>
            </section>
          ) : null}
          <Descriptions column={2} size="small" bordered>
            <Descriptions.Item label="状态">
              {taskStatusLabel(item)}
            </Descriptions.Item>
            <Descriptions.Item label="来源">
              {taskSourceLabel(item.source)}
            </Descriptions.Item>
            <Descriptions.Item label="结果" span={2}>
              {taskDeliveryLabel(item)}
            </Descriptions.Item>
            <Descriptions.Item label="最近更新" span={2}>
              {dayjs(item.updated_at).isValid()
                ? dayjs(item.updated_at).format("YYYY-MM-DD HH:mm:ss")
                : "—"}
            </Descriptions.Item>
          </Descriptions>

          <section className={styles.detailSection}>
            <header>
              <strong>当前摘要</strong>
            </header>
            <p>{item.summary || "暂无摘要"}</p>
          </section>

          {item.progress_report ? (
            <section className={styles.progressReport}>
              <header>
                <div>
                  <strong>{reportTitle(item.progress_report.kind)}</strong>
                  <span>
                    {dayjs(item.progress_report.generated_at).isValid()
                      ? dayjs(item.progress_report.generated_at).format(
                          "YYYY-MM-DD HH:mm:ss",
                        )
                      : "生成时间未知"}
                  </span>
                </div>
                <b data-terminal={item.progress_report.terminal}>
                  {item.progress_report.terminal ? "已结束" : "仍可继续"}
                </b>
              </header>

              {item.progress_report.narrative_status === "generated" &&
              item.progress_report.narrative ? (
                <p>{item.progress_report.narrative}</p>
              ) : null}
              {item.progress_report.narrative_status === "failed" ? (
                <Alert
                  type="warning"
                  showIcon
                  message="文字说明生成失败，结构化事实仍然有效"
                />
              ) : null}

              <dl className={styles.progressFacts}>
                <div>
                  <dt>运行状态</dt>
                  <dd>
                    {item.progress_report.status}
                    {item.progress_report.wait_reason
                      ? ` · 等待 ${item.progress_report.wait_reason}`
                      : ""}
                  </dd>
                </div>
                <div>
                  <dt>耗时</dt>
                  <dd>
                    总计 {formatDuration(item.progress_report.elapsed_seconds)}；
                    活跃 {formatDuration(item.progress_report.active_seconds)}；等待{" "}
                    {formatDuration(item.progress_report.wait_seconds)}
                  </dd>
                </div>
                <div>
                  <dt>费用</dt>
                  <dd>
                    已结算{" "}
                    {formatMicroCny(
                      item.progress_report.cost_summary.committed_micro_cny,
                    )}
                    ；预留{" "}
                    {formatMicroCny(
                      item.progress_report.cost_summary.reserved_micro_cny,
                    )}
                    ；上限{" "}
                    {formatMicroCny(
                      item.progress_report.cost_summary.limit_micro_cny,
                    )}
                  </dd>
                </div>
                <div>
                  <dt>调用账目</dt>
                  <dd>
                    已结算 {item.progress_report.cost_summary.settled_count}；待定{" "}
                    {item.progress_report.cost_summary.pending_count}；不明确{" "}
                    {item.progress_report.cost_summary.ambiguous_count}；超限{" "}
                    {item.progress_report.cost_summary.overrun_count}
                  </dd>
                </div>
                <div>
                  <dt>副作用</dt>
                  <dd>
                    未解决{" "}
                    {item.progress_report.effect_summary.unresolved_effect_ids
                      .length
                      ? item.progress_report.effect_summary.unresolved_effect_ids.join(
                          "、",
                        )
                      : "无"}
                    ；不明确{" "}
                    {item.progress_report.effect_summary.ambiguous_effect_ids
                      .length
                      ? item.progress_report.effect_summary.ambiguous_effect_ids.join(
                          "、",
                        )
                      : "无"}
                  </dd>
                </div>
              </dl>

              <div className={styles.progressLists}>
                <div>
                  <strong>已完成里程碑</strong>
                  {item.progress_report.completed_milestones.length ? (
                    <ul>
                      {item.progress_report.completed_milestones.map(
                        (milestone) => (
                          <li key={milestone.milestone_id}>
                            {milestone.statement}
                          </li>
                        ),
                      )}
                    </ul>
                  ) : (
                    <span>未记录</span>
                  )}
                </div>
                <div>
                  <strong>待验收</strong>
                  {item.progress_report.open_acceptance_items.length ? (
                    <ul>
                      {item.progress_report.open_acceptance_items.map((entry) => (
                        <li key={entry}>{entry}</li>
                      ))}
                    </ul>
                  ) : (
                    <span>无</span>
                  )}
                </div>
                <div>
                  <strong>产物引用</strong>
                  {item.progress_report.artifact_refs.length ? (
                    <ul>
                      {item.progress_report.artifact_refs.map((entry) => (
                        <li key={entry}>{entry}</li>
                      ))}
                    </ul>
                  ) : (
                    <span>无</span>
                  )}
                </div>
                <div>
                  <strong>阻塞与决策</strong>
                  {item.progress_report.blocked_reasons.length ||
                  item.progress_report.user_decisions.length ? (
                    <ul>
                      {item.progress_report.blocked_reasons.map((entry) => (
                        <li key={`blocked:${entry}`}>阻塞：{entry}</li>
                      ))}
                      {item.progress_report.user_decisions.map((entry) => (
                        <li key={`decision:${entry}`}>用户决策：{entry}</li>
                      ))}
                    </ul>
                  ) : (
                    <span>无</span>
                  )}
                </div>
                {item.progress_report.unknown_facts.length ? (
                  <div>
                    <strong>当前无法确认</strong>
                    <ul>
                      {item.progress_report.unknown_facts.map((entry) => (
                        <li key={entry}>{entry}</li>
                      ))}
                    </ul>
                  </div>
                ) : null}
              </div>
            </section>
          ) : null}

          <section className={styles.autonomySection}>
            <header>
              <div>
                <strong>后台续跑授权</strong>
                <span>
                  {grantActive
                    ? `已授权，${dayjs(grant?.expires_at).format("MM/DD HH:mm")} 失效`
                    : grant?.revocation_generation
                      ? "已撤销"
                      : grant
                        ? "已过期"
                        : "未授权"}
                </span>
              </div>
              {grantActive ? (
                <Popconfirm
                  title="撤销后台续跑授权？"
                  description="撤销后，后续唤醒和外部操作会立即被拒绝。"
                  okText="撤销授权"
                  cancelText="保留"
                  okButtonProps={{ danger: true, loading: busy }}
                  onConfirm={() => onRevokeAutonomy(item)}
                >
                  <Button danger icon={<ShieldOff size={15} />} loading={busy}>
                    撤销授权
                  </Button>
                </Popconfirm>
              ) : (
                <Button
                  icon={<ShieldCheck size={15} />}
                  disabled={!canManageAutonomy}
                  onClick={() => setGrantOpen(true)}
                >
                  授权后台续跑
                </Button>
              )}
            </header>
            {grantActive && grant ? (
              <dl>
                <div>
                  <dt>任务范围</dt>
                  <dd>{grant.run_id}</dd>
                </div>
                <div>
                  <dt>动作</dt>
                  <dd>{grant.allowed_operations.join("、")}</dd>
                </div>
                <div>
                  <dt>文件范围</dt>
                  <dd>{grant.allowed_paths.join("、") || "无"}</dd>
                </div>
                <div>
                  <dt>模型范围</dt>
                  <dd>
                    {grant.allowed_provider_ids.length
                      ? `${grant.allowed_provider_ids.join(", ")} / ${grant.allowed_model_ids.join(", ")}`
                      : "无"}
                  </dd>
                </div>
                <div>
                  <dt>人民币上限</dt>
                  <dd>
                    {grant.max_cost_micro_cny === null ||
                    grant.max_cost_micro_cny === undefined
                      ? "无模型调用授权"
                      : `¥${(grant.max_cost_micro_cny / 1_000_000).toFixed(6)}`}
                  </dd>
                </div>
                <div>
                  <dt>能力预检</dt>
                  <dd>
                    {grant.preflight_checks?.length
                      ? grant.preflight_checks
                          .map((check) => check.message)
                          .join("；")
                      : "未记录（旧授权）"}
                  </dd>
                </div>
              </dl>
            ) : (
              <p>
                {canManageAutonomy
                  ? "默认不允许无人值守续跑。授权时需明确任务、动作、路径、模型、费用和有效期。"
                  : "当前任务缺少会话或工作区绑定，不能开启后台续跑。"}
              </p>
            )}
          </section>

          <details className={styles.taskTechnicalDetails}>
            <summary>技术信息</summary>
            <dl>
              <div>
                <dt>Run ID</dt>
                <dd>
                  <code>{item.id}</code>
                </dd>
              </div>
              <div>
                <dt>运行状态</dt>
                <dd>{item.authority_status}</dd>
              </div>
              <div>
                <dt>交付状态</dt>
                <dd>{item.delivery_status}</dd>
              </div>
              <div>
                <dt>原始事件</dt>
                <dd>{events.length} 条</dd>
              </div>
            </dl>
          </details>

          <section className={styles.detailSection}>
            <header>
              <strong>运行时间线</strong>
              <span>{events.length} 条事件</span>
            </header>
            {loadingEvents ? (
              <Skeleton active paragraph={{ rows: 5 }} />
            ) : eventsError ? (
              <Alert
                type="error"
                showIcon
                message="运行记录暂时无法读取"
                description={eventsError}
                action={
                  <Button
                    size="small"
                    aria-label="重试读取运行记录"
                    onClick={() => setEventsReloadKey((value) => value + 1)}
                  >
                    重试
                  </Button>
                }
              />
            ) : events.length ? (
              <Timeline
                items={events.map((event) => ({
                  color:
                    event.status === "failed"
                      ? "red"
                      : event.status === "running"
                        ? "blue"
                        : "gray",
                  children: (
                    <div className={styles.timelineItem}>
                      <strong>{eventTitle(event)}</strong>
                      <span>
                        {event.created_at
                          ? dayjs(event.created_at).format("MM/DD HH:mm:ss")
                          : event.type}
                      </span>
                    </div>
                  ),
                }))}
              />
            ) : (
              <Empty
                image={Empty.PRESENTED_IMAGE_SIMPLE}
                description="任务尚未产生运行事件"
              />
            )}
          </section>
        </div>
      ) : null}
      <AutonomyGrantModal
        item={item}
        open={grantOpen}
        busy={busy}
        onCancel={() => setGrantOpen(false)}
        onSubmit={onGrantAutonomy}
      />
    </Drawer>
  );
}

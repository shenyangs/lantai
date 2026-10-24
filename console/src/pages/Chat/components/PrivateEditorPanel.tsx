import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Check,
  ChevronLeft,
  Download,
  FileClock,
  FilePenLine,
  RefreshCw,
  RotateCcw,
  Trash2,
  X,
} from "lucide-react";

import { privateEditorApi } from "../../../api/modules/privateEditor";
import type {
  PrivateEditorCandidate,
  PrivateEditorAngleProposal,
  PrivateEditorCurrentBody,
  PrivateEditorProject,
  PrivateEditorRecordEntry,
} from "../../../api/types/privateEditor";
import styles from "./PrivateEditorPanel.module.less";

type PanelView = "current" | "candidate" | "records" | "angles";

interface PrivateEditorPanelProps {
  projectId: string;
  onClose?: () => void;
  onRequestNewAngle?: () => void;
  onDevelopAngle?: (proposal: PrivateEditorAngleProposal) => void;
}

const recordSections: Array<{
  title: string;
  test: (entry: PrivateEditorRecordEntry) => boolean;
}> = [
  {
    title: "作者已说明",
    test: (entry) =>
      entry.source === "user_explicit" &&
      entry.status !== "superseded" &&
      entry.kind !== "decision" &&
      entry.supersedes.length === 0,
  },
  {
    title: "我的理解待确认",
    test: (entry) =>
      entry.source === "model_inferred" &&
      entry.kind !== "open_question" &&
      entry.status === "proposed",
  },
  {
    title: "已经取舍",
    test: (entry) =>
      entry.source === "user_explicit" &&
      ["accepted", "rejected"].includes(entry.status) &&
      (entry.kind === "decision" || entry.supersedes.length > 0),
  },
  {
    title: "未解决",
    test: (entry) =>
      entry.kind === "open_question" && ["open", "proposed"].includes(entry.status),
  },
  {
    title: "材料与来源",
    test: (entry) =>
      entry.kind === "material_note" && entry.status !== "superseded",
  },
];

function operationId(prefix: string): string {
  const id = globalThis.crypto?.randomUUID?.() ?? `${Date.now()}`;
  return `${prefix}-${id}`;
}

export default function PrivateEditorPanel({
  projectId,
  onClose,
  onRequestNewAngle,
  onDevelopAngle,
}: PrivateEditorPanelProps) {
  const [project, setProject] = useState<PrivateEditorProject | null>(null);
  const [currentBody, setCurrentBody] = useState<PrivateEditorCurrentBody | null>(
    null,
  );
  const [view, setView] = useState<PanelView>("candidate");
  const [selectedGroups, setSelectedGroups] = useState<Set<string>>(new Set());
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [confirmRevert, setConfirmRevert] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [researchConsent, setResearchConsent] = useState(false);

  const refresh = useCallback(async () => {
    setError(null);
    try {
      const [nextProject, nextBody, settings] = await Promise.all([
        privateEditorApi.getProject(projectId),
        privateEditorApi.getCurrentBody(projectId),
        privateEditorApi.getSettings(),
      ]);
      setProject(nextProject);
      setCurrentBody(nextBody);
      setResearchConsent(settings.research_consent);
      setSelectedGroups(new Set());
    } catch (loadError) {
      setError(loadError instanceof Error ? loadError.message : "读取作品失败");
    }
  }, [projectId]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const stagedCandidates = useMemo(
    () => project?.candidates.filter((candidate) => candidate.status === "staged") ?? [],
    [project?.candidates],
  );
  const activeCandidate = stagedCandidates[stagedCandidates.length - 1] ?? null;
  const previousWorkId = project?.history[project.history.length - 1]?.from_work_id ?? null;
  const mutable = project?.status === "active";

  const runMutation = useCallback(
    async (action: () => Promise<unknown>, success: string) => {
      setBusy(true);
      setError(null);
      setNotice(null);
      try {
        await action();
        setNotice(success);
        await refresh();
        return true;
      } catch (mutationError) {
        setError(
          mutationError instanceof Error ? mutationError.message : "操作未完成",
        );
        return false;
      } finally {
        setBusy(false);
      }
    },
    [refresh],
  );

  const toggleGroup = (groupId: string) => {
    setSelectedGroups((current) => {
      const next = new Set(current);
      if (next.has(groupId)) next.delete(groupId);
      else next.add(groupId);
      return next;
    });
  };

  const adopt = (candidate: PrivateEditorCandidate) =>
    runMutation(
      () =>
        privateEditorApi.adoptCandidate(projectId, candidate.id, {
          operation_id: operationId("adopt"),
          candidate_id: candidate.id,
          expected_base_work_id: candidate.base_work_id,
          expected_base_digest: candidate.base_digest,
          expected_record_revision: candidate.record_revision,
          revocation_epoch: candidate.revocation_epoch,
          selected_change_groups: candidate.change_groups
            .filter((group) => selectedGroups.has(group.id))
            .map((group) => group.id),
        }),
      "已保存为新版本，原版本仍可恢复。",
    );

  const captureWorkingCopy = () => {
    if (!project) return Promise.resolve();
    return runMutation(
      () =>
        privateEditorApi.captureWorkingCopy(projectId, {
          expected_base_work_id: project.current_work_id,
          expected_base_digest: project.current_digest,
          expected_record_revision: project.record_revision,
          expected_revocation_epoch: project.revocation_epoch,
        }),
      "工作副本已生成候选，当前稿未变。",
    );
  };

  const resolveRecord = (
    entry: PrivateEditorRecordEntry,
    resolution: "accepted" | "rejected",
  ) => {
    if (!project) return Promise.resolve();
    return runMutation(
      () =>
        privateEditorApi.resolveRecord(projectId, entry.id, {
          expected_current_work_id: project.current_work_id,
          expected_current_digest: project.current_digest,
          expected_record_revision: project.record_revision,
          expected_revocation_epoch: project.revocation_epoch,
          resolution,
        }),
      resolution === "accepted"
        ? "已记录作者确认。"
        : "已记录不采用；后续不会把它当成有效共识。",
    );
  };

  const resolveAngle = async (
    proposal: PrivateEditorAngleProposal,
    resolution: "continue" | "later" | "not_suitable" | "delete",
  ) => {
    if (!project) return;
    const success = await runMutation(
      () =>
        privateEditorApi.resolveAngleProposal(projectId, proposal.id, {
          expected_current_work_id: project.current_work_id,
          expected_current_digest: project.current_digest,
          expected_record_revision: project.record_revision,
          expected_revocation_epoch: project.revocation_epoch,
          resolution,
        }),
      resolution === "continue"
        ? "已记录继续发展；当前稿仍未变。"
          : resolution === "later"
          ? "已记录稍后处理，不会催促。"
          : resolution === "not_suitable"
            ? "已记录本次不适合。"
            : "已删除该提案，原稿未变。",
    );
    if (success && resolution === "continue") onDevelopAngle?.(proposal);
  };

  const invalidateSource = (sourceRef: string) => {
    if (!project) return Promise.resolve(false);
    return runMutation(
      () =>
        privateEditorApi.invalidateSource(projectId, {
          expected_current_work_id: project.current_work_id,
          expected_current_digest: project.current_digest,
          expected_record_revision: project.record_revision,
          expected_revocation_epoch: project.revocation_epoch,
          source_ref: sourceRef,
        }),
      "来源已移除；相关候选、提案与派生索引已失效。",
    );
  };

  const revert = () => {
    if (!project || !previousWorkId) return Promise.resolve();
    return runMutation(
      () =>
        privateEditorApi.revertProject(projectId, {
          operation_id: operationId("revert"),
          expected_current_work_id: project.current_work_id,
          expected_current_digest: project.current_digest,
          expected_record_revision: project.record_revision,
          revocation_epoch: project.revocation_epoch,
          target_work_id: previousWorkId,
        }),
      "已恢复到上一个已保存版本。",
    ).finally(() => setConfirmRevert(false));
  };

  const deleteProject = async () => {
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      await privateEditorApi.deleteProject(projectId);
      setConfirmDelete(false);
      setNotice("实验项目已删除；已保存 Works 版本与聊天记录仍保留。");
      onClose?.();
    } catch (deleteError) {
      setError(deleteError instanceof Error ? deleteError.message : "删除未完成");
    } finally {
      setBusy(false);
    }
  };

  return (
    <aside className={styles.panel} aria-label="私人编辑作品面板">
      <header className={styles.header}>
        <div>
          <span className={styles.eyebrow}>私人编辑 · 实验</span>
          <strong>{project?.title ?? "正在读取作品"}</strong>
        </div>
        {onClose ? (
          <button type="button" className={styles.iconButton} onClick={onClose} aria-label="收起作品面板">
            <X size={17} />
          </button>
        ) : null}
      </header>

      <nav className={styles.tabs} aria-label="作品视图">
        <button type="button" data-active={view === "current"} onClick={() => setView("current")}>
          当前稿
        </button>
        <button type="button" data-active={view === "candidate"} onClick={() => setView("candidate")}>
          候选{stagedCandidates.length ? ` ${stagedCandidates.length}` : ""}
        </button>
        <button type="button" data-active={view === "records"} onClick={() => setView("records")}>
          作品记录
        </button>
        <button type="button" data-active={view === "angles"} onClick={() => setView("angles")}>
          新切口{project?.angle_proposals.filter((proposal) => proposal.status !== "deleted").length
            ? ` ${project.angle_proposals.filter((proposal) => proposal.status !== "deleted").length}`
            : ""}
        </button>
      </nav>

      {(error || notice) && (
        <div className={error ? styles.error : styles.notice} role={error ? "alert" : "status"}>
          {error ?? notice}
        </div>
      )}

      <div className={styles.content}>
        {project?.import_warnings.map((warning) => (
          <p className={styles.warning} key={warning}>{warning}</p>
        ))}

        {onRequestNewAngle ? (
          <div className={styles.manualRequest}>
            <button
              type="button"
              className={styles.secondaryAction}
              disabled={!mutable || busy}
              onClick={onRequestNewAngle}
            >
              找一个新切口
            </button>
            <span>只会先放入当前对话输入框，确认发送后才开始。</span>
          </div>
        ) : null}

        {view === "current" ? (
          <section aria-label="当前稿">
            <div className={styles.sectionHeading}>
              <span><FilePenLine size={15} />当前已保存版本</span>
              <code>{project?.current_work_id.slice(0, 8)}</code>
            </div>
            <pre className={styles.bodyPreview}>{currentBody?.body ?? "正在读取正文……"}</pre>
            <div className={styles.actionRow}>
              <a className={styles.secondaryAction} href={privateEditorApi.exportCurrentUrl(projectId)} download>
                <Download size={14} />导出清洁当前稿
              </a>
              {previousWorkId && !confirmRevert ? (
                <button type="button" className={styles.secondaryAction} disabled={!mutable || busy} onClick={() => setConfirmRevert(true)}>
                  <RotateCcw size={14} />恢复上一版
                </button>
              ) : null}
            </div>
            {confirmRevert ? (
              <div className={styles.confirmRow}>
                <span>只移动当前版本指针，不删除任何版本。</span>
                <button type="button" disabled={busy} onClick={() => void revert()}>确认恢复</button>
                <button type="button" disabled={busy} onClick={() => setConfirmRevert(false)}>取消</button>
              </div>
            ) : null}
          </section>
        ) : null}

        {view === "candidate" ? (
          <section aria-label="候选修改">
            {activeCandidate ? (
              <>
                <div className={styles.unchanged}><FileClock size={15} /><strong>当前稿未变</strong><span>只有明确采纳后才会保存新版本。</span></div>
                {activeCandidate.change_groups.map((group) => (
                  <article className={styles.changeCard} key={group.id}>
                    <label className={styles.selectGroup}>
                      <input type="checkbox" aria-label="选择该改动组" checked={selectedGroups.has(group.id)} onChange={() => toggleGroup(group.id)} />
                      <span>{group.target_anchor}</span>
                    </label>
                    <dl className={styles.judgement}>
                      <dt>观察</dt><dd>{group.observation}</dd>
                      <dt>影响</dt><dd>{group.impact}</dd>
                      <dt>改法</dt><dd>{group.rationale}</dd>
                      <dt>依据</dt><dd>{group.evidence_refs.join("；")}</dd>
                      <dt>代价</dt><dd>{group.costs.length ? group.costs.join("；") : "无特别代价"}</dd>
                    </dl>
                    <div className={styles.diff}>
                      <div><span>原文</span><pre>{group.before}</pre></div>
                      <div><span>候选</span><pre>{group.after}</pre></div>
                    </div>
                  </article>
                ))}
                <button type="button" className={styles.primaryAction} disabled={!mutable || busy || selectedGroups.size === 0} onClick={() => void adopt(activeCandidate)}>
                  <Check size={15} />采纳已选改动
                </button>
              </>
            ) : (
              <div className={styles.emptyState}>暂无待采纳候选。零条重要修改也是合法结果。</div>
            )}
            {project?.working_copy_path ? (
              <button type="button" className={styles.secondaryAction} disabled={!mutable || busy} onClick={() => void captureWorkingCopy()}>
                <RefreshCw size={14} />将工作副本保存为候选
              </button>
            ) : (
              <p className={styles.muted}>版本已变化；重新打开“继续共作”后再编辑工作副本。</p>
            )}
          </section>
        ) : null}

        {view === "records" ? (
          <section aria-label="作品记录">
            {recordSections.map((section) => {
              const entries = project?.record_entries.filter(section.test) ?? [];
              return (
                <details className={styles.recordSection} key={section.title} open>
                  <summary>{section.title}<span>{entries.length}</span></summary>
                  {entries.length ? entries.map((entry) => (
                    <div className={styles.record} key={entry.id}>
                      <p>{entry.text}</p>
                      <small>{entry.status} · {entry.source_ref} · 仅当前作品</small>
                      {entry.source === "model_inferred" &&
                      entry.kind !== "open_question" &&
                      entry.status === "proposed" ? (
                        <div className={styles.recordActions}>
                          <button
                            type="button"
                            aria-label={`接受：${entry.text}`}
                            disabled={!mutable || busy}
                            onClick={() => void resolveRecord(entry, "accepted")}
                          >
                            <Check size={13} />接受
                          </button>
                          <button
                            type="button"
                            aria-label={`不采用：${entry.text}`}
                            disabled={!mutable || busy}
                            onClick={() => void resolveRecord(entry, "rejected")}
                          >
                            <X size={13} />不采用
                          </button>
                        </div>
                      ) : null}
                      {entry.kind === "material_note" &&
                      entry.status !== "superseded" ? (
                        <div className={styles.recordActions}>
                          <button
                            type="button"
                            aria-label={`移除来源：${entry.source_ref}`}
                            disabled={!mutable || busy}
                            onClick={() => void invalidateSource(entry.source_ref)}
                          >
                            <Trash2 size={13} />移除来源
                          </button>
                        </div>
                      ) : null}
                    </div>
                  )) : <p className={styles.muted}>暂无记录</p>}
                </details>
              );
            })}
          </section>
        ) : null}

        {view === "angles" ? (
          <section aria-label="新切口提案">
            {project?.angle_proposals.filter((proposal) => proposal.status !== "deleted").length ? (
              project.angle_proposals
                .filter((proposal) => proposal.status !== "deleted")
                .map((proposal) => (
                  <article className={styles.angleCard} key={proposal.id}>
                    <div className={styles.angleHeader}>
                      <strong>{proposal.key_idea}</strong>
                      <span>{proposal.status}</span>
                    </div>
                    <dl className={styles.judgement}>
                      <dt>为何</dt><dd>{proposal.why_now}</dd>
                      <dt>不同</dt><dd>{proposal.differs_from}</dd>
                      <dt>下一步</dt><dd>{proposal.next_action}</dd>
                      <dt>依据</dt><dd>{proposal.source_refs.join("；")}</dd>
                      <dt>待核实</dt><dd>{proposal.open_questions.length ? proposal.open_questions.join("；") : "无"}</dd>
                    </dl>
                    {proposal.status === "source_unavailable" ? (
                      <p className={styles.warning}>引用来源已删除，该提案不能继续当作可用依据。</p>
                    ) : null}
                    {["proposed", "deferred"].includes(proposal.status) ? (
                      <div className={styles.angleActions}>
                        <button type="button" disabled={!mutable || busy} onClick={() => void resolveAngle(proposal, "continue")}>继续发展</button>
                        <button type="button" disabled={!mutable || busy} onClick={() => void resolveAngle(proposal, "later")}>稍后</button>
                        <button type="button" disabled={!mutable || busy} onClick={() => void resolveAngle(proposal, "not_suitable")}>不适合</button>
                        <button type="button" disabled={!mutable || busy} onClick={() => void resolveAngle(proposal, "delete")}>删除</button>
                      </div>
                    ) : null}
                  </article>
                ))
            ) : (
              <div className={styles.emptyState}>暂无新切口提案。材料没有新价值时，空结果是合法的。</div>
            )}
          </section>
        ) : null}

        <details className={styles.privacyTools}>
          <summary>诊断、研究与退出</summary>
          <p>本地诊断只包含运行状态与计数，不包含正文、路径、提示或作品记录文本。</p>
          <div className={styles.actionRow}>
            <a className={styles.secondaryAction} href={privateEditorApi.diagnosticsUrl(projectId)} download>
              <Download size={14} />下载本地诊断
            </a>
            {researchConsent ? (
              <a className={styles.secondaryAction} href={privateEditorApi.researchExportUrl(projectId)} download>
                <Download size={14} />导出研究摘要
              </a>
            ) : null}
          </div>
          {!researchConsent ? (
            <p className={styles.muted}>研究同意未开启；功能启用不代表同意研究导出。</p>
          ) : null}
          {!confirmDelete ? (
            <button type="button" className={styles.dangerAction} disabled={busy} onClick={() => setConfirmDelete(true)}>
              <Trash2 size={14} />删除实验项目
            </button>
          ) : (
            <div className={styles.deleteConfirm}>
              <strong>删除作品记录、候选、操作收据和实验管理的工作副本。</strong>
              <span>已保存 Works 版本与聊天记录保留；Provider 与本地备份副本不在本次删除范围。</span>
              <div>
                <button type="button" disabled={busy} onClick={() => void deleteProject()}>确认删除项目数据</button>
                <button type="button" disabled={busy} onClick={() => setConfirmDelete(false)}>取消</button>
              </div>
            </div>
          )}
        </details>
      </div>

      <footer className={styles.footer}>
        <ChevronLeft size={14} />
        <span>{project?.status === "read_only" ? "实验已关闭：只读、导出与删除仍可用" : "修改仅在采纳后进入当前稿"}</span>
      </footer>
    </aside>
  );
}

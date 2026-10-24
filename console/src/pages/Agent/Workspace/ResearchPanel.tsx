import { useCallback, useEffect, useRef, useState } from "react";
import { Alert, Checkbox, Drawer, Empty, Input, InputNumber, Modal, Radio, Select, Spin, Tag, Typography } from "antd";
import { useNavigate } from "react-router-dom";
import { Button } from "@/vendor/lantaiDesign";
import { researchApi, type EvidenceSpan, type ReportClaim, type ResearchBudget, type ResearchDetail, type ResearchProject, type ResearchReport, type ResearchSource, type ReviewClaim } from "@/api/modules/research";
import type { WikiSourceInfo } from "@/api/types";
import SessionApi from "@/pages/Chat/sessionApi";
import { useAppMessage } from "@/hooks/useAppMessage";
import styles from "./research.module.less";
import ResearchPdfPreview from "./ResearchPdfPreview";
import ResearchLibraryPanel from "./ResearchLibraryPanel";
import ResearchCitationEditor from "./ResearchCitationEditor";
import ResearchEstimate from "./ResearchEstimate";
import { codepointSlice, rebaseClaims, selectionSpan } from "./researchCitationDraft";

const statusLabels: Record<string, string> = {
  not_reviewed: "待核验", reviewed: "已核验", needs_revision: "需要修订", source_changed: "资料已变化", metadata_changed: "引用元数据已变化",
};
const assessmentLabels: Record<string, string> = {
  partial_support: "部分支持", unrelated: "无关", uncertain: "无法判断",
  supports: "支持", contradicts: "反证", insufficient: "证据不足", overclaim: "结论过度", nonfactual: "非事实陈述",
};
function ReviewClaimCard({ claim }: { claim: ReviewClaim }) {
  return <div className={styles.evidence}>
    <Tag color={claim.assessment === "supports" ? "green" : "orange"}>{assessmentLabels[claim.assessment] ?? claim.assessment}</Tag>
    {claim.importance === "critical" && <Tag>重要论点</Tag>}
    {claim.disposition === "disclosed" && <Tag color="blue">已在正文披露</Tag>}
    {claim.disposition === "unresolved" && <Tag color="orange">待处理</Tag>}
    <p>{claim.text}</p><p className={styles.muted}>{claim.reason}</p>
    {claim.disclosure && <blockquote>正文披露：{claim.disclosure.text}</blockquote>}
  </div>;
}
const key = () => crypto.randomUUID();
const briefLines = (text: string) => text.split("\n").map((line) => line.trim()).filter(Boolean);
function BriefFields({ questions, outline, onQuestions, onOutline }: { questions: string; outline: string; onQuestions: (value: string) => void; onOutline: (value: string) => void }) {
  return <>
    <label className={styles.field}>研究子问题<Input.TextArea aria-label="研究子问题" value={questions} onChange={(e) => onQuestions(e.target.value)} placeholder="可选，每行一个子问题" /></label>
    <label className={styles.field}>报告大纲<Input.TextArea aria-label="报告大纲" value={outline} onChange={(e) => onOutline(e.target.value)} placeholder="可选，每行一个章节，按报告顺序排列" /></label>
  </>;
}
const defaultBudget: ResearchBudget = { reading_chars: 2000000, review_calls: 100, review_seconds: 1800, model_calls: 1000, model_tokens: 1000000, model_seconds: 3600 };
function BudgetFields({ value, onChange }: { value: ResearchBudget; onChange: (value: ResearchBudget) => void }) {
  return <>{([ ["reading_chars", "阅读字符上限"], ["review_calls", "审核调用上限"], ["review_seconds", "审核耗时上限（秒）"], ["model_calls", "模型调用上限"], ["model_tokens", "已报告 token 上限"], ["model_seconds", "模型调用耗时上限（秒）"] ] as const).map(([field, label]) =>
    <label className={styles.field} key={field}>{label}<InputNumber aria-label={label} min={1} max={1000000000} precision={0} value={value[field]}
      onChange={(n) => onChange({ ...value, [field]: n ?? 1 })} /></label>)}
    <p className={styles.muted}>累计统计阅读、审核与已接入项目账本的模型调用。失败也计数；token 达限后停止后续调用，单次调用可能超限。缺失用量保持未知，不能据此保证费用上限。已覆盖关联会话的正文、研究审核及会话内辅助文本模型。已覆盖关联会话上下文中的向量计算与带项目归属恢复的答后反思；无项目关联的后台调用不计入。离线项目暂不支持主聊天。</p></>;
}

export default function ResearchPanel({ sources, selectedSources }: { sources: WikiSourceInfo[]; selectedSources: WikiSourceInfo[] }) {
  const navigate = useNavigate();
  const { message } = useAppMessage();
  const [projects, setProjects] = useState<ResearchProject[]>([]);
  const [archived, setArchived] = useState(false);
  const [pendingDeletions, setPendingDeletions] = useState<{ project_id: string; revision: number }[]>([]);
  const [deleteProject, setDeleteProject] = useState<ResearchProject | null>(null);
  const [detail, setDetail] = useState<ResearchDetail | null>(null);
  const [loading, setLoading] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [budgetOpen, setBudgetOpen] = useState(false);
  const [budgetDraft, setBudgetDraft] = useState<ResearchBudget>(defaultBudget);
  const [createBudget, setCreateBudget] = useState<ResearchBudget>(defaultBudget);
  const [createOpen, setCreateOpen] = useState(false);
  const [question, setQuestion] = useState("");
  const [subquestions, setSubquestions] = useState("");
  const [outline, setOutline] = useState("");
  const [revisionSubquestions, setRevisionSubquestions] = useState("");
  const [revisionOutline, setRevisionOutline] = useState("");
  const [scope, setScope] = useState("");
  const [exclusions, setExclusions] = useState("");
  const [queryPreview, setQueryPreview] = useState(false);
  const [revisionQueryPreview, setRevisionQueryPreview] = useState(false);
  const [localOcr, setLocalOcr] = useState(false);
  const [policy, setPolicy] = useState<ResearchProject["policy"]>("local_sources");
  const [paths, setPaths] = useState<string[]>(selectedSources.map((s) => s.path));
  const [sourceLevels, setSourceLevels] = useState<Record<string, "fulltext" | "abstract" | "excerpt">>({});
  const [draft, setDraft] = useState("");
  const [draftClaims, setDraftClaims] = useState<ReportClaim[]>([]);
  const [reportSelection, setReportSelection] = useState<{ start: number; end: number } | null>(null);
  const [citation, setCitation] = useState<{ start: number; end: number; text: string; epoch: number } | null>(null);
  const [opinion, setOpinion] = useState("");
  const [opinionAnchor, setOpinionAnchor] = useState("");
  const [opinionVerdict, setOpinionVerdict] = useState<"agree" | "disagree" | "question">("question");
  const [reader, setReader] = useState<{ source: ResearchSource; span?: EvidenceSpan; block: number } | null>(null);
  const [history, setHistory] = useState<ResearchReport | null>(null);
  const [sourcesOpen, setSourcesOpen] = useState(false);
  const [revisionPaths, setRevisionPaths] = useState<string[]>([]);
  const [revisionLevels, setRevisionLevels] = useState<NonNullable<ResearchProject["source_levels"]>>({});
  const [reviseOpen, setReviseOpen] = useState(false);
  const [libraryOpen, setLibraryOpen] = useState(false);
  const [exportStyle, setExportStyle] = useState("gb7714");
  const [exportFormat, setExportFormat] = useState("markdown");
  const [revisionQuestion, setRevisionQuestion] = useState("");
  const active = useRef(true);
  const listEpoch = useRef(0);
  const detailEpoch = useRef(0);
  const readerEpoch = useRef(0);
  const busyRef = useRef(false);
  useEffect(() => { active.current = true; return () => { active.current = false; listEpoch.current++; detailEpoch.current++; readerEpoch.current++; }; }, []);

  const reload = useCallback(async () => {
    const epoch = ++listEpoch.current;
    setLoading(true);
    try {
      const value = await researchApi.projects(archived);
      if (active.current && epoch === listEpoch.current) { setProjects(value.items); setPendingDeletions(value.pending_deletions); }
    } catch (e) {
      if (active.current && epoch === listEpoch.current) setError(e instanceof Error ? e.message : "研究项目加载失败");
    } finally { if (active.current && epoch === listEpoch.current) setLoading(false); }
  }, [archived]);
  const openProject = useCallback(async (id: string) => {
    const epoch = ++detailEpoch.current;
    setCitation(null); setReportSelection(null);
    try {
      const value = await researchApi.project(id);
      if (!active.current || epoch !== detailEpoch.current) return;
      setDetail(value); setDraft(value.report?.text ?? ""); setDraftClaims(value.report?.claims ?? []); setError("");
      setOpinion(""); setOpinionAnchor(""); setOpinionVerdict("question");
    } catch (e) { if (active.current && epoch === detailEpoch.current) setError(e instanceof Error ? e.message : "项目加载失败"); }
  }, []);
  useEffect(() => { void reload(); }, [reload]);
  const perform = async (action: () => Promise<void>) => {
    if (busyRef.current) return;
    busyRef.current = true; setBusy(true); setError("");
    try { await action(); } catch (e) { if (active.current) setError(e instanceof Error ? e.message : "操作失败，请重试"); }
    finally { busyRef.current = false; if (active.current) setBusy(false); }
  };
  const create = () => perform(async () => {
    const p = await researchApi.create({ budget: createBudget, question, source_paths: paths,
      source_levels: Object.fromEntries(paths.map((path) => [path, sourceLevels[path] ?? "fulltext"])),
      local_ocr: localOcr, query_preview_required: queryPreview, policy, scope, exclusions, subquestions: briefLines(subquestions), outline: briefLines(outline), idempotency_key: key() });
    if (!active.current) return;
    setCreateOpen(false); await reload(); await openProject(p.project_id);
  });
  const continueChat = (reviewOnly = false) => perform(async () => {
    if (!detail) return;
    const p = detail.project;
    const prompt = `继续研究项目 ${p.project_id}，修订 ${p.revision}。研究问题：${p.question}\n` +
      `先启用 research 能力并调用 research_project get，按项目资料范围和数据策略工作。` +
      (reviewOnly ? `调用 research_review 核验当前完整报告 ${p.report_id}；根据核验记录处理反证、证据不足和过度结论，修订后再次核验。` :
        `读取全部资料块，保留原文、页码和字符范围，明确扫描页与表格图像的缺口。结合完整上下文形成有证据和反证的结论，使用 research_report 保存完整报告及准确证据节点，再调用 research_review 核验所有正文。`) +
      `可以复用已有阅读进度和核验检查点；停止时保留进度，不伪造完成。此任务沿用当前对话的 Runtime、工具授权和停止机制。`;
    // Project scope is persisted server-side; it also supports more than the chat attachment limit.
    const sessions = await SessionApi.createSession({ name: "New Chat", meta: { research_project_id: p.project_id } });
    if (!active.current) return;
    const session = sessions[0];
    if (!session?.id) throw new Error("无法创建研究对话");
    SessionApi.saveDraftText(session.id, prompt);
    // Locally created IDs use the same colon-delimited route as the chat page.
    navigate(`/chat/${session.id}`);
  });
  const openSource = async (id: string, span?: EvidenceSpan) => {
    const epoch = ++readerEpoch.current;
    try {
      const source = await researchApi.source(id);
      if (!active.current || epoch !== readerEpoch.current) return;
      const block = span ? source.blocks.findIndex((b) => b.start <= span.start && b.end > span.start) : 0;
      setReader({ source, span, block: Math.max(0, block) });
    } catch (e) { if (active.current && epoch === readerEpoch.current) setError(e instanceof Error ? e.message : "原文加载失败"); }
  };
  const save = () => perform(async () => {
    if (!detail) return;
    const { project: p, report } = detail;
    await researchApi.save(p.project_id, { revision: p.revision, text: draft,
      // Anchors survive only unchanged text; no passing review is inherited.
      claims: draftClaims, expected_report_id: report?.id ?? null, idempotency_key: key() });
    if (active.current) { await openProject(p.project_id); await reload(); message.success("已保存新版本，全文需要重新核验"); }
  });
  const addFigure = (file: File) => perform(async () => {
    if (!detail) return;
    const epoch = detailEpoch.current;
    const project = detail.project;
    try {
      if (!["image/png", "image/jpeg"].includes(file.type) || file.size > 6 * 1024 * 1024 || !file.size) {
        throw new Error("请选择不超过 6 MiB 的 PNG 或 JPEG 图片");
      }
      const data = await new Promise<string>((resolve, reject) => {
        const reader = new FileReader();
        reader.onload = () => resolve(String(reader.result).split(",", 2)[1]);
        reader.onerror = () => reject(new Error("图片读取失败，请重试"));
        reader.readAsDataURL(file);
      });
      if (!active.current || epoch !== detailEpoch.current) return;
      const result = await researchApi.uploadFigure(project.project_id, project.revision, data);
      if (!active.current || epoch !== detailEpoch.current) return;
      const caption = file.name.replace(/[\[\]\r\n]/g, " ").slice(0, 200);
      setDraft((value) => value + `\n\n![${caption}](research-figure:${result.id})\n`);
    } catch (e) {
      if (active.current && epoch === detailEpoch.current) throw e;
    }
  });
  const download = () => perform(async () => {
    if (!detail?.report) return;
    const result = await researchApi.exportReport(detail.project.project_id, detail.project.revision, detail.report.id, exportFormat, exportStyle);
    const blob = await researchApi.exportFile(result.id);
    if (!active.current) return;
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a"); link.href = url; link.download = result.filename; link.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  });
  const p = detail?.project;
  const draftDirty = draft !== (detail?.report?.text ?? "") || JSON.stringify(draftClaims) !== JSON.stringify(detail?.report?.claims ?? []);
  const sourceOptions = [...new Set([...sources.map((s) => s.path), ...(p?.source_paths ?? [])])].map((path) => ({
    value: path, label: sources.find((s) => s.path === path)?.display_title || sources.find((s) => s.path === path)?.filename || path,
  }));
  const sourceText = reader?.source.text ?? "";
  const block = reader?.source.blocks[reader.block];
  const a = reader?.span?.start ?? block?.start ?? 0;
  const b = reader?.span?.end ?? block?.end ?? 0;

  return <main className={styles.workspace}>
    <div className={styles.toolbar}>
      <div><h2>研究</h2><p>围绕问题组织资料、核对结论，保留每次报告的证据与版本。</p></div>
      <div className={styles.actions}>
        <Button onClick={() => { void reload(); if (p) void openProject(p.project_id); }} disabled={busy}>刷新</Button>
        <Button onClick={() => setLibraryOpen(true)}>文献库与检索</Button>
        <Button type="primary" onClick={() => { setPaths(selectedSources.map((s) => s.path)); setCreateOpen(true); }}>新建研究</Button>
      </div>
    </div>
    {error && <Alert type="error" message={error} showIcon closable onClose={() => setError("")} />}
    {pendingDeletions.length > 0 && <Alert type="warning" showIcon message={`${pendingDeletions.length} 个已删除项目的文件清理未完成，可以安全重试。`} action={<Button disabled={busy} onClick={() => void perform(async () => {
      for (const pending of pendingDeletions) await researchApi.delete(pending);
      await reload();
    })}>重试文件清理</Button>} />}
    <div className={styles.columns}>
      <aside className={styles.projectList} aria-label="研究项目">
        <Radio.Group aria-label="研究项目状态" value={archived} onChange={(e) => {
          detailEpoch.current++; readerEpoch.current++; setDetail(null); setReader(null); setHistory(null); setArchived(e.target.value);
        }}><Radio.Button value={false}>进行中</Radio.Button><Radio.Button value={true}>已归档</Radio.Button></Radio.Group>
        {loading && <Spin size="small" />}
        {!loading && !projects.length && <Empty description={archived ? "没有已归档的研究项目" : "从选中的资料开始一个研究问题"} />}
        {projects.map((item) => <button key={item.project_id} type="button" aria-current={p?.project_id === item.project_id ? "true" : undefined} onClick={() => void openProject(item.project_id)}>
          <strong>{item.question}</strong><span>修订 {item.revision} · {item.source_versions.length} 份资料</span>
          <span>{statusLabels[item.review_status] || item.review_status}</span>
        </button>)}
      </aside>
      <section className={styles.detail} aria-label="研究详情">
        {!detail || !p ? <Empty description="选择研究项目查看阅读进度与报告" /> : <>
          <div className={styles.projectTitle}><h3>{p.question}</h3><Tag color={p.review_status === "reviewed" ? "green" : "orange"}>{statusLabels[p.review_status] || p.review_status}</Tag></div>
          <p className={styles.muted}>修订 {p.revision} · {p.policy === "offline" ? "完全离线" : p.policy === "public_search" ? "允许公开检索" : "仅使用本地资料，按当前模型设置推理"}</p>
          {p.archived && <Alert type="info" message="项目已归档，报告和证据仍可查看。恢复项目后可以继续修改和研究。" showIcon />}
          {p.scope && <p>范围：{p.scope}</p>}{p.exclusions && <p>排除：{p.exclusions}</p>}
          {p.stale_bibliography.length > 0 && <Alert type="warning" message="文献元数据已变化。采用文献库的所选版本将创建新修订，保留正文与已读记录，并要求重新核验；历史引用保持原版本。" showIcon action={<Button onClick={() => void perform(async () => {
            await researchApi.adoptBibliography(p); await openProject(p.project_id); await reload();
          })} disabled={busy || p.archived || !!p.stale_sources.length}>采用所选文献版本</Button>} />}
          {p.stale_sources.length > 0 && <Alert type="warning" message="原始资料已变化，历史摘录仍可查看。更新资料后重新生成并核验报告。" showIcon action={<Button onClick={() => void perform(async () => {
            await researchApi.revise(p.project_id, { expected_revision: p.revision, refresh_sources: true, idempotency_key: key() }); await openProject(p.project_id); await reload();
          })} disabled={busy || p.archived}>更新资料</Button>} />}
          {!p.archived && <ResearchEstimate project={p} />}
          <div className={styles.actions}>
            <Button type="primary" disabled={busy || p.archived || !!p.stale_sources.length} onClick={() => void continueChat()}>到对话继续研究</Button>
            <Button disabled={busy || p.archived || !p.report_id || !!p.stale_sources.length} onClick={() => void continueChat(true)}>到对话核验报告</Button>
            <Button disabled={busy || p.archived} onClick={() => { setRevisionQueryPreview(!!p.query_preview_required); setRevisionQuestion(p.question); setRevisionSubquestions((p.subquestions ?? []).join("\n")); setRevisionOutline((p.outline ?? []).join("\n")); setReviseOpen(true); }}>修改问题</Button>
            <Button disabled={busy || p.archived || draftDirty} title={draftDirty ? "请先保存报告草稿" : undefined} onClick={() => {
              setRevisionPaths(p.source_paths ?? []); setRevisionLevels({ ...p.source_levels }); setSourcesOpen(true);
            }}>修改资料</Button>
            <Button disabled={busy} onClick={() => void perform(async () => {
              if (p.archived) await researchApi.restore(p); else await researchApi.archive(p);
              if (active.current) { detailEpoch.current++; setDetail(null); }
              await reload();
            })}>{p.archived ? "恢复项目" : "归档项目"}</Button>
            {p.archived && <Button disabled={busy} danger onClick={() => setDeleteProject(p)}>彻底删除</Button>}
          </div>
          {p.inference && <section aria-label="模型调用记录">
            <Typography.Title level={5}>模型调用记录</Typography.Title>
            <p>已记录 {p.inference.calls} 次调用 · 已报告 {p.inference.known_tokens} token · {Math.ceil(p.inference.seconds)} 秒</p>
            <p>用量未知：{p.inference.unknown_token_calls} 次 · 费用未知：{p.inference.unknown_cost_calls} 次 · 已知费用：{p.inference.known_cost_cny === null ? "未知" : `¥${p.inference.known_cost_cny}`}</p>
            <p className={styles.muted}>仅统计已接入账本的调用。未知部分不按零计费；本地停止不能证明远端已经停止计费。</p>
            {p.inference.configurations.map((config) => <p key={config.version}>
              配置 {config.version.slice(0, 12)} · {config.provider} / {config.model}
              {config.generation != null
                ? Object.keys(config.generation).length ? ` · ${JSON.stringify(config.generation)}` : " · 参数由当前 Provider 配置决定"
                : config.dimensions != null ? ` · 向量维度：${config.dimensions}` : " · 未记录参数"}
              {config.max_length != null && ` · 最大长度：${config.max_length}`}
              {config.use_dimensions != null && ` · 发送维度参数：${config.use_dimensions ? "是" : "否"}`}
            </p>)}
          </section>}
          {p.budget && p.budget_usage && <section aria-label="项目预算">
            <h4>项目预算</h4>
            <p>阅读字符：{p.budget_usage.reading_chars} / {p.budget.reading_chars} · 审核调用：{p.budget_usage.review_calls} / {p.budget.review_calls} · 审核耗时：{Math.ceil(p.budget_usage.review_seconds)} / {p.budget.review_seconds} 秒</p>
            {p.budget_remaining && <p className={styles.muted}>剩余：{p.budget_remaining.reading_chars} 字符、{p.budget_remaining.review_calls} 次、{Math.floor(p.budget_remaining.review_seconds)} 秒。额度耗尽后保留草稿和审核进度。</p>}
            <p>模型调用：{p.budget_usage.model_calls ?? "未知"} / {p.budget.model_calls ?? "未知"} 次 · 剩余 {p.budget_remaining?.model_calls ?? "未知"} 次</p>
            <p>已报告 token：{p.budget_usage.model_tokens ?? "未知"} / {p.budget.model_tokens ?? "未知"} · 剩余 {p.budget_remaining?.model_tokens ?? "未知"}</p>
            <p>模型调用耗时：{p.budget_usage.model_seconds == null ? "未知" : Math.ceil(p.budget_usage.model_seconds)} / {p.budget.model_seconds ?? "未知"} 秒 · 剩余 {p.budget_remaining?.model_seconds == null ? "未知" : Math.floor(p.budget_remaining.model_seconds)} 秒</p>
            <p className={styles.muted}>token 仅包含已报告用量；未报告部分和未知费用不按零计算。</p>
            <Button disabled={busy || p.archived} onClick={() => { setBudgetDraft(p.budget!); setBudgetOpen(true); }}>调整预算</Button>
          </section>}
          {!!p.subquestions?.length && <section aria-label="研究子问题"><h4>研究子问题</h4><ol>{p.subquestions.map((item, i) => <li key={i}>{item}</li>)}</ol></section>}
          {!!p.outline?.length && <section aria-label="报告大纲"><h4>报告大纲</h4><ol>{p.outline.map((item, i) => <li key={i}>{item}</li>)}</ol></section>}
          <h4>资料与阅读进度</h4>
          {detail.sources.some((source) => source.pages.some((page) => !!page.issue)) && <Alert type="warning" showIcon
            message="扫描页可在本机识别。重新提取会创建新修订，报告需重新生成和核验；旧版本保留。"
            action={<Button disabled={busy || p.archived} onClick={() => void perform(async () => {
              await researchApi.revise(p.project_id, { expected_revision: p.revision, local_ocr: true, idempotency_key: key() });
              await openProject(p.project_id); await reload();
            })}>本地 OCR 重新提取</Button>} />}

          <p className={styles.muted}>已读取 {p.reading.read_blocks} / {p.reading.total_blocks} 块。阅读进度不代表结论得到支持。</p>
          {detail.sources.map((source) => <div className={styles.sourceRow} key={source.id}>
            <button type="button" onClick={() => void openSource(source.id)}>{source.path.split("/").pop()}</button>
            <span>{source.pages.length} 页 · {source.blocks.length} 块</span><Tag>{source.evidence_level === "note" ? (source.source_kind === "model_summary" ? "模型笔记" : source.source_kind === "user_note" ? "用户笔记" : "导入笔记") : source.evidence_level === "abstract" ? "仅摘要" : source.evidence_level === "excerpt" ? "部分摘录" : "完整资料"}</Tag>
            {source.coverage !== "complete" && <Tag color="orange">提取不完整</Tag>}
          </div>)}
          <h4>报告</h4>
          {!detail.report && <p className={styles.muted}>在主聊天继续研究后，模型生成的报告会保存在这里。也可以先写入草稿。</p>}
          <Input.TextArea aria-label="研究报告正文" readOnly={p.archived || busy} value={draft} onChange={(e) => {
            const next = e.target.value; setDraftClaims(rebaseClaims(draft, next, draftClaims)); setDraft(next); setReportSelection(null); setCitation(null);
          }} onSelect={(event) => { const target = event.target as HTMLTextAreaElement; setReportSelection(selectionSpan(draft, target.selectionStart, target.selectionEnd)); }} autoSize={{ minRows: 9, maxRows: 28 }} />
          <Button disabled={busy || p.archived || !detail.sources.length || !reportSelection || !codepointSlice(draft, reportSelection.start, reportSelection.end).trim()}
            onClick={() => { if (reportSelection) setCitation({ ...reportSelection, text: draft, epoch: detailEpoch.current }); }}>为选中文字添加引用</Button>
          <label className={styles.field}>添加报告图片<input type="file" aria-label="添加报告图片" accept="image/png,image/jpeg" disabled={busy || p.archived}
            onChange={(event) => { const file = event.target.files?.[0]; event.target.value = ""; if (file) void addFigure(file); }} /></label>
          <p className={styles.muted}>支持 PNG/JPEG，每张不超过 6 MiB、1600 万像素，每项目累计 30 MiB。图片加入草稿后请保存新版本；可在图片标记的方括号中修改图注。三种格式均包含图片，图像内容和交叉引用仍需人工核对。</p>
          {draftDirty && <p className={styles.muted}>草稿尚未保存。未修改文字的引用会保留，改写范围内的引用需要重新添加；保存后全篇重新核验。</p>}
          {exportFormat === "docx" && <Typography.Paragraph type="secondary">Word 引用为静态文本，不含 Zotero 动态引用字段。更改文献或引用格式后请重新导出。</Typography.Paragraph>}
          <div className={styles.actions}><Button disabled={busy || p.archived || !draft.trim() || !draftDirty} onClick={() => void save()}>保存新版本</Button><Select aria-label="引用格式" value={exportStyle} onChange={setExportStyle} options={[{value:"gb7714",label:"GB/T 7714"},{value:"apa",label:"APA"},{value:"numeric",label:"数字编号"}]} /><Select aria-label="导出格式" value={exportFormat} onChange={setExportFormat} options={[{value:"markdown",label:"Markdown"},{value:"docx",label:"Word"},{value:"pdf",label:"PDF"}]} /><Button disabled={busy || p.archived || !detail.report || draftDirty} onClick={() => void download()}>导出报告</Button></div>
          {draftClaims.length ? <><h4>结论与原文证据{draftDirty ? "（草稿）" : ""}</h4>{draftClaims.map((claim, i) => <div className={styles.evidence} key={i}>
            <p>{codepointSlice(draft, claim.start, claim.end)}</p>
            {claim.evidence.map((span, j) => <button type="button" key={j} onClick={() => void openSource(span.source_version_id, span)}><blockquote>{span.quote}</blockquote><span>查看对应版本原文</span></button>)}
            <Button disabled={busy || p.archived} onClick={() => setDraftClaims(draftClaims.filter((_, index) => index !== i))}>移除此引用</Button>
          </div>)}</> : null}
          {detail.review && <><h4>全文核验</h4><p className={styles.muted}>{detail.review.identity.provider} / {detail.review.identity.model} · 扫描 {detail.review.coverage.length} 段 · 报告 {detail.review.report_hash.slice(0, 12)}</p>
            {detail.review.issues.map((issue, i) => <Alert key={i} type="warning" message={issue} />)}
            {detail.review.claims.map((claim, i) => <ReviewClaimCard claim={claim} key={i} />)}
            {!!detail.review.critical_review?.required_claims && <><h4>重要论点独立复核</h4><p className={styles.muted}>{detail.review.critical_review.required_claims} 项重要论点 · {detail.review.critical_review.checkpoint_ids.length} 次独立调用</p>
              {detail.review.critical_review.claims.map((claim, i) => <ReviewClaimCard claim={claim} key={i} />)}
            </>}
          </>}
          {detail.report && <><h4>人工复核意见</h4>
            <p className={styles.muted}>意见绑定当前报告版本，不覆盖模型核验，也不自动改变核验状态。意见一经记录保留原文；更正请追加新意见。</p>
            <Select aria-label="复核意见类型" value={opinionVerdict} onChange={setOpinionVerdict} options={[{value:"question",label:"待澄清"},{value:"agree",label:"认同"},{value:"disagree",label:"不同意"}]} />
            <Input aria-label="对应正文原句（可选）" placeholder="可选：粘贴需要复核的原句" value={opinionAnchor} onChange={(e) => setOpinionAnchor(e.target.value)} />
            <Input.TextArea aria-label="人工复核意见" value={opinion} maxLength={10000} onChange={(e) => setOpinion(e.target.value)} autoSize={{minRows: 2, maxRows: 8}} />
            <Button disabled={busy || p.archived || !opinion.trim() || draft !== detail.report.text} onClick={() => void perform(async () => {
              await researchApi.humanReview(p.project_id, { revision: p.revision, report_id: detail.report!.id,
                expected_review_id: p.review_id, text: opinion, verdict: opinionVerdict,
                anchor: opinionAnchor.trim() ? { text: opinionAnchor } : null, idempotency_key: key() });
              if (active.current) await openProject(p.project_id);
            })}>记录独立意见</Button>
          </>}
          {(detail.human_reviews ?? []).map((entry) => <div className={styles.evidence} key={entry.id}>
            <Tag>{entry.report_id === detail.report?.id ? "当前报告意见" : "历史报告意见"}</Tag>
            <Tag>{({agree:"认同",disagree:"不同意",question:"待澄清"})[entry.verdict]}</Tag>
            <p>{entry.text}</p>{entry.anchor && <blockquote>{entry.anchor.text}</blockquote>}
            <p className={styles.muted}>{new Date(entry.created_at * 1000).toLocaleString()} · 报告 {entry.report_hash.slice(0,12)} · {entry.report_id === detail.report?.id && entry.review_id === p.review_id ? "对应当前核验" : "对应历史核验"}</p>
          </div>)}
          {detail.report_history.length > (detail.report ? 1 : 0) && <><h4>报告历史</h4><div className={styles.actions}>{detail.report_history.map((r, i) => <Button key={r.id} onClick={() => void perform(async () => { const result = await researchApi.report(r.id); if (active.current) setHistory(result); })}>版本 {detail.report_history.length - i}</Button>)}</div></>}
        </>}
      </section>
    </div>
    <Modal title="彻底删除研究项目" open={!!deleteProject} onCancel={() => setDeleteProject(null)} okText="彻底删除" okButtonProps={{ danger: true }} confirmLoading={busy} onOk={() => void perform(async () => {
      if (!deleteProject) return;
      const result = await researchApi.delete(deleteProject);
      if (active.current) { setDeleteProject(null); detailEpoch.current++; setDetail(null); setReader(null); }
      await reload();
      if (result.status === "cleanup_pending") message.warning("项目记录已删除，文件清理未完成，请点击重试文件清理。");
    })}>
      <p>将删除“{deleteProject?.question}”的报告、核验记录、历史版本和导出文件，此操作无法撤销。</p>
      <p>知识库中的原始资料、文献库和其他项目共用的快照会保留。</p>
    </Modal>
    <Modal title="调整项目预算" open={budgetOpen} onCancel={() => setBudgetOpen(false)} okText="保存预算并保留进度" confirmLoading={busy} onOk={() => void perform(async () => {
      if (!p) return;
      await researchApi.revise(p.project_id, { expected_revision: p.revision, budget: budgetDraft, idempotency_key: key() });
      if (active.current) setBudgetOpen(false);
      await openProject(p.project_id); await reload();
    })}>
      <BudgetFields value={budgetDraft} onChange={setBudgetDraft} />
      <p>保存会创建新修订，保留报告、阅读记录与已有审核结果。已消耗额度不会清零。</p>
    </Modal>
    <Modal title="新建研究" open={createOpen} onCancel={() => setCreateOpen(false)} onOk={() => void create()} okText="创建项目" confirmLoading={busy} okButtonProps={{ disabled: !question.trim() }}>
      <label className={styles.field}>研究问题<Input.TextArea value={question} onChange={(e) => setQuestion(e.target.value)} placeholder="你需要哪些证据来回答这个问题？" /></label>
      <BriefFields questions={subquestions} outline={outline} onQuestions={setSubquestions} onOutline={setOutline} />
      <label className={styles.field}>研究范围<Input value={scope} onChange={(e) => setScope(e.target.value)} placeholder="人群、时间、方法或领域" /></label>
      <label className={styles.field}>排除条件<Input value={exclusions} onChange={(e) => setExclusions(e.target.value)} /></label>
      <label className={styles.field}>本地资料<Select mode="multiple" value={paths} onChange={setPaths} options={sources.map((s) => ({ value: s.path, label: s.display_title || s.filename }))} placeholder="选择资料；公开检索项目可以稍后导入" /></label>
      {paths.map((path) => <label className={styles.field} key={path}>{sources.find((s) => s.path === path)?.filename ?? path}
        <Select aria-label="资料内容范围" value={sourceLevels[path] ?? "fulltext"} onChange={(value) => setSourceLevels((old) => ({ ...old, [path]: value }))}
          options={[{ value: "fulltext", label: "完整资料" }, { value: "abstract", label: "仅论文摘要" }, { value: "excerpt", label: "部分摘录" }]} />
      </label>)}
      <BudgetFields value={createBudget} onChange={setCreateBudget} />
      <Checkbox checked={queryPreview} onChange={(e) => setQueryPreview(e.target.checked)}>外发检索词须先预览确认（敏感项目）</Checkbox>
      <Checkbox checked={localOcr} onChange={(e) => setLocalOcr(e.target.checked)}>扫描 PDF 页使用本地 OCR（macOS；识别结果需人工核对，不上传资料）</Checkbox>
      <Radio.Group value={policy} onChange={(e) => setPolicy(e.target.value)} className={styles.policies}><Radio value="local_sources">仅本地资料</Radio><Radio value="public_search">允许公开检索</Radio><Radio value="offline">完全离线</Radio></Radio.Group>
      <p className={styles.muted}>“仅本地资料”限制证据来源，仍使用当前模型；“完全离线”要求本地推理，也不发起检索、远程 OCR 或嵌入。</p>
    </Modal>
    <Modal title="修改研究问题与大纲" open={reviseOpen} onCancel={() => setReviseOpen(false)} okText="创建新修订" confirmLoading={busy} onOk={() => void perform(async () => {
      if (!p) return; await researchApi.revise(p.project_id, { expected_revision: p.revision, question: revisionQuestion, ...(revisionQueryPreview !== !!p.query_preview_required ? { query_preview_required: revisionQueryPreview } : {}), subquestions: briefLines(revisionSubquestions), outline: briefLines(revisionOutline), idempotency_key: key() }); if (active.current) setReviseOpen(false); await openProject(p.project_id); await reload();
    })}><label className={styles.field}>研究问题<Input.TextArea aria-label="研究问题" value={revisionQuestion} onChange={(e) => setRevisionQuestion(e.target.value)} /></label><BriefFields questions={revisionSubquestions} outline={revisionOutline} onQuestions={setRevisionSubquestions} onOutline={setRevisionOutline} /><Checkbox checked={revisionQueryPreview} onChange={(e) => setRevisionQueryPreview(e.target.checked)}>外发检索词须先预览确认（敏感项目）</Checkbox><p>保留历史报告。问题、子问题或大纲修改后，需要重新生成和核验报告。</p></Modal>
    <Modal title="修改研究资料" open={sourcesOpen} onCancel={() => setSourcesOpen(false)} okText="保存资料并创建修订" confirmLoading={busy} onOk={() => void perform(async () => {
      if (!p || p.archived || draftDirty) return;
      await researchApi.revise(p.project_id, { expected_revision: p.revision, source_paths: revisionPaths,
        source_levels: Object.fromEntries(revisionPaths.map((path) => [path, revisionLevels[path] ?? "fulltext"])), idempotency_key: key() });
      if (active.current) setSourcesOpen(false);
      await openProject(p.project_id); await reload();
    })}>
      <label className={styles.field}>研究资料<Select aria-label="研究资料" mode="multiple" value={revisionPaths} onChange={setRevisionPaths} options={sourceOptions} /></label>
      {revisionPaths.map((path) => <label className={styles.field} key={path}>{sourceOptions.find((s) => s.value === path)?.label ?? path}
        <Select aria-label={`资料内容范围：${path}`} value={revisionLevels[path] ?? "fulltext"} onChange={(value) => setRevisionLevels((old) => ({ ...old, [path]: value }))}
          options={[{ value: "fulltext", label: "完整资料" }, { value: "abstract", label: "仅论文摘要" }, { value: "excerpt", label: "部分摘录" }]} />
      </label>)}
      <p>选择已导入知识库的资料，可添加、移除或替换。保存后创建新修订，需要重新阅读、生成和核验报告；历史报告和原文引用仍保留，已消耗额度不会清零。</p>
    </Modal>
    <Drawer title={reader?.source.path.split("/").pop()} open={!!reader} zIndex={1100} onClose={() => { readerEpoch.current++; setReader(null); }} width="min(820px, 94vw)">
      {reader && <>
        <p className={styles.muted}>固定版本 {reader.source.raw_hash.slice(0, 12)} · {reader.source.coverage === "complete" ? "全部文本已提取" : "存在提取缺口"}</p>
        {reader.source.issues.map((issue, i) => <Alert type="warning" message={issue} key={i} />)}
        {reader.source.pages.filter((page) => page.issue).map((page) => <Alert key={page.page} type="warning" message={`第 ${page.page} 页：${page.issue === "ocr_low_confidence" ? "OCR 置信度低，请核对原页" : page.issue === "ocr_requires_visual_verification" ? "已本地识别，请核对原页、数字和阅读顺序" : page.issue === "local_ocr_unavailable_or_no_text" ? "本地 OCR 不可用或未识别出文字，请提供可读版本" : "没有可用文本，需要 OCR 或人工核对"}`} />)}
        <p className={styles.muted}>当前摘录匹配 {reader.source.pages.flatMap((page) => page.regions ?? []).filter((region) => region.start < b && region.end > a).length} 个页内定位框；未匹配时仅提供页码和字符位置。坐标不代表表格或图像已被理解。</p>
        {reader.source.snapshot_path.toLowerCase().endsWith(".pdf") && <ResearchPdfPreview key={`${reader.source.id}:${a}:${b}`} source={reader.source} initialPage={block?.page ?? reader.source.pages.find((page) => page.start <= a && page.end >= b)?.page ?? 1} start={a} end={b} />}
        {reader.span ? <><p>字符 {a}–{b} · 第 {reader.source.pages.find((page) => page.start <= a && page.end >= b)?.page ?? "—"} 页</p><pre className={styles.sourceText}>{codepointSlice(sourceText, Math.max(0, a - 1200), a)}<mark>{codepointSlice(sourceText, a, b)}</mark>{codepointSlice(sourceText, b, b + 1200)}</pre></> : <>
          <div className={styles.actions}><Button disabled={reader.block <= 0} onClick={() => setReader({ ...reader, block: reader.block - 1 })}>上一块</Button><span>第 {reader.block + 1} / {reader.source.blocks.length} 块 · 第 {block?.page ?? "—"} 页</span><Button disabled={reader.block + 1 >= reader.source.blocks.length} onClick={() => setReader({ ...reader, block: reader.block + 1 })}>下一块</Button></div>
          <pre className={styles.sourceText}>{codepointSlice(sourceText, a, b)}</pre>
          <Checkbox disabled={busy || !p || p.archived || !block} onChange={(e) => { if (e.target.checked && p) void perform(async () => { await researchApi.read(p.project_id, p.revision, reader.source.id, reader.block); await openProject(p.project_id); }); }}>记录已读这一块</Checkbox>
        </>}
      </>}
    </Drawer>
    <Drawer title="历史报告（保留原版本）" open={!!history} onClose={() => setHistory(null)} width="min(820px, 94vw)">
      <pre className={styles.sourceText}>{history?.text}</pre>
      {history?.claims.map((claim, i) => <div className={styles.evidence} key={i}>
        <p>{codepointSlice(history.text, claim.start, claim.end)}</p>
        {claim.evidence.map((span, j) => <button type="button" key={j} onClick={() => void openSource(span.source_version_id, span)}><blockquote>{span.quote}</blockquote><span>查看对应版本原文</span></button>)}
      </div>)}
    </Drawer>
    {citation && detail && citation.epoch === detailEpoch.current && citation.text === draft && <ResearchCitationEditor
      key={`${detail.project.project_id}:${detail.project.revision}`} detail={detail} text={codepointSlice(draft, citation.start, citation.end)}
      onClose={() => setCitation(null)} onAdd={(span) => {
        if (citation.epoch !== detailEpoch.current || citation.text !== draft) return;
        setDraftClaims((claims) => {
          const existing = claims.find((claim) => claim.start === citation.start && claim.end === citation.end);
          if (existing) return claims.map((claim) => claim !== existing || claim.evidence.some((item) => item.source_version_id === span.source_version_id && item.start === span.start && item.end === span.end)
            ? claim : { ...claim, evidence: [...claim.evidence, span] });
          return [...claims, { start: citation.start, end: citation.end, evidence: [span] }].sort((a, b) => a.start - b.start);
        });
        setCitation(null);
      }} />}
    <Drawer title="文献库与检索" open={libraryOpen} onClose={() => setLibraryOpen(false)} width="min(1100px, 96vw)">
      {libraryOpen && <ResearchLibraryPanel detail={p?.archived ? null : detail} onProjectChange={() => { if (p) void openProject(p.project_id); }} />}
    </Drawer>
  </main>;
}

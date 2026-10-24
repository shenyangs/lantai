import { useCallback, useEffect, useRef, useState } from "react";
import { Alert, Drawer, Empty, Input, InputNumber, Modal, Select, Spin, Tag } from "antd";
import { Button } from "@/vendor/lantaiDesign";
import { researchApi, type LibraryWork, type LiteratureResult, type QueryPreview, type ResearchDetail } from "@/api/modules/research";
import ResearchPdfRegionNote from "./ResearchPdfRegionNote";
import styles from "./research.module.less";

const issues: Record<string, string> = { missing_doi: "缺少 DOI", missing_authors: "缺少作者", missing_date: "缺少年份", metadata_conflict: "元数据有多个版本", retracted: "撤稿记录", abstract_only: "仅摘要", metadata_only: "仅元数据" };
export function downloadText(text: string, name: string) {
  const url = URL.createObjectURL(new Blob([text], { type: "text/plain;charset=utf-8" }));
  const a = document.createElement("a"); a.href = url; a.download = name; a.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

export default function ResearchLibraryPanel({ detail, onProjectChange }: { detail: ResearchDetail | null; onProjectChange: () => void }) {
  const [works, setWorks] = useState<LibraryWork[]>([]);
  const [work, setWork] = useState<LibraryWork | null>(null);
  const [loading, setLoading] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [query, setQuery] = useState("");
  const [queryMode, setQueryMode] = useState<"general" | "title_author">("general");
  const [author, setAuthor] = useState("");
  const [previews, setPreviews] = useState<QueryPreview[]>([]);
  const [result, setResult] = useState<LiteratureResult | null>(null);
  const [importOpen, setImportOpen] = useState(false);
  const [format, setFormat] = useState("csl-json");
  const [text, setText] = useState("");
  const [note, setNote] = useState("");
  const [anchorSource, setAnchorSource] = useState<string>();
  const [anchorQuote, setAnchorQuote] = useState("");
  const [anchorStart, setAnchorStart] = useState<number | null>(null);
  const [anchorEnd, setAnchorEnd] = useState<number | null>(null);
  const [tags, setTags] = useState("");
  const [collections, setCollections] = useState("");
  const [tagFilter, setTagFilter] = useState("");
  const [collectionFilter, setCollectionFilter] = useState("");
  const [metadata, setMetadata] = useState("");
  const [style, setStyle] = useState("gb7714");
  const [preview, setPreview] = useState<string[]>([]);
  const projectSelection = useRef("");
  projectSelection.current = `${detail?.project.project_id}:${detail?.project.revision}`;
  const active = useRef(true);
  const epoch = useRef(0);
  const busyRef = useRef(false);
  useEffect(() => { active.current = true; return () => { active.current = false; epoch.current++; }; }, []);
  const reload = useCallback(async () => {
    const n = ++epoch.current; setLoading(true);
    try { const data = await researchApi.library(); if (active.current && n === epoch.current) setWorks(data.items); }
    catch (e) { if (active.current && n === epoch.current) setError(e instanceof Error ? e.message : "文献库加载失败"); }
    finally { if (active.current && n === epoch.current) setLoading(false); }
  }, []);
  useEffect(() => { void reload(); }, [reload]);
  // A search result belongs to one project revision and must never migrate when selection changes.
  useEffect(() => { setResult(null); setNotice(""); }, [detail?.project.project_id, detail?.project.revision]);
  const refreshPreviews = useCallback(async () => {
    if (!detail?.project.query_preview_required) { setPreviews([]); return; }
    const selection = projectSelection.current;
    const value = await researchApi.queryPreviews(detail.project.project_id);
    if (active.current && selection === projectSelection.current) setPreviews(value.items);
  }, [detail?.project.project_id, detail?.project.revision, detail?.project.query_preview_required]);
  useEffect(() => { setPreviews([]); void refreshPreviews().catch((e) => { if (active.current) setError(e instanceof Error ? e.message : "查询预览加载失败"); }); }, [refreshPreviews]);
  const run = async (action: () => Promise<void>) => {
    if (busyRef.current) return; busyRef.current = true; setBusy(true); setError("");
    try { await action(); } catch (e) { if (active.current) setError(e instanceof Error ? e.message : "操作失败"); }
    finally { busyRef.current = false; if (active.current) setBusy(false); }
  };
  const open = (item: LibraryWork) => { setWork(item); setTags((item.organization?.tags ?? []).join("\n")); setCollections((item.organization?.collections ?? []).join("\n")); setMetadata(JSON.stringify(item.csl, null, 2)); setNote(""); setAnchorSource(undefined); setAnchorQuote(""); setAnchorStart(null); setAnchorEnd(null); };
  const visibleWorks = works.filter((item) => (!tagFilter.trim() || item.organization?.tags.some((label) => label.toLocaleLowerCase().includes(tagFilter.trim().toLocaleLowerCase()))) &&
    (!collectionFilter.trim() || item.organization?.collections.some((label) => label.toLocaleLowerCase().includes(collectionFilter.trim().toLocaleLowerCase()))));
  const labels = (text: string) => text.split("\n").map((label) => label.trim()).filter(Boolean);
  return <section className={styles.detail} aria-label="文献库">
    <div className={styles.actions}>
      <Button disabled={busy} onClick={() => void reload()}>刷新文献库</Button>
      <Button onClick={() => setImportOpen(true)}>导入文献</Button>
      <Button disabled={busy} onClick={() => void run(async () => {
        const value = await researchApi.zotero(); if (!active.current) return;
        const labels: Record<string, string> = { unavailable: "未连接到本机 Zotero，请打开 Zotero 后重试", permission_required: "请在 Zotero 设置中允许其他应用访问本地 API", retry_required: "Zotero 数据在导入期间变化，请重试", ok: "已读取 Zotero 文献、笔记和标注；附件尚未读取" };
        setNotice([labels[value.status] || value.message || value.status, ...(value.warnings || [])].join("；")); await reload();
      })}>从本机 Zotero 读取</Button>
      {(["csl-json", "bibtex", "ris"] as const).map((f) => <Button key={f} disabled={busy || !works.length} onClick={() => void run(async () => { const value = await researchApi.exportLibrary(f); if (active.current) downloadText(value.text, `澜台文献库.${f === "csl-json" ? "json" : f === "bibtex" ? "bib" : "ris"}`); })}>导出 {f === "csl-json" ? "CSL JSON" : f === "bibtex" ? "BibTeX" : "RIS"}</Button>)}
    </div>
    {error && <Alert type="error" message={error} closable onClose={() => setError("")} />}
    {notice && <Alert type="info" message={notice} closable onClose={() => setNotice("")} />}
    <h4>公开文献检索</h4>
    <p className={styles.muted}>{detail ? `当前项目：${detail.project.question}` : "选择允许公开检索的项目，再搜索和导入文献。"} 检索记录和摘要不代表已阅读全文。</p>
    <div className={styles.actions}>
      <Select aria-label="文献检索方式" value={queryMode} onChange={setQueryMode} style={{ minWidth: 150 }} options={[{ value: "general", label: "关键词或 DOI" }, { value: "title_author", label: "题名与作者" }]} />
      <Input aria-label={queryMode === "title_author" ? "文献题名" : "文献检索词或 DOI"} value={query} onChange={(e) => setQuery(e.target.value)} placeholder={queryMode === "title_author" ? "文献题名" : "题名、研究问题或 DOI"} maxLength={1000} style={{ flex: 1, minWidth: 200 }} />
      {queryMode === "title_author" && <Input aria-label="文献作者" value={author} onChange={(e) => setAuthor(e.target.value)} placeholder="一位作者姓名（可选，用空格分隔）" maxLength={200} style={{ flex: 1, minWidth: 200 }} />}
      <Button disabled={busy || !query.trim() || detail?.project.policy !== "public_search"} onClick={() => void run(async () => {
        if (!detail) return; const project = detail.project; const selection = projectSelection.current; const value = queryMode === "title_author"
          ? await researchApi.search(project, query, { query_mode: queryMode, author })
          : await researchApi.search(project, query);
        if (active.current && selection === projectSelection.current) {
          if (value.status === "approval_required") { setResult(null); await refreshPreviews(); }
          else setResult(value);
        }
      })}>检索</Button></div>
    {detail && detail.project.policy !== "public_search" && <p className={styles.muted}>此项目限制为本地资料。修改项目的数据策略后才能公开检索。</p>}
    {detail?.project.query_preview_required && <><p className={styles.muted}>每次外发检索需确认完整查询。仅发送下列检索词、作者、标识符与数量参数；失败或中断后不会自动重发。</p>
      <Button disabled={busy} onClick={() => void run(refreshPreviews)}>刷新待确认查询</Button>
      {previews.map((item) => <div className={styles.evidence} key={item.id}>
        <strong>{item.query}</strong><p>修订 {item.revision} · 每个来源最多 {item.limit} 条</p>
        {Object.entries(item.requests).map(([source, request]) => <p key={source}>{source} · {request.endpoint} · {JSON.stringify(request.params)}</p>)}
        {item.status === "pending" ? <div className={styles.actions}>
          <Button disabled={busy} onClick={() => void run(async () => {
            const selection = projectSelection.current; const value = await researchApi.decideQuery(item.id, true);
            if (active.current && selection === projectSelection.current) { setResult(value); await refreshPreviews(); }
          })}>确认并检索</Button>
          <Button disabled={busy} onClick={() => void run(async () => { await researchApi.decideQuery(item.id, false); await refreshPreviews(); })}>取消查询</Button>
        </div> : <p className={styles.muted}>{{ stale: "项目已变化，请重新检索", cancelled: "已取消", dispatched: "已开始发送，结果尚未保存；请勿重复确认", completed: "已保存检索结果" }[item.status] || item.status}</p>}
        {item.result_id && <Button disabled={busy} onClick={() => void run(async () => {
          const selection = projectSelection.current; const value = await researchApi.searchRecord(item.result_id!);
          if (active.current && selection === projectSelection.current) setResult(value);
        })}>查看此次结果</Button>}
      </div>)}
    </>}
    {result && <><p className={styles.muted}>找到 {result.papers.length} 条 · {result.status}</p>
      {Object.entries(result.sources).map(([name, state]) => <p key={name} className={styles.muted}>{name}：{state.cache === "hit" ? state.negative_cache ? "缓存中的零命中" : "使用缓存结果" : "本次查询"}{state.retrieved_at ? ` · 来源获取时间 ${state.retrieved_at}` : ""}{state.retry_at ? ` · 下次可请求时间 ${new Date(state.retry_at * 1000).toLocaleString()}` : ""}</p>)}
      {Object.entries(result.sources).filter(([, state]) => state.status !== "ok").map(([name, state]) => <Alert key={name} type="warning" message={`${name}：${state.status === "rate_limited" ? "请求受到限流" : "服务未完成检索"}${state.http_status ? `（HTTP ${state.http_status}）` : ""}；本次结果不代表完整检索。`} />)}
      {result.papers.map((paper, i) => <div key={`${paper.doi}-${i}`} className={styles.evidence}>
        <strong>{paper.title}</strong><p className={styles.muted}>{paper.authors.join("、")} · {paper.year ?? "年份待补"} · {paper.doi || "无 DOI"}</p>
        {paper.quality_issues.map((issue) => <Tag key={issue} color="orange">{issues[issue] || issue}</Tag>)}
        <div className={styles.actions}><Button disabled={busy} onClick={() => void run(async () => { await researchApi.importCandidate(result.id, i); await reload(); if (active.current) setNotice("文献元数据已导入。全文需另行导入和阅读。"); })}>导入文献库</Button></div>
      </div>)}</>}
    <h4>我的文献 · {visibleWorks.length} / {works.length}</h4>
    <div className={styles.actions}><Input aria-label="筛选标签" placeholder="按标签筛选" value={tagFilter} onChange={(e) => setTagFilter(e.target.value)} allowClear /><Input aria-label="筛选集合" placeholder="按集合筛选" value={collectionFilter} onChange={(e) => setCollectionFilter(e.target.value)} allowClear /></div>
    {loading && <Spin />}{!loading && !works.length && <Empty description="导入 BibTeX、RIS、CSL JSON 或从公开检索添加文献" />}
    {visibleWorks.map((item) => <div key={item.work_id} className={styles.sourceRow}><button type="button" onClick={() => open(item)}>{item.csl.title}</button><span>{item.csl.DOI}</span>{item.organization?.collections.map((label) => <Tag key={`collection:${label}`}>{label}</Tag>)}{item.organization?.tags.map((label) => <Tag key={`tag:${label}`} color="blue">{label}</Tag>)}<Tag>{item.source_versions.length ? "已有全文附件" : "仅文献记录"}</Tag>{item.quality_issues.map((issue) => <Tag key={issue} color="orange">{issues[issue] || issue}</Tag>)}</div>)}
    {works.length > 0 && <><h4>引用格式预览</h4><div className={styles.actions}><Select value={style} onChange={(v) => { setStyle(v); setPreview([]); }} options={[{ value: "gb7714", label: "GB/T 7714—2015" }, { value: "apa", label: "APA 第 7 版" }, { value: "numeric", label: "IEEE 编号" }]} />
      <Button disabled={busy} onClick={() => void run(async () => { const value = await researchApi.bibliography(works.map((w) => w.work_id), style); if (active.current) setPreview(value.bibliography); })}>预览参考文献</Button></div>{preview.map((entry, i) => <p key={i}>{entry}</p>)}</>}
    <p className={styles.muted}>引用由 <a href="https://citeproc-js.readthedocs.io/" target="_blank" rel="noreferrer">citeproc-js · Frank G. Bennett</a> 处理，样式和语言来自 <a href="https://citationstyles.org/" target="_blank" rel="noreferrer">Citation Style Language</a>。样式版本随导出记录保留。</p>
    <Modal title="导入文献" open={importOpen} onCancel={() => setImportOpen(false)} onOk={() => void run(async () => {
      const value = await researchApi.importLibrary(format, text); if (!active.current) return; setImportOpen(false); setText(""); setNotice(`导入 ${value.items.length} 条记录，${value.items.filter((i) => i.conflict).length} 条存在版本冲突。`); await reload();
    })} confirmLoading={busy} okText="导入" okButtonProps={{ disabled: !text.trim() }}>
      <Select value={format} onChange={setFormat} options={[{ value: "csl-json", label: "CSL JSON" }, { value: "bibtex", label: "BibTeX" }, { value: "ris", label: "RIS" }]} />
      <label className={styles.field}>选择文件<input type="file" accept=".json,.bib,.ris" onChange={(e) => { const file = e.target.files?.[0]; if (!file) return; if (file.size > 5000000) { setError("导入文件不能超过 5 MB"); return; } void file.text().then((value) => { if (active.current) setText(value); }); }} /></label>
      <Input.TextArea aria-label="文献导入内容" value={text} onChange={(e) => setText(e.target.value)} autoSize={{ minRows: 7, maxRows: 16 }} />
    </Modal>
    <Drawer title={work?.csl.title} open={!!work} onClose={() => setWork(null)} width="min(820px, 94vw)">
      {work && <>
        <p className={styles.muted}>来源：{Array.from(new Set(work.origins.map((o) => o.provider))).join("、")}。元数据修改生成新版本；历史版本和附件关联保留。</p>
        {work.csl.abstract && <><h4>摘要</h4><p>{work.csl.abstract}</p><Tag>摘要不能代替全文证据</Tag></>}
        <h4>集合与标签</h4>
        <label className={styles.field}>文献集合<Input.TextArea aria-label="文献集合" value={collections} onChange={(e) => setCollections(e.target.value)} placeholder="每行一个集合" /></label>
        <label className={styles.field}>文献标签<Input.TextArea aria-label="文献标签" value={tags} onChange={(e) => setTags(e.target.value)} placeholder="每行一个标签" /></label>
        <Button disabled={busy} onClick={() => void run(async () => {
          const value = await researchApi.organize(work.work_id, { expected_revision: work.organization?.revision ?? 0, tags: labels(tags), collections: labels(collections) });
          if (active.current) open(value); await reload();
        })}>保存分类</Button>
        <p className={styles.muted}>分类独立保存，不改变引用版本；重复导入不会覆盖已保存的分类。</p>
        <h4>版本</h4>{work.versions.map((v) => <div className={styles.actions} key={v.id}><span>{v.csl.title} · {v.metadata_hash.slice(0, 12)}</span><Button disabled={busy || v.id === work.selected_version_id} onClick={() => void run(async () => { const value = await researchApi.chooseVersion(work, v.id); if (active.current) open(value); await reload(); })}>{v.id === work.selected_version_id ? "当前版本" : "选择此版本"}</Button></div>)}
        <h4>编辑元数据（CSL JSON）</h4><Input.TextArea aria-label="文献元数据" value={metadata} onChange={(e) => setMetadata(e.target.value)} autoSize={{ minRows: 8, maxRows: 18 }} />
        <Button disabled={busy} onClick={() => void run(async () => { const value = await researchApi.importLibrary("csl-json", JSON.stringify([JSON.parse(metadata)])); const id = value.items[0]?.work_id; if (id && active.current) open(await researchApi.work(id)); await reload(); })}>保存元数据版本</Button>
        <h4>关联当前项目全文</h4>{detail ? <Select placeholder="选择已有资料附件" style={{ width: "100%" }} value={undefined} disabled={busy} options={detail.sources.filter((s) => s.evidence_level !== "abstract" && s.evidence_level !== "note").map((s) => ({ value: s.id, label: s.path.split("/").pop() }))} onChange={(source: string) => void run(async () => { const value = await researchApi.linkSource(work.work_id, source); if (active.current) setWork(value); await reload(); onProjectChange(); })} /> : <p>先选择研究项目，再关联已导入的全文。</p>}
        <h4>开放全文</h4>{((work.csl.custom as { oa_locations?: { license?: string; url: string; version?: string }[] } | undefined)?.oa_locations || []).map((location, index) => <div key={location.url} className={styles.actions}><span>{location.license || "许可待核对"} · {location.version || "版本待核对"}</span><Button disabled={busy || !location.license || detail?.project.policy !== "public_search"} onClick={() => void run(async () => {
          if (!detail) return; const value = await researchApi.fulltext(work.work_id, detail.project, index); if (active.current) { setWork(value.work); setNotice("全文已导入项目新修订，尚未阅读或核验。"); } onProjectChange(); await reload();
        })}>获取并加入当前项目</Button></div>)}
        <h4>笔记与标注</h4>{work.annotations.map((n) => <div className={styles.evidence} key={n.id}>
          <p>{n.text}</p><span>{n.origin.provider === "user" ? "用户笔记" : ["assistant-chat", "user-chat"].includes(n.origin.provider) ? "模型生成的笔记" : "导入笔记"} · {n.annotation_type}</span>
          {n.version_id && <p className={styles.muted}>文献版本：{n.version_id}</p>}
          {n.anchor && <><p className={styles.muted}>已核对原文 · {n.anchor.pages.filter((page) => page !== null).join("、") || "文本"} · 字符 {n.anchor.start}–{n.anchor.end} · {n.anchor.source_hash.slice(0, 12)}</p><blockquote>{n.anchor.quote}</blockquote></>}
          {n.region_anchor && <><p className={styles.muted}>手工区域 · 第 {n.region_anchor.page} 页 · 尚未核对原文 · {n.region_anchor.source_hash.slice(0, 12)}</p>
            <ResearchPdfRegionNote key={n.id} workId={work.work_id} sourceId={n.region_anchor.source_version_id} saved={n.region_anchor} />
          </>}
          {n.origin.external_anchor && <><p className={styles.muted}>Zotero 第 {n.origin.external_anchor.page_label || "未知"} 页 · 附件版本 {n.origin.external_anchor.attachment_version ?? "未知"} · 尚未与本地全文核对</p>{n.origin.external_anchor.quote && <blockquote>{n.origin.external_anchor.quote}</blockquote>}</>}
          <Button disabled={busy || !detail || detail.project.archived} onClick={() => void run(async () => {
            if (!detail) return; await researchApi.noteSource(n.id, detail.project); onProjectChange();
            if (active.current) setNotice("笔记已作为独立资料加入项目，需重新核验报告。它不能替代论文原文。");
          })}>作为笔记加入当前项目</Button>
        </div>)}
        {work.source_versions.map((linked) => <div key={`${work.work_id}:${linked.source}`}>
          <p className={styles.muted}>全文附件：{detail?.sources.find((s) => s.id === linked.source)?.path.split('/').pop() || linked.source}</p>
          <ResearchPdfRegionNote workId={work.work_id} sourceId={linked.source} onSaved={async () => {
            const currentId = work.work_id; const updated = await researchApi.work(currentId);
            if (active.current) setWork((current) => current?.work_id === currentId ? updated : current);
            await reload();
          }} />
        </div>)}
        <Input.TextArea aria-label="文献笔记" value={note} onChange={(e) => setNote(e.target.value)} />
        <Select aria-label="批注原文版本" allowClear placeholder="可选：选择已关联的原文版本" style={{ width: "100%" }} value={anchorSource} onChange={(value: string | undefined) => { setAnchorSource(value); setAnchorQuote(""); setAnchorStart(null); setAnchorEnd(null); }} options={work.source_versions.map((s) => ({ value: s.source, label: detail?.sources.find((source) => source.id === s.source)?.path.split("/").pop() || s.source }))} />
        {anchorSource && <><Input.TextArea aria-label="批注原文引句" value={anchorQuote} onChange={(e) => setAnchorQuote(e.target.value)} placeholder="从此版本原文复制准确引句" />
          <p className={styles.muted}>引句重复时，可填写从 0 开始的字符位置；结束位置不包含该字符。</p>
          <InputNumber aria-label="批注起始位置" min={0} precision={0} value={anchorStart} onChange={setAnchorStart} />
          <InputNumber aria-label="批注结束位置" min={1} precision={0} value={anchorEnd} onChange={setAnchorEnd} />
        </>}
        <Button disabled={busy || !note.trim() || (!!anchorSource && !anchorQuote.trim()) || ((anchorStart === null) !== (anchorEnd === null))} onClick={() => void run(async () => {
          await researchApi.note(work.work_id, note, anchorSource ? { source_version_id: anchorSource, quote: anchorQuote, ...(anchorStart !== null && anchorEnd !== null ? { start: anchorStart, end: anchorEnd } : {}) } : undefined);
          if (active.current) { setWork(await researchApi.work(work.work_id)); setNote(""); setAnchorSource(undefined); setAnchorQuote(""); setAnchorStart(null); setAnchorEnd(null); }
        })}>保存笔记</Button>
      </>}
    </Drawer>
  </section>;
}

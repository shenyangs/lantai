import { useEffect, useState } from "react";
import { Alert, Input, Modal, Select, Spin } from "antd";
import { Button } from "@/vendor/lantaiDesign";
import { researchApi, type EvidenceSpan, type ResearchDetail } from "@/api/modules/research";
import { codepointSlice, selectionSpan } from "./researchCitationDraft";
import styles from "./research.module.less";

type ReadBlock = Awaited<ReturnType<typeof researchApi.read>>["blocks"][number];
export default function ResearchCitationEditor({ detail, text, onAdd, onClose }: {
  detail: ResearchDetail; text: string; onAdd: (span: EvidenceSpan) => void; onClose: () => void;
}) {
  const [sourceId, setSourceId] = useState(detail.sources[0]?.id ?? "");
  const [blockIndex, setBlockIndex] = useState(0);
  const [loaded, setLoaded] = useState<{ source: string; index: number; block: ReadBlock } | null>(null);
  const [selected, setSelected] = useState<{ start: number; end: number } | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const source = detail.sources.find((item) => item.id === sourceId);
  const block = loaded?.source === sourceId && loaded.index === blockIndex ? loaded.block : null;
  useEffect(() => {
    let current = true;
    setLoaded(null); setSelected(null); setError("");
    if (!sourceId) return;
    setLoading(true);
    researchApi.read(detail.project.project_id, detail.project.revision, sourceId, blockIndex).then((result) => {
      if (!current) return;
      const value = result.blocks.find((item) => item.index === blockIndex);
      if (!value) throw new Error("这一块没有可引用的文字");
      setLoaded({ source: sourceId, index: blockIndex, block: value });
    }).catch((e) => { if (current) setError(e instanceof Error ? e.message : "原文读取失败"); })
      .finally(() => { if (current) setLoading(false); });
    return () => { current = false; };
  }, [detail.project.project_id, detail.project.revision, sourceId, blockIndex]);
  const quote = block && selected ? codepointSlice(block.text, selected.start, selected.end) : "";
  return <Modal title="为报告添加原文引用" open onCancel={onClose} footer={null} width={760}>
    <p>报告中选中的文字</p><blockquote>{text}</blockquote>
    <label className={styles.field}>证据来源<Select aria-label="引用证据来源" value={sourceId} onChange={(id) => {
      setSourceId(id); setBlockIndex(0); setLoaded(null); setSelected(null);
    }} options={detail.sources.map((item) => ({ value: item.id, label: item.path.split("/").pop() }))} /></label>
    <p className={styles.muted}>选择固定版本的原文摘录。读取计入项目阅读额度；添加引用后仍需保存并重新核验其支持程度。</p>
    <div className={styles.actions}>
      <Button disabled={loading || blockIndex === 0} onClick={() => { setSelected(null); setBlockIndex(blockIndex - 1); }}>上一块</Button>
      <span>第 {blockIndex + 1} / {source?.blocks.length ?? 0} 块{block?.page ? ` · 第 ${block.page} 页` : ""}</span>
      <Button disabled={loading || blockIndex + 1 >= (source?.blocks.length ?? 0)} onClick={() => { setSelected(null); setBlockIndex(blockIndex + 1); }}>下一块</Button>
    </div>
    {loading && <Spin size="small" />}
    {error && <Alert type="error" message={error} />}
    {block && <Input.TextArea aria-label="选择原文证据" readOnly value={block.text} autoSize={{ minRows: 6, maxRows: 16 }} onSelect={(event) => {
      const target = event.target as HTMLTextAreaElement;
      setSelected(selectionSpan(block.text, target.selectionStart, target.selectionEnd));
    }} />}
    {quote.trim() && <blockquote aria-label="待添加的摘录">{quote}</blockquote>}
    <Button type="primary" disabled={loading || !block || !quote.trim()} onClick={() => {
      if (block && selected && quote.trim()) onAdd({ source_version_id: sourceId,
        start: block.start + selected.start, end: block.start + selected.end, quote });
    }}>添加引用到草稿</Button>
  </Modal>;
}

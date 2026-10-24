import { useEffect, useRef, useState } from "react";
import { Alert, Input, Spin } from "antd";
import { Button } from "@/vendor/lantaiDesign";
import { researchApi, type ResearchSource, type PdfRegionAnchor } from "@/api/modules/research";
import ResearchPdfPreview from "./ResearchPdfPreview";

/** Key by work/source/annotation so in-flight responses cannot cross selections. */
export default function ResearchPdfRegionNote({ workId, sourceId, saved, onSaved }: {
  workId: string; sourceId: string; saved?: PdfRegionAnchor; onSaved?: () => Promise<void>;
}) {
  const [opened, setOpened] = useState(false);
  const [source, setSource] = useState<ResearchSource | null>(null);
  const [error, setError] = useState("");
  const [attempt, setAttempt] = useState(0);
  const [region, setRegion] = useState<PdfRegionAnchor | null>(null);
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState("");
  const saving = useRef(false);
  const active = useRef(true);
  useEffect(() => { active.current = true; return () => { active.current = false; }; }, []);
  useEffect(() => {
    if (!opened) return;
    let current = true; setSource(null); setRegion(null); setError("");
    void researchApi.source(sourceId).then((value) => {
      if (!current) return;
      if (value.id !== sourceId || (saved && value.raw_hash !== saved.source_hash)) throw new Error("批注与原文版本不一致");
      if (!value.snapshot_path?.toLowerCase().endsWith('.pdf')) throw new Error("此附件不是 PDF，请使用文字批注");
      setSource(value);
    }).catch((e: unknown) => { if (current) setError(e instanceof Error ? e.message : "资料加载失败"); });
    return () => { current = false; };
  }, [opened, sourceId, saved, attempt]);
  if (!opened) return <Button onClick={() => setOpened(true)}>{saved ? "查看批注区域" : "添加 PDF 区域批注"}</Button>;
  return <section aria-label={saved ? "已保存区域批注" : "添加区域批注"}>
    {error && <Alert type="error" message={error} />}
    {!source && (error ? <Button onClick={() => setAttempt((n) => n + 1)}>重试资料</Button> : <Spin />)}
    {source && <>
      <ResearchPdfPreview source={source} initialPage={saved?.page ?? source.pages.find((p) => p.page !== null)?.page ?? 1} start={0} end={0} savedRegion={saved} onRegionChange={saved ? undefined : setRegion} />
      {!saved && <>
        <Input.TextArea aria-label="区域批注内容" value={text} disabled={busy} onChange={(e) => setText(e.target.value)} />
        <Button disabled={busy || !region || !text.trim()} onClick={() => {
          if (saving.current || !region || !text.trim()) return;
          saving.current = true; setBusy(true); setError(""); setNotice("");
          void researchApi.regionNote(workId, text, region).then(async () => {
            if (!active.current) return;
            setText(""); setNotice("区域批注已保存，尚未核对原文。");
            await onSaved?.();
          }).catch((e: unknown) => { if (active.current) setError(e instanceof Error ? e.message : "区域批注保存失败"); })
            .finally(() => { saving.current = false; if (active.current) setBusy(false); });
        }}>保存区域批注</Button>
      </>}
    </>}
    {notice && <p role="status">{notice}</p>}
  </section>;
}

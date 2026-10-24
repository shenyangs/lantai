import { useEffect, useState } from "react";
import { Alert, Select, Spin } from "antd";
import { Button } from "@/vendor/lantaiDesign";
import { researchApi, type ResearchSource, type ResearchPagePreview, type PdfRegionAnchor } from "@/api/modules/research";

import ResearchPdfRegionSelection from "./ResearchPdfRegionSelection";
import ResearchPdfTables from "./ResearchPdfTables";

export default function ResearchPdfPreview({ source, initialPage, start, end, onRegionChange, savedRegion }: {
  source: ResearchSource; initialPage: number; start: number; end: number;
  onRegionChange?: (value: PdfRegionAnchor | null) => void; savedRegion?: PdfRegionAnchor;
}) {
  const [opened, setOpened] = useState(false);
  const [page, setPage] = useState(initialPage);
  const [attempt, setAttempt] = useState(0);
  const [result, setResult] = useState<ResearchPagePreview | null>(null);
  const [error, setError] = useState("");
  useEffect(() => {
    if (!opened) return;
    let active = true;
    setResult(null); setError("");
    void researchApi.sourcePage(source.id, page).then((value) => {
      if (!active) return;
      if (value.source_version_id !== source.id || value.raw_hash !== source.raw_hash || value.page !== page) {
        setError("原页与资料版本不一致，请重新打开资料"); return;
      }
      setResult(value);
    }).catch((reason: unknown) => {
      if (active) setError(reason instanceof Error ? reason.message : "原页加载失败");
    });
    return () => { active = false; };
  }, [opened, source.id, source.raw_hash, page, attempt]);
  const current = result?.source_version_id === source.id && result.page === page && result.raw_hash === source.raw_hash ? result : null;
  const regions = current?.overlay_allowed ? (source.pages.find((p) => p.page === page)?.regions ?? [])
    .filter((r) => r.start < end && r.end > start) : [];
  if (!opened) return <Button onClick={() => setOpened(true)}>查看 PDF 原页</Button>;
  return <section aria-label="PDF 原页预览">
    <p><Select aria-label="原页页码" value={page} onChange={(value) => { setPage(value); onRegionChange?.(null); }} style={{ minWidth: 130 }}
      options={source.pages.filter((p) => p.page !== null).map((p) => ({ value: p.page!, label: `第 ${p.page} 页` }))} />
      <Button onClick={() => { setOpened(false); onRegionChange?.(null); }}>收起原页</Button></p>
    {error ? <><Alert type="error" message={error} /><Button onClick={() => setAttempt((n) => n + 1)}>重试原页</Button></> : !current ? <Spin /> : <>
      {!current.overlay_allowed && <Alert type="info" message="此页的坐标无法可靠叠加，请结合下方摘录核对原页。" />}
      <ResearchPdfRegionSelection key={`region:${source.id}:${source.raw_hash}:${page}`} identity={{ source_version_id: source.id, source_hash: source.raw_hash, page }} saved={savedRegion} onChange={onRegionChange}>
        <img draggable={false} alt={`PDF 原始快照第 ${page} 页`} src={`data:image/png;base64,${current.png_base64}`}
          width={current.width} height={current.height} style={{ width: "100%", height: "auto", display: "block" }} />
        {regions.map((r, i) => <span key={i} aria-label="当前摘录定位框" style={{ position: "absolute", pointerEvents: "none",
          left: `${r.x * 100}%`, top: `${r.y * 100}%`, width: `${r.width * 100}%`, height: `${r.height * 100}%`,
          background: "rgba(255, 190, 40, .28)", outline: "1px solid #b98513" }} />)}
      </ResearchPdfRegionSelection>
      <p>原始快照 · 第 {page} 页 · 高亮仅定位当前摘录，OCR 文字与数字仍需核对。</p>
      <ResearchPdfTables key={`tables:${source.id}:${source.raw_hash}:${page}`} sourceId={source.id} rawHash={source.raw_hash} page={page} />
    </>}
  </section>;
}

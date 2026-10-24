import { useEffect, useState } from "react";
import { Alert, Spin } from "antd";
import { Button } from "@/vendor/lantaiDesign";
import { researchApi, type ResearchPageTables } from "@/api/modules/research";

export default function ResearchPdfTables({ sourceId, rawHash, page }: { sourceId: string; rawHash: string; page: number }) {
  const [attempt, setAttempt] = useState(0);
  const [result, setResult] = useState<ResearchPageTables | null>(null);
  const [error, setError] = useState("");
  useEffect(() => {
    if (!attempt) return;
    let active = true;
    setResult(null); setError("");
    void researchApi.sourceTables(sourceId, page).then((value) => {
      if (!active) return;
      if (value.source_version_id !== sourceId || value.raw_hash !== rawHash || value.page !== page) {
        setError("表格与资料版本不一致，请重试"); return;
      }
      setResult(value);
    }).catch((reason: unknown) => {
      if (active) setError(reason instanceof Error ? reason.message : "表格解析失败");
    });
    return () => { active = false; };
  }, [sourceId, rawHash, page, attempt]);
  if (!attempt) return <Button onClick={() => setAttempt(1)}>查看本页表格结构</Button>;
  return <section aria-label="PDF 表格结构">
    {error ? <><Alert type="error" message={error} /><Button onClick={() => setAttempt((n) => n + 1)}>重试表格解析</Button></> : !result ? <Spin /> : <>
      <Alert type="info" message={result.status === "needs_visual_review"
        ? "以下为网格提取结果，表头与脚注归属尚未确认；请对照原页核对数字、单位和合并单元格。"
        : result.status === "no_ruled_tables" ? "未检出带网格线的表格；无框线或扫描表格请直接核对原页。"
        : result.status === "size_limit" ? "本页表格或文字超出解析大小限制，请直接核对原页。"
        : "本页表格无法可靠解析，请直接核对原页。"} />
      {result.tables.map((table, index) => <div key={index} style={{ overflowX: "auto", marginBlock: 12 }}>
        <table aria-label={`本页表格 ${index + 1}`} style={{ borderCollapse: "collapse", width: "100%" }}>
          <caption>表格 {index + 1} · 第 {page} 页 · 待核对</caption>
          <tbody>{table.rows.map((row, r) => <tr key={r}>{row.map((cell, c) => <td key={c}
            style={{ border: "1px solid var(--ant-color-border, #d9d9d9)", padding: "6px 10px", whiteSpace: "pre-wrap", verticalAlign: "top" }}>
            {cell === null ? <small>合并或缺失位置</small> : cell.text || <span aria-label="空单元格">—</span>}
          </td>)}</tr>)}</tbody>
        </table>
      </div>)}
      {result.page_text && <details><summary>本页文字（含可能的表头与脚注，关联未判定）</summary>
        <pre style={{ whiteSpace: "pre-wrap", overflowWrap: "anywhere", maxHeight: 320, overflowY: "auto" }}>{result.page_text}</pre>
      </details>}
    </>}
  </section>;
}

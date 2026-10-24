import { useRef, useState, type ReactNode, type PointerEvent } from "react";
import { Button } from "@/vendor/lantaiDesign";
import type { PdfRegionAnchor } from "@/api/modules/research";

type Rect = Pick<PdfRegionAnchor, "x" | "y" | "width" | "height">;
const valid = (r: Rect) => Object.values(r).every(Number.isFinite) && r.x >= 0 && r.y >= 0 && r.width > 0 && r.height > 0 && r.x + r.width <= 1 && r.y + r.height <= 1;
export default function ResearchPdfRegionSelection({ children, identity, saved, onChange }: {
  children: ReactNode; identity: Pick<PdfRegionAnchor, "source_version_id" | "source_hash" | "page">;
  saved?: PdfRegionAnchor; onChange?: (value: PdfRegionAnchor | null) => void;
}) {
  const [drawing, setDrawing] = useState(false);
  const [rect, setRect] = useState<Rect | null>(null);
  const [fields, setFields] = useState<Rect>({ x: .1, y: .1, width: .3, height: .2 });
  const origin = useRef<{ x: number; y: number; id: number } | null>(null);
  const publish = (r: Rect | null) => { setRect(r); onChange?.(r ? { ...identity, coordinate_system: "pdf_raster_normalized_top_left_v1", ...r } : null); };
  const point = (e: PointerEvent<HTMLDivElement>) => {
    const bounds = e.currentTarget.getBoundingClientRect();
    if (bounds.width <= 0 || bounds.height <= 0) return null;
    return { x: Math.max(0, Math.min(1, (e.clientX - bounds.left) / bounds.width)), y: Math.max(0, Math.min(1, (e.clientY - bounds.top) / bounds.height)) };
  };
  const move = (e: PointerEvent<HTMLDivElement>) => {
    const p = point(e), start = origin.current;
    if (!p || !start || start.id !== e.pointerId) return null;
    const r = { x: Math.min(p.x, start.x), y: Math.min(p.y, start.y), width: Math.abs(p.x - start.x), height: Math.abs(p.y - start.y) };
    setRect(r); return r;
  };
  const marker = saved?.source_version_id === identity.source_version_id && saved.source_hash === identity.source_hash && saved.page === identity.page ? saved : rect;
  return <>
    {onChange && <><Button onClick={() => { origin.current = null; publish(null); setDrawing(!drawing); }}>{drawing ? "取消画选区" : "画选区"}</Button>
      <Button onClick={() => { origin.current = null; publish(null); setDrawing(false); }}>清除选区</Button>
      <p>在原页上拖动画框；也可输入相对于页面左上角的百分比，再应用坐标。区域仅用于定位，不代表核验原文。</p>
      <div>{([['x', '左侧'], ['y', '顶部'], ['width', '宽度'], ['height', '高度']] as const).map(([key, label]) => <label key={key}>{label} % <input aria-label={`选区${label}百分比`} type="number" min={0} max={100} step="any" value={Number.isNaN(fields[key]) ? '' : Math.round(fields[key] * 100000) / 1000} onChange={(e) => setFields({ ...fields, [key]: e.target.value === '' ? NaN : Number(e.target.value) / 100 })} style={{ width: 75 }} /></label>)}</div>
      <Button disabled={!valid(fields)} onClick={() => { origin.current = null; setDrawing(false); publish(fields); }}>应用选区坐标</Button>
    </>}
    <div aria-label="PDF 区域选择" tabIndex={drawing ? 0 : undefined} onKeyDown={(e) => { if (e.key === 'Escape') { origin.current = null; publish(null); setDrawing(false); } }}
      style={{ position: "relative", width: "100%", lineHeight: 0, touchAction: drawing ? "none" : "auto", cursor: drawing ? "crosshair" : "auto" }}
      onPointerDown={(e) => { if (!drawing || !onChange || e.button !== 0 || origin.current) return; const p = point(e); if (!p) return; e.preventDefault(); e.currentTarget.focus(); origin.current = { ...p, id: e.pointerId }; publish(null); e.currentTarget.setPointerCapture?.(e.pointerId); }}
      onPointerMove={(e) => { if (origin.current) move(e); }}
      onPointerUp={(e) => { if (!origin.current || origin.current.id !== e.pointerId) return; const r = move(e); origin.current = null; if (r && valid(r)) { publish(r); setFields(r); } else publish(null); e.currentTarget.releasePointerCapture?.(e.pointerId); setDrawing(false); }}
      onPointerCancel={() => { origin.current = null; publish(null); setDrawing(false); }}
      onLostPointerCapture={() => { if (origin.current) { origin.current = null; publish(null); setDrawing(false); } }}>
      {children}
      {marker && <span aria-label="手工批注区域" style={{ position: "absolute", pointerEvents: "none", left: `${marker.x * 100}%`, top: `${marker.y * 100}%`, width: `${marker.width * 100}%`, height: `${marker.height * 100}%`, background: "rgba(60,120,220,.2)", outline: "2px solid #3c78dc" }} />}
    </div>
  </>;
}

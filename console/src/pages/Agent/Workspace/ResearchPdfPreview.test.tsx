import { useState } from "react";
import { fireEvent, render, screen, waitFor, cleanup } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import ResearchPdfPreview from "./ResearchPdfPreview";
import type { ResearchSource } from "@/api/modules/research";
const { sourcePage, sourceTables } = vi.hoisted(() => ({ sourcePage: vi.fn(), sourceTables: vi.fn() }));
vi.mock("@/api/modules/research", () => ({ researchApi: { sourcePage, sourceTables } }));
afterEach(cleanup);
const tableResult = { source_version_id: "one", raw_hash: "hash", page: 1, status: "needs_visual_review", page_text: "* Units need checking.",
  tables: [{ rows: [[{ text: "Value*", bbox: [0, 0, 10, 10] }, null], [{ text: "12.5", bbox: [0, 10, 10, 20] }, { text: "", bbox: [10, 10, 20, 20] }]], header_status: "unreviewed", footnote_status: "unresolved" }] };
it("loads table cells only on request and keeps uncertainty and page context visible", async () => {
  sourcePage.mockReset().mockResolvedValue(page);
  sourceTables.mockReset().mockResolvedValue(tableResult);
  render(<ResearchPdfPreview source={source} initialPage={1} start={0} end={5} />);
  fireEvent.click(screen.getByRole("button", { name: "查看 PDF 原页" }));
  await screen.findByAltText("PDF 原始快照第 1 页");
  expect(sourceTables).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "查看本页表格结构" }));
  await screen.findByRole("table", { name: "本页表格 1" });
  expect(screen.getByText("12.5")).toBeTruthy();
  expect(screen.getByText("合并或缺失位置")).toBeTruthy();
  expect(screen.getByText(/表头与脚注归属尚未确认/)).toBeTruthy();
  expect(screen.getByText("* Units need checking.")).toBeTruthy();
  expect(screen.getByAltText("PDF 原始快照第 1 页")).toBeTruthy();
});
it("rejects mismatched table versions and retries without hiding the original page", async () => {
  sourcePage.mockReset().mockResolvedValue(page);
  sourceTables.mockReset().mockResolvedValueOnce({ ...tableResult, raw_hash: "stale" }).mockResolvedValue(tableResult);
  render(<ResearchPdfPreview source={source} initialPage={1} start={0} end={5} />);
  fireEvent.click(screen.getByRole("button", { name: "查看 PDF 原页" }));
  await screen.findByAltText("PDF 原始快照第 1 页");
  fireEvent.click(screen.getByRole("button", { name: "查看本页表格结构" }));
  await screen.findByText("表格与资料版本不一致，请重试");
  expect(screen.queryByRole("table")).toBeNull();
  expect(screen.getByAltText("PDF 原始快照第 1 页")).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "重试表格解析" }));
  await screen.findByText("12.5");
});
it("discards a late table response when moving to another page", async () => {
  let resolve!: (value: typeof tableResult) => void;
  sourcePage.mockReset().mockImplementation((_id, n) => Promise.resolve({ ...page, page: n }));
  sourceTables.mockReset().mockImplementationOnce(() => new Promise((r) => { resolve = r; }));
  render(<ResearchPdfPreview source={{ ...source, pages: [...source.pages, { page: 2 } as ResearchSource['pages'][0]] }} initialPage={1} start={0} end={0} />);
  fireEvent.click(screen.getByRole("button", { name: "查看 PDF 原页" }));
  await screen.findByAltText("PDF 原始快照第 1 页");
  fireEvent.click(screen.getByRole("button", { name: "查看本页表格结构" }));
  fireEvent.mouseDown(screen.getByRole("combobox", { name: "原页页码" }));
  fireEvent.click(screen.getByText("第 2 页"));
  await screen.findByAltText("PDF 原始快照第 2 页");
  resolve(tableResult);
  await waitFor(() => expect(screen.getByRole("button", { name: "查看本页表格结构" })).toBeTruthy());
  expect(screen.queryByText("12.5")).toBeNull();
  expect(sourceTables).toHaveBeenCalledTimes(1);
});
const source = { id: "one", raw_hash: "hash", pages: [{ page: 1, regions: [
  { start: 0, end: 5, x: .1, y: .2, width: .3, height: .04 },
  { start: 10, end: 15, x: .5, y: .2, width: .2, height: .04 },
] }] } as ResearchSource;
const page = { source_version_id: "one", raw_hash: "hash", page: 1, width: 600, height: 800, png_base64: "image", overlay_allowed: true, overlay_reason: "" };
it("keeps one preview and preserves table state when a parent stores region changes", async () => {
  sourcePage.mockReset().mockResolvedValue(page);
  sourceTables.mockReset().mockResolvedValue(tableResult);
  function Parent() {
    const [region, setRegion] = useState<unknown>(null);
    return <><output>{JSON.stringify(region)}</output><ResearchPdfPreview source={source} initialPage={1} start={0} end={0} onRegionChange={setRegion} /></>;
  }
  render(<Parent />);
  fireEvent.click(screen.getByRole("button", { name: "查看 PDF 原页" }));
  await screen.findByAltText("PDF 原始快照第 1 页");
  fireEvent.click(screen.getByRole("button", { name: "查看本页表格结构" }));
  await screen.findByRole("table", { name: "本页表格 1" });
  for (const left of [18, 25, 30]) {
    fireEvent.change(screen.getByRole("spinbutton", { name: "选区左侧百分比" }), { target: { value: String(left) } });
    fireEvent.click(screen.getByRole("button", { name: "应用选区坐标" }));
    expect(screen.getAllByLabelText("PDF 区域选择")).toHaveLength(1);
    expect(screen.getAllByAltText("PDF 原始快照第 1 页")).toHaveLength(1);
    expect(screen.getAllByRole("spinbutton", { name: "选区左侧百分比" })).toHaveLength(1);
    expect(screen.getByLabelText("手工批注区域").style.left).toBe(`${left}%`);
    expect(screen.getByRole("table", { name: "本页表格 1" })).toBeTruthy();
  }
  fireEvent.click(screen.getByRole("button", { name: "清除选区" }));
  expect(screen.queryByLabelText("手工批注区域")).toBeNull();
  expect(screen.getAllByLabelText("PDF 区域选择")).toHaveLength(1);
  expect(sourcePage).toHaveBeenCalledTimes(1);
  expect(sourceTables).toHaveBeenCalledTimes(1);
});
it("loads only on request and highlights the selected evidence", async () => {
  sourcePage.mockReset().mockResolvedValue(page);
  render(<ResearchPdfPreview source={source} initialPage={1} start={0} end={5} />);
  expect(sourcePage).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "查看 PDF 原页" }));
  await screen.findByAltText("PDF 原始快照第 1 页");
  expect(screen.getAllByLabelText("当前摘录定位框")).toHaveLength(1);
});
it("declines uncertain geometry and does not display stale responses", async () => {
  let resolve!: (value: typeof page) => void;
  sourcePage.mockReset().mockImplementationOnce(() => new Promise((r) => { resolve = r; })).mockResolvedValue({ ...page, source_version_id: "two", overlay_allowed: false });
  const view = render(<ResearchPdfPreview source={source} initialPage={1} start={0} end={5} />);
  fireEvent.click(screen.getByRole("button", { name: "查看 PDF 原页" }));
  view.rerender(<ResearchPdfPreview source={{ ...source, id: "two" }} initialPage={1} start={0} end={5} />);
  resolve(page);
  await waitFor(() => expect(screen.getByText(/坐标无法可靠叠加/)).toBeTruthy());
  expect(screen.queryByLabelText("当前摘录定位框")).toBeNull();
});
it("shows an error with retry without hiding the reader", async () => {
  sourcePage.mockReset().mockRejectedValueOnce(new Error("快照校验失败")).mockResolvedValue(page);
  render(<ResearchPdfPreview source={source} initialPage={1} start={0} end={5} />);
  fireEvent.click(screen.getByRole("button", { name: "查看 PDF 原页" }));
  await screen.findByText("快照校验失败");
  fireEvent.click(screen.getByRole("button", { name: "重试原页" }));
  await screen.findByAltText("PDF 原始快照第 1 页");
});

it("draws a manual raster region and clears it when changing pages", async () => {
  sourcePage.mockReset().mockImplementation((_id, n) => Promise.resolve({ ...page, page: n, overlay_allowed: false }));
  const change = vi.fn();
  const view = render(<ResearchPdfPreview source={{ ...source, pages: [...source.pages, { page: 2 } as ResearchSource['pages'][0]] }} initialPage={1} start={0} end={0} onRegionChange={change} />);
  fireEvent.click(screen.getByRole('button', { name: '查看 PDF 原页' }));
  await screen.findByAltText('PDF 原始快照第 1 页');
  fireEvent.click(screen.getByRole('button', { name: '画选区' }));
  const area = screen.getByLabelText('PDF 区域选择');
  vi.spyOn(area, 'getBoundingClientRect').mockReturnValue({ left: 0, top: 0, width: 600, height: 800 } as DOMRect);
  vi.stubGlobal('PointerEvent', MouseEvent);
  fireEvent.pointerDown(area, { clientX: 240, clientY: 400, button: 0 });
  fireEvent.pointerMove(area, { clientX: 60, clientY: 160 });
  fireEvent.pointerUp(area, { clientX: 60, clientY: 160 });
  expect(change).toHaveBeenLastCalledWith(expect.objectContaining({ source_version_id: 'one', source_hash: 'hash', page: 1, x: .1, y: .2, width: expect.closeTo(.3), height: expect.closeTo(.3) }));
  expect(screen.queryByLabelText('当前摘录定位框')).toBeNull();
  fireEvent.mouseDown(screen.getByRole('combobox', { name: '原页页码' }));
  fireEvent.click(screen.getByText('第 2 页'));
  await screen.findByAltText('PDF 原始快照第 2 页');
  expect(change).toHaveBeenLastCalledWith(null);
  view.unmount(); vi.unstubAllGlobals();
});

it('supports keyboard coordinates and clears cancelled or closed selections', async () => {
  sourcePage.mockReset().mockResolvedValue(page);
  const change = vi.fn();
  render(<ResearchPdfPreview source={source} initialPage={1} start={0} end={0} onRegionChange={change} />);
  fireEvent.click(screen.getByRole('button', { name: '查看 PDF 原页' }));
  await screen.findByAltText('PDF 原始快照第 1 页');
  fireEvent.change(screen.getByRole('spinbutton', { name: '选区左侧百分比' }), { target: { value: '95' } });
  expect((screen.getByRole('button', { name: '应用选区坐标' }) as HTMLButtonElement).disabled).toBe(true);
  fireEvent.change(screen.getByRole('spinbutton', { name: '选区左侧百分比' }), { target: { value: '25' } });
  fireEvent.click(screen.getByRole('button', { name: '应用选区坐标' }));
  expect(change).toHaveBeenLastCalledWith(expect.objectContaining({ x: .25 }));
  fireEvent.click(screen.getByRole('button', { name: '画选区' }));
  fireEvent.keyDown(screen.getByLabelText('PDF 区域选择'), { key: 'Escape' });
  expect(change).toHaveBeenLastCalledWith(null);
  expect(screen.queryByLabelText('手工批注区域')).toBeNull();
  fireEvent.click(screen.getByRole('button', { name: '应用选区坐标' }));
  fireEvent.pointerCancel(screen.getByLabelText('PDF 区域选择'));
  expect(change).toHaveBeenLastCalledWith(null);
  fireEvent.click(screen.getByRole('button', { name: '应用选区坐标' }));
  fireEvent.click(screen.getByRole('button', { name: '收起原页' }));
  expect(change).toHaveBeenLastCalledWith(null);
});

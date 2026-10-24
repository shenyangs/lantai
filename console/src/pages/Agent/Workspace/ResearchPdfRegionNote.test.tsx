import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { expect, it, vi } from "vitest";
import ResearchPdfRegionNote from "./ResearchPdfRegionNote";
const api = vi.hoisted(() => ({ source: vi.fn(), sourcePage: vi.fn(), regionNote: vi.fn() }));
vi.mock("@/api/modules/research", () => ({ researchApi: api }));
const anchor = { source_version_id: "s", source_hash: "hash", page: 1, coordinate_system: "pdf_raster_normalized_top_left_v1" as const, x: .1, y: .1, width: .3, height: .2 };
function prepare() {
  api.source.mockReset().mockResolvedValue({ id: 's', raw_hash: 'hash', snapshot_path: 'snapshot.pdf', pages: [{ page: 1 }] });
  api.sourcePage.mockReset().mockResolvedValue({ source_version_id: 's', raw_hash: 'hash', page: 1, width: 600, height: 800, png_base64: 'image', overlay_allowed: false });
  api.regionNote.mockReset();
}
it('preserves text and selected region on save failure and allows retry', async () => {
  prepare(); api.regionNote.mockRejectedValueOnce(new Error('保存失败')).mockResolvedValue({});
  const saved = vi.fn();
  render(<ResearchPdfRegionNote workId="w" sourceId="s" onSaved={saved} />);
  fireEvent.click(screen.getByRole('button', { name: '添加 PDF 区域批注' }));
  fireEvent.click(await screen.findByRole('button', { name: '查看 PDF 原页' }));
  await screen.findByAltText('PDF 原始快照第 1 页');
  fireEvent.click(screen.getByRole('button', { name: '应用选区坐标' }));
  fireEvent.change(screen.getByRole('textbox', { name: '区域批注内容' }), { target: { value: 'Compare this chart' } });
  fireEvent.click(screen.getByRole('button', { name: '保存区域批注' }));
  await screen.findByText('保存失败');
  expect((screen.getByRole('textbox', { name: '区域批注内容' }) as HTMLTextAreaElement).value).toBe('Compare this chart');
  expect(api.regionNote).toHaveBeenLastCalledWith('w', 'Compare this chart', anchor);
  expect(saved).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole('button', { name: '保存区域批注' }));
  await waitFor(() => expect(saved).toHaveBeenCalledTimes(1));
});
it('reopens only the saved region against its snapshot and rejects a changed hash', async () => {
  prepare();
  const view = render(<ResearchPdfRegionNote workId="w" sourceId="s" saved={anchor} />);
  fireEvent.click(screen.getByRole('button', { name: '查看批注区域' }));
  fireEvent.click(await screen.findByRole('button', { name: '查看 PDF 原页' }));
  await screen.findByLabelText('手工批注区域');
  expect(screen.queryByRole('button', { name: '保存区域批注' })).toBeNull();
  view.unmount(); api.source.mockResolvedValue({ id: 's', raw_hash: 'changed' });
  render(<ResearchPdfRegionNote workId="w" sourceId="s" saved={anchor} />);
  fireEvent.click(screen.getByRole('button', { name: '查看批注区域' }));
  await screen.findByText('批注与原文版本不一致');
  expect(screen.queryByLabelText('手工批注区域')).toBeNull();
});

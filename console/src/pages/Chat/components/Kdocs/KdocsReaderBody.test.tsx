import { createRef } from "react";
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, it, expect, vi } from "vitest";
import KdocsReaderBody from "./KdocsReaderBody";

const text =
  "# 预算\n\n重复 **跨节点** 文本。重复。\n\n## 明细\n\n| 项目 | 说明 |\n| --- | --- |\n| 搭建 | 第一行<br>第二行 |";
function mount(target?: { text: string; start?: number; end?: number }) {
  const contentRef = createRef<HTMLDivElement>();
  const onError = vi.fn();
  const onPosition = vi.fn();
  const view = render(
    <KdocsReaderBody
      text={text}
      contentRef={contentRef}
      position={{ scroll: 0, query: "", match: 0 }}
      onPosition={onPosition}
      onError={onError}
      target={target}
    />,
  );
  return { ...view, contentRef, onError, onPosition };
}
describe("reader interactions", () => {
  it("searches rendered text across inline markup and navigates matches", () => {
    mount();
    const input = screen.getByRole("textbox", { name: "文内搜索" });
    fireEvent.change(input, { target: { value: "重复" } });
    expect(screen.getByRole("status")).toHaveTextContent("1 / 2");
    fireEvent.click(screen.getByRole("button", { name: "下一处" }));
    expect(screen.getByRole("status")).toHaveTextContent("2 / 2");
    fireEvent.change(input, { target: { value: "重复 跨节点" } });
    expect(screen.getByRole("status")).toHaveTextContent("1 / 1");
    fireEvent.keyDown(input, { key: "Escape" });
    expect(input).toHaveValue("");
  });
  it("provides actual headings and safe table line breaks", () => {
    const { container } = mount();
    expect(
      screen.getByRole("combobox", { name: "章节目录" }),
    ).toHaveTextContent("预算");
    expect(container.querySelector("td br")).not.toBeNull();
    expect(screen.getByLabelText("表格，可横向滚动")).toContainElement(
      screen.getByRole("table"),
    );
  });
  it("refuses ambiguous excerpt fallback", () => {
    const { onError } = mount({ text: "重复" });
    expect(onError).toHaveBeenCalledWith(
      expect.stringContaining("无法唯一定位"),
    );
  });
  it("accepts an exact offset among duplicate passages", () => {
    const { onError } = mount({ text: "重复", start: 3, end: 5 });
    expect(onError).not.toHaveBeenCalled();
  });
  it("restores the snapshot's search and scroll without jumping to a search match", () => {
    const contentRef = createRef<HTMLDivElement>();
    const onPosition = vi.fn();
    render(
      <KdocsReaderBody
        text={text}
        contentRef={contentRef}
        position={{ scroll: 240, query: "重复", match: 1 }}
        onPosition={onPosition}
        onError={vi.fn()}
      />,
    );
    expect(contentRef.current?.scrollTop).toBe(240);
    expect(screen.getByRole("textbox", { name: "文内搜索" })).toHaveValue(
      "重复",
    );
    expect(screen.getByRole("status")).toHaveTextContent("2 / 2");
    expect(onPosition).toHaveBeenLastCalledWith({
      scroll: 240,
      query: "重复",
      match: 1,
    });
  });
});

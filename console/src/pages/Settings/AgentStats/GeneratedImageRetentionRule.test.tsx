import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { GeneratedImageRetentionRule } from "./GeneratedImageRetentionRule";

describe("GeneratedImageRetentionRule", () => {
  it("offers the five generated-image retention choices", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(
      <GeneratedImageRetentionRule
        days={30}
        disabled={false}
        onChange={onChange}
      />,
    );

    const select = screen.getByRole("combobox", {
      name: "AI 生成图片临时文件",
    });
    expect(
      screen.getAllByRole("option").map((option) => option.textContent),
    ).toEqual([
      "保留 3 天",
      "保留 7 天",
      "保留 15 天",
      "保留 1 个月",
      "永久保留",
    ]);

    await user.selectOptions(select, "7");

    expect(onChange).toHaveBeenCalledWith(7);
  });

  it("explains that downloaded copies are not deleted", () => {
    render(
      <GeneratedImageRetentionRule
        days={30}
        disabled={false}
        onChange={() => undefined}
      />,
    );

    expect(
      screen.getByText("不会清理你已下载到“下载”文件夹的副本。"),
    ).toBeVisible();
  });
});

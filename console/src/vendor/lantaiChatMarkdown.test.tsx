import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { Markdown } from "./lantaiChat";

describe("streaming markdown cursor", () => {
  it("renders the Lantai breathing caret inline without exposing cursor text", () => {
    const { container } = render(<Markdown content="- 从多平台账号" cursor />);

    const listItem = screen.getByText("从多平台账号").closest("li");
    const cursor = listItem?.querySelector(".lantai-stream-cursor");

    expect(cursor).toBeInTheDocument();
    expect(cursor).toHaveAttribute("aria-hidden", "true");
    expect(container).not.toHaveTextContent("▋");
  });

  it("keeps ordinary markdown links intact while streaming", () => {
    render(<Markdown content="[资料](https://example.com)" cursor />);

    expect(screen.getByRole("link", { name: "资料" })).toHaveAttribute(
      "href",
      "https://example.com",
    );
  });
});

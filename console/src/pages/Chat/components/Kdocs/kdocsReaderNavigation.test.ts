import { describe, expect, it } from "vitest";
import { findTextMatches, textRange } from "./kdocsReaderNavigation";

describe("reading snapshot navigation", () => {
  it("uses literal queries and preserves repeated occurrences", () => {
    expect(findTextMatches("a.b A.B a?b", "a.b")).toEqual([
      [0, 3],
      [4, 7],
    ]);
    expect(findTextMatches("正文", " ")).toEqual([]);
  });
  it("locates rendered text across inline markup without confusing duplicates", () => {
    const root = document.createElement("div");
    root.innerHTML = "<p>预算<strong>八万</strong></p><p>预算八万</p>";
    const range = textRange(root, 4, 8);
    expect(range?.toString()).toBe("预算八万");
    expect(range?.startContainer.parentElement?.tagName).toBe("P");
    expect(textRange(root, 90, 99)).toBeNull();
  });
});

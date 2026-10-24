import { describe, expect, it } from "vitest";

import { isInternalWorkspaceFile } from "./utils";

describe("isInternalWorkspaceFile", () => {
  it("keeps Agent canon and on-demand references out of user knowledge", () => {
    expect(isInternalWorkspaceFile("SELF_CANON.md")).toBe(true);
    expect(isInternalWorkspaceFile("VISUAL_CANON.md")).toBe(true);
    expect(isInternalWorkspaceFile("PRODUCT_GUIDE.md")).toBe(true);
    expect(isInternalWorkspaceFile("campaign-notes.md")).toBe(false);
  });
});

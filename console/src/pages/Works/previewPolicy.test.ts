import { describe, expect, it } from "vitest";

import { workCardPreviewMode } from "./previewPolicy";

const work = (kind: string, cover_path: string | null = null) =>
  ({ kind, cover_path }) as any;

describe("workCardPreviewMode", () => {
  it("uses a stable thumbnail for HTML instead of executing it in the list", () => {
    expect(workCardPreviewMode(work("html"))).toBe("native-thumbnail");
  });

  it("prefers an explicit generated cover for every file type", () => {
    expect(workCardPreviewMode(work("html", "cover.png"))).toBe(
      "generated-cover",
    );
  });
});

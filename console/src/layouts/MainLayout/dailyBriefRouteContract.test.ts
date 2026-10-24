import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

const source = readFileSync(
  join(process.cwd(), "src/layouts/MainLayout/index.tsx"),
  "utf8",
);

describe("daily brief route contract", () => {
  it("maps the workbench route to its own sidebar entry", () => {
    expect(source).toContain('"/daily-brief": "daily-brief"');
  });

  it("keeps a single in-app workbench route", () => {
    expect(source).toContain(
      '<Route path="/daily-brief" element={<DailyBriefPage />} />',
    );
  });
});

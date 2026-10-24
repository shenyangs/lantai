import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

const stylesheet = readFileSync(
  join(process.cwd(), "src/pages/Control/CronJobs/index.module.less"),
  "utf8",
);
const pageSource = readFileSync(
  join(process.cwd(), "src/pages/Control/CronJobs/index.tsx"),
  "utf8",
);

describe("automation calendar responsive layout", () => {
  it("does not make each day wider at the intermediate desktop breakpoint", () => {
    const intermediate = stylesheet.match(
      /@media \(max-width: 1320px\)[\s\S]*?(?=@media \(max-width: 760px\))/,
    )?.[0];

    expect(intermediate).toBeTruthy();
    expect(intermediate).not.toContain("minmax(178px, 1fr)");
    expect(intermediate).toContain("minmax(0, 1fr)");
  });

  it("announces the selected view and uses the shared add icon", () => {
    expect(pageSource).toContain('role="tablist"');
    expect(pageSource).toContain('role="tab"');
    expect(pageSource).toContain('aria-selected={viewMode === "calendar"}');
    expect(pageSource).toContain('aria-selected={viewMode === "manage"}');
    expect(pageSource).toContain("icon={<PlusOutlined />}");
  });
});

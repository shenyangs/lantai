import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";

const css = readFileSync(
  resolve(
    process.cwd(),
    "src/pages/Chat/components/TurnEventPreview.module.less",
  ),
  "utf8",
);

function rule(selector: string): string {
  const start = css.indexOf(`${selector} {`);
  const end = css.indexOf("\n}", start);
  return start >= 0 && end > start ? css.slice(start, end + 2) : "";
}

describe("TurnEventPreview visual contracts", () => {
  it("keeps the workbench progress and timeline structurally lightweight", () => {
    const progress = rule(".workbenchProgress");
    const stepMarker = rule(".itemMarker");

    expect(progress).toContain("border-bottom:");
    expect(progress).not.toContain("border: 1px");
    expect(stepMarker).toContain("width: 22px;");
    expect(stepMarker).toContain("border-radius: 50%;");
    expect(stepMarker).not.toContain("border:");
  });

  it("uses a quiet fill instead of another outline for the active step", () => {
    const runningStep = rule('&[data-step-status="running"]');

    expect(runningStep).toContain("background:");
    expect(runningStep).not.toContain("border-color:");
  });
});

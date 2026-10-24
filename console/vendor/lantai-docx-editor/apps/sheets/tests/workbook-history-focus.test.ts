import { describe, expect, it } from "vitest";

import {
  EDITOR_ACTIVATED,
  FOCUSING_FX_BAR_EDITOR,
  FOCUSING_SHEET,
} from "@univerjs/core";
import { focusWorkbookHistoryStack } from "../src/renderer/workbook-history-focus";

describe("focusWorkbookHistoryStack", () => {
  it("clears stale editor contexts before focusing the workbook", () => {
    const calls: string[] = [];

    focusWorkbookHistoryStack(
      {
        setContextValue(key, value) {
          calls.push(`context:${key}:${value}`);
        },
      },
      {
        focusUnit(unitId) {
          calls.push(`focus:${unitId}`);
        },
      },
      "workbook-1",
    );

    expect(calls).toEqual([
      `context:${EDITOR_ACTIVATED}:false`,
      `context:${FOCUSING_FX_BAR_EDITOR}:false`,
      `context:${FOCUSING_SHEET}:true`,
      "focus:workbook-1",
    ]);
  });
});

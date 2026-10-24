import {
  EDITOR_ACTIVATED,
  FOCUSING_FX_BAR_EDITOR,
  FOCUSING_SHEET,
} from "@univerjs/core";

export interface WorkbookHistoryContext {
  setContextValue(key: string, value: boolean): void;
}

export interface WorkbookHistoryInstances {
  focusUnit(unitId: string): void;
}

/**
 * Route commands from the app-owned toolbar to the workbook history stack.
 *
 * Univer resolves the active undo/redo stack from editor context before it
 * consults the focused unit. A toolbar click outside the canvas can therefore
 * leave a committed cell editor (or formula bar) marked active and silently
 * send Undo/Redo to that internal docs stack. Clear those stale editor flags
 * before focusing the workbook so the next history command addresses cells.
 */
export function focusWorkbookHistoryStack(
  context: WorkbookHistoryContext,
  instances: WorkbookHistoryInstances,
  workbookId: string,
): void {
  context.setContextValue(EDITOR_ACTIVATED, false);
  context.setContextValue(FOCUSING_FX_BAR_EDITOR, false);
  context.setContextValue(FOCUSING_SHEET, true);
  instances.focusUnit(workbookId);
}

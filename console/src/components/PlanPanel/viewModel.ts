import type { SubTaskResponse } from "../../api/modules/plan";

export function getActiveTaskIndex(subtasks: SubTaskResponse[]): number {
  const runningIndex = subtasks.findIndex(
    (task) => task.state === "in_progress",
  );
  if (runningIndex >= 0) return runningIndex;

  const todoIndex = subtasks.findIndex((task) => task.state === "todo");
  return todoIndex >= 0 ? todoIndex : Math.max(0, subtasks.length - 1);
}

export function getPlanPreviewTasks(
  subtasks: SubTaskResponse[],
  activeIndex: number,
  limit = 3,
): Array<{ task: SubTaskResponse; index: number }> {
  if (subtasks.length <= limit) {
    return subtasks.map((task, index) => ({ task, index }));
  }

  const safeLimit = Math.max(1, limit);
  const start = Math.max(
    0,
    Math.min(activeIndex - 1, subtasks.length - safeLimit),
  );
  return subtasks
    .slice(start, start + safeLimit)
    .map((task, offset) => ({ task, index: start + offset }));
}

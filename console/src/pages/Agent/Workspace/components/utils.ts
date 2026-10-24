import dayjs from "dayjs";

export const formatTimeAgo = (timestamp: number | string): string => {
  const time =
    typeof timestamp === "string" ? new Date(timestamp).getTime() : timestamp;
  if (isNaN(time)) return "-";

  return dayjs(time).fromNow();
};

export const isDailyMemoryFile = (filename: string): boolean => {
  return /^\d{4}-\d{2}-\d{2}\.md$/.test(filename);
};

const INTERNAL_WORKSPACE_FILES = new Set([
  "AGENTS.MD",
  "SOUL.MD",
  "PROFILE.MD",
  "BACKSTORY.MD",
  "SELF_CANON.MD",
  "VISUAL_CANON.MD",
  "PRODUCT_GUIDE.MD",
  "STATE.MD",
  "WORKBENCH.MD",
  "BOOTSTRAP.MD",
  "HEARTBEAT.MD",
  "MEMORY.MD",
]);

export const isInternalWorkspaceFile = (filename: string): boolean => {
  return INTERNAL_WORKSPACE_FILES.has(filename.trim().toUpperCase());
};

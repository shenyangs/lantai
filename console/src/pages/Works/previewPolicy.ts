import type { WorkItem } from "../../api/types/works";

export type WorkCardPreviewMode =
  | "generated-cover"
  | "native-thumbnail"
  | "image-content"
  | "static";

export function workCardPreviewMode(work: WorkItem): WorkCardPreviewMode {
  if (work.cover_path) return "generated-cover";
  if (
    [
      "html",
      "presentation",
      "document",
      "spreadsheet",
      "pdf",
      "video",
    ].includes(work.kind)
  ) {
    return "native-thumbnail";
  }
  if (work.kind === "image") return "image-content";
  return "static";
}

export function canInlinePreviewWork(work: WorkItem) {
  return (
    ["image", "pdf", "audio", "video"].includes(work.kind) ||
    work.kind === "markdown" ||
    Boolean(work.mime_type?.startsWith("text/"))
  );
}

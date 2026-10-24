import { X } from "lucide-react";
import type { ArtifactEditTarget } from "./artifactExperience";
import styles from "./ArtifactEditTargetChip.module.less";

export interface ArtifactEditDraft {
  target: ArtifactEditTarget;
  title: string;
}

interface ArtifactEditTargetChipProps {
  draft: ArtifactEditDraft | null;
  onClear: () => void;
}

function selectionLabel(target: ArtifactEditTarget): string | null {
  const selection = target.selection;
  if (!selection) return null;
  if (selection.kind === "page") return `第 ${selection.page} 页`;
  if (selection.kind === "paragraph") {
    return `第 ${selection.paragraph} 段`;
  }
  return selection.region;
}

export default function ArtifactEditTargetChip({
  draft,
  onClear,
}: ArtifactEditTargetChipProps) {
  if (!draft) return null;
  const selection = selectionLabel(draft.target);
  return (
    <div className={styles.root} role="group" aria-label="成果修改目标">
      <span className={styles.eyebrow}>修改目标</span>
      <span className={styles.title} title={draft.title}>
        {draft.title}
      </span>
      <span className={styles.detail}>
        V{draft.target.base_revision.version}
        {selection ? ` · ${selection}` : ""}
      </span>
      <button
        type="button"
        className={styles.clear}
        aria-label="移除成果修改目标"
        onClick={onClear}
      >
        <X aria-hidden="true" size={14} />
      </button>
    </div>
  );
}

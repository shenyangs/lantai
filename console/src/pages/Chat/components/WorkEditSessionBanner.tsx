import { useChatAnywhereSessionsState } from "@/vendor/lantaiChat";
import { ExternalLink, PencilLine } from "lucide-react";
import { useNavigate } from "react-router-dom";
import styles from "../index.module.less";

type WorkEditSession = {
  workId: string;
  title: string;
  sourceVersion: number | null;
};

const asRecord = (value: unknown): Record<string, unknown> | null =>
  value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;

const asText = (value: unknown): string =>
  typeof value === "string" ? value.trim() : "";

const asVersion = (value: unknown): number | null => {
  const version =
    typeof value === "number"
      ? value
      : typeof value === "string" && value.trim()
        ? Number(value)
        : Number.NaN;
  return Number.isInteger(version) && version > 0 ? version : null;
};

const titleFromSessionName = (value: unknown): string =>
  asText(value)
    .replace(/^\s*修改\s*[：:]\s*/, "")
    .trim();

function getWorkEditSession(
  session: Record<string, unknown> | undefined,
): WorkEditSession | null {
  if (!session) return null;
  const meta = asRecord(session.meta);
  if (!meta) return null;

  const structured = asRecord(meta.work_edit_session);
  const structuredWorkId = asText(structured?.work_id);
  if (structuredWorkId) {
    return {
      workId: structuredWorkId,
      title:
        asText(structured?.title) ||
        titleFromSessionName(session.name) ||
        "当前作品",
      sourceVersion: asVersion(structured?.source_version),
    };
  }

  const legacyWorkId = asText(meta.work_edit_id);
  if (!legacyWorkId) return null;
  return {
    workId: legacyWorkId,
    title: titleFromSessionName(session.name) || "当前作品",
    sourceVersion: asVersion(meta.work_edit_parent_version),
  };
}

export default function WorkEditSessionBanner() {
  const navigate = useNavigate();
  const { sessions, currentSessionId } = useChatAnywhereSessionsState();
  const session = sessions.find((candidate) => {
    const extended = candidate as typeof candidate & {
      realId?: string;
      sessionId?: string;
    };
    return [candidate.id, extended.realId, extended.sessionId].some(
      (value) => value === currentSessionId,
    );
  }) as (Record<string, unknown> & { id: string }) | undefined;
  const workEditSession = getWorkEditSession(session);
  if (!workEditSession) return null;

  const copyExplanation = workEditSession.sourceVersion
    ? `基于 V${workEditSession.sourceVersion} 创建的独立副本 · 原作品不会被覆盖`
    : "独立修改副本 · 原作品不会被覆盖";

  return (
    <section
      className={styles.workEditSessionBanner}
      role="region"
      aria-label={`正在修改 ${workEditSession.title} 的独立副本`}
    >
      <span className={styles.workEditSessionIcon} aria-hidden="true">
        <PencilLine />
      </span>
      <span className={styles.workEditSessionText}>
        <span className={styles.workEditSessionHeading}>
          <span>正在修改</span>
          <strong title={workEditSession.title}>{workEditSession.title}</strong>
        </span>
        <small>{copyExplanation}</small>
      </span>
      <button
        className={styles.workEditSessionAction}
        type="button"
        onClick={() =>
          navigate(`/works?work=${encodeURIComponent(workEditSession.workId)}`)
        }
      >
        <span>查看原作品</span>
        <ExternalLink aria-hidden="true" />
      </button>
    </section>
  );
}

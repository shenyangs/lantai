import { FileTextOutlined } from "@ant-design/icons";
import { useChatAnywhereSessionsState } from "@/vendor/lantaiChat";
import styles from "../index.module.less";

type KnowledgeSource = {
  path: string;
  filename: string;
};

const asKnowledgeSource = (value: unknown): KnowledgeSource | null => {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const record = value as Record<string, unknown>;
  const path = String(record.path || "").trim();
  const filename = String(record.filename || "").trim();
  return path && filename ? { path, filename } : null;
};

export function knowledgeScopeExplanation(exact: boolean): string {
  return exact
    ? "检索仅限所选资料；实际引用会在回答中显示来源链接"
    : "优先使用所选资料；回答也可检索并补充其他信息";
}

export default function KnowledgeSourceBanner() {
  const { sessions, currentSessionId } = useChatAnywhereSessionsState();
  const session = sessions.find((candidate) => {
    const extended = candidate as typeof candidate & {
      realId?: string;
      sessionId?: string;
    };
    return [candidate.id, extended.realId, extended.sessionId].some(
      (value) => value === currentSessionId,
    );
  });
  const meta = (session as { meta?: Record<string, unknown> } | undefined)
    ?.meta;
  const sources = Array.isArray(meta?.knowledge_sources)
    ? meta.knowledge_sources
        .map(asKnowledgeSource)
        .filter((source): source is KnowledgeSource => Boolean(source))
    : [];
  const legacySource = asKnowledgeSource(meta?.knowledge_source);
  if (!sources.length && legacySource) sources.push(legacySource);
  if (!sources.length) return null;
  const exact = meta?.knowledge_scope_mode !== "expanded";
  const label =
    sources.length === 1 ? sources[0].filename : `${sources.length} 份资料`;

  return (
    <div
      className={styles.knowledgeSourceBanner}
      role="status"
      aria-label={`当前对话已选择 ${label}`}
      title={sources.map((source) => source.path).join("\n")}
    >
      <span className={styles.knowledgeSourceIcon} aria-hidden="true">
        <FileTextOutlined />
      </span>
      <span className={styles.knowledgeSourceText}>
        <span className={styles.knowledgeSourceSelection}>
          <span>{exact ? "仅依据" : "优先使用"}</span>
          <strong>{label}</strong>
        </span>
        <small>{knowledgeScopeExplanation(exact)}</small>
      </span>
      <span className={styles.knowledgeSourceStatus}>
        {exact ? "仅依据所选资料" : "可补充其他信息"}
      </span>
    </div>
  );
}

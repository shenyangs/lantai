import type { KdocsReference } from "../../kdocsReferences";
import styles from "./Kdocs.module.less";

export function messageKdocsReferences(message: {
  metadata?: unknown;
  cards?: Array<{ data?: any }>;
}): KdocsReference[] {
  const containers = [
    message.metadata,
    ...(message.cards || []).flatMap((card) => [
      card.data?.metadata,
      ...(Array.isArray(card.data?.input) ? card.data.input : []).map(
        (item: any) => item.metadata,
      ),
    ]),
  ];
  for (const container of containers) {
    const references = (container as { kdocs_references?: unknown } | undefined)
      ?.kdocs_references;
    if (Array.isArray(references))
      return references.filter(
        (item) =>
          item?.schema_version === 1 &&
          item.file_id &&
          item.drive_id &&
          item.key &&
          ["file", "excerpt"].includes(item.kind),
      );
  }
  return [];
}

export default function KdocsMessageReferences({
  message,
  onOpen,
}: {
  message: Parameters<typeof messageKdocsReferences>[0];
  onOpen: (reference: KdocsReference) => void;
}) {
  const references = messageKdocsReferences(message);
  if (!references.length) return null;
  return (
    <div className={styles.sentReferences} aria-label="本条消息的文档快照">
      {references.map((reference) => (
        <button
          key={reference.key}
          onClick={() => onOpen(reference)}
          title="查看发送时的内容快照"
        >
          {reference.name_snapshot}
          {reference.excerpt?.location
            ? ` · ${reference.excerpt.location}`
            : ""}
        </button>
      ))}
    </div>
  );
}

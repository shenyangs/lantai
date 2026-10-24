import { useEffect, useState } from "react";
import { MessageSquare } from "lucide-react";
import { dailyBriefApi } from "@/api/modules/dailyBrief";
import styles from "../index.module.less";

export default function DailyBriefDiscussionDraft({
  draftId,
  onInsert,
  onClose,
}: {
  draftId: string;
  onInsert: (text: string) => boolean;
  onClose: () => void;
}) {
  const [ready, setReady] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  useEffect(() => {
    let active = true;
    setReady(false);
    setError("");
    void dailyBriefApi
      .getDiscussionDraft(draftId)
      .then(() => {
        if (active) setReady(true);
      })
      .catch(() => {
        if (active) setError("草稿已不可用，请返回简报重新准备");
      });
    return () => {
      active = false;
    };
  }, [draftId]);

  const insert = async () => {
    setBusy(true);
    try {
      const draft = await dailyBriefApi.getDiscussionDraft(draftId);
      if (onInsert(draft.composer_text)) onClose();
      else setError("输入框尚未准备好，请稍后重试");
    } catch {
      setReady(false);
      setError("草稿已不可用，请返回简报重新准备");
    } finally {
      setBusy(false);
    }
  };
  return (
    <div
      role="region"
      aria-label="简报讨论草稿"
      className={styles.knowledgeSourceBanner}
    >
      <span className={styles.knowledgeSourceIcon} aria-hidden="true">
        <MessageSquare size={16} />
      </span>
      <div className={styles.knowledgeSourceText}>
        {error ? (
          <span role="alert">{error}</span>
        ) : (
          <>
            <strong>简报讨论草稿已准备</strong>
            <small>插入后可编辑，由你发送。</small>
          </>
        )}
      </div>
      <div>
        {ready && (
          <button
            className={styles.workEditSessionAction}
            type="button"
            disabled={busy}
            onClick={() => void insert()}
          >
            插入到输入框
          </button>
        )}
        <button
          className={styles.workEditSessionAction}
          type="button"
          disabled={busy}
          onClick={onClose}
        >
          取消
        </button>
      </div>
    </div>
  );
}

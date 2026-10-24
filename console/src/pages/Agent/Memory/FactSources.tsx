import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { Button, Popconfirm } from "antd";
import { workspaceApi } from "../../../api/modules/workspace";
import type {
  MemoryFactSource,
  MemoryFactSourcesResponse,
} from "../../../api/types";
import { formatMemoryTime } from "./formatTime";
import styles from "./FactSources.module.less";

function sourceTitle(source: MemoryFactSource): string {
  if (source.source_role === "user") return "聊天原话";
  if (source.source_role === "manual") return "你手动添加的记忆";
  if (source.source_role === "derived") return "模型整理结果，不能当作用户原话";
  if (source.source_role === "historical_summary") return "旧版记忆摘要，未保存聊天原话";
  if (source.source_role === "tool_result") return "引用材料的提取文本";
  return "历史来源记录";
}

function sourceStatus(source: MemoryFactSource): string {
  if (source.status === "missing") return "来源已不可用";
  if (source.status === "version_mismatch") {
    return "来源版本已变化，无法确认原句";
  }
  if (source.status === "source_only") {
    return "来源记录可用，但无法定位逐字引文";
  }
  if (source.source_role === "derived") return "已定位模型整理文本";
  if (source.source_role === "historical_summary") return "已定位旧版摘要文本";
  return "已定位原句";
}

function Source({
  source, onDelete, deleting,
}: {
  source: MemoryFactSource;
  onDelete: () => Promise<void>;
  deleting: boolean;
}) {
  const available = source.status === "located" || source.status === "source_only";
  return (
    <li className={styles.source}>
      <div className={styles.meta}>
        <strong>{sourceTitle(source)}</strong>
        {source.source_created_at ? (
          <span>原消息时间：{formatMemoryTime(source.source_created_at)}</span>
        ) : source.created_at ? (
          <span>记忆收录时间：{formatMemoryTime(source.created_at)}</span>
        ) : null}
        <span>{sourceStatus(source)}</span>
        {source.support_status === "supported" ? <span>语义已复核</span> :
          source.support_status === "user_confirmed" ? <span>用户已确认</span> :
          source.support_status === "explicit_request" ? <span>用户明确要求记住</span> :
          <span>语义尚未核实</span>}
      </div>
      {source.status === "located" ? (
        <blockquote className={styles.quote}>{source.quote}</blockquote>
      ) : null}
      {available && source.content ? (
        <details className={styles.context}>
          <summary>{source.source_role === "derived" ? "查看完整整理文本" :
            source.source_role === "historical_summary" ? "查看完整历史摘要" : "查看完整原文"}</summary>
          <pre>{source.content}</pre>
        </details>
      ) : null}
      {available && source.source_role === "user" && source.session_id ? (
        <Link to={`/chat/${encodeURIComponent(source.session_id)}`}>
          打开所在对话
        </Link>
      ) : null}
      {available ? (
        <Popconfirm
          title="从记忆中删除此来源？"
          description="若这是唯一来源，相关记忆也会停止使用。原聊天记录仍由对话管理。"
          okText="删除来源"
          cancelText="取消"
          onConfirm={onDelete}
        >
          <Button danger type="link" loading={deleting}>删除记忆来源</Button>
        </Popconfirm>
      ) : null}
    </li>
  );
}

export function FactSources({
  factId,
  agentId,
  onChanged,
}: {
  factId: string;
  agentId: string;
  onChanged?: () => void;
}) {
  const [open, setOpen] = useState(false);
  const [data, setData] = useState<MemoryFactSourcesResponse | null>(null);
  const [error, setError] = useState(false);
  const [deletingId, setDeletingId] = useState("");

  async function deleteSource(eventId: string) {
    setDeletingId(eventId);
    setError(false);
    try {
      await workspaceApi.deleteMemoryFactSource(factId, eventId);
      setData(await workspaceApi.openMemoryFactSources(factId));
      onChanged?.();
    } catch {
      setError(true);
    } finally {
      setDeletingId("");
    }
  }

  useEffect(() => {
    if (!open) return;
    let current = true;
    setData(null);
    setError(false);
    void workspaceApi.openMemoryFactSources(factId).then(
      (result) => {
        if (current) setData(result);
      },
      () => {
        if (current) setError(true);
      },
    );
    return () => { current = false; };
  }, [factId, agentId, open]);

  return (
    <details
      className={styles.container}
      onToggle={(event) => setOpen(event.currentTarget.open)}
    >
      <summary>查看来源</summary>
      {open ? (
        error ? <p>来源暂时无法读取。</p> :
        !data ? <p>正在读取来源…</p> :
        !data.sources.length ? <p>目前无法追溯到原文。</p> :
        <ol className={styles.sources}>
          {data.sources.map((source, index) => (
            <Source
              key={`${source.event_id}-${index}`}
              source={source}
              deleting={deletingId === source.event_id}
              onDelete={() => deleteSource(source.event_id)}
            />
          ))}
        </ol>
      ) : null}
    </details>
  );
}

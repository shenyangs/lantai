import { useCallback, useEffect, useRef, useState } from "react";
import {
  DeleteOutlined,
  FileTextOutlined,
  InboxOutlined,
  ReadOutlined,
  UndoOutlined,
} from "@ant-design/icons";
import { Empty, Modal, Skeleton } from "antd";
import { Button } from "@/vendor/lantaiDesign";
import { workspaceApi } from "@/api/modules/workspace";
import { readingApi, type ReadingEntry } from "@/api/modules/reading";
import type { WikiTrashItem } from "@/api/types";
import { useAppMessage } from "@/hooks/useAppMessage";
import styles from "./recycleBin.module.less";

type TrashRow =
  | {
      id: string;
      origin: "knowledge";
      title: string;
      detail: string;
      removedAt: number;
      raw: WikiTrashItem;
    }
  | {
      id: string;
      origin: "reading";
      title: string;
      detail: string;
      removedAt: number;
      raw: ReadingEntry;
    };

export default function RecycleBin({ onRestore }: { onRestore: () => void }) {
  const { message } = useAppMessage();
  const [rows, setRows] = useState<TrashRow[]>([]);
  const [loading, setLoading] = useState(true);
  const [busyId, setBusyId] = useState("");
  const active = useRef(true);
  const reload = useCallback(async () => {
    setLoading(true);
    try {
      const [knowledge, reading] = await Promise.all([
        workspaceApi.listWikiTrash(),
        readingApi.entries({
          view: "trash",
          offset: "0",
          limit: "100",
          q: "",
          feed_id: "",
        }),
      ]);
      if (!active.current) return;
      setRows(
        [
          ...knowledge.map<TrashRow>((item) => ({
            id: item.id,
            origin: "knowledge",
            title: item.title,
            detail:
              item.kind === "source"
                ? `资料 · ${item.original_path}`
                : `知识条目 · ${item.original_path}`,
            removedAt: Date.parse(item.archived_at),
            raw: item,
          })),
          ...reading.items.map<TrashRow>((item) => ({
            id: item.id,
            origin: "reading",
            title: item.title,
            detail: `阅读文章 · ${item.source_feed || item.url}`,
            removedAt: Number(item.archived_at || 0) * 1000,
            raw: item,
          })),
        ].sort((left, right) => right.removedAt - left.removedAt),
      );
    } catch (error) {
      if (active.current)
        message.error(
          error instanceof Error ? error.message : "回收站读取失败",
        );
    } finally {
      if (active.current) setLoading(false);
    }
  }, [message]);
  useEffect(() => {
    active.current = true;
    void reload();
    return () => {
      active.current = false;
    };
  }, [reload]);

  const restore = async (row: TrashRow) => {
    setBusyId(row.id);
    try {
      if (row.origin === "knowledge")
        await workspaceApi.restoreWikiTrashItem(row.id);
      else await readingApi.restoreEntry(row.id);
      message.success(`“${row.title}”已恢复`);
      onRestore();
      await reload();
    } catch (error) {
      message.error(
        error instanceof Error ? error.message : "恢复失败，请重试",
      );
    } finally {
      setBusyId("");
    }
  };

  const confirmPurge = (row: TrashRow) => {
    Modal.confirm({
      title: `彻底删除“${row.title}”？`,
      content: "删除后无法从澜台恢复。",
      okText: "彻底删除",
      cancelText: "取消",
      okButtonProps: { danger: true },
      onOk: async () => {
        setBusyId(row.id);
        try {
          if (row.origin === "knowledge")
            await workspaceApi.purgeWikiTrashItem(row.id);
          else await readingApi.purgeEntry(row.id);
          message.success("已彻底删除");
          await reload();
        } catch (error) {
          message.error(
            error instanceof Error ? error.message : "彻底删除失败，请重试",
          );
        } finally {
          setBusyId("");
        }
      },
    });
  };

  return (
    <main className={styles.bin} aria-label="知识库回收站">
      <header>
        <div>
          <h2>回收站</h2>
          <p>删除的资料、知识条目和阅读文章会先放在这里。</p>
        </div>
        <Button onClick={() => void reload()}>刷新</Button>
      </header>
      {loading ? (
        <div className={styles.loading}>
          <Skeleton active paragraph={{ rows: 5 }} />
        </div>
      ) : rows.length ? (
        <div className={styles.list}>
          {rows.map((row) => (
            <article key={`${row.origin}:${row.id}`}>
              <span className={styles.icon}>
                {row.origin === "reading" ? (
                  <ReadOutlined />
                ) : row.raw.kind === "source" ? (
                  <InboxOutlined />
                ) : (
                  <FileTextOutlined />
                )}
              </span>
              <div>
                <h3>{row.title}</h3>
                <p>{row.detail}</p>
                <small>
                  {Number.isFinite(row.removedAt) && row.removedAt
                    ? new Intl.DateTimeFormat("zh-CN", {
                        dateStyle: "medium",
                        timeStyle: "short",
                      }).format(row.removedAt)
                    : "移除时间未知"}
                </small>
              </div>
              <div className={styles.actions}>
                <Button
                  aria-label={`恢复 ${row.title}`}
                  icon={<UndoOutlined />}
                  loading={busyId === row.id}
                  onClick={() => void restore(row)}
                >
                  恢复
                </Button>
                <Button
                  aria-label={`彻底删除 ${row.title}`}
                  danger
                  type="text"
                  icon={<DeleteOutlined />}
                  disabled={Boolean(busyId)}
                  onClick={() => confirmPurge(row)}
                >
                  彻底删除
                </Button>
              </div>
            </article>
          ))}
        </div>
      ) : (
        <Empty
          image={Empty.PRESENTED_IMAGE_SIMPLE}
          description="回收站是空的"
        />
      )}
    </main>
  );
}

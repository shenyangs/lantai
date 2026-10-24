import { useCallback, useEffect, useRef, useState } from "react";
import { DeleteOutlined } from "@ant-design/icons";
import { Alert, Checkbox, Empty, Input, Modal, Spin } from "antd";
import {
  BookmarkPlus,
  CircleDot,
  Compass,
  Inbox,
  RefreshCw,
  Rss,
  Star,
  Trash2,
} from "lucide-react";
import { useNavigate } from "react-router-dom";
import { Button } from "@/vendor/lantaiDesign";
import {
  readingApi,
  type ReadingEntry,
  type ReadingFeed,
} from "@/api/modules/reading";
import {
  CREATE_NEW_CHAT_CONTEXT_KEY,
  type ChatKnowledgeSourceReference,
} from "@/pages/Chat/chatSessionEvents";
import { useAppMessage } from "@/hooks/useAppMessage";
import styles from "./reading.module.less";
import { useAgentStore } from "@/stores/agentStore";

const bodyLabels = {
  none: "尚未获取正文",
  summary: "订阅源摘要",
  feed: "订阅源内容",
  page: "已保存网页正文",
};

export default function ReadingPanel(props: {
  onKnowledgeChange: () => void;
  onOpenChannels: () => void;
}) {
  const agent = useAgentStore((state) => state.selectedAgent);
  return <ScopedReadingPanel key={agent} {...props} />;
}

function ScopedReadingPanel({
  onKnowledgeChange,
  onOpenChannels,
}: {
  onKnowledgeChange: () => void;
  onOpenChannels: () => void;
}) {
  const navigate = useNavigate();
  const { message } = useAppMessage();
  const [feeds, setFeeds] = useState<ReadingFeed[]>([]);
  const [items, setItems] = useState<ReadingEntry[]>([]);
  const [total, setTotal] = useState(0);
  const [offset, setOffset] = useState(0);
  const [view, setView] = useState("all");
  const [feedId, setFeedId] = useState("");
  const [query, setQuery] = useState("");
  const [search, setSearch] = useState("");
  const [selected, setSelected] = useState<ReadingEntry | null>(null);
  const [chosen, setChosen] = useState<string[]>([]);
  const [loading, setLoading] = useState(false);
  const [detailLoading, setDetailLoading] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [adding, setAdding] = useState<"feed" | "link" | null>(null);
  const [url, setUrl] = useState("");
  const [folder, setFolder] = useState("");
  const [tagDraft, setTagDraft] = useState("");
  const listVersion = useRef(0);
  const detailVersion = useRef(0);
  const panelRef = useRef<HTMLElement>(null);
  const readerRef = useRef<HTMLElement>(null);
  const listRef = useRef<HTMLDivElement>(null);
  const active = useRef(true);
  const busyRef = useRef(false);
  useEffect(() => {
    active.current = true;
    return () => {
      active.current = false;
      listVersion.current++;
      detailVersion.current++;
    };
  }, []);
  useEffect(() => {
    // Match the container breakpoint: navigation can narrow a wide window.
    const width = panelRef.current?.clientWidth ?? 0;
    if (selected && width > 0 && width <= 1180) {
      readerRef.current?.scrollIntoView?.({ block: "start" });
    }
  }, [selected?.id]);

  const reload = useCallback(async () => {
    if (!active.current) return;
    const version = ++listVersion.current;
    setLoading(true);
    try {
      const [sources, result] = await Promise.all([
        readingApi.feeds(),
        readingApi.entries({
          view,
          feed_id: feedId,
          q: search,
          offset: String(offset),
        }),
      ]);
      if (!active.current || version !== listVersion.current) return;
      setFeeds(sources);
      setItems(result.items);
      setTotal(result.total);
      setError("");
    } catch (reason) {
      if (active.current && version === listVersion.current)
        setError(
          reason instanceof Error ? reason.message : "读取列表失败，请重试",
        );
    } finally {
      if (active.current && version === listVersion.current) setLoading(false);
    }
  }, [view, feedId, search, offset]);
  const reloadRef = useRef(reload);
  reloadRef.current = reload;
  useEffect(() => {
    void reload();
  }, [reload]);

  const run = async (operation: () => Promise<void>) => {
    if (busyRef.current) return;
    busyRef.current = true;
    setBusy(true);
    setError("");
    try {
      await operation();
    } catch (reason) {
      if (active.current)
        setError(reason instanceof Error ? reason.message : "操作失败，请重试");
    } finally {
      busyRef.current = false;
      if (active.current) setBusy(false);
    }
  };
  const open = async (id: string) => {
    const version = ++detailVersion.current;
    setSelected(null);
    setDetailLoading(true);
    setError("");
    try {
      const entry = await readingApi.entry(id);
      if (!active.current || version !== detailVersion.current) return;
      setSelected(entry);
      setTagDraft(entry.tags.join("，"));
    } catch (reason) {
      if (version === detailVersion.current && active.current)
        setError(String(reason));
    } finally {
      if (version === detailVersion.current && active.current)
        setDetailLoading(false);
    }
  };
  const update = (values: {
    is_read?: boolean;
    starred?: boolean;
    tags?: string[];
  }) => {
    if (!selected) return;
    const id = selected.id;
    const version = detailVersion.current;
    void run(async () => {
      const result = await readingApi.update(id, values);
      if (active.current && version === detailVersion.current)
        setSelected(result);
      await reloadRef.current();
    });
  };
  const addKnowledge = (ids: string[], discuss: boolean) =>
    void run(async () => {
      const references: ChatKnowledgeSourceReference[] = [];
      for (const id of ids) {
        if (!active.current) return;
        try {
          const result = await readingApi.knowledge(id);
          const source = [...result.imported, ...(result.skipped ?? [])][0];
          if (!source)
            throw new Error("资料尚未成功入库，请到知识库检查处理状态");
          references.push({
            path: source.path,
            filename: source.display_title || source.filename,
            kind: source.kind,
          });
        } catch (reason) {
          if (active.current && references.length) onKnowledgeChange();
          const detail = reason instanceof Error ? reason.message : "入库失败";
          const symptom = new Error(
            `已入库 ${references.length}/${ids.length} 篇${references.length ? `（${references.map((item) => item.filename).join("、")}）` : ""}；当前文章未能确认入库：${detail}。其余 ${ids.length - references.length - 1} 篇尚未处理。已入库内容保留，未进入讨论。`,
          ) as Error & { cause?: unknown };
          symptom.cause = reason;
          throw symptom;
        }
      }
      if (!active.current) return;
      onKnowledgeChange();
      if (discuss) {
        window.sessionStorage.setItem(
          CREATE_NEW_CHAT_CONTEXT_KEY,
          JSON.stringify({
            knowledgeSources: references,
            knowledgeScopeMode: "exact",
            ...(references.length === 1
              ? { knowledgeSource: references[0] }
              : {}),
          }),
        );
        navigate("/chat?lantaiColdStart=1");
      } else message.success("已交给知识库处理，来源和内容范围一并保留");
    });
  const currentFeed = feeds.find((feed) => feed.id === feedId);
  const changeView = (next: string, nextFeed = "") => {
    setView(next);
    setFeedId(nextFeed);
    setOffset(0);
    setChosen([]);
  };

  const confirmArchive = (ids: string[], title: string) => {
    if (!ids.length) return;
    Modal.confirm({
      title,
      content: "文章会移到知识库回收站，收藏、已读状态和正文都会保留。",
      okText: "移到回收站",
      cancelText: "取消",
      okButtonProps: { danger: true },
      onOk: async () => {
        try {
          await readingApi.archiveEntries(ids);
          if (!active.current) return;
          setChosen((current) => current.filter((id) => !ids.includes(id)));
          if (selected && ids.includes(selected.id)) setSelected(null);
          await reloadRef.current();
          message.success(`已将 ${ids.length} 篇文章移到回收站`);
        } catch (reason) {
          if (active.current)
            message.error(
              reason instanceof Error ? reason.message : "删除失败，请重试",
            );
        }
      },
    });
  };

  return (
    <section ref={panelRef} className={styles.panel} aria-label="阅读与订阅">
      <div className={styles.toolbar}>
        <div>
          <h2>阅读</h2>
          <p>订阅更新，留住值得再读的文章。</p>
        </div>
        <div className={styles.actions}>
          <Button
            icon={<Compass size={15} strokeWidth={1.9} />}
            disabled={busy}
            onClick={onOpenChannels}
          >
            浏览频道
          </Button>
          <Button
            icon={<BookmarkPlus size={15} strokeWidth={1.9} />}
            onClick={() => {
              setAdding("link");
              setUrl("");
            }}
          >
            收藏链接
          </Button>
          <Button
            type="primary"
            icon={<Rss size={15} strokeWidth={1.9} />}
            onClick={() => {
              setAdding("feed");
              setUrl("");
            }}
          >
            添加订阅
          </Button>
          <Button
            icon={<RefreshCw size={15} strokeWidth={1.9} />}
            loading={busy}
            disabled={!feeds.some((feed) => !feed.paused)}
            onClick={() =>
              void run(async () => {
                const failures: string[] = [];
                for (const feed of feeds.filter((item) => !item.paused)) {
                  if (!active.current) return;
                  try {
                    await readingApi.refresh(feed.id);
                  } catch {
                    failures.push(feed.title);
                  }
                }
                await reloadRef.current();
                if (failures.length)
                  throw new Error(
                    `以下订阅更新失败：${failures.join("、")}。已有文章仍可阅读。`,
                  );
              })
            }
          >
            刷新订阅
          </Button>
        </div>
      </div>
      {error && (
        <Alert
          type="error"
          showIcon
          message={error}
          closable
          onClose={() => setError("")}
        />
      )}
      {adding && (
        <form
          className={styles.addForm}
          onSubmit={(event) => {
            event.preventDefault();
            void run(async () => {
              if (adding === "feed") await readingApi.subscribe(url, folder);
              else {
                const entry = await readingApi.save(url);
                await open(entry.id);
              }
              if (active.current) {
                setAdding(null);
                setUrl("");
              }
              await reloadRef.current();
            });
          }}
        >
          <label>
            {adding === "feed" ? "RSS / Atom 地址" : "文章地址"}
            <Input
              autoFocus
              value={url}
              onChange={(event) => setUrl(event.target.value)}
              placeholder="https://"
              maxLength={2048}
              required
            />
          </label>
          {adding === "feed" && (
            <label>
              订阅分组
              <Input
                value={folder}
                onChange={(event) => setFolder(event.target.value)}
                placeholder="例如：科技、设计"
                maxLength={80}
              />
            </label>
          )}
          <Button
            htmlType="submit"
            type="primary"
            loading={busy}
            disabled={!url.trim()}
          >
            保存
          </Button>
          <Button onClick={() => setAdding(null)} disabled={busy}>
            取消
          </Button>
          <span>仅访问公开 HTTPS 地址；收藏链接后可手动读取全文。</span>
        </form>
      )}
      <div
        className={`${styles.columns} ${selected ? styles.readingOpen : ""}`}
      >
        <nav className={styles.sources} aria-label="阅读分类与订阅源">
          {[
            { key: "all", label: "全部文章", icon: Inbox },
            { key: "unread", label: "未读", icon: CircleDot },
            { key: "starred", label: "收藏", icon: Star },
          ].map(({ key, label, icon: Icon }) => (
            <button
              key={key}
              className={styles.sourceFilter}
              aria-current={!feedId && view === key ? "page" : undefined}
              onClick={() => changeView(key)}
            >
              <Icon size={15} strokeWidth={1.8} aria-hidden="true" />
              <span>{label}</span>
            </button>
          ))}
          <h3>订阅源</h3>
          {!feeds.length && <p>添加第一个订阅源，更新会出现在这里。</p>}
          {feeds.map((feed) => (
            <button
              key={feed.id}
              className={styles.feedButton}
              aria-current={feedId === feed.id ? "page" : undefined}
              onClick={() => changeView("all", feed.id)}
            >
              <span>{feed.title}</span>
              <small>
                {feed.folder || "未分组"} ·{" "}
                {feed.paused ? "已暂停" : `${feed.unread} 篇未读`}
                {feed.error ? " · 更新失败" : ""}
              </small>
            </button>
          ))}
          <p>点击刷新检查更新。关闭阅读页不会启动后台抓取。</p>
        </nav>
        <div ref={listRef} className={styles.list}>
          <div className={styles.searchArea}>
            <Input.Search
              aria-label="搜索文章和标签"
              placeholder="搜索文章或标签"
              value={query}
              onChange={(event) => {
                setQuery(event.target.value);
                if (!event.target.value) {
                  setSearch("");
                  setOffset(0);
                }
              }}
              onSearch={(value) => {
                setSearch(value);
                setOffset(0);
              }}
              allowClear
              maxLength={200}
            />
          </div>
          {currentFeed && (
            <div className={styles.feedSettings}>
              <strong>{currentFeed.title}</strong>
              <FeedFolderEditor
                key={currentFeed.id + currentFeed.folder}
                value={currentFeed.folder}
                busy={busy}
                onSave={(value) =>
                  void run(async () => {
                    await readingApi.updateFeed(currentFeed.id, {
                      folder: value,
                    });
                    await reloadRef.current();
                    if (active.current) message.success("订阅分组已保存");
                  })
                }
              />
              <div className={styles.actions}>
                <Button
                  size="small"
                  disabled={busy}
                  onClick={() =>
                    void run(async () => {
                      await readingApi.updateFeed(currentFeed.id, {
                        paused: !currentFeed.paused,
                      });
                      await reloadRef.current();
                    })
                  }
                >
                  {currentFeed.paused ? "恢复更新" : "暂停更新"}
                </Button>
                <Button
                  size="small"
                  disabled={busy}
                  onClick={() =>
                    void run(async () => {
                      await readingApi.unsubscribe(currentFeed.id);
                      changeView("all");
                      await reloadRef.current();
                    })
                  }
                >
                  取消订阅
                </Button>
              </div>
              <small>取消订阅会保留文章和收藏。</small>
              {currentFeed.error && (
                <span role="status">{currentFeed.error}</span>
              )}
            </div>
          )}
          <div className={styles.selection}>
            <span>{total} 篇文章</span>
            <div className={styles.selectionActions}>
              <Button
                size="small"
                disabled={busy || !chosen.length}
                onClick={() => addKnowledge(chosen, true)}
              >
                入库并讨论（{chosen.length}）
              </Button>
              <Button
                size="small"
                danger
                icon={<DeleteOutlined />}
                disabled={busy || !chosen.length}
                onClick={() =>
                  confirmArchive(chosen, `删除所选 ${chosen.length} 篇文章？`)
                }
              >
                删除
              </Button>
            </div>
          </div>
          {loading ? (
            <Spin aria-label="正在加载文章" />
          ) : !items.length ? (
            <Empty
              image={Empty.PRESENTED_IMAGE_SIMPLE}
              description={search ? "没有找到相关文章" : "这里还没有文章"}
            />
          ) : (
            <div className={styles.rows}>
              {items.map((item) => (
                <div className={styles.row} key={item.id}>
                  <Checkbox
                    aria-label={`选择 ${item.title}`}
                    disabled={chosen.length >= 100 && !chosen.includes(item.id)}
                    checked={chosen.includes(item.id)}
                    onChange={(event) =>
                      setChosen((current) =>
                        event.target.checked
                          ? [...current, item.id]
                          : current.filter((id) => id !== item.id),
                      )
                    }
                  />
                  <button
                    className={styles.rowContent}
                    aria-label={item.title}
                    onClick={() => void open(item.id)}
                    aria-current={selected?.id === item.id ? "true" : undefined}
                  >
                    <strong>{item.title}</strong>
                    <span>{item.excerpt || "打开后可读取全文"}</span>
                    <small>
                      {item.starred ? "已收藏 · " : ""}
                      {item.is_read ? "已读" : "未读"} ·{" "}
                      {bodyLabels[item.body_kind]}
                    </small>
                  </button>
                  <Button
                    className={styles.rowDelete}
                    type="text"
                    danger
                    size="small"
                    icon={<DeleteOutlined />}
                    aria-label={`删除 ${item.title}`}
                    title="移到回收站"
                    onClick={() =>
                      confirmArchive([item.id], `删除“${item.title}”？`)
                    }
                  />
                </div>
              ))}
            </div>
          )}
          <div className={styles.pagination}>
            <Button
              size="small"
              disabled={!offset || loading}
              onClick={() => setOffset(Math.max(0, offset - 100))}
            >
              上一页
            </Button>
            <span>
              {Math.floor(offset / 100) + 1} /{" "}
              {Math.max(1, Math.ceil(total / 100))}
            </span>
            <Button
              size="small"
              disabled={offset + 100 >= total || loading}
              onClick={() => setOffset(offset + 100)}
            >
              下一页
            </Button>
          </div>
        </div>
        <article
          ref={readerRef}
          className={styles.reader}
          aria-label="文章正文"
          aria-busy={detailLoading}
        >
          {detailLoading ? (
            <Spin />
          ) : selected ? (
            <>
              <Button
                className={styles.backToList}
                type="text"
                size="small"
                onClick={() => {
                  setSelected(null);
                  window.setTimeout(
                    () => listRef.current?.scrollIntoView?.({ block: "start" }),
                    0,
                  );
                }}
              >
                返回文章列表
              </Button>
              <header className={styles.readerHeader}>
                <h2>{selected.title}</h2>
                <div className={styles.readerProvenance}>
                  <a
                    href={
                      /^https?:\/\//.test(selected.url)
                        ? selected.url
                        : undefined
                    }
                    target="_blank"
                    rel="noopener noreferrer"
                  >
                    {selected.link_kind === "feed"
                      ? "查看来源频道"
                      : "打开原文"}
                  </a>
                  <span className={styles.meta}>
                    {bodyLabels[selected.body_kind]}
                    {selected.published ? ` · ${selected.published}` : ""}
                  </span>
                </div>
              </header>
              <div className={`${styles.actions} ${styles.readerActions}`}>
                <Button
                  size="small"
                  disabled={busy}
                  onClick={() => update({ starred: !selected.starred })}
                >
                  {selected.starred ? "取消收藏" : "收藏"}
                </Button>
                <Button
                  size="small"
                  disabled={busy}
                  onClick={() => update({ is_read: !selected.is_read })}
                >
                  {selected.is_read ? "标为未读" : "标为已读"}
                </Button>
                <Button
                  size="small"
                  disabled={
                    busy || selected.link_kind === "feed" || !selected.url
                  }
                  onClick={() => {
                    const id = selected.id;
                    const version = detailVersion.current;
                    void run(async () => {
                      const result = await readingApi.fetch(id);
                      if (active.current && detailVersion.current === version)
                        setSelected(result);
                      await reloadRef.current();
                    });
                  }}
                >
                  读取全文
                </Button>
                <Button
                  size="small"
                  disabled={busy || !selected.body}
                  onClick={() => addKnowledge([selected.id], false)}
                >
                  加入知识库
                </Button>
                <Button
                  size="small"
                  disabled={busy || !selected.body}
                  onClick={() => addKnowledge([selected.id], true)}
                >
                  入库并讨论
                </Button>
                <Button
                  size="small"
                  danger
                  type="text"
                  icon={<Trash2 size={15} strokeWidth={1.8} />}
                  onClick={() =>
                    confirmArchive([selected.id], `删除“${selected.title}”？`)
                  }
                >
                  删除
                </Button>
              </div>
              <div className={styles.tags}>
                <label htmlFor="reading-tags">标签</label>
                <Input
                  id="reading-tags"
                  value={tagDraft}
                  onChange={(event) => setTagDraft(event.target.value)}
                  placeholder="用逗号分隔"
                  maxLength={1600}
                />
                <Button
                  size="small"
                  disabled={busy}
                  onClick={() =>
                    update({
                      tags: tagDraft
                        .split(/[,，]/)
                        .map((tag) => tag.trim())
                        .filter(Boolean),
                    })
                  }
                >
                  保存标签
                </Button>
              </div>
              <div className={styles.body}>
                {selected.body ||
                  "这篇文章尚未保存正文。点击“读取全文”，或打开原文阅读。"}
              </div>
            </>
          ) : (
            <div className={styles.readerEmpty}>
              <Empty
                image={Empty.PRESENTED_IMAGE_SIMPLE}
                description="选择一篇文章，开始阅读"
              />
              <p>收藏保存在本机；加入知识库后可作为对话资料。</p>
            </div>
          )}
        </article>
      </div>
    </section>
  );
}

function FeedFolderEditor({
  value,
  busy,
  onSave,
}: {
  value: string;
  busy: boolean;
  onSave: (value: string) => void;
}) {
  const [draft, setDraft] = useState(value);
  return (
    <form
      onSubmit={(event) => {
        event.preventDefault();
        onSave(draft);
      }}
    >
      <label>
        订阅分组
        <Input
          value={draft}
          maxLength={80}
          onChange={(event) => setDraft(event.target.value)}
        />
      </label>
      <div className={styles.actions}>
        <Button
          size="small"
          htmlType="submit"
          disabled={busy || draft === value}
        >
          保存分组
        </Button>
        <Button
          size="small"
          disabled={busy || draft === value}
          onClick={() => setDraft(value)}
        >
          取消修改
        </Button>
      </div>
    </form>
  );
}

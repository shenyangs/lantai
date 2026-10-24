import React, {
  useState,
  useCallback,
  useRef,
  useEffect,
  useMemo,
} from "react";
import { Alert, Drawer, Input, Typography, Empty, Spin } from "antd";
import type { InputRef } from "antd";
import { IconButton } from "@/vendor/lantaiDesign";
import { SparkSearchLine } from "@/vendor/lantaiIcons";
import { useChatAnywhereSessionsState } from "@/vendor/lantaiChat";
import { useTranslation } from "react-i18next";
import { useNavigate } from "react-router-dom";
import { X } from "lucide-react";
import sessionApi from "../../sessionApi";
import { requestChatMessageNavigation } from "@/vendor/lantaiChatMessageNavigation";
import {
  loadUnifiedSearch,
  SEARCH_GROUP_ORDER,
  type UnifiedSearchResult,
} from "./unifiedSearch";
import styles from "./index.module.less";

interface ChatSearchPanelProps {
  open: boolean;
  onClose: () => void;
}

export const queueSearchResultMessageNavigation = (
  result: Pick<UnifiedSearchResult, "chatId" | "messageId">,
): void => {
  const messageId = String(result.messageId || "").trim();
  if (!messageId) return;
  requestChatMessageNavigation({
    chatId: result.chatId,
    messageId,
    source: "search",
  });
};

/** Format timestamp for display */
const formatTimestamp = (raw: string | null | undefined): string => {
  if (!raw) return "";
  const date = new Date(raw);
  if (isNaN(date.getTime())) return "";
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(
    date.getDate(),
  )} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
};

const ChatSearchPanel: React.FC<ChatSearchPanelProps> = ({ open, onClose }) => {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const { sessions, setCurrentSessionId } = useChatAnywhereSessionsState();
  const [searchQuery, setSearchQuery] = useState("");
  const [loading, setLoading] = useState(false);
  const [searchResults, setSearchResults] = useState<UnifiedSearchResult[]>([]);
  const [failedSources, setFailedSources] = useState<string[]>([]);
  const inputRef = useRef<InputRef>(null);
  const searchTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  /** Monotonic id so slow list/getChat responses cannot overwrite a newer search. */
  const searchSeqRef = useRef(0);

  // Clean up on unmount: clear timers and release result data
  useEffect(() => {
    return () => {
      if (searchTimeoutRef.current) {
        clearTimeout(searchTimeoutRef.current);
      }
      setSearchResults([]);
      setSearchQuery("");
    };
  }, []);

  // Focus input when drawer opens
  useEffect(() => {
    if (open) {
      setTimeout(() => {
        inputRef.current?.focus();
      }, 100);
    } else {
      setSearchQuery("");
      setSearchResults([]);
      setFailedSources([]);
    }
  }, [open]);

  // Debounce one concurrent fan-out across the five indexed product sources.
  useEffect(() => {
    const seq = ++searchSeqRef.current;

    if (!searchQuery.trim()) {
      setSearchResults([]);
      setFailedSources([]);
      setLoading(false);
      return;
    }

    if (searchTimeoutRef.current) {
      clearTimeout(searchTimeoutRef.current);
    }

    searchTimeoutRef.current = setTimeout(async () => {
      setLoading(true);
      setFailedSources([]);
      try {
        const response = await loadUnifiedSearch(searchQuery.trim());
        if (seq !== searchSeqRef.current) return;
        setSearchResults(response.results);
        setFailedSources(response.failedSources);
      } catch (err) {
        if (seq !== searchSeqRef.current) return;
        console.error("Search failed:", err);
        setSearchResults([]);
        setFailedSources(["全部范围"]);
      } finally {
        if (seq === searchSeqRef.current) {
          setLoading(false);
        }
      }
    }, 300);

    return () => {
      if (searchTimeoutRef.current) {
        clearTimeout(searchTimeoutRef.current);
      }
    };
  }, [searchQuery]);

  const groupedResults = useMemo(
    () =>
      SEARCH_GROUP_ORDER.map((kind) => ({
        kind,
        items: searchResults.filter((item) => item.kind === kind).slice(0, 8),
      })).filter((group) => group.items.length > 0),
    [searchResults],
  );

  // Navigate to chat when clicking result
  const handleResultClick = useCallback(
    (result: UnifiedSearchResult) => {
      if (result.kind !== "chat" || !result.chatId) {
        navigate(result.path);
        onClose();
        return;
      }
      queueSearchResultMessageNavigation(result);
      // Find the session in the local list
      const session = sessions.find((s) => {
        const realId = sessionApi.getRealIdForSession(s.id || "");
        return (
          realId === result.chatId ||
          s.id === result.chatId ||
          (s as any).sessionId === result.chatId
        );
      });

      if (session?.id) {
        // Switch through the same explicit-selection path as the header/drawer.
        sessionApi.selectSession(session.id);
        setCurrentSessionId(session.id);
      } else {
        // Session not in local list, navigate by chat ID directly
        navigate(result.path);
      }

      onClose();
    },
    [sessions, setCurrentSessionId, navigate, onClose],
  );

  return (
    <Drawer
      open={open}
      onClose={onClose}
      destroyOnClose
      placement="right"
      width={360}
      closable={false}
      title={null}
      styles={{
        header: { display: "none" },
        body: {
          padding: 0,
          display: "flex",
          flexDirection: "column",
          height: "100%",
          overflow: "hidden",
        },
        mask: { background: "transparent" },
      }}
      className={styles.drawer}
    >
      {/* Header bar */}
      <div className={styles.header}>
        <div className={styles.headerLeft}>
          <span className={styles.headerTitle}>搜索工作内容</span>
        </div>
        <div className={styles.headerRight}>
          <IconButton
            bordered={false}
            icon={<X size={17} aria-hidden="true" />}
            aria-label="关闭搜索"
            title="关闭搜索"
            onClick={onClose}
          />
        </div>
      </div>

      {/* Search input */}
      <div className={styles.searchSection}>
        <Input
          ref={inputRef}
          placeholder="搜索聊天、记忆、资料、任务或作品"
          aria-label="搜索聊天、记忆、资料、任务或作品"
          prefix={<SparkSearchLine style={{ color: "rgba(0,0,0,0.25)" }} />}
          value={searchQuery}
          onChange={(e) => setSearchQuery(e.target.value)}
          allowClear
          className={styles.searchInput}
        />
        <small className={styles.searchScope}>
          当前智能体 · 包含普通、已归档与编程会话
        </small>
      </div>

      {/* Results count / search progress */}
      {searchQuery.trim() && (
        <div className={styles.resultsCount}>
          <Typography.Text type="secondary">
            {loading
              ? t("chat.search.loading")
              : t("chat.search.resultsCount", {
                  count: searchResults.length,
                })}
          </Typography.Text>
        </div>
      )}

      {/* Results list */}
      <div className={styles.listWrapper}>
        <div className={styles.topGradient} />
        <div className={styles.list}>
          {failedSources.length > 0 ? (
            <Alert
              className={styles.partialAlert}
              type="warning"
              showIcon
              message={
                failedSources.includes("全部范围")
                  ? "搜索暂时不可用，请稍后重试"
                  : `${failedSources.join("、")}暂时无法读取，其它结果仍可使用`
              }
            />
          ) : null}
          {!searchQuery.trim() ? (
            <section className={styles.searchGuide} aria-label="可搜索的内容">
              <strong>从一个具体词开始</strong>
              <p>可搜索项目名、文件名、任务标题，或你记得的一句话。</p>
              <div>
                <span>聊天</span>
                <span>记忆</span>
                <span>资料</span>
                <span>任务</span>
                <span>作品</span>
              </div>
            </section>
          ) : loading && searchResults.length === 0 ? (
            <div
              style={{ display: "flex", justifyContent: "center", padding: 40 }}
            >
              <Spin />
            </div>
          ) : searchQuery.trim() && !loading && searchResults.length === 0 ? (
            <Empty description="没有找到匹配内容" style={{ marginTop: 40 }} />
          ) : (
            groupedResults.map((group) => (
              <section className={styles.resultGroup} key={group.kind}>
                <div className={styles.resultGroupHeader}>
                  <strong>{group.items[0]?.groupLabel}</strong>
                  <span>{group.items.length}</span>
                </div>
                {group.items.map((item) => (
                  <div
                    key={item.id}
                    className={styles.searchResultItem}
                    onClick={() => handleResultClick(item)}
                    role="button"
                    tabIndex={0}
                    onKeyDown={(event) => {
                      if (event.key === "Enter" || event.key === " ") {
                        event.preventDefault();
                        handleResultClick(item);
                      }
                    }}
                  >
                    <div className={styles.resultHeader}>
                      <span className={styles.resultChatName}>
                        {item.title}
                      </span>
                      <span className={styles.resultRole}>{item.meta}</span>
                    </div>
                    <div className={styles.resultContent}>
                      <Typography.Text ellipsis style={{ fontSize: 13 }}>
                        {item.excerpt}
                      </Typography.Text>
                    </div>
                    {item.timestamp && (
                      <div className={styles.resultTime}>
                        {formatTimestamp(item.timestamp)}
                      </div>
                    )}
                  </div>
                ))}
              </section>
            ))
          )}
        </div>
        <div className={styles.bottomGradient} />
      </div>
    </Drawer>
  );
};

export default ChatSearchPanel;

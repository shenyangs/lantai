import { Alert, Button, Empty, Input, Segmented, Select, Skeleton } from "antd";
import { Columns3, History, List, Plus, RefreshCw } from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";

import { runtimeApi } from "../../../api/modules/runtime";
import type {
  CreateRuntimeTaskRequest,
  RuntimeAutonomyGrantRequest,
  RuntimeTaskBoardItem,
  RuntimeTaskBoardResponse,
} from "../../../api/types/runtime";
import { CapabilityPage } from "../../../components/CapabilityLayout";
import { useAppMessage } from "../../../hooks/useAppMessage";
import { useAgentStore } from "../../../stores/agentStore";
import CreateTaskDrawer from "./CreateTaskDrawer";
import TaskBoard from "./TaskBoard";
import TaskDetailsDrawer from "./TaskDetailsDrawer";
import TaskList from "./TaskList";
import { taskBoardPollInterval } from "./taskBoardModel";
import styles from "./index.module.less";

type TaskView = "board" | "list" | "history";

const TASK_VIEW_STORAGE_KEY = "lantai:task-center-view:v2";
const EMPTY_BOARD: RuntimeTaskBoardResponse = {
  total: 0,
  visible_total: 0,
  generated_at: "",
  counts: {
    pending: 0,
    active: 0,
    needs_user: 0,
    done: 0,
    needs_action: 0,
    history: 0,
  },
  items: [],
};

function loadInitialView(): TaskView {
  try {
    const value = window.localStorage.getItem(TASK_VIEW_STORAGE_KEY);
    return value === "list" || value === "history" ? value : "board";
  } catch {
    return "board";
  }
}

function emitTaskBoardChanged() {
  window.dispatchEvent(new Event("lantai:task-board-changed"));
}

export default function TaskCenterV2Page() {
  const navigate = useNavigate();
  const [searchParams, setSearchParams] = useSearchParams();
  const { message } = useAppMessage();
  const selectedAgent = useAgentStore((state) => state.selectedAgent);
  const [view, setView] = useState<TaskView>(loadInitialView);
  const [source, setSource] = useState("all");
  const [search, setSearch] = useState("");
  const [debouncedSearch, setDebouncedSearch] = useState("");
  const [board, setBoard] = useState<RuntimeTaskBoardResponse>(EMPTY_BOARD);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [createOpen, setCreateOpen] = useState(false);
  const [creating, setCreating] = useState(false);
  const [selected, setSelected] = useState<RuntimeTaskBoardItem | null>(null);
  const [actionRunId, setActionRunId] = useState<string | null>(null);
  const requestSeq = useRef(0);
  const hasLoaded = useRef(false);
  const requestedRunId = String(searchParams.get("run") || "").trim();

  useEffect(() => {
    const timer = window.setTimeout(
      () => setDebouncedSearch(search.trim()),
      260,
    );
    return () => window.clearTimeout(timer);
  }, [search]);

  useEffect(() => {
    try {
      window.localStorage.setItem(TASK_VIEW_STORAGE_KEY, view);
    } catch {
      // The preference is optional in restricted browser contexts.
    }
  }, [view]);

  const loadBoard = useCallback(
    async (foreground = false) => {
      const seq = ++requestSeq.current;
      if (foreground) setRefreshing(true);
      try {
        const response = await runtimeApi.getTaskBoard({
          view: view === "history" ? "history" : "board",
          source: source === "all" ? undefined : source,
          query: debouncedSearch || undefined,
          limit: 300,
        });
        if (seq !== requestSeq.current) return;
        setBoard(response);
        setError(null);
        let requestedItem = requestedRunId
          ? response.items.find((item) => item.id === requestedRunId)
          : undefined;
        if (requestedRunId && !requestedItem) {
          const lookup = await runtimeApi.getTaskBoard({
            view: "history",
            query: requestedRunId,
            limit: 10,
          });
          if (seq !== requestSeq.current) return;
          requestedItem = lookup.items.find(
            (item) => item.id === requestedRunId,
          );
        }
        setSelected(
          (current) =>
            requestedItem ||
            (current
              ? response.items.find((item) => item.id === current.id) || current
              : null),
        );
      } catch (loadError) {
        if (seq !== requestSeq.current) return;
        setError(
          loadError instanceof Error
            ? loadError.message
            : "任务状态暂时无法读取",
        );
      } finally {
        if (seq === requestSeq.current) {
          hasLoaded.current = true;
          setLoading(false);
          setRefreshing(false);
        }
      }
    },
    [debouncedSearch, requestedRunId, source, view],
  );

  useEffect(() => {
    if (!hasLoaded.current) setLoading(true);
    void loadBoard();
  }, [loadBoard, selectedAgent]);

  useEffect(() => {
    let timer = 0;
    let disposed = false;
    const schedule = () => {
      if (disposed) return;
      timer = window.setTimeout(
        async () => {
          await loadBoard();
          schedule();
        },
        taskBoardPollInterval(board.items, document.hidden),
      );
    };
    const refreshNow = () => void loadBoard();
    schedule();
    window.addEventListener("focus", refreshNow);
    window.addEventListener("visibilitychange", refreshNow);
    window.addEventListener("lantai:task-board-changed", refreshNow);
    return () => {
      disposed = true;
      window.clearTimeout(timer);
      window.removeEventListener("focus", refreshNow);
      window.removeEventListener("visibilitychange", refreshNow);
      window.removeEventListener("lantai:task-board-changed", refreshNow);
    };
  }, [board.items, loadBoard]);

  const needsUserItems = useMemo(
    () => board.items.filter((item) => item.lane === "needs_user"),
    [board.items],
  );

  const createTask = async (payload: CreateRuntimeTaskRequest) => {
    setCreating(true);
    try {
      const result = await runtimeApi.createTask(payload);
      setCreateOpen(false);
      message.success(
        result.start ? "任务已保存并开始运行" : "任务已保存到待开始",
      );
      emitTaskBoardChanged();
      await loadBoard(true);
    } catch (createError) {
      message.error(
        createError instanceof Error ? createError.message : "任务创建失败",
      );
      throw createError;
    } finally {
      setCreating(false);
    }
  };

  const runAction = async (
    item: RuntimeTaskBoardItem,
    action: () => Promise<unknown>,
    successText: string,
  ): Promise<boolean> => {
    setActionRunId(item.id);
    try {
      await action();
      message.success(successText);
      emitTaskBoardChanged();
      await loadBoard(true);
      return true;
    } catch (actionError) {
      message.error(
        actionError instanceof Error ? actionError.message : "操作失败",
      );
      return false;
    } finally {
      setActionRunId(null);
    }
  };

  const startTask = (item: RuntimeTaskBoardItem) =>
    void runAction(item, () => runtimeApi.startTask(item.id), "任务已开始");

  const interruptTask = (item: RuntimeTaskBoardItem) =>
    void runAction(
      item,
      () =>
        runtimeApi.interruptRun(item.id, {
          expected_attempt_id: item.attempt_id,
          expected_revision: item.revision,
          chat_id: item.chat_id,
          session_id: item.session_id,
          workspace_dir: item.workspace_dir,
        }),
      "停止请求已提交",
    );

  const continueTask = (item: RuntimeTaskBoardItem) =>
    void runAction(
      item,
      () =>
        runtimeApi.continueRun(item.id, {
          expected_attempt_id: item.attempt_id,
          expected_revision: item.revision,
          chat_id: item.chat_id,
          session_id: item.session_id,
          workspace_dir: item.workspace_dir,
        }),
      "继续上下文已准备，正在打开会话",
    ).then((succeeded) => {
      if (succeeded && item.chat_id) {
        navigate(`/chat/${encodeURIComponent(item.chat_id)}`);
      }
    });

  const grantAutonomy = (
    item: RuntimeTaskBoardItem,
    request: RuntimeAutonomyGrantRequest,
  ) =>
    runAction(
      item,
      () => runtimeApi.grantRunAutonomy(item.id, request),
      "后台续跑授权已生效",
    );

  const revokeAutonomy = (item: RuntimeTaskBoardItem) =>
    runAction(
      item,
      () =>
        runtimeApi.revokeRunAutonomy(item.id, {
          expected_attempt_id: item.attempt_id,
          expected_revision: item.revision,
          chat_id: item.chat_id,
          session_id: item.session_id,
          workspace_dir: item.workspace_dir,
          reason: "用户在任务详情中明确撤销后台续跑授权",
        }),
      "后台续跑授权已撤销",
    );

  const openChat = (item: RuntimeTaskBoardItem) => {
    if (item.chat_id) navigate(`/chat/${encodeURIComponent(item.chat_id)}`);
  };

  const openTask = (item: RuntimeTaskBoardItem) => {
    setSelected(item);
    const next = new URLSearchParams(searchParams);
    next.set("run", item.id);
    setSearchParams(next, { replace: true });
  };

  const closeTask = () => {
    setSelected(null);
    const next = new URLSearchParams(searchParams);
    next.delete("run");
    setSearchParams(next, { replace: true });
  };

  return (
    <CapabilityPage
      section="控制"
      title="任务"
      description="把智能体工作保存、启动、跟踪和处理放在同一张任务图上。卡片状态来自运行账本，不靠手工拖动。"
      className={styles.page}
      stats={[
        { label: "进行中", value: board.counts.active, tone: "info" },
        {
          label: "等你决定",
          value: board.counts.needs_user,
          tone: board.counts.needs_user ? "warning" : "muted",
        },
        {
          label: "需要处理",
          value: board.counts.needs_action,
          tone: board.counts.needs_action ? "danger" : "muted",
        },
        { label: "已交付", value: board.counts.done, tone: "success" },
      ]}
      actions={
        <>
          <Button
            icon={<RefreshCw size={15} />}
            loading={refreshing}
            onClick={() => void loadBoard(true)}
          >
            刷新
          </Button>
          <Button
            type="primary"
            icon={<Plus size={16} />}
            onClick={() => setCreateOpen(true)}
          >
            创建任务
          </Button>
        </>
      }
      toolbar={
        <div className={styles.toolbarInner}>
          <Segmented<TaskView>
            value={view}
            onChange={setView}
            options={[
              { value: "board", label: "看板", icon: <Columns3 size={15} /> },
              { value: "list", label: "列表", icon: <List size={15} /> },
              {
                value: "history",
                label: "历史",
                icon: <History size={15} />,
              },
            ]}
          />
          <div className={styles.filters}>
            <Input.Search
              allowClear
              value={search}
              onChange={(event) => setSearch(event.target.value)}
              placeholder="搜索任务名称或技术 ID"
              aria-label="搜索任务"
            />
            <Select
              value={source}
              onChange={setSource}
              aria-label="筛选任务来源"
              options={[
                { value: "all", label: "全部来源" },
                { value: "chat", label: "聊天与任务" },
                { value: "cron", label: "定时任务" },
                { value: "browser", label: "浏览器" },
                { value: "capability", label: "能力" },
                { value: "pipeline", label: "流水线" },
                { value: "reminder", label: "提醒" },
              ]}
            />
          </div>
        </div>
      }
    >
      <div className={styles.workspace}>
        {error ? (
          <Alert
            type="error"
            showIcon
            message="任务状态读取失败"
            description={error}
            action={<Button onClick={() => void loadBoard(true)}>重试</Button>}
          />
        ) : null}

        {needsUserItems.length ? (
          <button
            type="button"
            className={styles.attentionStrip}
            onClick={() => openTask(needsUserItems[0])}
          >
            <span>{needsUserItems.length}</span>
            <strong>个任务在等你决定</strong>
            <em>{needsUserItems[0].next_action || "打开任务查看所需操作"}</em>
          </button>
        ) : null}

        {loading ? (
          <div className={styles.boardSkeleton} aria-label="正在加载任务">
            {[0, 1, 2, 3, 4].map((key) => (
              <Skeleton key={key} active paragraph={{ rows: 5 }} />
            ))}
          </div>
        ) : board.items.length ? (
          view === "board" ? (
            <TaskBoard
              items={board.items}
              actionRunId={actionRunId}
              onOpen={openTask}
              onStart={startTask}
            />
          ) : (
            <TaskList items={board.items} onOpen={openTask} />
          )
        ) : (
          <div className={styles.emptyState}>
            <Empty
              image={Empty.PRESENTED_IMAGE_SIMPLE}
              description={
                view === "history"
                  ? "还没有可回看的任务"
                  : search
                    ? "没有匹配的任务"
                    : "还没有任务。先把一件要做的事保存下来。"
              }
            >
              {view !== "history" && !search ? (
                <Button type="primary" onClick={() => setCreateOpen(true)}>
                  创建第一个任务
                </Button>
              ) : null}
            </Empty>
          </div>
        )}
      </div>

      <CreateTaskDrawer
        open={createOpen}
        busy={creating}
        onClose={() => setCreateOpen(false)}
        onCreate={createTask}
      />
      <TaskDetailsDrawer
        item={selected}
        busy={actionRunId === selected?.id}
        onClose={closeTask}
        onStart={startTask}
        onInterrupt={interruptTask}
        onContinue={continueTask}
        onOpenChat={openChat}
        onGrantAutonomy={grantAutonomy}
        onRevokeAutonomy={revokeAutonomy}
        onOpenAdvanced={(item) =>
          navigate(`/task-center/legacy?run=${encodeURIComponent(item.id)}`)
        }
      />
    </CapabilityPage>
  );
}

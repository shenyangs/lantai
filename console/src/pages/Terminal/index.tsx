import { Button, Input, InputNumber, List, Space, Tag } from "antd";
import {
  CircleStop,
  ClipboardCopy,
  Play,
  RefreshCw,
  TerminalSquare,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { codingApi } from "../../api/modules/coding";
import type { CodeTaskRun } from "../../api/types";
import { PageHeader } from "../../components/PageHeader";
import { useAppMessage } from "../../hooks/useAppMessage";
import { SettingsEmptyState } from "../Settings/components/SettingsEmptyState";
import { SettingsNotice } from "../Settings/components/SettingsNotice";
import styles from "./index.module.less";

const ACTIVE_STATUSES = new Set(["queued", "running", "stopping"]);

const STATUS_LABELS: Record<string, string> = {
  queued: "排队中",
  running: "运行中",
  stopping: "正在停止",
  completed: "已完成",
  failed: "失败",
  timeout: "超时",
  stalled: "异常中断",
  cancelled: "已停止",
};

const STATUS_COLORS: Record<string, string> = {
  queued: "blue",
  running: "processing",
  stopping: "orange",
  completed: "success",
  failed: "error",
  timeout: "warning",
  stalled: "warning",
  cancelled: "default",
};

function taskOutput(task: CodeTaskRun | null): string {
  if (!task) return "";
  const sections: string[] = [];
  if (task.stdout_tail) sections.push(task.stdout_tail);
  if (task.stderr_tail) sections.push(`[stderr]\n${task.stderr_tail}`);
  if (task.error && !task.stderr_tail.includes(task.error)) {
    sections.push(`[status]\n${task.error}`);
  }
  return sections.join("\n");
}

function formatTime(value?: string | null): string {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleTimeString("zh-CN", {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  });
}

export default function TerminalPage() {
  const { message } = useAppMessage();
  const [command, setCommand] = useState("");
  const [timeoutSeconds, setTimeoutSeconds] = useState(300);
  const [workspaceDir, setWorkspaceDir] = useState("");
  const [tasks, setTasks] = useState<CodeTaskRun[]>([]);
  const [activeTaskId, setActiveTaskId] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [submitting, setSubmitting] = useState(false);
  const outputRef = useRef<HTMLPreElement | null>(null);

  const activeTask = useMemo(
    () =>
      tasks.find((task) => task.task_id === activeTaskId) || tasks[0] || null,
    [activeTaskId, tasks],
  );
  const output = useMemo(() => taskOutput(activeTask), [activeTask]);
  const hasRunningTask = tasks.some((task) => ACTIVE_STATUSES.has(task.status));

  const refresh = useCallback(
    async (quiet = false) => {
      if (!quiet) setLoading(true);
      try {
        const [state, taskList] = await Promise.all([
          codingApi.getWorkbenchState(),
          codingApi.listTasks(),
        ]);
        setWorkspaceDir(state.workspace_dir);
        setTasks(taskList.tasks);
        setActiveTaskId((current) => {
          if (
            current &&
            taskList.tasks.some((task) => task.task_id === current)
          ) {
            return current;
          }
          return taskList.tasks[0]?.task_id || null;
        });
      } catch (error) {
        if (!quiet) {
          message.error(
            error instanceof Error ? error.message : "命令状态加载失败",
          );
        }
      } finally {
        if (!quiet) setLoading(false);
      }
    },
    [message],
  );

  useEffect(() => {
    void refresh();
  }, [refresh]);

  useEffect(() => {
    const interval = window.setInterval(
      () => void refresh(true),
      hasRunningTask ? 700 : 2500,
    );
    return () => window.clearInterval(interval);
  }, [hasRunningTask, refresh]);

  useEffect(() => {
    const node = outputRef.current;
    if (node) node.scrollTop = node.scrollHeight;
  }, [output, activeTask?.output_seq]);

  const startTask = async () => {
    const value = command.trim();
    if (!value) {
      message.warning("请输入要执行的命令");
      return;
    }
    setSubmitting(true);
    try {
      const task = await codingApi.startTask({
        command: value,
        confirmed: true,
        timeout_seconds: timeoutSeconds,
      });
      setTasks((current) => [
        task,
        ...current.filter((item) => item.task_id !== task.task_id),
      ]);
      setActiveTaskId(task.task_id);
      setCommand("");
      message.success("命令已开始执行");
    } catch (error) {
      message.error(error instanceof Error ? error.message : "命令启动失败");
    } finally {
      setSubmitting(false);
    }
  };

  const stopTask = async () => {
    if (!activeTask || !ACTIVE_STATUSES.has(activeTask.status)) return;
    try {
      const stopped = await codingApi.stopTask(activeTask.task_id);
      setTasks((current) =>
        current.map((task) =>
          task.task_id === stopped.task_id ? stopped : task,
        ),
      );
    } catch (error) {
      message.error(error instanceof Error ? error.message : "停止失败");
    }
  };

  const copyContext = async () => {
    if (!activeTask) return;
    const text = [
      `工作目录: ${activeTask.workspace_dir}`,
      `命令: ${activeTask.command}`,
      `状态: ${activeTask.status}`,
      `退出码: ${activeTask.returncode ?? "未结束"}`,
      "",
      output || "（暂无输出）",
    ].join("\n");
    try {
      await navigator.clipboard.writeText(text);
      message.success("排障上下文已复制，可粘贴回聊天继续分析");
    } catch {
      message.error("复制失败");
    }
  };

  return (
    <div className={styles.page}>
      <PageHeader
        parent="设置"
        current="工作区命令"
        description="用于排障、测试和构建的一次性命令工具。普通使用不需要进入这里。"
      />

      <div className={styles.content}>
        <SettingsNotice title="仅用于当前工作区的排障与验证">
          命令受 Coding Tool Guard 和工作区边界保护。
        </SettingsNotice>

        {activeTask ? (
          <aside className={styles.activeTaskStrip} aria-label="当前命令">
            <span>当前命令</span>
            <code>{activeTask.command}</code>
            <Tag color={STATUS_COLORS[activeTask.status] || "default"}>
              {STATUS_LABELS[activeTask.status] || activeTask.status}
            </Tag>
            <button
              type="button"
              onClick={() =>
                outputRef.current?.scrollIntoView({ block: "center" })
              }
            >
              查看输出
            </button>
          </aside>
        ) : null}

        <section className={styles.commandPanel}>
          <header className={styles.panelHeader}>
            <div>
              <h2>运行一次命令</h2>
              <p>输出会持续刷新，并仅保留最近的排障上下文。</p>
            </div>
            <Tag>当前工作区</Tag>
          </header>
          <div className={styles.panelBody}>
            <div className={styles.workspaceLine}>
              <span>工作目录</span>
              <code title={workspaceDir}>{workspaceDir || "正在读取…"}</code>
            </div>
            <label className={styles.commandField}>
              <span>命令</span>
              <Input.TextArea
                value={command}
                onChange={(event) => setCommand(event.target.value)}
                placeholder="例如：npm test、pytest -q、git status"
                aria-label="当前工作区命令"
                autoSize={{ minRows: 2, maxRows: 6 }}
                onPressEnter={(event) => {
                  if ((event.metaKey || event.ctrlKey) && !submitting)
                    void startTask();
                }}
              />
            </label>
            <div className={styles.commandActions}>
              <Space size={8}>
                <span className={styles.muted}>超时</span>
                <InputNumber
                  min={1}
                  max={1200}
                  value={timeoutSeconds}
                  onChange={(value) => setTimeoutSeconds(Number(value || 300))}
                  suffix="秒"
                />
              </Space>
              <Space size={8}>
                <Button
                  icon={<RefreshCw size={15} />}
                  onClick={() => void refresh()}
                  loading={loading}
                >
                  刷新状态
                </Button>
                <Button
                  type="primary"
                  icon={<Play size={15} />}
                  loading={submitting}
                  onClick={() => void startTask()}
                >
                  运行命令
                </Button>
              </Space>
            </div>
          </div>
        </section>

        {tasks.length ? (
          <div className={styles.workArea}>
            <section className={styles.taskList}>
              <div className={styles.sectionHeader}>
                <strong>最近运行</strong>
                <span>{tasks.length} 条</span>
              </div>
              <List
                dataSource={tasks}
                renderItem={(task) => (
                  <List.Item
                    className={`${styles.taskItem} ${task.task_id === activeTask?.task_id ? styles.taskItemActive : ""}`}
                    onClick={() => setActiveTaskId(task.task_id)}
                  >
                    <div className={styles.taskItemBody}>
                      <code>{task.command}</code>
                      <div>
                        <Tag color={STATUS_COLORS[task.status] || "default"}>
                          {STATUS_LABELS[task.status] || task.status}
                        </Tag>
                        <span>{formatTime(task.started_at)}</span>
                      </div>
                    </div>
                  </List.Item>
                )}
              />
            </section>

            <section className={styles.outputPanel}>
              <div className={styles.sectionHeader}>
                <div className={styles.outputTitle}>
                  <strong>实时输出</strong>
                  {activeTask ? (
                    <Tag color={STATUS_COLORS[activeTask.status] || "default"}>
                      {STATUS_LABELS[activeTask.status] || activeTask.status}
                    </Tag>
                  ) : null}
                </div>
                <Space size={6}>
                  <Button
                    size="small"
                    icon={<ClipboardCopy size={14} />}
                    disabled={!activeTask}
                    onClick={() => void copyContext()}
                  >
                    复制排障上下文
                  </Button>
                  <Button
                    size="small"
                    danger
                    icon={<CircleStop size={14} />}
                    disabled={
                      !activeTask || !ACTIVE_STATUSES.has(activeTask.status)
                    }
                    onClick={() => void stopTask()}
                  >
                    停止
                  </Button>
                </Space>
              </div>
              <div className={styles.commandMeta}>
                <code>{activeTask?.command}</code>
                <span>
                  {activeTask?.returncode === null ||
                  activeTask?.returncode === undefined
                    ? "进程未结束"
                    : `退出码 ${activeTask.returncode}`}
                </span>
              </div>
              <pre ref={outputRef} className={styles.output}>
                {output ||
                  (activeTask && ACTIVE_STATUSES.has(activeTask.status)
                    ? "等待进程输出…"
                    : "该命令没有输出")}
              </pre>
            </section>
          </div>
        ) : (
          <section className={styles.emptyPanel}>
            <SettingsEmptyState
              className={styles.compactEmpty}
              icon={<TerminalSquare size={17} />}
              title="暂无命令记录"
              description="这是低频排障工具；只有需要验证构建、测试或工作区状态时才需要运行命令。"
            />
          </section>
        )}
      </div>
    </div>
  );
}

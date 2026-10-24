import { useCallback, useEffect, useRef, useState } from "react";
import { ArrowLeftOutlined, FileTextOutlined } from "@ant-design/icons";
import { Button } from "antd";
import { FileWarning, Loader2 } from "lucide-react";
import { useNavigate } from "react-router-dom";
import styles from "./index.module.less";
import { requireOfficeWorkbenchResult } from "./ipcResult";

const KIND_LABELS: Record<string, string> = {
  docs: "文档",
  sheets: "表格",
  slides: "演示",
  pdf: "PDF",
};
const MIN_EDITOR_WIDTH = 320;
const MIN_EDITOR_HEIGHT = 200;

export default function OfficeWorkbench() {
  const navigate = useNavigate();
  const hostRef = useRef<HTMLDivElement>(null);
  const [state, setState] = useState<OfficeWorkbenchState | null>(null);
  const [loadState, setLoadState] = useState<
    "loading" | "ready" | "empty" | "unsupported" | "error"
  >("loading");
  const [errorText, setErrorText] = useState("");
  const [errorKind, setErrorKind] = useState<"state" | "layout">("state");
  const [operationError, setOperationError] = useState("");

  const loadWorkbench = useCallback(async () => {
    const api = window.electronAPI?.officeWorkbench;
    if (!api) {
      setState(null);
      setLoadState("unsupported");
      return;
    }
    setLoadState("loading");
    setErrorText("");
    setOperationError("");
    try {
      const next = requireOfficeWorkbenchResult(
        await api.getState(),
        "无法读取本地编辑器状态",
      );
      setState(next);
      setLoadState(next?.workbench_id ? "ready" : "empty");
    } catch (error) {
      setState(null);
      setErrorKind("state");
      setErrorText(
        error instanceof Error ? error.message : "无法读取本地编辑器状态",
      );
      setLoadState("error");
    }
  }, []);

  const syncLayout = useCallback(() => {
    const api = window.electronAPI?.officeWorkbench;
    const host = hostRef.current;
    if (!api || !host || !state?.workbench_id) return;
    const rect = host.getBoundingClientRect();
    if (rect.width < MIN_EDITOR_WIDTH || rect.height < MIN_EDITOR_HEIGHT) {
      return;
    }
    void api
      .layout({
        workbench_id: state.workbench_id,
        bounds: {
          x: Math.round(rect.left),
          y: Math.round(rect.top),
          width: Math.round(rect.width),
          height: Math.round(rect.height),
        },
      })
      .then((result) =>
        requireOfficeWorkbenchResult(result, "编辑器窗口布局失败"),
      )
      .catch((error) => {
        setErrorKind("layout");
        setErrorText(
          error instanceof Error ? error.message : "编辑器窗口布局失败",
        );
        setLoadState("error");
      });
  }, [state?.workbench_id]);

  useEffect(() => {
    const api = window.electronAPI?.officeWorkbench;
    if (!api) {
      setLoadState("unsupported");
      return undefined;
    }
    let disposed = false;
    void api
      .getState()
      .then((result) =>
        requireOfficeWorkbenchResult(result, "无法读取本地编辑器状态"),
      )
      .then((next) => {
        if (!disposed) {
          setState(next);
          setLoadState(next?.workbench_id ? "ready" : "empty");
        }
      })
      .catch((error) => {
        if (!disposed) {
          setErrorKind("state");
          setErrorText(
            error instanceof Error ? error.message : "无法启动本地编辑器",
          );
          setLoadState("error");
        }
      });
    return () => {
      disposed = true;
      void api
        .hide()
        .then((result) =>
          requireOfficeWorkbenchResult(result, "无法隐藏本地编辑器"),
        )
        .catch(() => undefined);
    };
  }, []);

  useEffect(() => {
    const host = hostRef.current;
    if (!host || !state?.workbench_id) return undefined;
    const observer = new ResizeObserver(syncLayout);
    observer.observe(host);
    window.addEventListener("resize", syncLayout);
    const handleVisibility = () => {
      if (document.visibilityState === "visible") syncLayout();
    };
    window.addEventListener("pageshow", syncLayout);
    document.addEventListener("visibilitychange", handleVisibility);
    // Probe synchronously but refuse a zero-sized cold-start host. Resize,
    // pageshow and visibility recovery will submit the first usable bounds.
    syncLayout();
    const frame = window.requestAnimationFrame(syncLayout);
    return () => {
      window.cancelAnimationFrame(frame);
      window.removeEventListener("resize", syncLayout);
      window.removeEventListener("pageshow", syncLayout);
      document.removeEventListener("visibilitychange", handleVisibility);
      observer.disconnect();
    };
  }, [state?.workbench_id, syncLayout]);

  const closeWorkbench = async () => {
    const sourceChatId = String(state?.source_chat_id || "").trim();
    const api = window.electronAPI?.officeWorkbench;
    setOperationError("");
    if (api) {
      try {
        requireOfficeWorkbenchResult(
          await api.hide(),
          "无法确认编辑器已经隐藏",
        );
      } catch (error) {
        setOperationError(
          error instanceof Error ? error.message : "无法确认编辑器已经隐藏",
        );
        return;
      }
    }
    navigate(
      sourceChatId ? `/chat/${encodeURIComponent(sourceChatId)}` : "/chat",
    );
  };

  return (
    <section className={styles.workbench} aria-label="本地 Office 工作台">
      <div className={styles.toolbar}>
        <Button
          type="text"
          icon={<ArrowLeftOutlined />}
          onClick={closeWorkbench}
        >
          返回对话
        </Button>
        <div className={styles.identity}>
          <FileTextOutlined />
          <strong>{state?.display_name || "本地 Office 工作台"}</strong>
          <span>{KIND_LABELS[state?.kind || ""] || "Office"}</span>
        </div>
        {operationError ? (
          <span className={styles.operationError} role="alert">
            {operationError}
          </span>
        ) : (
          <span className={styles.hint}>在澜台内编辑，保存仍写回原文件</span>
        )}
      </div>
      {loadState === "ready" ? (
        <div ref={hostRef} className={styles.editorHost} />
      ) : (
        <div className={styles.stateStage} role="status">
          {loadState === "loading" ? (
            <>
              <Loader2 className={styles.spin} size={28} />
              <strong>正在启动本地编辑器</strong>
              <span>文件仍保留在原位置，加载完成前不会写入。</span>
            </>
          ) : (
            <>
              <FileWarning size={30} strokeWidth={1.5} />
              <strong>
                {loadState === "unsupported"
                  ? "当前环境不支持本地 Office 工作台"
                  : loadState === "empty"
                    ? "没有打开的 Office 文件"
                    : errorKind === "layout"
                      ? "编辑器视图不可用"
                      : "编辑器状态不可用"}
              </strong>
              <span>
                {loadState === "unsupported"
                  ? "请在澜台桌面版中从作品或聊天成果打开文档、表格、演示或 PDF。"
                  : loadState === "empty"
                    ? "直接进入此页面不会创建空白文件，请从原会话或作品选择已有文件。"
                    : `${errorText || "无法确认本地编辑器状态。"} 本次错误无法确认文件保存结果，保存状态需要在原文件中核对。`}
              </span>
              <div className={styles.stateActions}>
                {loadState === "error" ? (
                  <Button onClick={() => void loadWorkbench()}>重新检查</Button>
                ) : null}
                <Button type="primary" onClick={() => navigate("/works")}>
                  打开作品
                </Button>
                <Button
                  onClick={() =>
                    navigate(
                      state?.source_chat_id
                        ? `/chat/${encodeURIComponent(state.source_chat_id)}`
                        : "/chat",
                    )
                  }
                >
                  返回原会话
                </Button>
              </div>
            </>
          )}
        </div>
      )}
    </section>
  );
}

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { presentationApi } from "../../api/modules/presentation";
import type {
  PresentationArtifacts,
  PresentationJobEvent,
  PresentationJobRecord,
} from "../../api/modules/presentation";
import styles from "./index.module.less";

const CONVERSATION_KEY = "lantai:presentation-workbench:conversation";
const ACTIVE_JOB_KEY = "lantai:presentation-workbench:active-job";

function stableConversationId() {
  const stored = window.localStorage.getItem(CONVERSATION_KEY);
  if (stored) return stored;
  const id = `presentation-${globalThis.crypto?.randomUUID?.() || Date.now()}`;
  window.localStorage.setItem(CONVERSATION_KEY, id);
  return id;
}

const stageLabels: Record<string, string> = {
  created: "准备任务",
  brief: "理解目标",
  research: "整理材料",
  plan: "设计叙事与大纲",
  deck_ir: "构建可编辑页面",
  compose: "合成演示文稿",
  render: "逐页渲染",
  quality: "检查排版和内容",
  repair: "修复页面",
  completed: "已完成",
  paused: "已暂停",
  cancelled: "已取消",
  failed: "需要处理",
};

const statusLabels: Record<string, string> = {
  pending: "等待开始",
  queued: "等待开始",
  running: "任务在后台持续运行，可安全切换页面",
  paused: "任务已暂停，可恢复",
  completed: "演示文稿已完成",
  cancelled: "任务已取消",
  failed: "任务未完成，请展开任务详情查看原因",
};

export default function PresentationWorkbench() {
  const conversationId = useMemo(stableConversationId, []);
  const [requestText, setRequestText] = useState("");
  const [audience, setAudience] = useState("通用商务受众");
  const [slideCount, setSlideCount] = useState(10);
  const [themeId, setThemeId] = useState("lantai-ink");
  const [job, setJob] = useState<PresentationJobRecord | null>(null);
  const [events, setEvents] = useState<PresentationJobEvent[]>([]);
  const [artifacts, setArtifacts] = useState<PresentationArtifacts | null>(
    null,
  );
  const [history, setHistory] = useState<PresentationJobRecord[]>([]);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [editingSlideId, setEditingSlideId] = useState<string | null>(null);
  const [editInstructions, setEditInstructions] = useState<
    Record<string, string>
  >({});
  const lastSequence = useRef(0);

  const refresh = useCallback(
    async (jobId?: string) => {
      const id = jobId || job?.job_id;
      if (!id) return;
      try {
        const [next, nextEvents, nextArtifacts] = await Promise.all([
          presentationApi.get(id, conversationId),
          presentationApi.events(id, conversationId, lastSequence.current),
          presentationApi.artifacts(id, conversationId),
        ]);
        setJob(next);
        if (nextEvents.length) {
          setEvents((current) =>
            [...current, ...nextEvents].filter(
              (event, index, all) =>
                all.findIndex((item) => item.sequence === event.sequence) ===
                index,
            ),
          );
          lastSequence.current = Math.max(
            lastSequence.current,
            ...nextEvents.map((item) => item.sequence),
          );
        }
        setArtifacts(nextArtifacts);
      } catch (cause) {
        setError(cause instanceof Error ? cause.message : "无法读取任务状态");
      }
    },
    [conversationId, job?.job_id],
  );

  useEffect(() => {
    presentationApi
      .list(conversationId)
      .then((items) => {
        setHistory(items);
        const activeId = window.localStorage.getItem(ACTIVE_JOB_KEY);
        const selected =
          items.find((item) => item.job_id === activeId) || items[0];
        if (selected) {
          setJob(selected);
          void refresh(selected.job_id);
        }
      })
      .catch(() => undefined);
  }, [conversationId]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    if (!job || ["completed", "cancelled"].includes(job.status)) return;
    const timer = window.setInterval(() => void refresh(), 2000);
    return () => window.clearInterval(timer);
  }, [job?.job_id, job?.status, refresh]);

  const create = async () => {
    if (!requestText.trim()) return;
    setBusy(true);
    setError("");
    try {
      const created = await presentationApi.create({
        conversation_id: conversationId,
        user_request: requestText.trim(),
        audience,
        slide_count: slideCount,
        language: "zh-CN",
        mode: "stable",
        theme_id: themeId,
        materials: [],
      });
      window.localStorage.setItem(ACTIVE_JOB_KEY, created.job_id);
      lastSequence.current = 0;
      setEvents([]);
      setArtifacts(null);
      setJob(created);
      setHistory((items) => [
        created,
        ...items.filter((item) => item.job_id !== created.job_id),
      ]);
      await refresh(created.job_id);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "创建任务失败");
    } finally {
      setBusy(false);
    }
  };

  const action = async (name: "pause" | "resume" | "cancel") => {
    if (!job) return;
    await presentationApi.action(job.job_id, conversationId, name);
    await refresh(job.job_id);
  };

  const download = async () => {
    if (!job) return;
    const blob = await presentationApi.outputBlob(job.job_id, conversationId);
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = `${job.request.user_request.slice(0, 36) || "澜台演示文稿"}.pptx`;
    link.click();
    URL.revokeObjectURL(url);
  };

  const slides = artifacts?.deck_ir?.slides || [];
  const running = Boolean(job && ["pending", "running"].includes(job.status));
  const baseOutputSha256 = artifacts?.quality?.output_sha256 || "";

  const applySlideEdit = async (slideId: string) => {
    if (!job) return;
    const instruction = (editInstructions[slideId] || "").trim();
    if (!instruction || !baseOutputSha256) return;
    setEditingSlideId(slideId);
    setError("");
    try {
      await presentationApi.redoSlide(
        job.job_id,
        conversationId,
        slideId,
        instruction,
        baseOutputSha256,
      );
      setEditInstructions((current) => ({ ...current, [slideId]: "" }));
      await refresh(job.job_id);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "单页修改未通过验证");
      await refresh(job.job_id);
    } finally {
      setEditingSlideId(null);
    }
  };

  return (
    <main className={styles.page}>
      <header className={styles.hero}>
        <div>
          <span className={styles.eyebrow}>PRESENTATION STUDIO</span>
          <h1>演示文稿</h1>
          <p>
            从叙事到原生可编辑 PPTX。生成任务与 Office
            编辑互相独立，完成后再进入本地编辑器。
          </p>
        </div>
        <span className={styles.mode}>稳定模式</span>
      </header>

      <section className={styles.grid}>
        <div className={styles.composer}>
          <label>你想讲清楚什么</label>
          <textarea
            value={requestText}
            onChange={(event) => setRequestText(event.target.value)}
            placeholder="例如：为产品团队生成一份 10 页的季度复盘，突出增长来源、问题和下一步动作。"
          />
          <div className={styles.fields}>
            <label>
              受众
              <input
                value={audience}
                onChange={(event) => setAudience(event.target.value)}
              />
            </label>
            <label>
              页数
              <input
                type="number"
                min={6}
                max={15}
                value={slideCount}
                onChange={(event) =>
                  setSlideCount(
                    Math.min(15, Math.max(6, Number(event.target.value))),
                  )
                }
              />
            </label>
            <label>
              视觉系统
              <select
                value={themeId}
                onChange={(event) => setThemeId(event.target.value)}
              >
                <option value="lantai-ink">澜台·墨</option>
                <option value="lantai-slate">澜台·岩</option>
                <option value="lantai-paper">澜台·纸</option>
              </select>
            </label>
          </div>
          <button
            className={styles.primary}
            disabled={busy || !requestText.trim()}
            onClick={create}
          >
            {busy ? "正在创建…" : "生成 PPT"}
          </button>
          {error && <p className={styles.error}>{error}</p>}
        </div>

        <aside className={styles.history}>
          <h2>最近任务</h2>
          {history.length === 0 && <p className={styles.muted}>尚无生成记录</p>}
          {history.slice(0, 8).map((item) => (
            <button
              key={item.job_id}
              className={item.job_id === job?.job_id ? styles.activeJob : ""}
              onClick={() => {
                setJob(item);
                setEvents([]);
                lastSequence.current = 0;
                window.localStorage.setItem(ACTIVE_JOB_KEY, item.job_id);
                void refresh(item.job_id);
              }}
            >
              <strong>{item.request.user_request}</strong>
              <span>
                {stageLabels[item.stage] || "处理中"} ·{" "}
                {item.request.slide_count} 页
              </span>
            </button>
          ))}
        </aside>
      </section>

      {job && (
        <section className={styles.jobPanel}>
          <div className={styles.jobHeader}>
            <div>
              <span
                className={styles.statusDot}
                data-running={running || undefined}
              />
              <strong>{stageLabels[job.stage] || "处理中"}</strong>
              <small>{statusLabels[job.status] || "正在更新任务状态"}</small>
            </div>
            <div className={styles.actions}>
              {running && <button onClick={() => action("pause")}>暂停</button>}
              {job.status === "paused" && (
                <button onClick={() => action("resume")}>恢复</button>
              )}
              {!["completed", "cancelled"].includes(job.status) && (
                <button onClick={() => action("cancel")}>取消</button>
              )}
              {job.status === "completed" && (
                <button className={styles.primary} onClick={download}>
                  下载 PPTX
                </button>
              )}
            </div>
          </div>
          <details className={styles.taskDetails}>
            <summary>任务详情</summary>
            <div className={styles.timeline}>
              {events.map((event) => (
                <div key={event.sequence}>
                  <b>{event.sequence}</b>
                  <span>{event.message}</span>
                  <time>{stageLabels[event.stage] || event.stage}</time>
                </div>
              ))}
            </div>
          </details>
          {slides.length > 0 && (
            <div className={styles.slides}>
              {slides.map((slide, index) => (
                <article key={slide.id}>
                  <span>{String(index + 1).padStart(2, "0")}</span>
                  <h3>
                    {slide.title ||
                      slide.objects?.find(
                        (item) => item.kind === "text" && item.slot === "title",
                      )?.text ||
                      `第 ${index + 1} 页`}
                  </h3>
                  <textarea
                    aria-label={`第 ${index + 1} 页修改要求`}
                    placeholder="例如：标题改为……；或把“原文”改为“新文”"
                    value={editInstructions[slide.id] || ""}
                    onChange={(event) =>
                      setEditInstructions((current) => ({
                        ...current,
                        [slide.id]: event.target.value,
                      }))
                    }
                  />
                  <button
                    disabled={
                      running ||
                      job.status !== "completed" ||
                      !baseOutputSha256 ||
                      !(editInstructions[slide.id] || "").trim() ||
                      editingSlideId !== null
                    }
                    onClick={() => void applySlideEdit(slide.id)}
                  >
                    {editingSlideId === slide.id
                      ? "正在渲染验证…"
                      : "应用并验证"}
                  </button>
                </article>
              ))}
            </div>
          )}
        </section>
      )}
    </main>
  );
}

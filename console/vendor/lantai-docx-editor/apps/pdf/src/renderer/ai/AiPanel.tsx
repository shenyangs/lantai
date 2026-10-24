import { useEffect, useRef, useState } from "react";
import type { PointerEvent as ReactPointerEvent, ReactElement } from "react";
import { AgentLoop, type AgentRunReceipt } from "@genoffice/agent-core";
import type { AiSettings } from "@genoffice/ai-provider";
import {
  AiComposer,
  AiTypingIndicator,
  OfficeAgentAvatar,
  OfficeAgentCapabilities,
  OfficeCoCreationModeBar,
  OfficeAgentModelBinding,
  OfficeAgentRunReceipt,
  buildOfficeCoCreationInstruction,
  OfficeSuggestionCard,
  type OfficeCoCreationMode,
  type OfficeCoCreationResponseKind,
  parseOfficeCoCreationResponse,
} from "@genoffice/ui";
import { aiLangDirective, t as tGlobal, useI18n } from "../i18n/locale";
import { Markdown } from "@genoffice/ui";
import sendEnterOn from "../assets/send-enter-on.png";
import sendEnterOff from "../assets/send-enter-off.png";
import sendStop from "../assets/send-stop.png";
import { createPdfSkill } from "./pdf-skill";
import { createElectronTransport } from "./transport";
import type { PdfAiDeps } from "./tools";

const PANEL_WIDTH_KEY = "pdf-ai-panel-width";
const PANEL_WIDTH_DEFAULT = 360;
const PANEL_WIDTH_MIN = 280;

function clampPanelWidth(w: number): number {
  // Electron can briefly report a tiny viewport while the BrowserWindow is
  // being attached. Treat that as an uninitialised measurement; otherwise a
  // 360px panel can be persisted in-memory as a 34px sliver before first paint.
  const viewportMax =
    window.innerWidth >= PANEL_WIDTH_MIN
      ? Math.max(
          PANEL_WIDTH_MIN,
          Math.min(720, Math.round(window.innerWidth * 0.6)),
        )
      : 720;
  return Math.min(Math.max(w, PANEL_WIDTH_MIN), viewportMax);
}

function loadPanelWidth(): number {
  const saved = Number(localStorage.getItem(PANEL_WIDTH_KEY));
  return Number.isFinite(saved) && saved > 0
    ? clampPanelWidth(saved)
    : PANEL_WIDTH_DEFAULT;
}

interface ToolActivity {
  name: string;
  summary: string;
  isError?: boolean;
  output?: string;
}

interface ChatEntry {
  role: "user" | "assistant";
  text: string;
  /** The behavioural contract used for this assistant turn. */
  mode?: OfficeCoCreationMode;
  responseKind?: OfficeCoCreationResponseKind;
  streaming?: boolean;
  isError?: boolean;
  /** the run failed and this user message was rolled back out of the model context */
  undelivered?: boolean;
  tools?: ToolActivity[];
  /** normalized, factual receipt for the complete agent run */
  receipt?: AgentRunReceipt;
}

type Phase = "thinking" | "replying" | "working";

export function AiPanel({
  api,
  hasDocument,
  onBusyChange,
  onCollapse,
  preset,
}: {
  api: PdfAiDeps;
  hasDocument: boolean;
  onBusyChange?: ((busy: boolean) => void) | undefined;
  onCollapse: () => void;
  /** Ribbon AI buttons push a one-shot prompt; a new nonce triggers an auto-run */
  preset?: { text: string; nonce: number } | null;
}): ReactElement {
  const { lang, t } = useI18n();
  const [chat, setChat] = useState<ChatEntry[]>([]);
  const [prompt, setPrompt] = useState("");
  const [busy, setBusy] = useState(false);
  const [coCreationMode, setCoCreationMode] =
    useState<OfficeCoCreationMode>("suggest");
  const lastModeRef = useRef<OfficeCoCreationMode>("suggest");
  useEffect(() => {
    onBusyChange?.(busy);
    return () => onBusyChange?.(false);
  }, [busy, onBusyChange]);
  const [phase, setPhase] = useState<Phase>("thinking");
  const chatRef = useRef<HTMLDivElement>(null);
  const stickToBottomRef = useRef(true);
  const [panelWidth, setPanelWidth] = useState(loadPanelWidth);
  const [resizing, setResizing] = useState(false);
  const asideRef = useRef<HTMLElement>(null);

  // The .ai-dock wrapper owns the animated width (docs-style 180ms slide);
  // it tracks the resizable panel width through this variable
  useEffect(() => {
    const dock = asideRef.current?.closest(".ai-dock") as HTMLElement | null;
    dock?.style.setProperty("--ai-panel-width", `${panelWidth}px`);
  }, [panelWidth]);
  const settingsRef = useRef<AiSettings | null>(null);
  const [modelSettings, setModelSettings] = useState<AiSettings | null>(null);
  const langRef = useRef(lang);
  langRef.current = lang;
  const apiRef = useRef(api);
  apiRef.current = api;

  // Fetch once on panel mount so the user sees the editor-window binding
  // before sending a prompt. `send` refreshes it again immediately before a
  // run to preserve the existing safety behaviour.
  useEffect(() => {
    void window.pdfApi
      .getAiSettings()
      .then((settings) => {
        settingsRef.current = settings;
        setModelSettings(settings);
      })
      .catch(() => undefined);
  }, []);

  const patchLast = (
    patch: Partial<ChatEntry> | ((last: ChatEntry) => Partial<ChatEntry>),
  ) => {
    setChat((prev) => {
      const next = [...prev];
      const last = next[next.length - 1];
      if (!last || last.role !== "assistant") return prev;
      next[next.length - 1] = {
        ...last,
        ...(typeof patch === "function" ? patch(last) : patch),
      };
      return next;
    });
  };

  // The loop is built once; every mutable value goes through a ref getter
  const loopRef = useRef<AgentLoop | null>(null);
  if (!loopRef.current) {
    const deps: PdfAiDeps = {
      doc: () => apiRef.current.doc(),
      fileName: () => apiRef.current.fileName(),
      pageCount: () => apiRef.current.pageCount(),
      currentPage: () => apiRef.current.currentPage(),
      readOnly: () => apiRef.current.readOnly(),
      outline: () => apiRef.current.outline(),
      searchIndex: () => apiRef.current.searchIndex(),
      isDeleted: (i) => apiRef.current.isDeleted(i),
      gotoPage: (p) => apiRef.current.gotoPage(p),
      addMarkup: (type, idx, rects) =>
        apiRef.current.addMarkup(type, idx, rects),
      editText: (input) => apiRef.current.editText(input),
      editFonts: () => apiRef.current.editFonts(),
      formEdits: () => apiRef.current.formEdits(),
      applyFormEdit: (v) => apiRef.current.applyFormEdit(v),
      rotatePage: (idx, dir) => apiRef.current.rotatePage(idx, dir),
      deletePage: (idx) => apiRef.current.deletePage(idx),
      pageGeom: (idx) => apiRef.current.pageGeom(idx),
      listImages: () => apiRef.current.listImages(),
      isImageClaimed: (ref) => apiRef.current.isImageClaimed(ref),
      insertImage: (idx, png, rect, layer) =>
        apiRef.current.insertImage(idx, png, rect, layer),
      transformImage: (ref, rect, layer, quarterTurns) =>
        apiRef.current.transformImage(ref, rect, layer, quarterTurns),
      replaceImage: (ref, png) => apiRef.current.replaceImage(ref, png),
      deleteImage: (ref) => apiRef.current.deleteImage(ref),
      searchImages: (query, max) => apiRef.current.searchImages(query, max),
      generateImage: (op) => apiRef.current.generateImage(op),
      fetchImage: (url) => apiRef.current.fetchImage(url),
      visiblePageOrder: () => apiRef.current.visiblePageOrder(),
      reorderPages: (pages) => apiRef.current.reorderPages(pages),
      extractPages: (pages) => apiRef.current.extractPages(pages),
      insertPdf: (afterPage) => apiRef.current.insertPdf(afterPage),
      ocrPages: (pages, languages) => apiRef.current.ocrPages(pages, languages),
      secureRedact: (targets) => apiRef.current.secureRedact(targets),
    };
    loopRef.current = new AgentLoop({
      transport: createElectronTransport(() => settingsRef.current!),
      skill: createPdfSkill(deps),
      systemSuffix: () => aiLangDirective(langRef.current),
      events: {
        onText: (text) => {
          setPhase("replying");
          patchLast({ text });
        },
        onToolExecuted: ({ call, execution }) => {
          setPhase("working");
          patchLast((last) => ({
            tools: [
              ...(last.tools ?? []),
              {
                name: call.name,
                summary: execution.summary,
                isError: execution.isError,
                output: execution.output?.slice(0, 2000),
              },
            ],
          }));
        },
        onTurnEnd: () => {
          setPhase("thinking");
          patchLast({ streaming: false });
          setChat((prev) => [
            ...prev,
            {
              role: "assistant",
              text: "",
              streaming: true,
              mode: lastModeRef.current,
            },
          ]);
        },
        onReceipt: (receipt) => patchLast({ receipt }),
        onDone: ({ text, cancelled, turnLimit }) => {
          const final = turnLimit
            ? [text, tGlobal("aiTurnLimit")].filter(Boolean).join("\n\n")
            : text || (cancelled ? tGlobal("aiStopped") : "");
          const parsed = parseOfficeCoCreationResponse(
            final,
            lastModeRef.current,
          );
          patchLast((last) => ({
            streaming: false,
            responseKind: parsed.responseKind,
            text:
              parsed.text ||
              (last.tools?.length ? last.text : tGlobal("aiNoReply")),
          }));
          setBusy(false);
        },
        onError: (error) => {
          setChat((prev) => {
            const next = [...prev];
            // the loop rolled this run's user message out of the model context — surface that
            for (let i = next.length - 1; i >= 0; i--) {
              const entry = next[i]!;
              if (entry.role === "user") {
                next[i] = { ...entry, undelivered: true };
                break;
              }
            }
            const last = next.at(-1);
            if (last?.role === "assistant") {
              next[next.length - 1] = {
                ...last,
                streaming: false,
                text: error,
                isError: true,
              };
            }
            return next;
          });
          setBusy(false);
        },
      },
    });
  }

  useEffect(() => {
    if (stickToBottomRef.current) {
      chatRef.current?.scrollTo({ top: chatRef.current.scrollHeight });
    }
  }, [chat, busy]);

  const onChatScroll = (): void => {
    const el = chatRef.current;
    if (!el) return;
    stickToBottomRef.current =
      el.scrollHeight - el.scrollTop - el.clientHeight < 48;
  };

  const send = (text: string, modeOverride?: OfficeCoCreationMode): void => {
    const instruction = text.trim();
    const loop = loopRef.current;
    if (!instruction || !loop || loop.busy) return;
    const effectiveMode = modeOverride ?? coCreationMode;
    lastModeRef.current = effectiveMode;
    stickToBottomRef.current = true;
    setChat((prev) => [
      ...prev,
      { role: "user", text: instruction },
      { role: "assistant", text: "", streaming: true, mode: effectiveMode },
    ]);
    setPrompt("");
    setBusy(true);
    setPhase("thinking");
    void (async () => {
      try {
        settingsRef.current = await window.pdfApi.getAiSettings();
        setModelSettings(settingsRef.current);
        await loop.run(
          buildOfficeCoCreationInstruction(instruction, effectiveMode, "pdf"),
        );
      } catch (err) {
        patchLast({
          streaming: false,
          text: err instanceof Error ? err.message : String(err),
          isError: true,
        });
        setBusy(false);
      }
    })();
  };

  const stop = (): void => loopRef.current?.cancel();

  // One-click AI actions from the ribbon (same pattern as the docs ribbon presets)
  useEffect(() => {
    if (preset) send(preset.text, "execute");
    // eslint-disable-next-line react-hooks/exhaustive-deps -- run once per nonce
  }, [preset?.nonce]);

  // Re-clamp the persisted width when the window shrinks (max is 60% of the window)
  useEffect(() => {
    const onResize = (): void => setPanelWidth((w) => clampPanelWidth(w));
    window.addEventListener("resize", onResize);
    return () => window.removeEventListener("resize", onResize);
  }, []);

  const resizeCleanupRef = useRef<(() => void) | null>(null);
  useEffect(() => () => resizeCleanupRef.current?.(), []);

  /** Drag the left edge to resize the right-docked co-creation panel. */
  const startResize = (e: ReactPointerEvent<HTMLDivElement>): void => {
    e.preventDefault();
    const resizer = e.currentTarget;
    setResizing(true);
    document.body.style.cursor = "col-resize";
    document.body.style.userSelect = "none";
    const onMove = (ev: PointerEvent): void => {
      setPanelWidth(clampPanelWidth(window.innerWidth - ev.clientX));
    };
    let done = false;
    const cleanup = (): void => {
      if (done) return;
      done = true;
      resizeCleanupRef.current = null;
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", cleanup);
      window.removeEventListener("pointercancel", cleanup);
      resizer.removeEventListener("lostpointercapture", cleanup);
      document.body.style.cursor = "";
      document.body.style.userSelect = "";
      setResizing(false);
      setPanelWidth((w) => {
        localStorage.setItem(PANEL_WIDTH_KEY, String(Math.round(w)));
        return w;
      });
    };
    resizeCleanupRef.current = cleanup;
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", cleanup);
    window.addEventListener("pointercancel", cleanup);
    // lostpointercapture also fires if the resizer is unmounted mid-drag (panel collapse)
    resizer.addEventListener("lostpointercapture", cleanup);
    resizer.setPointerCapture(e.pointerId);
  };

  const typingLabel =
    phase === "replying"
      ? t("aiReplying")
      : phase === "working"
        ? t("aiWorking")
        : t("aiThinking");

  return (
    <aside
      ref={asideRef}
      data-co-creation-surface="office-pdf"
      className={`copilot${resizing ? " ai-panel-resizing" : ""}`}
      style={{ width: "100%" }}
    >
      <div
        className="ai-panel-resizer"
        onPointerDown={startResize}
        role="separator"
        aria-orientation="vertical"
        aria-label="Lantai"
      />
      <header className="ai-panel-header">
        <span className="ai-panel-title">
          <OfficeAgentAvatar size={22} />
          <span data-office-agent-persona="沈行知">沈行知</span>
          <OfficeAgentModelBinding
            settings={modelSettings}
            onSelect={async (binding) => {
              const current =
                modelSettings ?? (await window.pdfApi.getAiSettings());
              const next = {
                ...current,
                lantaiModelBinding: binding,
              } as AiSettings;
              await window.pdfApi.setAiSettings(next);
              settingsRef.current = next;
              setModelSettings(next);
            }}
          />
        </span>
        <div className="ai-panel-header-actions">
          {chat.length > 0 && (
            <button
              className="ai-header-btn"
              onClick={() => {
                stop();
                loopRef.current?.reset();
                setBusy(false);
                setChat([]);
              }}
              title={t("aiNewChat")}
            >
              <IconNewChat />
            </button>
          )}
          <button
            className="ai-header-btn"
            onClick={onCollapse}
            title={t("aiCollapsePanel")}
          >
            <IconCollapse />
          </button>
        </div>
      </header>

      <OfficeCoCreationModeBar
        mode={coCreationMode}
        suite="pdf"
        onChange={setCoCreationMode}
        disabled={busy}
      />

      <div className="ai-chat" ref={chatRef} onScroll={onChatScroll}>
        {chat.length === 0 && (
          <div className="ai-chat-empty">
            <div className="ai-chat-empty-title">{t("aiEmptyTitle")}</div>
            <div className="ai-chat-empty-body">{t("aiEmptyBody")}</div>
            {hasDocument && (
              <OfficeAgentCapabilities
                actions={[
                  {
                    id: "pdf-summary",
                    label: t("aiQuickSummary"),
                    title: t("aiQuickSummaryPrompt"),
                    evidence: "pdf:read_pages+ocr_pages",
                    disabled: busy,
                    onInvoke: () => send(t("aiQuickSummaryPrompt"), "ask"),
                  },
                  {
                    id: "pdf-key-points",
                    label: t("aiQuickKeyPoints"),
                    title: t("aiQuickKeyPointsPrompt"),
                    evidence: "pdf:read_pages+ocr_pages",
                    disabled: busy,
                    onInvoke: () => send(t("aiQuickKeyPointsPrompt"), "ask"),
                  },
                ]}
              />
            )}
          </div>
        )}
        {chat.map((entry, i) => {
          if (entry.role === "user") {
            return (
              <div key={i} className="ai-msg ai-msg-user">
                {entry.text}
                {entry.undelivered && (
                  <div className="ai-msg-undelivered">
                    {t("aiUndelivered")}
                    {!busy && (
                      <button
                        className="ai-retry-btn"
                        onClick={() => send(entry.text)}
                      >
                        {t("aiRetry")}
                      </button>
                    )}
                  </div>
                )}
              </div>
            );
          }
          const hasTools = (entry.tools?.length ?? 0) > 0;
          if (!entry.text && !hasTools && !entry.receipt) return null;
          return (
            <div
              key={i}
              className={`ai-msg ai-msg-assistant${entry.isError ? " ai-msg-error" : ""}`}
            >
              {hasTools && <ToolChipList tools={entry.tools!} />}
              {entry.text &&
                (entry.responseKind === "proposal" &&
                !entry.streaming &&
                !entry.isError ? (
                  <OfficeSuggestionCard
                    id={`pdf-suggestion-${i}`}
                    suite="pdf"
                    scopeLabel="当前建议明确指定的页面或批注"
                    disabled={busy}
                    onRefine={() =>
                      setPrompt(
                        `请继续完善这条建议，先不要修改 PDF：\n\n${entry.text}`,
                      )
                    }
                    onAccept={() =>
                      send(
                        `请在副本上执行这条已经确认的建议，只处理建议对应的页面或批注：\n\n${entry.text}`,
                        "execute",
                      )
                    }
                  >
                    <Markdown text={entry.text} />
                  </OfficeSuggestionCard>
                ) : (
                  <Markdown text={entry.text} />
                ))}
              {entry.receipt && (
                <OfficeAgentRunReceipt
                  receipt={entry.receipt}
                  rollbackAvailable
                />
              )}
            </div>
          );
        })}
        {/* In-progress state: a standalone three-dot row at the end of the stream, kept until done */}
        {busy && <AiTypingIndicator label={typingLabel} />}
      </div>

      <div className="ai-composer">
        <AiComposer
          value={prompt}
          busy={busy}
          placeholder={t("aiComposerPlaceholder")}
          hintIdle={t("aiHintIdle")}
          hintBusy={t("aiHintBusy")}
          sendLabel={t("aiSend")}
          stopLabel={t("aiStop")}
          iconOnly
          sendIconEnabled={<img src={sendEnterOn} alt="" aria-hidden />}
          sendIconDisabled={<img src={sendEnterOff} alt="" aria-hidden />}
          stopIcon={<img src={sendStop} alt="" aria-hidden />}
          onChange={setPrompt}
          onSend={() => send(prompt)}
          onStop={stop}
        />
      </div>
    </aside>
  );
}

/** Tool row list (unified with docs/slides/sheets): dot + summary, expandable details when there's output */
/** Step-row status icons (timeline glyphs: 14px in a 20px slot, 1.6 stroke) */
function StepIcon({ status }: { status: "running" | "done" | "error" }) {
  if (status === "running") {
    return (
      <svg
        viewBox="0 0 24 24"
        width="14"
        height="14"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.6"
        strokeLinecap="round"
        strokeLinejoin="round"
        aria-hidden
      >
        <path d="M6.5 3.5h11M6.5 20.5h11M8 3.5v3.2c0 2.6 4 4.2 4 5.3 0 1.1 4 2.7 4 5.3v3.2M16 3.5v3.2c0 2.6-4 4.2-4 5.3 0 1.1-4 2.7-4 5.3v3.2" />
      </svg>
    );
  }
  if (status === "error") {
    return (
      <svg
        viewBox="0 0 24 24"
        width="14"
        height="14"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.6"
        strokeLinecap="round"
        strokeLinejoin="round"
        aria-hidden
      >
        <circle cx="12" cy="12" r="9" />
        <path d="m9.2 9.2 5.6 5.6M14.8 9.2l-5.6 5.6" />
      </svg>
    );
  }
  return (
    <svg
      viewBox="0 0 24 24"
      width="14"
      height="14"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.6"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden
    >
      <circle cx="12" cy="12" r="9" />
      <path d="m8.5 12.4 2.4 2.4 4.6-5" />
    </svg>
  );
}

/** Tool activity group: a single quiet summary row
 *  that auto-opens while tools run, auto-collapses into "Worked · N steps" when they finish,
 *  and a manual toggle that always wins. Rows inside are step rows with 1px connectors. */
function ToolChipList({ tools }: { tools: ToolActivity[] }) {
  const { t: tr } = useI18n();
  const [expanded, setExpanded] = useState<Set<number>>(new Set());
  const [userOpen, setUserOpen] = useState<boolean | null>(null);

  const toggle = (j: number) => {
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(j)) next.delete(j);
      else next.add(j);
      return next;
    });
  };

  const open = userOpen ?? false;
  const label = tr("aiWorkedSteps", { n: tools.length });

  return (
    <div className="ai-work-group">
      <button
        type="button"
        className={`ai-work-group-summary`}
        aria-expanded={open}
        onClick={() => setUserOpen(!open)}
      >
        <span className="ai-work-group-label">{label}</span>
        <span
          className={`ai-tool-chip-caret${open ? " open" : ""}`}
          aria-hidden
        >
          ›
        </span>
      </button>
      <div className={`ai-work-group-body${open ? " open" : ""}`}>
        <div className="ai-work-group-body-inner">
          {tools.map((tool, j) => {
            const hasOutput = !!tool.output;
            const isOpen = expanded.has(j);
            const stepStatus = tool.isError ? "error" : "done";
            return (
              <div key={j} className="ai-step-row">
                <span className={`ai-step-icon ${stepStatus}`} aria-hidden>
                  <StepIcon status={stepStatus} />
                </span>
                <div className="ai-step-content">
                  {hasOutput ? (
                    <button
                      type="button"
                      className="ai-step-title clickable"
                      title={tool.name}
                      aria-expanded={isOpen}
                      onClick={() => toggle(j)}
                    >
                      {tool.summary}
                    </button>
                  ) : (
                    <span className="ai-step-title" title={tool.name}>
                      {tool.summary}
                    </span>
                  )}
                  {hasOutput && isOpen && (
                    <div className="ai-step-detail">
                      <div className="ai-tool-output">
                        <div className="ai-tool-output-pre">{tool.output}</div>
                      </div>
                    </div>
                  )}
                </div>
              </div>
            );
          })}
        </div>
      </div>
    </div>
  );
}

function Svg({ children }: { children: React.ReactNode }): ReactElement {
  return (
    <svg
      width="15"
      height="15"
      viewBox="0 0 16 16"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.2"
      strokeLinecap="round"
      aria-hidden
    >
      {children}
    </svg>
  );
}

function IconNewChat(): ReactElement {
  return (
    <Svg>
      <path
        d="M13.5 7.2v-3A1.7 1.7 0 0 0 11.8 2.5H4.2a1.7 1.7 0 0 0-1.7 1.7v6.1a1.7 1.7 0 0 0 1.7 1.7h1.1v2l2.6-2h1.3"
        strokeLinejoin="round"
      />
      <path d="M12.2 9.4v4M10.2 11.4h4" />
    </Svg>
  );
}

/* Same glyph as the sheets IconCollapse (16×16 viewBox, 1.2/1.3 stroke), rendered at 15px */
function IconCollapse(): ReactElement {
  return (
    <svg
      width={15}
      height={15}
      viewBox="0 0 16 16"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.2"
      strokeLinecap="round"
      aria-hidden
    >
      {/* Mirrored: the AI panel docks on the LEFT, so the divider and arrow point left */}
      <rect x="1.5" y="2.5" width="13" height="11" rx="1" />
      <path d="M5.5 2.5v11" />
      <path
        d="M12.5 8H8.1M9.8 5.9 7.7 8l2.1 2.1"
        strokeWidth="1.3"
        strokeLinejoin="round"
      />
    </svg>
  );
}

/** Calm Lantai co-creation mark: a soft horizon and one guiding star. */
export function LantaiMark({
  size = 18,
}: {
  size?: number;
}): React.JSX.Element {
  return (
    <svg
      data-lantai-mark="co-creation"
      width={size}
      height={size}
      viewBox="0 0 32 32"
      fill="none"
      aria-hidden
    >
      <rect
        x="1.5"
        y="1.5"
        width="29"
        height="29"
        rx="10"
        fill="#F4F7FB"
        stroke="#D8E1EC"
      />
      <path
        d="M6.5 19.1c2.5-3.15 4.65 3.15 7.2 0 2.52-3.13 4.67 3.13 7.2 0 1.34-1.66 2.55-.62 4.6.55"
        stroke="#526B89"
        strokeWidth="2"
        strokeLinecap="round"
      />
      <path
        d="m22.1 7.1.72 1.9 1.9.72-1.9.72-.72 1.9-.72-1.9-1.9-.72 1.9-.72.72-1.9Z"
        fill="#7B8FC9"
      />
    </svg>
  );
}

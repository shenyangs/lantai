const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const path = require("node:path");
const { app, BrowserWindow, session } = require("electron");

function parseArgs(argv) {
  const args = {};
  for (let index = 2; index < argv.length; index += 1) {
    const value = argv[index];
    if (!value.startsWith("--")) continue;
    const key = value.slice(2);
    const next = argv[index + 1];
    if (!next || next.startsWith("--")) {
      args[key] = "1";
      continue;
    }
    args[key] = next;
    index += 1;
  }
  return args;
}

function normalizeBaseUrl(value) {
  return String(value || "").replace(/\/$/, "");
}

function createWindow() {
  return new BrowserWindow({
    width: 960,
    height: 720,
    show: false,
    title: "Configured Provider Stream Smoke",
    webPreferences: {
      sandbox: false,
      contextIsolation: true,
      nodeIntegration: false,
    },
  });
}

async function observeWithScreenshots(win, options, screenshotDir) {
  const pending = observeConfiguredProviderTurn(win, options);
  if (screenshotDir) {
    await fs.mkdir(screenshotDir, { recursive: true });
  }
  const captured = new Set();
  let settled = false;
  let result;
  let failure;
  void pending.then(
    (value) => {
      result = value;
      settled = true;
    },
    (error) => {
      failure = error;
      settled = true;
    },
  );

  while (!settled) {
    if (screenshotDir) {
      try {
        const state = await win.webContents.executeJavaScript(
          "window.__lantaiConfiguredSmokePhase || null",
          true,
        );
        const phase = String(state?.phase || "");
        const turnNumber = Number(state?.turn_number || 0);
        const key = `${turnNumber}:${phase}`;
        if (
          turnNumber === options.turnNumber &&
          ["user", "thinking", "answer", "final"].includes(phase) &&
          !captured.has(key)
        ) {
          captured.add(key);
          const image = await win.webContents.capturePage();
          const filename = `turn-${turnNumber}-${phase}.png`;
          await fs.writeFile(path.join(screenshotDir, filename), image.toPNG());
        }
      } catch {
        // The underlying smoke owns failure reporting. Screenshot capture must
        // never turn a healthy chat run into a false product failure.
      }
    }
    await new Promise((resolve) => setTimeout(resolve, 80));
  }

  if (failure) {
    if (screenshotDir) {
      try {
        const image = await win.webContents.capturePage();
        await fs.writeFile(
          path.join(screenshotDir, `turn-${options.turnNumber}-failure.png`),
          image.toPNG(),
        );
      } catch {
        // Preserve the original product failure if diagnostic capture fails.
      }
    }
    throw failure;
  }
  const finalKey = `${options.turnNumber}:final`;
  if (screenshotDir && !captured.has(finalKey)) {
    const state = await win.webContents.executeJavaScript(
      "window.__lantaiConfiguredSmokePhase || null",
      true,
    );
    if (
      Number(state?.turn_number || 0) === options.turnNumber &&
      String(state?.phase || "") === "final"
    ) {
      const image = await win.webContents.capturePage();
      await fs.writeFile(
        path.join(screenshotDir, `turn-${options.turnNumber}-final.png`),
        image.toPNG(),
      );
      captured.add(finalKey);
    }
  }
  const runDrawer = await win.webContents.executeJavaScript(
    `
      (async () => {
        const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
        const triggers = Array.from(
          document.querySelectorAll('button[aria-label^="打开本轮执行"]'),
        );
        const trigger = triggers.at(-1);
        if (!trigger) {
          throw new Error("completed turn did not expose the run drawer trigger");
        }
        trigger.click();
        const deadline = Date.now() + 3000;
        let lastDiagnostic = null;
        while (Date.now() < deadline) {
          const drawer = document.querySelector(
            '[aria-label="本轮执行"], [aria-label$="的工作台"], [aria-label$="的本轮工作"]',
          );
          const wrapperSelector =
            ".lantai-drawer-content-wrapper, .ant-drawer-content-wrapper";
          const contentWrapper =
            drawer?.closest(wrapperSelector) ||
            drawer?.querySelector(wrapperSelector);
          const stages = drawer?.querySelector('[data-testid="execution-stages"]');
          const stageCount = stages?.children.length || 0;
          const bounds = contentWrapper?.getBoundingClientRect();
          const drawerBounds = drawer?.getBoundingClientRect();
          lastDiagnostic = {
            viewport_width: window.innerWidth,
            drawer: drawer
              ? {
                  tag: drawer.tagName,
                  class_name: String(drawer.className || ""),
                  left: Math.round(drawerBounds?.left || 0),
                  right: Math.round(drawerBounds?.right || 0),
                  width: Math.round(drawerBounds?.width || 0),
                  transform: getComputedStyle(drawer).transform,
                }
              : null,
            wrapper: contentWrapper
              ? {
                  tag: contentWrapper.tagName,
                  class_name: String(contentWrapper.className || ""),
                  left: Math.round(bounds?.left || 0),
                  right: Math.round(bounds?.right || 0),
                  width: Math.round(bounds?.width || 0),
                  transform: getComputedStyle(contentWrapper).transform,
                }
              : null,
            candidates: Array.from(
              document.querySelectorAll('[class*="drawer-content-wrapper"]'),
            ).map((node) => {
              const rect = node.getBoundingClientRect();
              return {
                class_name: String(node.className || ""),
                left: Math.round(rect.left),
                right: Math.round(rect.right),
                width: Math.round(rect.width),
                transform: getComputedStyle(node).transform,
              };
            }),
            stage_count: stageCount,
          };
          const fullyOpen =
            bounds &&
            bounds.width >= 380 &&
            bounds.left >= -1 &&
            bounds.right <= window.innerWidth + 1;
          if (drawer && stages && stageCount > 0 && fullyOpen) {
            return {
              visible: true,
              stage_count: stageCount,
              trigger_completed: String(
                trigger.getAttribute("aria-label") || "",
              ).match(/已完成|任务成功|回复结束/) !== null,
              geometry: {
                left: Math.round(bounds.left),
                right: Math.round(bounds.right),
                width: Math.round(bounds.width),
                viewport_width: window.innerWidth,
              },
            };
          }
          await sleep(50);
        }
        throw new Error(
          "run drawer did not render an execution stage: " +
            JSON.stringify(lastDiagnostic),
        );
      })()
    `,
    true,
  );
  if (!runDrawer?.visible || !runDrawer?.trigger_completed) {
    throw new Error(
      `run drawer was not healthy: ${JSON.stringify(runDrawer || null)}`,
    );
  }
  if (screenshotDir) {
    const image = await win.webContents.capturePage();
    await fs.writeFile(
      path.join(screenshotDir, `turn-${options.turnNumber}-run-drawer.png`),
      image.toPNG(),
    );
  }
  await win.webContents.executeJavaScript(
    `
      (() => {
        const close = document.querySelector(
          'button[aria-label="关闭本轮执行"], button[aria-label="关闭工作台"]',
        );
        close?.click();
        return Boolean(close);
      })()
    `,
    true,
  );
  result.run_drawer = runDrawer;
  return result;
}

async function enableVisibleThinking({ baseUrl, desktopToken }) {
  const response = await fetch(`${baseUrl}/api/settings/appearance`, {
    method: "PUT",
    headers: {
      Accept: "application/json",
      "Content-Type": "application/json",
      "X-Lantai-Desktop-Token": desktopToken,
    },
    body: JSON.stringify({ display_settings: { showThinking: true } }),
  });
  if (!response.ok) {
    throw new Error(
      `failed to enable visible thinking: HTTP ${response.status}`,
    );
  }
  const payload = await response.json();
  if (payload?.display_settings?.showThinking !== true) {
    throw new Error("visible thinking setting did not persist");
  }
}

async function safeRuntimeEventTrace({ baseUrl, desktopToken, chatId }) {
  try {
    const response = await fetch(
      `${baseUrl}/api/chats/${encodeURIComponent(chatId)}/events?after_seq=0`,
      {
        headers: { "X-Lantai-Desktop-Token": desktopToken },
      },
    );
    if (!response.ok) return { status: response.status, events: [] };
    const payload = await response.json();
    const slots = new Map();
    const slotFor = (prefix, value) => {
      const key = String(value || "").trim();
      if (!key) return "";
      const existing = slots.get(`${prefix}:${key}`);
      if (existing) return existing;
      const slot = `${prefix}${slots.size + 1}`;
      slots.set(`${prefix}:${key}`, slot);
      return slot;
    };
    const events = Array.isArray(payload?.events) ? payload.events : [];
    return {
      status: response.status,
      event_count: events.length,
      events: events.slice(-24).map((event) => {
        const eventPayload = event?.payload || {};
        const content = Array.isArray(eventPayload?.message?.content)
          ? eventPayload.message.content
          : [];
        return {
          seq: Number(event?.seq || 0),
          created_at: String(event?.created_at || ""),
          type: String(event?.type || ""),
          status: String(event?.status || ""),
          source: String(event?.source || ""),
          legacy_kind: String(eventPayload.legacy_kind || ""),
          turn: slotFor(
            "turn",
            event?.turn_id || eventPayload.turn_id || eventPayload.run_id,
          ),
          item: slotFor("item", event?.item_id || eventPayload.message_id),
          content_type: String(eventPayload.content_type || ""),
          stream_message_type: String(eventPayload.stream_message_type || ""),
          message_block_types: content.map((block) =>
            String(block?.type || ""),
          ),
        };
      }),
    };
  } catch (_error) {
    return { status: 0, events: [] };
  }
}

async function observeConfiguredProviderTurn(
  win,
  {
    baseUrl,
    chatId,
    promptText,
    answerSentinel,
    timeoutMs,
    turnNumber,
    artifactFilename = "",
    permissionPreset = "",
  },
) {
  if (turnNumber === 1) {
    await win.loadURL(`${baseUrl}/#/chat/${encodeURIComponent(chatId)}`);
  }
  return win.webContents.executeJavaScript(
    `
      (async () => {
        const chatId = ${JSON.stringify(chatId)};
        const promptText = ${JSON.stringify(promptText)};
        const answerSentinel = ${JSON.stringify(answerSentinel)};
        const timeoutMs = ${JSON.stringify(timeoutMs)};
        const turnNumber = ${JSON.stringify(turnNumber)};
        const artifactFilename = ${JSON.stringify(artifactFilename)};
        const permissionPreset = ${JSON.stringify(permissionPreset)};
        const startedAt = performance.now();
        const deadline = Date.now() + timeoutMs;
        window.__lantaiConfiguredSmokePhase = {
          turn_number: turnNumber,
          phase: "starting",
        };
        window.__lantaiChatStreamProbe = {
          startedAt,
          frames: [],
        };
        const consoleChatTransport = { responses: [], frames: [] };
        const transportIdentitySlots = new Map();
        const transportIdentitySlot = (kind, value) => {
          const normalized = String(value || "").trim();
          if (!normalized) return "";
          const key = kind + ":" + normalized;
          if (!transportIdentitySlots.has(key)) {
            const count = Array.from(transportIdentitySlots.keys()).filter(
              (candidate) => candidate.startsWith(kind + ":"),
            ).length;
            transportIdentitySlots.set(key, kind + String(count + 1));
          }
          return transportIdentitySlots.get(key);
        };
        const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
        const recordConsoleChatFrame = (raw) => {
          const data = String(raw || "")
            .split(/\\r?\\n/)
            .filter((line) => line.startsWith("data:"))
            .map((line) => line.slice(5).trimStart())
            .join("\\n")
            .trim();
          let payload = null;
          try {
            payload = data ? JSON.parse(data) : null;
          } catch {
            payload = null;
          }
          const content = Array.isArray(payload?.content) ? payload.content : [];
          const metadata =
            payload?.metadata && typeof payload.metadata === "object"
              ? payload.metadata
              : {};
          const contentTextLength = content.reduce(
            (total, item) =>
              total +
              String(item?.text || item?.thinking || item?.refusal || "").length,
            0,
          );
          consoleChatTransport.frames.push({
            at_ms: Math.round(performance.now() - startedAt),
            object: typeof payload?.object === "string" ? payload.object : "",
            type: typeof payload?.type === "string" ? payload.type : "",
            status: typeof payload?.status === "string" ? payload.status : "",
            delta: payload?.delta === true,
            id: transportIdentitySlot("id", payload?.id),
            msg_id: transportIdentitySlot("msg", payload?.msg_id),
            source_message_id: transportIdentitySlot(
              "source",
              metadata.source_message_id,
            ),
            client_turn_id: transportIdentitySlot(
              "turn",
              metadata.client_turn_id,
            ),
            content_blocks: content.length,
            text_length:
              String(payload?.text || payload?.thinking || "").length +
              contentTextLength,
          });
          if (consoleChatTransport.frames.length > 40) {
            consoleChatTransport.frames.shift();
          }
        };
        const captureConsoleChatStream = async (response) => {
          if (!response.body) return;
          const reader = response.clone().body.getReader();
          const decoder = new TextDecoder();
          let buffer = "";
          try {
            while (true) {
              const { done, value } = await reader.read();
              if (value) {
                buffer += decoder.decode(value, { stream: !done });
                let separator = buffer.indexOf("\\n\\n");
                while (separator !== -1) {
                  recordConsoleChatFrame(buffer.slice(0, separator));
                  buffer = buffer.slice(separator + 2);
                  separator = buffer.indexOf("\\n\\n");
                }
              }
              if (done) {
                buffer += decoder.decode();
                if (buffer.trim()) recordConsoleChatFrame(buffer);
                return;
              }
            }
          } catch (error) {
            consoleChatTransport.frames.push({
              at_ms: Math.round(performance.now() - startedAt),
              capture_error: String(error?.message || error || "unknown"),
            });
          }
        };
        const originalFetch = (
          window.__lantaiConfiguredSmokeOriginalFetch || window.fetch
        ).bind(window);
        window.__lantaiConfiguredSmokeOriginalFetch = originalFetch;
        window.fetch = async (...args) => {
          const rawInput = args[0];
          const url =
            typeof rawInput === "string"
              ? rawInput
              : rawInput instanceof URL
                ? rawInput.toString()
                : rawInput?.url || "";
          const method = String(
            args[1]?.method || rawInput?.method || "GET",
          ).toUpperCase();
          let deepThinkingRequest = null;
          let reconnectRequest = false;
          let requestShape = null;
          if (method === "POST" && String(url).includes("/api/console/chat")) {
            try {
              let rawBody = args[1]?.body;
              if (
                rawBody == null &&
                typeof Request !== "undefined" &&
                rawInput instanceof Request
              ) {
                rawBody = await rawInput.clone().text();
              }
              const parsedBody =
                typeof rawBody === "string" ? JSON.parse(rawBody) : null;
              const admittedDeepThinkingControl = document.querySelector(
                'button[data-chat-deep-thinking="1"]',
              );
              requestShape = {
                keys:
                  parsedBody && typeof parsedBody === "object"
                    ? Object.keys(parsedBody).sort()
                    : [],
                has_biz_params:
                  Boolean(
                    parsedBody?.biz_params &&
                      typeof parsedBody.biz_params === "object",
                  ),
                biz_params_has_deep_thinking: Boolean(
                  parsedBody?.biz_params?.deep_thinking &&
                    typeof parsedBody.biz_params.deep_thinking === "object",
                ),
                deep_thinking_marker_count: document.querySelectorAll(
                  'button[data-chat-deep-thinking="1"]',
                ).length,
                deep_thinking_control_active:
                  admittedDeepThinkingControl?.getAttribute("aria-pressed") ===
                  "true",
                permission_mode: String(parsedBody?.permission_mode || ""),
                permission_profile: String(parsedBody?.permission_profile || ""),
              };
              reconnectRequest = parsedBody?.reconnect === true;
              const requested = parsedBody?.deep_thinking;
              deepThinkingRequest = {
                present: Boolean(
                  requested && typeof requested === "object",
                ),
                enabled: requested?.enabled === true,
                effort:
                  typeof requested?.effort === "string"
                    ? requested.effort
                    : "",
              };
            } catch {
              deepThinkingRequest = { present: false, enabled: false, effort: "" };
            }
          }
          const response = await originalFetch(...args);
          if (method === "POST" && String(url).includes("/api/console/chat")) {
            consoleChatTransport.responses.push({
              at_ms: Math.round(performance.now() - startedAt),
              status: response.status,
              reconnect: reconnectRequest,
              deep_thinking: deepThinkingRequest,
              request_shape: requestShape,
            });
            void captureConsoleChatStream(response);
          }
          return response;
        };
        const uniqueNodes = (...selectors) =>
          Array.from(
            new Set(
              selectors.flatMap((selector) =>
                Array.from(document.querySelectorAll(selector))
              )
            )
          );
        const snapshot = () => {
          const textarea = document.querySelector('[class*="sender"] textarea');
          const deepThinkingButtons = Array.from(
            document.querySelectorAll("button"),
          ).filter((node) => {
            const className = String(node.className || "");
            const label = String(node.getAttribute("aria-label") || "").toLowerCase();
            return (
              className.includes("deepThinkingButton") ||
              label.includes("深度思考") ||
              label.includes("deep thinking")
            );
          });
          const deepThinkingVisibleButtons = deepThinkingButtons.filter((node) => {
            const rect = node.getBoundingClientRect();
            return rect.width > 0 && rect.height > 0;
          });
          const deepThinkingButton =
            deepThinkingVisibleButtons[0] || deepThinkingButtons[0];
          const deepThinkingClassName = String(
            deepThinkingButton?.className || "",
          );
          const deepThinkingAriaPressed =
            deepThinkingButton?.getAttribute("aria-pressed") || "";
          const userNodes = Array.from(
            document.querySelectorAll(
              ".lantai-bubble-list-item.lantai-bubble-user",
            ),
          );
          const userTexts = userNodes.map((node) =>
            (node.textContent || "").trim(),
          );
          const assistantNodes = Array.from(
            document.querySelectorAll(
              ".lantai-bubble-list-item.lantai-bubble-assistant",
            ),
          );
          const assistantTexts = assistantNodes.map(
            (node) => (node.textContent || "").trim(),
          );
          const assistantAnswerNodes = Array.from(
            document.querySelectorAll('[data-lantai-assistant-answer="1"]'),
          );
          const answerNodeIdentities = assistantAnswerNodes.map((node) => {
            const fiberKey = Object.keys(node).find((key) =>
              key.startsWith("__reactFiber$"),
            );
            const fiber = fiberKey ? node[fiberKey] : null;
            const child = fiber?.memoizedProps?.children;
            const data = child?.props?.data;
            const metadata =
              data?.metadata && typeof data.metadata === "object"
                ? data.metadata
                : {};
            const slot = (value) =>
              transportIdentitySlot("value", String(value || ""));
            return {
              output_id: slot(data?.id),
              source_id: slot(
                metadata.source_message_id ||
                  metadata.native_runtime_logical_source_id ||
                  metadata.native_runtime_source_id,
              ),
              client_turn_id: slot(
                metadata.client_turn_id || metadata.native_runtime_turn_id,
              ),
              run_id: slot(metadata.run_id || metadata.turn_id),
              native_runtime_stream: metadata.native_runtime_stream === true,
              status: String(data?.status || ""),
            };
          });
          const assistantAnswerTexts = assistantAnswerNodes.map((node) =>
            (node.textContent || "").trim(),
          );
          const artifactCards = Array.from(
            document.querySelectorAll('[data-lantai-artifact="file"]'),
          );
          const matchingArtifactCards = artifactFilename
            ? artifactCards.filter(
                (card) =>
                  (card.querySelector("strong")?.textContent || "").trim() ===
                  artifactFilename,
              )
            : [];
          const matchingArtifactCard = matchingArtifactCards[0] || null;
          const artifactCardHasOpenAction = Boolean(
            matchingArtifactCard?.querySelector(
              '[aria-label^="打开文件"], ' +
                '[aria-label^="在澜台中编辑"], ' +
                '[aria-label^="预览文件"]',
            ),
          );
          const artifactCardHasDownloadAction = Boolean(
            matchingArtifactCard?.querySelector('[aria-label="下载文件"]'),
          );
          const answerNodeSentinelOccurrences = assistantAnswerTexts.map(
            (text) => {
              let cursor = 0;
              let occurrences = 0;
              while (answerSentinel && cursor <= text.length) {
                const found = text.indexOf(answerSentinel, cursor);
                if (found === -1) break;
                occurrences += 1;
                cursor = found + answerSentinel.length;
              }
              return occurrences;
            },
          );
          const thinkingNodes = Array.from(
            document.querySelectorAll('[data-chat-thinking="1"]'),
          );
          const responseAvatars = uniqueNodes(
            '[data-lantai-response-avatar="1"]',
          );
          const actionBars = uniqueNodes(
            '[data-lantai-response-actions="1"]',
            ".lantai-response-actions",
          );
          const assistantAnswerText = assistantAnswerTexts.join("\\n");
          const assistantSentinelOccurrences = assistantAnswerTexts.reduce(
            (total, text) => {
              let cursor = 0;
              let occurrences = 0;
              while (answerSentinel && cursor <= text.length) {
                const found = text.indexOf(answerSentinel, cursor);
                if (found === -1) break;
                occurrences += 1;
                cursor = found + answerSentinel.length;
              }
              return total + occurrences;
            },
            0,
          );
          const composerTop =
            document
              .querySelector(".lantai-chat-anywhere-input")
              ?.getBoundingClientRect().top ?? window.innerHeight;
          const messageListTop =
            document
              .querySelector(".lantai-chat-anywhere-message-list")
              ?.getBoundingClientRect().top ?? 0;
          const contentRect = (node) => {
            if (!node) return null;
            const ownRect = node.getBoundingClientRect();
            if (ownRect.height > 0) return ownRect;
            const range = document.createRange();
            range.selectNodeContents(node);
            const rangeRect = range.getBoundingClientRect();
            range.detach?.();
            return rangeRect;
          };
          const isClearOfComposer = (node) => {
            const rect = contentRect(node);
            const entirelyAboveMessageViewport =
              Boolean(rect) && rect.bottom <= messageListTop + 8;
            const visibleAboveComposer =
              Boolean(rect) &&
              rect.top < composerTop - 8 &&
              rect.bottom <= composerTop - 8 &&
              rect.bottom > messageListTop + 8;
            return Boolean(
              rect &&
                rect.height > 0 &&
                // A long message may legitimately begin above the viewport
                // after following its live tail, and an earlier turn may be
                // entirely above the message viewport.  Both states are
                // clear of the composer; only actual overlap is a failure.
                (entirelyAboveMessageViewport || visibleAboveComposer),
            );
          };
          const roundedRect = (node) => {
            const rect = contentRect(node);
            if (!rect) return null;
            return {
              top: Math.round(rect.top),
              bottom: Math.round(rect.bottom),
              height: Math.round(rect.height),
            };
          };
          const currentUserNode = userNodes.find((node) =>
            (node.textContent || "").includes(promptText),
          );
          const currentAnswerNode = assistantAnswerNodes.find((node) =>
            (node.textContent || "").includes(answerSentinel),
          );
          const currentAnswerNodeIndex = currentAnswerNode
            ? assistantAnswerNodes.indexOf(currentAnswerNode)
            : -1;
          const currentAnswerNodeStatus =
            currentAnswerNodeIndex >= 0
              ? answerNodeIdentities[currentAnswerNodeIndex]?.status || ""
              : "";
          const currentThinkingNode =
            thinkingNodes.length > 0
              ? thinkingNodes[thinkingNodes.length - 1]
              : null;
          const inlineTimeline = document.querySelector(
            '[data-testid="inline-execution-timeline"]',
          );
          const runDrawerTrigger = document.querySelector(
            'button[aria-label^="打开本轮执行"]',
          );
          const runDrawerTriggerLabel =
            runDrawerTrigger?.getAttribute("aria-label") || "";
          return {
            has_textarea: Boolean(textarea),
            textarea_value: textarea?.value || "",
            location_hash: window.location.hash,
            user_bubble_count: userTexts.length,
            assistant_bubble_count: assistantTexts.length,
            empty_assistant_count: assistantTexts.filter((text) => !text).length,
            thinking_count: thinkingNodes.length,
            thinking_statuses: thinkingNodes.map(
              (node) => node.getAttribute("data-status") || "",
            ),
            response_avatar_count: responseAvatars.length,
            action_bar_count: actionBars.length,
            deep_thinking_control_present: Boolean(deepThinkingButton),
            deep_thinking_control_enabled:
              Boolean(deepThinkingButton) && !deepThinkingButton.disabled,
            deep_thinking_control_active:
              deepThinkingAriaPressed === "true" ||
              deepThinkingClassName.includes("deepThinkingButtonActive"),
            deep_thinking_control_diagnostic: {
              count: deepThinkingButtons.length,
              visible_count: deepThinkingVisibleButtons.length,
              aria_pressed: deepThinkingAriaPressed,
              disabled: Boolean(deepThinkingButton?.disabled),
              marker_count: document.querySelectorAll(
                'button[data-chat-deep-thinking="1"]',
              ).length,
              marker_value:
                deepThinkingButton?.getAttribute("data-chat-deep-thinking") ||
                "",
            },
            user_contains_prompt: userTexts.some((text) =>
              text.includes(promptText),
            ),
            assistant_contains_sentinel:
              assistantAnswerText.includes(answerSentinel),
            assistant_sentinel_occurrences: assistantSentinelOccurrences,
            answer_node_completed: currentAnswerNodeStatus === "completed",
            assistant_answer_node_count: assistantAnswerNodes.length,
            answer_node_sentinel_occurrences: answerNodeSentinelOccurrences,
            answer_node_identities: answerNodeIdentities,
            user_clear_of_composer: isClearOfComposer(currentUserNode),
            thinking_clear_of_composer:
              isClearOfComposer(currentThinkingNode),
            answer_clear_of_composer: isClearOfComposer(currentAnswerNode),
            geometry: {
              viewport_height: window.innerHeight,
              message_list_top: Math.round(messageListTop),
              composer_top: Math.round(composerTop),
              user: roundedRect(currentUserNode),
              thinking: roundedRect(currentThinkingNode),
              answer: roundedRect(currentAnswerNode),
            },
            inline_timeline_present: Boolean(inlineTimeline),
            run_drawer_trigger_present: Boolean(runDrawerTrigger),
            run_drawer_trigger_completed:
              runDrawerTriggerLabel.includes("已完成") ||
              runDrawerTriggerLabel.includes("任务成功") ||
              runDrawerTriggerLabel.includes("回复结束"),
            artifact_card_count: artifactCards.length,
            matching_artifact_card_count: matchingArtifactCards.length,
            artifact_card_has_open_action: artifactCardHasOpenAction,
            artifact_card_has_download_action: artifactCardHasDownloadAction,
          };
        };
        const publicSnapshot = (current) => ({
          user_bubble_count: current.user_bubble_count,
          assistant_bubble_count: current.assistant_bubble_count,
          empty_assistant_count: current.empty_assistant_count,
          thinking_count: current.thinking_count,
          thinking_statuses: current.thinking_statuses,
          response_avatar_count: current.response_avatar_count,
          action_bar_count: current.action_bar_count,
          deep_thinking_control_present:
            current.deep_thinking_control_present,
          deep_thinking_control_enabled:
            current.deep_thinking_control_enabled,
          deep_thinking_control_active:
            current.deep_thinking_control_active,
          user_contains_prompt: current.user_contains_prompt,
          assistant_contains_sentinel: current.assistant_contains_sentinel,
          assistant_sentinel_occurrences:
            current.assistant_sentinel_occurrences,
          answer_node_completed: current.answer_node_completed,
          user_clear_of_composer: current.user_clear_of_composer,
          thinking_clear_of_composer: current.thinking_clear_of_composer,
          answer_clear_of_composer: current.answer_clear_of_composer,
          geometry: current.geometry,
          inline_timeline_present: current.inline_timeline_present,
          run_drawer_trigger_present: current.run_drawer_trigger_present,
          run_drawer_trigger_completed: current.run_drawer_trigger_completed,
          artifact_card_count: current.artifact_card_count,
          matching_artifact_card_count:
            current.matching_artifact_card_count,
          artifact_card_has_open_action:
            current.artifact_card_has_open_action,
          artifact_card_has_download_action:
            current.artifact_card_has_download_action,
        });
        const diagnosticSnapshot = (current) => ({
          elapsed_ms: Math.round(performance.now() - startedAt),
          users: current.user_bubble_count,
          assistants: current.assistant_bubble_count,
          empty_assistants: current.empty_assistant_count,
          thinking: current.thinking_count,
          thinking_statuses: current.thinking_statuses,
          avatars: current.response_avatar_count,
          actions: current.action_bar_count,
          answer_visible: current.assistant_contains_sentinel,
          answer_occurrences: current.assistant_sentinel_occurrences,
          answer_node_completed: current.answer_node_completed,
          answer_nodes: current.assistant_answer_node_count,
          answer_occurrences_by_node:
            current.answer_node_sentinel_occurrences,
          answer_node_identities: current.answer_node_identities,
          artifact_cards: current.artifact_card_count,
          matching_artifact_cards: current.matching_artifact_card_count,
          artifact_open_action: current.artifact_card_has_open_action,
          artifact_download_action: current.artifact_card_has_download_action,
        });
        const timeline = [];
        let previousTimelineKey = "";
        let maxAssistantSentinelOccurrences = 0;
        const recordSnapshot = (current) => {
          maxAssistantSentinelOccurrences = Math.max(
            maxAssistantSentinelOccurrences,
            current.assistant_sentinel_occurrences,
          );
          const matchingAnswerNodes =
            current.answer_node_sentinel_occurrences.filter(
              (occurrences) => occurrences > 0,
            ).length;
          if (matchingAnswerNodes > 1) {
            throw new Error(
              "configured provider rendered the answer in more than one node; " +
                "snapshot=" + JSON.stringify(diagnosticSnapshot(current)) +
                "; console_chat_transport=" +
                JSON.stringify(consoleChatTransport) +
                "; stream_probe=" +
                JSON.stringify(window.__lantaiChatStreamProbe?.frames || []),
            );
          }
          if (
            artifactFilename &&
            current.matching_artifact_card_count > 1
          ) {
            throw new Error(
              "configured provider rendered the delivered artifact in more than one card; " +
                "snapshot=" + JSON.stringify(diagnosticSnapshot(current)),
            );
          }
          const publicState = diagnosticSnapshot(current);
          // Elapsed time changes on every poll. Do not put it in the
          // de-duplication key or a long timeout will evict the only useful
          // transitions (user -> Thinking -> answer) from the diagnostic.
          const key = JSON.stringify({
            users: publicState.users,
            assistants: publicState.assistants,
            empty_assistants: publicState.empty_assistants,
            thinking: publicState.thinking,
            thinking_statuses: publicState.thinking_statuses,
            avatars: publicState.avatars,
            actions: publicState.actions,
            answer_visible: publicState.answer_visible,
            answer_occurrences: publicState.answer_occurrences,
            artifact_cards: publicState.artifact_cards,
            matching_artifact_cards: publicState.matching_artifact_cards,
            artifact_open_action: publicState.artifact_open_action,
            artifact_download_action: publicState.artifact_download_action,
          });
          if (key === previousTimelineKey) return;
          previousTimelineKey = key;
          timeline.push(publicState);
          if (timeline.length > 48) timeline.shift();
        };
        const waitFor = async (label, predicate, limitMs = timeoutMs) => {
          const localDeadline = Math.min(deadline, Date.now() + limitMs);
          while (Date.now() < localDeadline) {
            const current = snapshot();
            recordSnapshot(current);
            if (predicate(current)) return current;
            await sleep(60);
          }
          const current = snapshot();
          recordSnapshot(current);
          throw new Error(
            label + " timed out; snapshot=" + JSON.stringify(publicSnapshot(current)) +
              "; timeline=" + JSON.stringify(timeline) +
              "; console_chat_transport=" + JSON.stringify(consoleChatTransport) +
              "; stream_probe=" +
                JSON.stringify(window.__lantaiChatStreamProbe?.frames || []),
          );
        };
        const healthyTurn = (current) =>
          current.assistant_bubble_count === turnNumber &&
          current.empty_assistant_count === 0 &&
          current.response_avatar_count === turnNumber &&
          current.action_bar_count === turnNumber;
        const completedThinking = (current) =>
          (artifactFilename
            ? current.thinking_count >= turnNumber
            : current.thinking_count === turnNumber) &&
          current.thinking_statuses.every((status) => status === "completed");
        const artifactDeliveryHealthy = (current) =>
          !artifactFilename ||
          (current.artifact_card_count === 1 &&
            current.matching_artifact_card_count === 1 &&
            current.artifact_card_has_open_action &&
            current.artifact_card_has_download_action);
        const setTextareaValue = (textarea, value) => {
          const previousValue = textarea.value;
          textarea.focus();
          const descriptor = Object.getOwnPropertyDescriptor(
            window.HTMLTextAreaElement.prototype,
            "value",
          );
          if (descriptor?.set) {
            descriptor.set.call(textarea, value);
          } else {
            textarea.value = value;
          }
          textarea._valueTracker?.setValue(previousValue);
          textarea.dispatchEvent(
            new InputEvent("input", {
              bubbles: true,
              cancelable: true,
              data: value,
              inputType: "insertText",
            }),
          );
          textarea.dispatchEvent(new Event("input", { bubbles: true }));
          textarea.dispatchEvent(new Event("change", { bubbles: true }));
        };
        const closeModelPromptIfOpen = async () => {
          const buttons = Array.from(
            document.querySelectorAll(".ant-modal button"),
          );
          const button = buttons.find((node) => {
            const text = (node.textContent || "").replace(/\\s+/g, "").toLowerCase();
            return (
              text.includes("跳过") ||
              text.includes("稍后") ||
              text.includes("skip") ||
              text.includes("later")
            );
          });
          if (button) {
            button.click();
            await sleep(120);
          }
        };
        const selectPermissionPreset = async (preset) => {
          if (!preset) return;
          if (preset !== "full") {
            throw new Error("unsupported configured-provider permission preset");
          }
          const trigger = document.querySelector(
            'button[data-chat-permission-trigger="1"]',
          );
          if (!trigger) {
            throw new Error("configured provider permission control was not found");
          }
          if (String(trigger.getAttribute("aria-label") || "").includes("完全访问")) {
            return;
          }
          trigger.click();
          await waitFor(
            "configured provider full-access option",
            () =>
              Array.from(document.querySelectorAll('[role="menuitemradio"]')).some(
                (node) =>
                  String(node.textContent || "")
                    .replace(/\\s+/g, "")
                    .includes("完全访问"),
              ),
            3000,
          );
          const option = Array.from(
            document.querySelectorAll('[role="menuitemradio"]'),
          ).find((node) =>
            String(node.textContent || "")
              .replace(/\\s+/g, "")
              .includes("完全访问"),
          );
          if (!option) {
            throw new Error("configured provider full-access option was not found");
          }
          option.click();
          await waitFor(
            "configured provider full access selected",
            () =>
              String(
                document
                  .querySelector('button[data-chat-permission-trigger="1"]')
                  ?.getAttribute("aria-label") || "",
              ).includes("完全访问"),
            3000,
          );
        };
        const findDeepThinkingButton = () =>
          Array.from(document.querySelectorAll("button")).find(
            (node) => String(node.className || "").includes("deepThinkingButton"),
          );
        const waitForDeepThinkingControlResolution = async (limitMs) => {
          const localDeadline = Math.min(deadline, Date.now() + limitMs);
          let current = snapshot();
          while (
            Date.now() < localDeadline &&
            current.deep_thinking_control_present &&
            !current.deep_thinking_control_enabled &&
            !current.deep_thinking_control_active
          ) {
            recordSnapshot(current);
            await sleep(60);
            current = snapshot();
          }
          recordSnapshot(current);
          return current;
        };
        const enableDeepThinkingWhenAvailable = async () => {
          let button = findDeepThinkingButton();
          if (!button) {
            // Some providers emit reasoning but expose no user-toggleable
            // control.  The smoke must verify the rendered Thinking stream,
            // not fail before submission because that optional control is
            // absent for the active model.
            return;
          }
          let current = snapshot();
          if (
            !current.deep_thinking_control_active &&
            !current.deep_thinking_control_enabled
          ) {
            // Capability discovery and the React commit enabling this control
            // are asynchronous on a clean profile.  Do not submit the first
            // turn while the visible control is merely in its loading state.
            current = await waitForDeepThinkingControlResolution(8000);
            button = findDeepThinkingButton();
          }
          if (!button || current.deep_thinking_control_active) return;
          if (!current.deep_thinking_control_enabled) {
            // A visible-but-disabled control means the active model does not
            // admit user-selected reasoning.  That is a valid configured
            // provider state; the smoke still verifies the ordinary Thinking
            // lifecycle and the final transcript below.
            return;
          }
          button.click();
          await waitFor(
            "configured provider deep thinking enabled",
            (current) => current.deep_thinking_control_active,
            3000,
          );
        };
        await closeModelPromptIfOpen();
        await waitFor(
          "configured provider initial chat state",
          (current) =>
            current.has_textarea &&
            current.location_hash.includes(encodeURIComponent(chatId)) &&
            current.user_bubble_count === turnNumber - 1 &&
            current.assistant_bubble_count === turnNumber - 1,
          8000,
        );
        await selectPermissionPreset(permissionPreset);
        await enableDeepThinkingWhenAvailable();
        const textarea = document.querySelector('[class*="sender"] textarea');
        if (!textarea) throw new Error("configured provider textarea was not found");
        setTextareaValue(textarea, promptText);
        await waitFor(
          "configured provider draft populated",
          (current) => current.textarea_value === promptText,
          3000,
        );
        textarea.dispatchEvent(
          new KeyboardEvent("keydown", {
            key: "Enter",
            code: "Enter",
            bubbles: true,
            cancelable: true,
          }),
        );
        await sleep(100);
        if (snapshot().user_bubble_count === 0) {
          const submitButton = document.querySelector(".lantai-sender-actions-btn");
          if (!submitButton || submitButton.disabled) {
            throw new Error("configured provider submit control was unavailable");
          }
          submitButton.click();
        }
        const user = await waitFor(
          "configured provider user message",
          (current) =>
            current.user_bubble_count === turnNumber &&
            current.user_contains_prompt &&
            current.user_clear_of_composer &&
            !current.inline_timeline_present &&
            current.run_drawer_trigger_present &&
            current.textarea_value !== promptText,
          5000,
        );
        window.__lantaiConfiguredSmokePhase = {
          turn_number: turnNumber,
          phase: "user",
        };
        const userVisibleMs = Math.round(performance.now() - startedAt);
        const thinking = await waitFor(
          "configured provider thinking before answer",
          (current) =>
            current.thinking_count >= turnNumber &&
            current.thinking_statuses.at(-1) === "in_progress" &&
            !current.assistant_contains_sentinel &&
            current.thinking_clear_of_composer &&
            !current.inline_timeline_present &&
            current.run_drawer_trigger_present &&
            healthyTurn(current),
          Math.min(timeoutMs, 30000),
        );
        window.__lantaiConfiguredSmokePhase = {
          turn_number: turnNumber,
          phase: "thinking",
        };
        const thinkingVisibleMs = Math.round(performance.now() - startedAt);
        const answer = await waitFor(
          "configured provider answer after thinking",
          (current) =>
            current.assistant_contains_sentinel &&
            current.answer_clear_of_composer &&
            !current.inline_timeline_present &&
            current.run_drawer_trigger_present &&
            healthyTurn(current) &&
            completedThinking(current) &&
            artifactDeliveryHealthy(current),
          Math.min(timeoutMs, 60000),
        );
        window.__lantaiConfiguredSmokePhase = {
          turn_number: turnNumber,
          phase: "answer",
        };
        const answerVisibleMs = Math.round(performance.now() - startedAt);
        let stableSamples = 0;
        let lastKey = "";
        const final = await waitFor(
          "configured provider final stable rendering",
          (current) => {
            if (
              !current.assistant_contains_sentinel ||
              !current.answer_clear_of_composer ||
              current.inline_timeline_present ||
              !current.run_drawer_trigger_present ||
              !current.run_drawer_trigger_completed ||
              !current.answer_node_completed ||
              !healthyTurn(current) ||
              !completedThinking(current) ||
              !artifactDeliveryHealthy(current)
            ) {
              stableSamples = 0;
              lastKey = "";
              return false;
            }
            const key = JSON.stringify(publicSnapshot(current));
            stableSamples = key === lastKey ? stableSamples + 1 : 1;
            lastKey = key;
            return stableSamples >= 3;
          },
          Math.min(timeoutMs, 60000),
        );
        window.__lantaiConfiguredSmokePhase = {
          turn_number: turnNumber,
          phase: "final",
        };
        // This probe is installed independently for each turn, so its local
        // response list always starts at zero.  A hydration reconnect can race
        // with first paint; ignore that control-plane POST and inspect the
        // latest actual submission instead of indexing by the conversation's
        // absolute turn number.
        const transport = [...consoleChatTransport.responses]
          .reverse()
          .find((item) => item.reconnect !== true) || null;
        if (
          turnNumber > 1 &&
          final.deep_thinking_control_present &&
          transport?.request_shape?.deep_thinking_marker_count === 0
        ) {
          throw new Error(
            "configured provider lost the mounted deep-thinking control before " +
              "a subsequent submission; transport=" + JSON.stringify(transport) +
              "; control=" +
              JSON.stringify(final.deep_thinking_control_diagnostic),
          );
        }
        if (
          transport?.request_shape?.deep_thinking_marker_count > 0 &&
          transport?.request_shape?.deep_thinking_control_active &&
          (!transport?.deep_thinking?.present ||
            !transport?.deep_thinking?.enabled ||
            transport?.deep_thinking?.effort !== "high")
        ) {
          throw new Error(
            "configured provider submitted with active deep thinking but omitted it " +
              "from the chat request; transport=" + JSON.stringify(transport) +
              "; control=" +
              JSON.stringify(final.deep_thinking_control_diagnostic),
          );
        }
        if (
          permissionPreset === "full" &&
          (transport?.request_shape?.permission_mode !== "full_auto" ||
            transport?.request_shape?.permission_profile !== "full")
        ) {
          throw new Error(
            "configured provider full-access selection was not preserved in " +
              "the chat request; transport=" + JSON.stringify(transport),
          );
        }
        return {
          ok: true,
          chat_id: chatId,
          turn_number: turnNumber,
          user_visible_ms: userVisibleMs,
          thinking_visible_ms: thinkingVisibleMs,
          answer_visible_ms: answerVisibleMs,
          max_assistant_sentinel_occurrences:
            maxAssistantSentinelOccurrences,
          user: publicSnapshot(user),
          thinking: publicSnapshot(thinking),
          answer: publicSnapshot(answer),
          final: publicSnapshot(final),
          deep_thinking_transport: transport?.deep_thinking || null,
          deep_thinking_admission: transport?.request_shape || null,
          artifact_delivery: artifactFilename
            ? {
                card_count: final.artifact_card_count,
                matching_card_count: final.matching_artifact_card_count,
                open_action: final.artifact_card_has_open_action,
                download_action: final.artifact_card_has_download_action,
              }
            : null,
        };
      })()
    `,
    true,
  );
}

async function main() {
  const args = parseArgs(process.argv);
  const baseUrl = normalizeBaseUrl(args["base-url"]);
  const desktopToken = String(args["desktop-token"] || "");
  const chatId = String(args["chat-id"] || "");
  const promptText = String(args["prompt"] || "");
  const answerSentinel = String(args["answer-sentinel"] || "");
  const secondPromptText = String(args["second-prompt"] || "");
  const secondAnswerSentinel = String(args["second-answer-sentinel"] || "");
  const artifactPrompt = String(args["artifact-prompt"] || "");
  const artifactFilename = String(args["artifact-filename"] || "");
  const artifactAnswerSentinel = String(args["artifact-answer-sentinel"] || "");
  const permissionPreset = String(args["permission-preset"] || "");
  const timeoutMs = Number(args["timeout-ms"] || 90000);
  const screenshotDir = String(args["screenshot-dir"] || "").trim();
  if (!baseUrl || !desktopToken || !chatId || !promptText || !answerSentinel) {
    throw new Error(
      "--base-url, --desktop-token, --chat-id, --prompt and --answer-sentinel are required",
    );
  }
  if (!Number.isFinite(timeoutMs) || timeoutMs < 1000) {
    throw new Error("--timeout-ms must be at least 1000");
  }
  if (Boolean(secondPromptText) !== Boolean(secondAnswerSentinel)) {
    throw new Error(
      "--second-prompt and --second-answer-sentinel must be supplied together",
    );
  }
  if (
    new Set([
      Boolean(artifactPrompt),
      Boolean(artifactFilename),
      Boolean(artifactAnswerSentinel),
    ]).size !== 1
  ) {
    throw new Error(
      "--artifact-prompt, --artifact-filename and --artifact-answer-sentinel " +
        "must be supplied together",
    );
  }
  if (permissionPreset && permissionPreset !== "full") {
    throw new Error("--permission-preset currently supports only full");
  }

  const origin = new URL(baseUrl).origin;
  const requestTrace = [];
  let runtimeEventStreamStarts = 0;
  const appendTrace = (details) => {
    const url = new URL(details.url);
    if (!url.pathname.startsWith("/api/")) return;
    requestTrace.push({
      method: details.method,
      path: url.pathname,
      status: details.statusCode,
    });
    if (requestTrace.length > 24) requestTrace.shift();
  };
  session.defaultSession.webRequest.onBeforeSendHeaders(
    { urls: [`${origin}/*`] },
    (details, callback) => {
      const url = new URL(details.url);
      if (url.pathname.startsWith("/api/")) {
        details.requestHeaders["X-Lantai-Desktop-Token"] = desktopToken;
      }
      callback({ requestHeaders: details.requestHeaders });
    },
  );
  session.defaultSession.webRequest.onBeforeRequest(
    { urls: [`${origin}/*`] },
    (details, callback) => {
      const url = new URL(details.url);
      if (url.pathname.endsWith("/events/stream")) {
        runtimeEventStreamStarts += 1;
      }
      callback({});
    },
  );
  session.defaultSession.webRequest.onCompleted(
    { urls: [`${origin}/*`] },
    (details) => appendTrace(details),
  );
  await enableVisibleThinking({ baseUrl, desktopToken });

  const win = createWindow();
  try {
    let result;
    try {
      const firstTurn = await observeWithScreenshots(
        win,
        {
          baseUrl,
          chatId,
          promptText,
          answerSentinel,
          timeoutMs,
          turnNumber: 1,
          permissionPreset,
        },
        screenshotDir,
      );
      const turns = [firstTurn];
      if (secondPromptText) {
        turns.push(
          await observeWithScreenshots(
            win,
            {
              baseUrl,
              chatId,
              promptText: secondPromptText,
              answerSentinel: secondAnswerSentinel,
              timeoutMs,
              turnNumber: 2,
              permissionPreset,
            },
            screenshotDir,
          ),
        );
      }
      if (artifactPrompt) {
        turns.push(
          await observeWithScreenshots(
            win,
            {
              baseUrl,
              chatId,
              promptText: artifactPrompt,
              answerSentinel: artifactAnswerSentinel,
              timeoutMs,
              turnNumber: turns.length + 1,
              artifactFilename,
              permissionPreset,
            },
            screenshotDir,
          ),
        );
      }
      result = { ok: true, chat_id: chatId, turns, final: turns.at(-1).final };
    } catch (error) {
      // Keep failures diagnosable without retaining prompt, credential, or model text.
      const runtimeEventTrace = await safeRuntimeEventTrace({
        baseUrl,
        desktopToken,
        chatId,
      });
      throw new Error(
        `${error instanceof Error ? error.message : String(error)}; ` +
          `request_trace=${JSON.stringify(requestTrace)}; ` +
          `runtime_event_stream_starts=${runtimeEventStreamStarts}; ` +
          `runtime_event_trace=${JSON.stringify(runtimeEventTrace)}`,
      );
    }
    assert.equal(result.ok, true);
    assert.equal(result.chat_id, chatId);
    for (const turn of result.turns) {
      assert.equal(
        turn.thinking_visible_ms < turn.answer_visible_ms,
        true,
        `thinking did not appear before answer for turn ${turn.turn_number}`,
      );
    }
    const expectedTurnCount = result.turns.length;
    assert.equal(result.final.assistant_bubble_count, expectedTurnCount);
    assert.equal(result.final.empty_assistant_count, 0);
    assert.equal(result.final.response_avatar_count, expectedTurnCount);
    assert.equal(result.final.action_bar_count, expectedTurnCount);
    console.log(
      JSON.stringify(
        {
          ...result,
          electron: process.versions.electron,
          chrome: process.versions.chrome,
          screenshots: screenshotDir
            ? result.turns.flatMap((turn) =>
                ["user", "thinking", "answer", "final", "run-drawer"].map(
                  (phase) => `turn-${turn.turn_number}-${phase}.png`,
                ),
              )
            : [],
        },
        null,
        2,
      ),
    );
  } finally {
    if (!win.isDestroyed()) win.close();
    app.quit();
  }
}

app.whenReady().then(() => {
  main().catch((error) => {
    console.error(error);
    app.exit(1);
  });
});

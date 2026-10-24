const assert = require("node:assert/strict");
const { app, BrowserWindow, session } = require("electron");

function parseArgs(argv) {
  const args = {};
  for (let index = 2; index < argv.length; index += 1) {
    const value = argv[index];
    if (!value.startsWith("--")) {
      continue;
    }
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
    title: "Chat Runtime Stream Smoke",
    webPreferences: {
      sandbox: false,
      contextIsolation: true,
      nodeIntegration: false,
    },
  });
}

async function enableVisibleThinking({ baseUrl, desktopToken }) {
  const requestJson = async (path, options = {}) => {
    const response = await fetch(`${baseUrl}/api${path}`, {
      ...options,
      headers: {
        Accept: "application/json",
        "Content-Type": "application/json",
        "X-Lantai-Desktop-Token": desktopToken,
        ...(options.headers || {}),
      },
    });
    if (!response.ok) {
      const text = await response.text();
      throw new Error(
        `renderer smoke settings request failed: ${path} ${response.status} ${text}`,
      );
    }
    if (response.status === 204) return null;
    return response.json();
  };

  const response = await fetch(`${baseUrl}/api/settings/appearance`, {
    method: "PUT",
    headers: {
      Accept: "application/json",
      "Content-Type": "application/json",
      "X-Lantai-Desktop-Token": desktopToken,
    },
    body: JSON.stringify({
      display_settings: {
        showThinking: true,
      },
    }),
  });
  if (!response.ok) {
    const text = await response.text();
    throw new Error(
      `failed to enable visible thinking for renderer smoke: ${response.status} ${text}`,
    );
  }
  const payload = await response.json();
  if (payload?.display_settings?.showThinking !== true) {
    throw new Error(
      `visible thinking setting did not persist: ${JSON.stringify(payload)}`,
    );
  }

  // ThemeContext treats the customization control plane as authoritative and
  // can overwrite the legacy appearance projection during renderer startup.
  // Seed the effective value too, otherwise the smoke can falsely report that
  // a valid reasoning stream never rendered.
  const [state, profiles] = await Promise.all([
    requestJson("/customization/state"),
    requestJson("/customization/profiles"),
  ]);
  const currentEffective = await requestJson("/customization/effective-state");
  // The lifecycle smoke may deliberately restart the packaged application
  // against the same local profile.  Once this control-plane value has been
  // persisted, submitting the identical change again correctly returns a
  // "no effective change" validation error.  Treat that state as success,
  // rather than turning a healthy restarted app into a false-negative smoke.
  if (currentEffective?.values?.["conversation.show_thinking"] === true) {
    return { appearance: payload, effective: currentEffective };
  }
  let profile = Array.isArray(profiles)
    ? profiles.find((item) => item?.id === state?.active_profile_id)
    : null;
  if (!profile) {
    profile = await requestJson("/customization/profiles", {
      method: "POST",
      body: JSON.stringify({ name: "Renderer smoke" }),
    });
  }
  const preview = await requestJson("/customization/plans/preview", {
    method: "POST",
    body: JSON.stringify({
      target_profile_id: profile.id,
      base_revision: profile.revision,
      intent: "Show Thinking during renderer smoke",
      operations: [
        { op: "set", path: "conversation.show_thinking", value: true },
      ],
    }),
  });
  let confirmationToken = null;
  if (preview?.requires_confirmation) {
    const receipt = await requestJson(
      `/customization/plans/${encodeURIComponent(preview.plan.id)}/confirm`,
      { method: "POST" },
    );
    confirmationToken = receipt?.token || null;
  }
  await requestJson("/customization/plans/apply", {
    method: "POST",
    body: JSON.stringify({
      plan_id: preview.plan.id,
      confirmation_token: confirmationToken,
    }),
  });
  await requestJson(
    `/customization/profiles/${encodeURIComponent(profile.id)}/activate`,
    { method: "POST" },
  );
  const effective = await requestJson("/customization/effective-state");
  if (effective?.values?.["conversation.show_thinking"] !== true) {
    throw new Error(
      `visible thinking customization did not become effective: ${JSON.stringify(effective)}`,
    );
  }
  return { appearance: payload, effective };
}

async function waitForProviderStreamUiSend(
  win,
  {
    baseUrl,
    chatId,
    promptText,
    thinkingText,
    partialText,
    answerText,
    queuedSecondText,
    queuedThirdText,
    timeoutMs,
  },
) {
  await win.loadURL(`${baseUrl}/#/chat/${encodeURIComponent(chatId)}`);
  return win.webContents.executeJavaScript(
    `
      (async () => {
        const baseUrl = ${JSON.stringify(baseUrl)};
        const chatId = ${JSON.stringify(chatId)};
        const promptText = ${JSON.stringify(promptText)};
        const thinkingText = ${JSON.stringify(thinkingText)};
        const partialText = ${JSON.stringify(partialText)};
        const answerText = ${JSON.stringify(answerText)};
        const queuedSecondText = ${JSON.stringify(queuedSecondText)};
        const queuedThirdText = ${JSON.stringify(queuedThirdText)};
        const answerPrefix = answerText.split(": ", 1)[0] || partialText;
        const queuedSecondAnswerText = answerPrefix + ": " + queuedSecondText.slice(0, 80);
        const queuedThirdAnswerText = answerPrefix + ": " + queuedThirdText.slice(0, 80);
        const timeoutMs = ${JSON.stringify(timeoutMs)};
        const deadline = Date.now() + timeoutMs;
        const startedAt = performance.now();
        const consoleChatTransport = {
          responses: [],
          frames: [],
        };
        const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
        const uniqueNodes = (...selectors) =>
          Array.from(
            new Set(
              selectors.flatMap((selector) =>
                Array.from(document.querySelectorAll(selector))
              )
            )
          );
        const listTexts = (selector) =>
          Array.from(document.querySelectorAll(selector)).map((node) =>
            (node.textContent || "").trim()
          );
        const occurrenceCount = (value, needle) => {
          if (!needle) return 0;
          return String(value || "").split(needle).length - 1;
        };
        const textsInOrder = (texts, expected) => {
          let previousIndex = -1;
          for (const needle of expected) {
            const index = texts.findIndex(
              (text, candidateIndex) =>
                candidateIndex > previousIndex && text.includes(needle),
            );
            if (index <= previousIndex) return false;
            previousIndex = index;
          }
          return true;
        };
        const conversationItemsInOrder = (items, expected) => {
          let previousIndex = -1;
          for (const expectedItem of expected) {
            const index = items.findIndex(
              (item, candidateIndex) =>
                candidateIndex > previousIndex &&
                item.role === expectedItem.role &&
                item.text.includes(expectedItem.text),
            );
            if (index <= previousIndex) return false;
            previousIndex = index;
          }
          return true;
        };
        const snapshot = () => {
          const textarea = document.querySelector('[class*="sender"] textarea');
          const assistantNodes = Array.from(
            document.querySelectorAll(
              ".lantai-bubble-list-item.lantai-bubble-assistant"
            )
          );
          const userTexts = listTexts(".lantai-bubble-list-item.lantai-bubble-user");
          const assistantTexts = listTexts(".lantai-bubble-list-item.lantai-bubble-assistant");
          const guidanceCards = Array.from(
            document.querySelectorAll(".lantai-guidance-queue .lantai-guidance-card"),
          );
          const guidanceTexts = guidanceCards.map((node) =>
            (node.textContent || "").trim(),
          );
          const guidanceReceipts = guidanceCards.map((node) => ({
            status: node.getAttribute("data-guidance-status") || "",
            content: (node.querySelector(".lantai-guidance-content")?.textContent || "").trim(),
            text: (node.querySelector(".lantai-guidance-status")?.textContent || "").trim(),
          }));
          const userQueueStatuses = Array.from(
            document.querySelectorAll(".lantai-user-message-queue-status"),
          ).map((node) => ({
            status: node.getAttribute("data-status") || "",
            text: (node.textContent || "").trim(),
          }));
          const conversation_items = Array.from(
            document.querySelectorAll(".lantai-bubble-list-item"),
          )
            .map((node) => ({
              role: node.classList.contains("lantai-bubble-user")
                ? "user"
                : node.classList.contains("lantai-bubble-assistant")
                  ? "assistant"
                  : "",
              text: (node.textContent || "").trim(),
            }))
            .filter((item) => item.role);
          const thinkingNodes = Array.from(document.querySelectorAll('[data-chat-thinking="1"]'));
          const composerTurnProgressNodes = Array.from(
            document.querySelectorAll(
              '[data-chat-turn-preview="1"][data-variant="presence"] button[aria-describedby][aria-busy]',
            ),
          );
          const composerTurnProgressStatusTexts = composerTurnProgressNodes.map(
            (node) => {
              const statusId = node.getAttribute("aria-describedby") || "";
              return statusId
                ? (document.getElementById(statusId)?.textContent || "").trim()
                : "";
            },
          );
          const responseAvatars = uniqueNodes('[data-lantai-response-avatar="1"]');
          const actionBars = uniqueNodes('[data-lantai-response-actions="1"]', ".lantai-response-actions");
          const submitButton = document.querySelector(".lantai-sender-actions-btn");
          const intentButton = document.querySelector(
            ".lantai-sender-control-intent-btn",
          );
          const stopButton = document.querySelector(".lantai-sender-stop-btn");
          const attachmentNodes = Array.from(
            document.querySelectorAll(".lantai-composer-attachment"),
          );
          return {
            has_textarea: Boolean(textarea),
            textarea_value: textarea?.value || "",
            location_hash: window.location.hash,
            user_bubble_count: userTexts.length,
            assistant_bubble_count: assistantTexts.length,
            pending_assistant_count: assistantNodes.filter((node) =>
              node.querySelector('[data-lantai-assistant-pending="1"]')
            ).length,
            composer_turn_progress_count: composerTurnProgressNodes.length,
            composer_turn_progress_busy_count: composerTurnProgressNodes.filter(
              (node) => node.getAttribute("aria-busy") === "true",
            ).length,
            composer_turn_progress_status_texts:
              composerTurnProgressStatusTexts,
            response_avatar_count: responseAvatars.length,
            action_bar_count: actionBars.length,
            empty_assistant_count: assistantTexts.filter((text) => !text).length,
            window_current_session_id: String(window.currentSessionId || ""),
            pending_user_cache: Array.from(
              { length: window.sessionStorage.length },
              (_, index) => window.sessionStorage.key(index),
            )
              .filter((key) => key?.startsWith("lantai_pending_user_msg_"))
              .map((key) => ({ key, value: window.sessionStorage.getItem(key) })),
            session_alias_cache: Array.from(
              { length: window.sessionStorage.length },
              (_, index) => window.sessionStorage.key(index),
            )
              .filter((key) => key?.startsWith("lantai_session_alias_"))
              .map((key) => ({ key, value: window.sessionStorage.getItem(key) })),
            thinking_count: thinkingNodes.length,
            thinking_open_count: thinkingNodes.filter(
              (node) => node instanceof HTMLDetailsElement && node.open
            ).length,
            thinking_statuses: thinkingNodes.map((node) => node.getAttribute("data-status") || ""),
            user_texts: userTexts,
            guidance_texts: guidanceTexts,
            queued_guidance_count: guidanceTexts.length,
            guidance_receipts: guidanceReceipts,
            user_queue_statuses: userQueueStatuses,
            attachment_count: attachmentNodes.length,
            attachment_names: attachmentNodes.map(
              (node) =>
                (node.querySelector(".lantai-composer-attachment-name")
                  ?.textContent || "").trim(),
            ),
            attachment_states: attachmentNodes.map(
              (node) =>
                Array.from(node.classList).find((name) =>
                  name.startsWith("is-"),
                ) || "",
            ),
            assistant_texts: assistantTexts,
            conversation_items,
            assistant_text: assistantTexts.join("\\n"),
            answer_occurrence_count: assistantTexts.reduce(
              (count, text) => count + occurrenceCount(text, answerText),
              0,
            ),
            console_chat_transport: {
              responses: consoleChatTransport.responses.slice(-4),
              frames: consoleChatTransport.frames.slice(-12),
            },
            body_text: document.body?.textContent || "",
            submit_button_present: Boolean(submitButton),
            submit_button_disabled: Boolean(submitButton?.disabled),
            submit_button_label:
              submitButton?.getAttribute("aria-label") ||
              submitButton?.getAttribute("title") ||
              "",
            intent_button_present: Boolean(intentButton),
            intent_button_disabled: Boolean(intentButton?.disabled),
            intent_button_label:
              intentButton?.getAttribute("aria-label") ||
              intentButton?.getAttribute("title") ||
              "",
            stop_button_present: Boolean(stopButton),
            stop_button_disabled: Boolean(stopButton?.disabled),
            stop_button_label:
              stopButton?.getAttribute("aria-label") ||
              stopButton?.getAttribute("title") ||
              "",
            visible_menu_items: Array.from(
              document.querySelectorAll('[role="menuitem"]'),
            )
              .filter((node) => {
                const rect = node.getBoundingClientRect();
                return rect.width > 0 && rect.height > 0;
              })
              .map((node) => (node.textContent || "").trim()),
          };
        };
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
          consoleChatTransport.frames.push({
            at_ms: Math.round(performance.now() - startedAt),
            bytes: String(raw || "").length,
            object: typeof payload?.object === "string" ? payload.object : "",
            type: typeof payload?.type === "string" ? payload.type : "",
            status: typeof payload?.status === "string" ? payload.status : "",
            initial_status: payload?.metadata?.initial_status === true,
            run_id:
              typeof payload?.metadata?.run_id === "string"
                ? payload.metadata.run_id
                : "",
            receipt_run_id:
              typeof payload?.metadata?.turn_receipt?.run_id === "string"
                ? payload.metadata.turn_receipt.run_id
                : "",
            receipt_attempt_id:
              typeof payload?.metadata?.turn_receipt?.attempt_id === "string"
                ? payload.metadata.turn_receipt.attempt_id
                : "",
            turn_key:
              typeof payload?.metadata?.client_turn_id === "string"
                ? payload.metadata.client_turn_id
                : typeof payload?.metadata?.turn_id === "string"
                  ? payload.metadata.turn_id
                  : "",
            event_count: Array.isArray(payload?.events) ? payload.events.length : 0,
            has_thinking: String(raw || "").includes(thinkingText),
            has_partial: String(raw || "").includes(partialText),
            has_answer: String(raw || "").includes(answerText),
          });
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
        const originalFetch = window.fetch.bind(window);
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
          const response = await originalFetch(...args);
          if (method === "POST" && String(url).includes("/api/console/chat")) {
            consoleChatTransport.responses.push({
              at_ms: Math.round(performance.now() - startedAt),
              status: response.status,
            });
            void captureConsoleChatStream(response);
          }
          return response;
        };
        const textFromContent = (value) => {
          if (typeof value === "string") return value;
          if (Array.isArray(value)) {
            return value.map((item) => textFromContent(item)).filter(Boolean).join("\\n");
          }
          if (value && typeof value === "object") {
            return [
              value.text,
              value.thinking,
              value.content,
              value.output,
              value.value,
              value.message,
            ]
              .map((item) => textFromContent(item))
              .filter(Boolean)
              .join("\\n");
          }
          return "";
        };
        const backendSnapshot = async () => {
          const response = await fetch(
            \`\${baseUrl}/api/chats/\${encodeURIComponent(chatId)}\`,
          );
          if (!response.ok) {
            throw new Error(
              \`provider stream backend snapshot failed: \${response.status}\`
            );
          }
          const payload = await response.json();
          const messages = Array.isArray(payload.messages) ? payload.messages : [];
          const roleOf = (message) => String(message?.role || message?.type || "");
          const messageText = (message) =>
            textFromContent(
              message?.content ??
                message?.text ??
                message?.message ??
                message?.output ??
                "",
            );
          const userMessages = messages.filter((message) => roleOf(message) === "user");
          const assistantMessages = messages.filter(
            (message) => roleOf(message) === "assistant",
          );
          return {
            message_count: messages.length,
            user_message_count: userMessages.length,
            assistant_message_count: assistantMessages.length,
            pending_user_message:
              typeof payload.pending_user_message === "string"
                ? payload.pending_user_message
                : "",
            user_texts: userMessages.map(messageText).filter(Boolean),
            assistant_texts: assistantMessages.map(messageText).filter(Boolean),
          };
        };
        const waitFor = async (label, predicate, limitMs = timeoutMs) => {
          const localDeadline = Math.min(deadline, Date.now() + limitMs);
          while (Date.now() < localDeadline) {
            const current = snapshot();
            if (predicate(current)) return current;
            await sleep(60);
          }
          throw new Error(
            label +
              " timed out; snapshot=" +
              JSON.stringify(snapshot()).slice(0, 1800) +
              " transport=" +
              JSON.stringify(consoleChatTransport)
          );
        };
        const waitForStableSnapshot = async (label, predicate, limitMs = 5000) => {
          const localDeadline = Math.min(deadline, Date.now() + limitMs);
          let lastKey = "";
          let stableSamples = 0;
          while (Date.now() < localDeadline) {
            const current = snapshot();
            if (predicate(current)) {
              const key = JSON.stringify({
                user_bubble_count: current.user_bubble_count,
                assistant_bubble_count: current.assistant_bubble_count,
                response_avatar_count: current.response_avatar_count,
                action_bar_count: current.action_bar_count,
                empty_assistant_count: current.empty_assistant_count,
                assistant_texts: current.assistant_texts,
                textarea_value: current.textarea_value,
              });
              stableSamples = key === lastKey ? stableSamples + 1 : 1;
              lastKey = key;
              if (stableSamples >= 3) return current;
            } else {
              stableSamples = 0;
              lastKey = "";
            }
            await sleep(100);
          }
          throw new Error(
            label +
              " did not become stable; snapshot=" +
              JSON.stringify(snapshot()).slice(0, 1800) +
              " transport=" +
              JSON.stringify(consoleChatTransport)
          );
        };
        const waitForBackend = async (label, predicate, limitMs = timeoutMs) => {
          const localDeadline = Math.min(deadline, Date.now() + limitMs);
          let current = null;
          while (Date.now() < localDeadline) {
            current = await backendSnapshot();
            if (predicate(current)) return current;
            await sleep(100);
          }
          throw new Error(
            label +
              " timed out; backend=" +
              JSON.stringify(current).slice(0, 1800) +
              " dom=" +
              JSON.stringify(snapshot()).slice(0, 1200)
          );
        };
        const setTextareaValue = (textarea, value) => {
          const previousValue = textarea.value;
          textarea.focus();
          textarea.select();
          const inserted =
            document.queryCommandSupported?.("insertText") &&
            document.execCommand("insertText", false, value) &&
            textarea.value === value;
          if (!inserted) {
            const descriptor = Object.getOwnPropertyDescriptor(
              window.HTMLTextAreaElement.prototype,
              "value",
            );
            if (descriptor?.set) {
              descriptor.set.call(textarea, value);
            } else {
              textarea.value = value;
            }
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
        const promptButtonScore = (node) => {
          const text = (node.textContent || "").trim();
          const compact = text.replace(/\\s+/g, "");
          const lower = text.toLowerCase();
          if (
            compact.includes("跳过") ||
            compact.includes("稍后") ||
            lower.includes("skip") ||
            lower.includes("later")
          ) {
            return 2;
          }
          if (
            compact.includes("配置模型") ||
            compact.includes("配置") ||
            lower.includes("configure")
          ) {
            return 1;
          }
          return 0;
        };
        const closeModelPromptIfOpen = async () => {
          const buttons = Array.from(document.querySelectorAll(".ant-modal button"));
          const button =
            buttons.find((node) => promptButtonScore(node) === 2) ||
            buttons.find((node) => promptButtonScore(node) === 1);
          if (button) {
            button.click();
            await sleep(120);
          }
        };
        await closeModelPromptIfOpen();
        await waitFor(
          "provider stream chat initial empty state",
          (current) =>
            current.has_textarea &&
            current.location_hash.includes(encodeURIComponent(chatId)) &&
            current.user_bubble_count === 0 &&
            current.assistant_bubble_count === 0,
          8000,
        );
        const textarea = document.querySelector('[class*="sender"] textarea');
        if (!textarea) throw new Error("provider stream textarea not found");
        setTextareaValue(textarea, promptText);
        await waitFor(
          "provider stream textarea populated",
          (current) => current.textarea_value === promptText,
          3000,
        );
        const responsesBeforeComposerChecks =
          consoleChatTransport.responses.length;
        const sender = textarea.closest(".lantai-sender");
        if (!sender) throw new Error("provider stream sender not found");
        if (typeof DataTransfer !== "function") {
          throw new Error("provider stream DataTransfer is unavailable");
        }
        const draggedFileName = "steering-drag.png";
        const draggedPngBytes = Uint8Array.from(
          atob(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Y9Z4s8AAAAASUVORK5CYII=",
          ),
          (character) => character.charCodeAt(0),
        );
        const draggedFile = new File(
          [draggedPngBytes],
          draggedFileName,
          {
            type: "image/png",
            lastModified: 1789948800000,
          },
        );
        const transfer = new DataTransfer();
        transfer.items.add(draggedFile);
        const dragoverPrevented =
          sender.dispatchEvent(
            new DragEvent("dragover", {
              bubbles: true,
              cancelable: true,
              dataTransfer: transfer,
            }),
          ) === false;
        const dropPrevented =
          sender.dispatchEvent(
            new DragEvent("drop", {
              bubbles: true,
              cancelable: true,
              dataTransfer: transfer,
            }),
          ) === false;
        const uploadedAttachment = await waitFor(
          "provider stream dragged attachment ready",
          (current) =>
            current.attachment_count === 1 &&
            current.attachment_names[0] === draggedFileName &&
            current.attachment_states[0] === "is-ready" &&
            current.textarea_value === promptText,
          10000,
        );
        sender.dispatchEvent(
          new DragEvent("drop", {
            bubbles: true,
            cancelable: true,
            dataTransfer: transfer,
          }),
        );
        await sleep(250);
        const duplicateAttachment = snapshot();
        const duplicateDeduplicated =
          duplicateAttachment.attachment_count === 1 &&
          duplicateAttachment.attachment_names[0] === draggedFileName;
        const removeAttachmentButton = Array.from(
          document.querySelectorAll(
            ".lantai-composer-attachment-action[aria-label]",
          ),
        ).find(
          (node) =>
            node.getAttribute("aria-label") ===
            "移除附件 " + draggedFileName,
        );
        if (!removeAttachmentButton) {
          throw new Error("provider stream remove attachment button not found");
        }
        removeAttachmentButton.click();
        const attachmentRemoved = await waitFor(
          "provider stream dragged attachment removed",
          (current) =>
            current.attachment_count === 0 &&
            current.textarea_value === promptText,
          3000,
        );
        const attachmentChatSubmissionCount =
          consoleChatTransport.responses.length - responsesBeforeComposerChecks;
        const compositionStart = new CompositionEvent("compositionstart", {
          bubbles: true,
          cancelable: true,
          data: "中",
        });
        textarea.dispatchEvent(compositionStart);
        const compositionEnterPrevented =
          textarea.dispatchEvent(
            new KeyboardEvent("keydown", {
              key: "Enter",
              code: "Enter",
              bubbles: true,
              cancelable: true,
              isComposing: true,
            }),
          ) === false;
        await sleep(180);
        const afterCompositionEnter = snapshot();
        const compositionEnterBlocked =
          compositionEnterPrevented &&
          afterCompositionEnter.textarea_value === promptText &&
          afterCompositionEnter.user_bubble_count === 0 &&
          afterCompositionEnter.assistant_bubble_count === 0 &&
          consoleChatTransport.responses.length ===
            responsesBeforeComposerChecks;
        textarea.dispatchEvent(
          new CompositionEvent("compositionend", {
            bubbles: true,
            cancelable: true,
            data: "中",
          }),
        );
        await sleep(80);
        const dispatchEnterSubmit = (target) =>
          target.dispatchEvent(
            new KeyboardEvent("keydown", {
              key: "Enter",
              code: "Enter",
              bubbles: true,
              cancelable: true,
            }),
          );
        dispatchEnterSubmit(textarea);
        const submissionFeedback = await (async () => {
          const localDeadline = Math.min(deadline, Date.now() + 4000);
          let user = null;
          let pending = null;
          let userVisibleMs = 0;
          let pendingVisibleMs = 0;
          while (Date.now() < localDeadline) {
            const current = snapshot();
            if (
              !user &&
              current.user_bubble_count === 1 &&
              current.user_texts.some((item) => item.includes(promptText)) &&
              current.textarea_value !== promptText
            ) {
              user = current;
              userVisibleMs = Math.round(performance.now() - startedAt);
            }
            if (
              !pending &&
              current.assistant_bubble_count === 1 &&
              current.pending_assistant_count === 0 &&
              current.composer_turn_progress_count === 1 &&
              current.composer_turn_progress_busy_count === 1 &&
              current.composer_turn_progress_status_texts.some((text) =>
                text.includes("正在发送"),
              )
            ) {
              pending = current;
              pendingVisibleMs = Math.round(performance.now() - startedAt);
            }
            if (user && pending) {
              return { user, pending, userVisibleMs, pendingVisibleMs };
            }
            await sleep(20);
          }
          throw new Error(
            "provider stream submission feedback timed out; observed=" +
              JSON.stringify({
                user: Boolean(user),
                pending: Boolean(pending),
                user_visible_ms: userVisibleMs,
                pending_visible_ms: pendingVisibleMs,
              }) +
              " snapshot=" +
              JSON.stringify(snapshot()).slice(0, 1800) +
              " transport=" +
              JSON.stringify(consoleChatTransport)
          );
        })();
        const {
          user,
          pending,
          userVisibleMs,
          pendingVisibleMs,
        } = submissionFeedback;
        await waitFor(
          "provider stream post-composition single submission",
          () =>
            consoleChatTransport.responses.length ===
            responsesBeforeComposerChecks + 1,
          3000,
        );
        const postCompositionSubmissionCount =
          consoleChatTransport.responses.length - responsesBeforeComposerChecks;
        const hasThinking = (current) =>
          current.assistant_text.includes(thinkingText) ||
          current.body_text.includes(thinkingText);
        const hasPartial = (current) =>
          current.assistant_text.includes(partialText) ||
          current.body_text.includes(partialText);
        const hasAnswer = (current) =>
          current.assistant_text.includes(answerText) ||
          current.body_text.includes(answerText);
        const hasRedirectAnswer = (current) =>
          current.assistant_text.includes(queuedThirdAnswerText) ||
          current.body_text.includes(queuedThirdAnswerText);
        const healthySingleAssistant = (current) =>
          current.assistant_bubble_count === 1 &&
          current.empty_assistant_count === 0 &&
          current.response_avatar_count <= 1 &&
          current.action_bar_count <= 1;
        const thinking = await waitFor(
          "provider stream thinking-only phase",
          (current) =>
            hasThinking(current) &&
            !hasPartial(current) &&
            !hasAnswer(current) &&
            // Raw provider reasoning is an inspectable evidence surface, but
            // it is private-by-default while the turn is live.  Require the
            // disclosure to exist exactly once without forcing it open.
            current.thinking_count === 1 &&
            current.thinking_open_count === 0 &&
            healthySingleAssistant(current),
          8000,
        );
        const thinkingVisibleMs = Math.round(performance.now() - startedAt);
        if (
          thinking.submit_button_present ||
          thinking.intent_button_present ||
          !thinking.stop_button_present ||
          thinking.stop_button_label !== "停止当前任务" ||
          thinking.stop_button_disabled
        ) {
          throw new Error(
            "provider stream empty-draft active controls mismatch; snapshot=" +
              JSON.stringify(thinking).slice(0, 1800),
          );
        }
        let populatedDraftControls = null;
        let intentMenu = null;
        const submitActiveInput = async ({
          label,
          value,
          intentLabel = "",
          expectedStatus,
        }) => {
          const before = snapshot();
          if (hasAnswer(before)) {
            throw new Error(
              label +
                " was not queued before the first answer; snapshot=" +
                JSON.stringify(before).slice(0, 1800)
            );
          }
          const queuedTextarea = document.querySelector('[class*="sender"] textarea');
          if (!queuedTextarea) throw new Error(label + " textarea not found");
          setTextareaValue(queuedTextarea, value);
          populatedDraftControls = await waitFor(
            label + " populated-draft active controls",
            (current) =>
              current.textarea_value === value &&
              current.submit_button_present &&
              current.submit_button_label === "补充当前任务" &&
              !current.submit_button_disabled &&
              current.intent_button_present &&
              current.intent_button_label === "选择运行中输入方式" &&
              !current.intent_button_disabled &&
              current.stop_button_present &&
              current.stop_button_label === "停止当前任务" &&
              !current.stop_button_disabled,
            600,
          );
          if (intentLabel) {
            const activeIntentButton = document.querySelector(
              ".lantai-sender-control-intent-btn",
            );
            if (!(activeIntentButton instanceof HTMLButtonElement)) {
              throw new Error(label + " intent selector not found");
            }
            activeIntentButton.click();
            intentMenu = await waitFor(
              label + " intent menu visible",
              (current) =>
                current.visible_menu_items.includes("立即调整方向") &&
                current.visible_menu_items.includes("排队下一任务") &&
                current.visible_menu_items.includes(intentLabel),
              1200,
            );
            const intentItem = [...document.querySelectorAll('[role="menuitem"]')]
              .filter((node) => {
                const rect = node.getBoundingClientRect();
                return rect.width > 0 && rect.height > 0;
              })
              .find((node) => (node.textContent || "").trim() === intentLabel);
            if (!(intentItem instanceof HTMLElement)) {
              throw new Error(label + " intent item not found: " + intentLabel);
            }
            intentItem.click();
          } else {
            dispatchEnterSubmit(queuedTextarea);
          }
          await waitFor(
            label + " accepted",
            (current) => current.textarea_value === "",
            1200,
          );
          return await waitFor(
            label + " receipt visible",
            (current) =>
              current.guidance_receipts.some(
                (item) =>
                  item.status === "admitted" &&
                  item.content === value &&
                  item.text === expectedStatus,
              ),
            2000,
          );
        };
        // Observe the provider's answer delta before deliberately perturbing
        // the renderer with rapid queued guidance. Otherwise those rerenders
        // can consume the entire partial-only window even though transport
        // evidence proves the partial frame arrived correctly.
        const partial = await waitFor(
          "provider stream partial answer phase",
          (current) =>
            hasThinking(current) &&
            hasPartial(current) &&
            !hasAnswer(current) &&
            healthySingleAssistant(current),
          8000,
        );
        const partialVisibleMs = Math.round(performance.now() - startedAt);
        const queuedBeforeFirstAnswer = !hasAnswer(snapshot());
        const supplementReceipt = await submitActiveInput({
          label: "provider stream second supplement",
          value: queuedSecondText,
          expectedStatus: "已接收 · 等待当前任务安全边界",
        });
        const redirectReceipt = await submitActiveInput({
          label: "provider stream third redirect",
          value: queuedThirdText,
          intentLabel: "立即调整方向",
          expectedStatus: "已接收 · 等待安全让步",
        });
        const final = await waitFor(
          "provider stream redirected final answer phase",
          (current) =>
            hasThinking(current) &&
            hasRedirectAnswer(current) &&
            !hasAnswer(current) &&
            current.assistant_bubble_count === 2 &&
            current.user_queue_statuses.some(
              (item) => item.status === "superseded",
            ) &&
            current.user_queue_statuses.some(
              (item) => item.status === "applied",
            ) &&
            current.response_avatar_count <= current.assistant_bubble_count &&
            current.action_bar_count <= current.assistant_bubble_count,
          16000,
        );
        const finalVisibleMs = Math.round(performance.now() - startedAt);
        const rapidFinal = await waitForStableSnapshot(
          "provider stream rapid queue final state",
          (current) =>
            current.user_bubble_count === 3 &&
            current.assistant_bubble_count === 2 &&
            current.queued_guidance_count === 0 &&
            current.empty_assistant_count === 0 &&
            current.response_avatar_count <= current.assistant_bubble_count &&
            current.action_bar_count <= current.assistant_bubble_count &&
            current.textarea_value === "" &&
            textsInOrder(current.user_texts, [
              promptText,
              queuedSecondText,
              queuedThirdText,
            ]) &&
            current.assistant_texts.some((text) => text.includes(partialText)) &&
            !current.assistant_texts.some((text) => text.includes(answerText)) &&
            !current.assistant_texts.some((text) =>
              text.includes(queuedSecondAnswerText),
            ) &&
            current.assistant_texts.some((text) =>
              text.includes(queuedThirdAnswerText),
            ) &&
            current.user_queue_statuses.some(
              (item) => item.status === "superseded",
            ) &&
            current.user_queue_statuses.some(
              (item) => item.status === "applied",
            ),
          45000,
        );
        const backend = await waitForBackend(
          "provider stream backend persisted rapid queue",
          (current) =>
            current.user_message_count === 3 &&
            current.assistant_message_count >= 1 &&
            textsInOrder(current.user_texts, [
              promptText,
              queuedSecondText,
              queuedThirdText,
            ]) &&
            !current.assistant_texts.some((item) => item.includes(answerText)) &&
            !current.assistant_texts.some((item) =>
              item.includes(queuedSecondAnswerText),
            ) &&
            current.assistant_texts.some((item) =>
              item.includes(queuedThirdAnswerText),
            ) &&
            current.pending_user_message === "",
          45000,
        );
        const rapidUserTextsInFifoOrder = textsInOrder(rapidFinal.user_texts, [
          promptText,
          queuedSecondText,
          queuedThirdText,
        ]);
        const backendUserTextsInFifoOrder = textsInOrder(backend.user_texts, [
          promptText,
          queuedSecondText,
          queuedThirdText,
        ]);
        const assistantTextsInFifoOrder = textsInOrder(
          rapidFinal.assistant_texts,
          [partialText, queuedThirdAnswerText],
        );
        const supplementDisposition = rapidFinal.user_queue_statuses.find(
          (item) => item.status === "superseded",
        );
        const redirectDisposition = rapidFinal.user_queue_statuses.find(
          (item) => item.status === "applied",
        );
        const oldPartialPreserved = rapidFinal.assistant_texts.some((text) =>
          text.includes(partialText),
        );
        const oldFullAnswerAbsent = !rapidFinal.assistant_texts.some((text) =>
          text.includes(answerText),
        );
        const supersededAnswerAbsent = !rapidFinal.assistant_texts.some((text) =>
          text.includes(queuedSecondAnswerText),
        );
        const redirectAnswerPresent = rapidFinal.assistant_texts.some((text) =>
          text.includes(queuedThirdAnswerText),
        );
        const conversationItemsPreserveFifoOrder =
          rapidUserTextsInFifoOrder &&
          assistantTextsInFifoOrder &&
          oldPartialPreserved &&
          oldFullAnswerAbsent &&
          supersededAnswerAbsent &&
          redirectAnswerPresent;
        return {
          ok: true,
          chat_id: chatId,
          prompt_text: promptText,
          queued_second_text: queuedSecondText,
          queued_third_text: queuedThirdText,
          thinking_text: thinkingText,
          partial_text: partialText,
          answer_text: answerText,
          user_visible_ms: userVisibleMs,
          pending_visible_ms: pendingVisibleMs,
          thinking_visible_ms: thinkingVisibleMs,
          partial_visible_ms: partialVisibleMs,
          final_visible_ms: finalVisibleMs,
          user: {
            user_bubble_count: user.user_bubble_count,
            assistant_bubble_count: user.assistant_bubble_count,
            body_text_length: user.body_text.length,
          },
          pending: {
            phase: "pending",
            feedback_visible: true,
            assistant_bubble_count: pending.assistant_bubble_count,
            pending_assistant_count: pending.pending_assistant_count,
            composer_turn_progress_count:
              pending.composer_turn_progress_count,
            composer_turn_progress_busy_count:
              pending.composer_turn_progress_busy_count,
            composer_turn_progress_status_texts:
              pending.composer_turn_progress_status_texts,
            body_text_length: pending.body_text.length,
          },
          active_controls: {
            primary_label: populatedDraftControls?.submit_button_label || "",
            intent_selector_label:
              populatedDraftControls?.intent_button_label || "",
            stop_label: populatedDraftControls?.stop_button_label || "",
            empty_draft: {
              primary_present: thinking.submit_button_present,
              intent_selector_present: thinking.intent_button_present,
              stop_present: thinking.stop_button_present,
              stop_label: thinking.stop_button_label,
              stop_disabled: thinking.stop_button_disabled,
            },
            explicit_intents: (intentMenu?.visible_menu_items || []).filter((item) =>
              ["立即调整方向", "排队下一任务"].includes(item),
            ),
          },
          input_method: {
            composition_enter_blocked: compositionEnterBlocked,
            draft_preserved:
              afterCompositionEnter.textarea_value === promptText,
            post_composition_submission_count:
              postCompositionSubmissionCount,
          },
          attachment_drag: {
            file_name: draggedFileName,
            media_type: draggedFile.type,
            dragover_prevented: dragoverPrevented,
            drop_prevented: dropPrevented,
            uploaded_ready:
              uploadedAttachment.attachment_count === 1 &&
              uploadedAttachment.attachment_states[0] === "is-ready",
            duplicate_deduplicated: duplicateDeduplicated,
            removed_cleanly: attachmentRemoved.attachment_count === 0,
            chat_submission_count: attachmentChatSubmissionCount,
          },
          thinking: {
            phase: "thinking",
            thinking_visible: true,
            assistant_bubble_count: thinking.assistant_bubble_count,
            thinking_count: thinking.thinking_count,
            thinking_open_count: thinking.thinking_open_count,
            thinking_statuses: thinking.thinking_statuses,
            response_avatar_count: thinking.response_avatar_count,
            action_bar_count: thinking.action_bar_count,
            body_text_length: thinking.body_text.length,
          },
          partial: {
            phase: "partial",
            thinking_visible: true,
            partial_visible: true,
            assistant_bubble_count: partial.assistant_bubble_count,
            thinking_count: partial.thinking_count,
            thinking_statuses: partial.thinking_statuses,
            response_avatar_count: partial.response_avatar_count,
            action_bar_count: partial.action_bar_count,
            body_text_length: partial.body_text.length,
          },
          final: {
            phase: "final",
            thinking_visible: hasThinking(final),
            redirected_answer_visible: hasRedirectAnswer(final),
            old_full_answer_visible: hasAnswer(final),
            assistant_bubble_count: final.assistant_bubble_count,
            response_avatar_count: final.response_avatar_count,
            action_bar_count: final.action_bar_count,
            empty_assistant_count: final.empty_assistant_count,
            body_text_length: final.body_text.length,
          },
          rapid_queue: {
            queued_submission_count: 2,
            queued_before_first_answer: queuedBeforeFirstAnswer,
            queued_drafts: [queuedSecondText, queuedThirdText],
            submission_intents: ["supplement", "redirect"],
            admitted_statuses: {
              supplement: supplementReceipt.guidance_receipts.find(
                (item) =>
                  item.content === queuedSecondText &&
                  item.text === "已接收 · 等待当前任务安全边界",
              ),
              redirect: redirectReceipt.guidance_receipts.find(
                (item) =>
                  item.content === queuedThirdText &&
                  item.text === "已接收 · 等待安全让步",
              ),
            },
            final: {
              user_bubble_count: rapidFinal.user_bubble_count,
              assistant_bubble_count: rapidFinal.assistant_bubble_count,
              response_avatar_count: rapidFinal.response_avatar_count,
              action_bar_count: rapidFinal.action_bar_count,
              empty_assistant_count: rapidFinal.empty_assistant_count,
              user_texts_in_fifo_order: rapidUserTextsInFifoOrder,
              assistant_texts_in_fifo_order: assistantTextsInFifoOrder,
              old_partial_preserved: oldPartialPreserved,
              old_full_answer_absent: oldFullAnswerAbsent,
              superseded_answer_absent: supersededAnswerAbsent,
              redirect_answer_present: redirectAnswerPresent,
              supplement_disposition: supplementDisposition,
              redirect_disposition: redirectDisposition,
              conversation_items_preserve_fifo_order:
                conversationItemsPreserveFifoOrder,
              body_text_length: rapidFinal.body_text.length,
            },
          },
          backend: {
            ...backend,
            user_texts_in_fifo_order: backendUserTextsInFifoOrder,
          },
          console_chat_transport: consoleChatTransport,
        };
      })()
    `,
    true,
  );
}

async function waitForProviderStopResendUi(
  win,
  {
    baseUrl,
    chatId,
    stopPromptText,
    thinkingText,
    stopAnswerText,
    resendText,
    resendAnswerText,
    timeoutMs,
  },
) {
  await win.loadURL(`${baseUrl}/#/chat/${encodeURIComponent(chatId)}`);
  return win.webContents.executeJavaScript(
    `
      (async () => {
        const baseUrl = ${JSON.stringify(baseUrl)};
        const chatId = ${JSON.stringify(chatId)};
        const stopPromptText = ${JSON.stringify(stopPromptText)};
        const thinkingText = ${JSON.stringify(thinkingText)};
        const stopAnswerText = ${JSON.stringify(stopAnswerText)};
        const resendText = ${JSON.stringify(resendText)};
        const resendAnswerText = ${JSON.stringify(resendAnswerText)};
        const timeoutMs = ${JSON.stringify(timeoutMs)};
        const deadline = Date.now() + timeoutMs;
        const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
        const stopRequests = [];
        const originalFetch = window.fetch.bind(window);
        window.fetch = async (...args) => {
          const response = await originalFetch(...args);
          const url = String(args[0] || "");
          if (url.includes("/console/chat/stop")) {
            const body = await response.clone().text().catch(() => "");
            stopRequests.push({
              url,
              status: response.status,
              body: body.slice(0, 400),
            });
          }
          return response;
        };
        const listTexts = (selector) =>
          Array.from(document.querySelectorAll(selector)).map((node) =>
            (node.textContent || "").trim()
          );
        const snapshot = () => {
          const textarea = document.querySelector('[class*="sender"] textarea');
          const userTexts = listTexts(".lantai-bubble-list-item.lantai-bubble-user");
          const assistantTexts = listTexts(".lantai-bubble-list-item.lantai-bubble-assistant");
          const thinkingNodes = Array.from(document.querySelectorAll('[data-chat-thinking="1"]'));
          const actionButton = document.querySelector(".lantai-sender-actions-btn");
          const stopButton = document.querySelector(".lantai-sender-stop-btn");
          return {
            location_hash: window.location.hash,
            has_textarea: Boolean(textarea),
            textarea_value: textarea?.value || "",
            user_texts: userTexts,
            assistant_texts: assistantTexts,
            user_bubble_count: userTexts.length,
            assistant_bubble_count: assistantTexts.length,
            empty_assistant_count: assistantTexts.filter((text) => !text).length,
            response_avatar_count: document.querySelectorAll('[data-lantai-response-avatar="1"]').length,
            action_bar_count: document.querySelectorAll('[data-lantai-response-actions="1"], .lantai-response-actions').length,
            thinking_statuses: thinkingNodes.map((node) => node.getAttribute("data-status") || ""),
            action_button_present: Boolean(actionButton),
            action_button_disabled: Boolean(actionButton?.disabled),
            action_button_label:
              actionButton?.getAttribute("aria-label") ||
              actionButton?.getAttribute("title") ||
              (actionButton?.textContent || "").trim(),
            stop_button_present: Boolean(stopButton),
            stop_button_disabled: Boolean(stopButton?.disabled),
            body_text: document.body?.textContent || "",
          };
        };
        const waitFor = async (label, predicate, limitMs = timeoutMs) => {
          const localDeadline = Math.min(deadline, Date.now() + limitMs);
          while (Date.now() < localDeadline) {
            const current = snapshot();
            if (predicate(current)) return current;
            await sleep(60);
          }
          throw new Error(label + " timed out; snapshot=" + JSON.stringify(snapshot()).slice(0, 2000));
        };
        const setTextareaValue = (textarea, value) => {
          const previousValue = textarea.value;
          const descriptor = Object.getOwnPropertyDescriptor(
            window.HTMLTextAreaElement.prototype,
            "value",
          );
          textarea.focus();
          if (descriptor?.set) descriptor.set.call(textarea, value);
          else textarea.value = value;
          textarea._valueTracker?.setValue(previousValue);
          textarea.dispatchEvent(new InputEvent("input", { bubbles: true, data: value, inputType: "insertText" }));
          textarea.dispatchEvent(new Event("change", { bubbles: true }));
        };
        const submitText = async (label, value) => {
          await waitFor(
            label + " textarea ready",
            (current) => current.has_textarea,
            8000,
          );
          const textarea = document.querySelector('[class*="sender"] textarea');
          if (!textarea) throw new Error(label + " textarea not found");
          setTextareaValue(textarea, value);
          await waitFor(label + " textarea populated", (current) => current.textarea_value === value, 2000);
          textarea.dispatchEvent(new KeyboardEvent("keydown", {
            key: "Enter",
            code: "Enter",
            bubbles: true,
            cancelable: true,
          }));
        };
        const runtimeEvents = async () => {
          const response = await fetch(
            baseUrl + "/api/chats/" + encodeURIComponent(chatId) + "/events?after_seq=0",
          );
          if (!response.ok) throw new Error("stop-resend runtime events failed: " + response.status);
          const payload = await response.json();
          return Array.isArray(payload.events) ? payload.events : [];
        };
        const terminalKind = (event) => {
          const type = String(event?.type || "");
          const status = String(event?.status || event?.payload?.final_task_status || "");
          if (type === "turn.interrupted" || status === "interrupted") return "interrupted";
          if (type === "turn.cancelled" || status === "cancelled") return "cancelled";
          return "";
        };
        const runtimeEventSummary = (events) =>
          events.map((event) => ({
            type: String(event?.type || ""),
            status: String(event?.status || event?.payload?.final_task_status || ""),
            kind: String(event?.payload?.legacy_kind || ""),
            turn_id: String(event?.turn_id || ""),
          }));
        const backendSnapshot = async () => {
          const response = await fetch(
            baseUrl + "/api/chats/" + encodeURIComponent(chatId),
          );
          if (!response.ok) throw new Error("stop-resend backend snapshot failed: " + response.status);
          const payload = await response.json();
          const messages = Array.isArray(payload.messages) ? payload.messages : [];
          const role = (message) => String(message?.role || message?.type || "");
          const text = (message) => {
            const value = message?.content ?? message?.text ?? message?.message ?? "";
            if (typeof value === "string") return value;
            if (Array.isArray(value)) return value.map((item) => item?.text || item?.content || "").join("\\n");
            return String(value?.text || value?.content || value?.output || "");
          };
          return {
            user_message_count: messages.filter((message) => role(message) === "user").length,
            assistant_message_count: messages.filter((message) => role(message) === "assistant").length,
            user_texts: messages.filter((message) => role(message) === "user").map(text),
            assistant_texts: messages.filter((message) => role(message) === "assistant").map(text),
            pending_user_message: typeof payload.pending_user_message === "string" ? payload.pending_user_message : "",
          };
        };

        await waitFor(
          "stop-resend initial empty chat",
          (current) => current.user_bubble_count === 0 && current.assistant_bubble_count === 0,
          8000,
        );
        await submitText("stop-resend first turn", stopPromptText);
        const thinking = await waitFor(
          "stop-resend thinking-only phase",
          (current) =>
            current.user_texts.some((text) => text.includes(stopPromptText)) &&
            current.body_text.includes(thinkingText) &&
            current.thinking_statuses.some((status) => status === "in_progress") &&
            current.stop_button_present &&
            !current.stop_button_disabled,
          8000,
        );
        const stopButton = document.querySelector(".lantai-sender-stop-btn");
        if (!stopButton || stopButton.disabled) {
          throw new Error("stop-resend stop button unavailable; snapshot=" + JSON.stringify(snapshot()).slice(0, 1600));
        }
        const stopButtonLabel =
          stopButton.getAttribute("aria-label") ||
          stopButton.getAttribute("title") ||
          (stopButton.textContent || "").trim();
        stopButton.click();

        let stopTerminal = "";
        while (Date.now() < deadline && !stopTerminal) {
          const events = await runtimeEvents();
          stopTerminal = events.map(terminalKind).find(Boolean) || "";
          if (!stopTerminal) await sleep(80);
        }
        if (!stopTerminal) {
          const events = await runtimeEvents();
          throw new Error(
            "stop-resend did not persist an interrupted/cancelled terminal event; diagnostics=" +
              JSON.stringify({
                window_session_id: String(window.currentSessionId || ""),
                stop_requests: stopRequests,
                runtime_events: runtimeEventSummary(events),
              }).slice(0, 2400),
          );
        }
        const stopped = await waitFor(
          "stop-resend UI settled after stop",
          (current) =>
            current.user_bubble_count === 1 &&
            !current.body_text.includes(stopAnswerText) &&
            current.empty_assistant_count === 0 &&
            !current.stop_button_present &&
            current.action_button_present &&
            !current.action_button_disabled,
          15000,
        );

        await submitText("stop-resend second turn", resendText);
        const final = await waitFor(
          "stop-resend second answer",
          (current) =>
            current.user_bubble_count === 2 &&
            current.user_texts.some((text) => text.includes(resendText)) &&
            current.assistant_texts.some((text) => text.includes(resendAnswerText)) &&
            current.empty_assistant_count === 0 &&
            current.response_avatar_count <= current.assistant_bubble_count &&
            current.action_bar_count <= current.assistant_bubble_count,
          10000,
        );
        // A resend must settle before this scenario reaches its real deadline.
        const backendDeadline = deadline;
        let backend = null;
        while (Date.now() < backendDeadline) {
          backend = await backendSnapshot();
          if (
            backend.user_message_count === 2 &&
            backend.assistant_message_count >= 1 &&
            backend.user_texts.some((text) => text.includes(resendText)) &&
            backend.assistant_texts.some((text) => text.includes(resendAnswerText)) &&
            backend.pending_user_message === ""
          ) break;
          await sleep(80);
        }
        if (!backend || backend.user_message_count !== 2 || backend.pending_user_message !== "") {
          throw new Error("stop-resend backend did not settle; backend=" + JSON.stringify(backend));
        }
        return {
          ok: true,
          chat_id: chatId,
          stop_prompt_text: stopPromptText,
          resend_text: resendText,
          stop_terminal: stopTerminal,
          stop_button_label: stopButtonLabel,
          full_stopped_answer_absent: !final.assistant_texts.some((text) => text.includes(stopAnswerText)),
          thinking: {
            assistant_bubble_count: thinking.assistant_bubble_count,
            thinking_statuses: thinking.thinking_statuses,
          },
          stopped: {
            user_bubble_count: stopped.user_bubble_count,
            assistant_bubble_count: stopped.assistant_bubble_count,
            empty_assistant_count: stopped.empty_assistant_count,
          },
          final: {
            user_bubble_count: final.user_bubble_count,
            assistant_bubble_count: final.assistant_bubble_count,
            empty_assistant_count: final.empty_assistant_count,
            response_avatar_count: final.response_avatar_count,
            action_bar_count: final.action_bar_count,
          },
          backend,
        };
      })()
    `,
    true,
  );
}

async function waitForProviderStreamRouteSwitchUi(
  win,
  {
    baseUrl,
    chatId,
    isolationChatId,
    promptText,
    thinkingText,
    answerText,
    timeoutMs,
  },
) {
  await win.loadURL(`${baseUrl}/#/chat/${encodeURIComponent(chatId)}`);
  return win.webContents.executeJavaScript(
    `
      (async () => {
        const baseUrl = ${JSON.stringify(baseUrl)};
        const chatId = ${JSON.stringify(chatId)};
        const isolationChatId = ${JSON.stringify(isolationChatId)};
        const promptText = ${JSON.stringify(promptText)};
        const thinkingText = ${JSON.stringify(thinkingText)};
        const answerText = ${JSON.stringify(answerText)};
        const timeoutMs = ${JSON.stringify(timeoutMs)};
        const deadline = Date.now() + timeoutMs;
        const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
        const chatHistoryResponses = [];
        const originalFetch = window.fetch.bind(window);
        window.fetch = async (...args) => {
          const response = await originalFetch(...args);
          const url = String(args[0] || "");
          if (url.includes("/api/chats/") && !url.includes("/events")) {
            const body = await response.clone().text().catch(() => "");
            chatHistoryResponses.push({
              url,
              status: response.status,
              body: body.slice(0, 1200),
            });
          }
          return response;
        };
        const listTexts = (selector) =>
          Array.from(document.querySelectorAll(selector)).map((node) =>
            (node.textContent || "").trim()
          );
        const snapshot = () => {
          const textarea = document.querySelector('[class*="sender"] textarea');
          const userTexts = listTexts(".lantai-bubble-list-item.lantai-bubble-user");
          const assistantTexts = listTexts(".lantai-bubble-list-item.lantai-bubble-assistant");
          return {
            location_hash: window.location.hash,
            has_textarea: Boolean(textarea),
            textarea_value: textarea?.value || "",
            user_texts: userTexts,
            assistant_texts: assistantTexts,
            user_bubble_count: userTexts.length,
            assistant_bubble_count: assistantTexts.length,
            empty_assistant_count: assistantTexts.filter((text) => !text).length,
            response_avatar_count: document.querySelectorAll('[data-lantai-response-avatar="1"]').length,
            action_bar_count: document.querySelectorAll('[data-lantai-response-actions="1"], .lantai-response-actions').length,
            window_current_session_id: String(window.currentSessionId || ""),
            pending_user_cache: Array.from(
              { length: window.sessionStorage.length },
              (_, index) => window.sessionStorage.key(index),
            )
              .filter((key) => key?.startsWith("lantai_pending_user_msg_"))
              .map((key) => ({ key, value: window.sessionStorage.getItem(key) })),
            session_alias_cache: Array.from(
              { length: window.sessionStorage.length },
              (_, index) => window.sessionStorage.key(index),
            )
              .filter((key) => key?.startsWith("lantai_session_alias_"))
              .map((key) => ({ key, value: window.sessionStorage.getItem(key) })),
            chat_history_responses: chatHistoryResponses.slice(-6),
            body_text: document.body?.textContent || "",
          };
        };
        const waitFor = async (label, predicate, limitMs = timeoutMs) => {
          const localDeadline = Math.min(deadline, Date.now() + limitMs);
          while (Date.now() < localDeadline) {
            const current = snapshot();
            if (predicate(current)) return current;
            await sleep(60);
          }
          throw new Error(label + " timed out; snapshot=" + JSON.stringify(snapshot()).slice(0, 2000));
        };
        const setTextareaValue = (textarea, value) => {
          const previousValue = textarea.value;
          const descriptor = Object.getOwnPropertyDescriptor(
            window.HTMLTextAreaElement.prototype,
            "value",
          );
          textarea.focus();
          if (descriptor?.set) descriptor.set.call(textarea, value);
          else textarea.value = value;
          textarea._valueTracker?.setValue(previousValue);
          textarea.dispatchEvent(new InputEvent("input", { bubbles: true, data: value, inputType: "insertText" }));
          textarea.dispatchEvent(new Event("change", { bubbles: true }));
        };
        await waitFor(
          "route-switch initial empty chat",
          (current) => current.user_bubble_count === 0 && current.assistant_bubble_count === 0,
          8000,
        );
        await waitFor(
          "route-switch textarea ready",
          (current) => current.has_textarea,
          8000,
        );
        const textarea = document.querySelector('[class*="sender"] textarea');
        if (!textarea) throw new Error("route-switch textarea not found");
        setTextareaValue(textarea, promptText);
        await waitFor("route-switch textarea populated", (current) => current.textarea_value === promptText, 2000);
        textarea.dispatchEvent(new KeyboardEvent("keydown", {
          key: "Enter",
          code: "Enter",
          bubbles: true,
          cancelable: true,
        }));
        const thinking = await waitFor(
          "route-switch thinking-only phase",
          (current) =>
            current.user_texts.some((text) => text.includes(promptText)) &&
            current.body_text.includes(thinkingText) &&
            !current.body_text.includes(answerText) &&
            current.assistant_bubble_count === 1,
          8000,
        );
        window.location.hash = "/chat/" + encodeURIComponent(isolationChatId);
        const isolation = await waitFor(
          "route-switch isolated chat",
          // Scope isolation to the active conversation. The full document also
          // contains sidebar titles and background-run status from other chats.
          (current) =>
            current.location_hash.includes(encodeURIComponent(isolationChatId)) &&
            current.user_bubble_count === 0 &&
            current.assistant_bubble_count === 0,
          8000,
        );
        await sleep(500);
        window.location.hash = "/chat/" + encodeURIComponent(chatId);
        const restored = await waitFor(
          "route-switch original stream restored",
          (current) =>
            current.location_hash.includes(encodeURIComponent(chatId)) &&
            current.user_bubble_count === 1 &&
            current.assistant_bubble_count === 1 &&
            current.user_texts.some((text) => text.includes(promptText)) &&
            current.assistant_texts.some((text) => text.includes(answerText)) &&
            current.empty_assistant_count === 0 &&
            current.response_avatar_count <= 1 &&
            current.action_bar_count <= 1,
          // The adversarial provider deliberately holds five stream steps for
          // 2.25s each. Prompt-fingerprint projection may retain a harmless
          // terminal prefix until the authoritative completed frame. In the
          // full stop/resend + route matrix that frame has taken up to ~25.5s
          // under Electron scheduling, so retain a bounded 35s ceiling while
          // still requiring the exact answer and cleared pending state.
          35000,
        );
        const backendSnapshot = async () => {
          const response = await fetch(
            baseUrl + "/api/chats/" + encodeURIComponent(chatId),
          );
          if (!response.ok) throw new Error("route-switch backend snapshot failed: " + response.status);
          const backendPayload = await response.json();
          const messages = Array.isArray(backendPayload.messages) ? backendPayload.messages : [];
          const role = (message) => String(message?.role || message?.type || "");
          const text = (message) => {
            const value = message?.content ?? message?.text ?? message?.message ?? "";
            if (typeof value === "string") return value;
            if (Array.isArray(value)) return value.map((item) => item?.text || item?.content || "").join("\\n");
            return String(value?.text || value?.content || value?.output || "");
          };
          const occurrenceCount = (value, needle) => {
            if (!needle) return 0;
            return String(value || "").split(needle).length - 1;
          };
          const isPersonaAside = (message) => {
            const metadata = message?.metadata;
            const originalMetadata = metadata?.metadata || metadata;
            return Boolean(originalMetadata?.ququ_delivery);
          };
          // A Ququ aside is deliberately persisted as a separate assistant
          // message so the UI can render it with the Shen Xingzhi response.
          // It is not a second model answer, even when a test provider echoes
          // the sentinel text while producing that optional aside.
          const assistantMessages = messages.filter(
            (message) => role(message) === "assistant" && !isPersonaAside(message),
          );
          const assistantTexts = assistantMessages.map(text).filter(Boolean);
          return {
            user_message_count: messages.filter((message) => role(message) === "user").length,
            // The chat API may project one canonical assistant memory record as
            // separate thinking and text messages. Validate semantic fragments
            // instead of treating that projection as duplicate turns.
            assistant_message_count: assistantMessages.length,
            thinking_occurrence_count: assistantTexts.reduce(
              (count, value) => count + occurrenceCount(value, thinkingText),
              0,
            ),
            answer_occurrence_count: assistantTexts.reduce(
              (count, value) => count + occurrenceCount(value, answerText),
              0,
            ),
            message_summaries: messages.map((message) => ({
              id: String(message?.id || message?.message_id || ""),
              role: role(message),
              turn_id: String(message?.turn_id || message?.runtime_turn_id || ""),
              text: text(message).slice(0, 240),
            })),
            pending_user_message:
              typeof backendPayload.pending_user_message === "string"
                ? backendPayload.pending_user_message
                : "",
          };
        };
        // The producer intentionally survives a route change. A provider can
        // finish its visible stream before post-reply persistence hooks
        // (memory, delivery verification and persona projection) complete.
        // Keep observing until the scenario's actual deadline: this checks
        // durability instead of mistaking that short finalising phase for a
        // lost answer.
        const backendDeadline = deadline;
        let backend = null;
        while (Date.now() < backendDeadline) {
          backend = await backendSnapshot();
          if (
            backend.user_message_count === 1 &&
            backend.assistant_message_count >= 1 &&
            backend.assistant_message_count <= 2 &&
            backend.thinking_occurrence_count === 1 &&
            backend.answer_occurrence_count === 1 &&
            backend.pending_user_message === ""
          ) break;
          await sleep(80);
        }
        if (
          !backend ||
          backend.user_message_count !== 1 ||
          backend.assistant_message_count < 1 ||
          backend.assistant_message_count > 2 ||
          backend.thinking_occurrence_count !== 1 ||
          backend.answer_occurrence_count !== 1 ||
          backend.pending_user_message !== ""
        ) {
          throw new Error("route-switch backend did not settle; backend=" + JSON.stringify(backend));
        }
        return {
          ok: true,
          chat_id: chatId,
          isolation_chat_id: isolationChatId,
          prompt_text: promptText,
          thinking: {
            user_bubble_count: thinking.user_bubble_count,
            assistant_bubble_count: thinking.assistant_bubble_count,
          },
          isolation: {
            user_bubble_count: isolation.user_bubble_count,
            assistant_bubble_count: isolation.assistant_bubble_count,
          },
          restored: {
            user_bubble_count: restored.user_bubble_count,
            assistant_bubble_count: restored.assistant_bubble_count,
            empty_assistant_count: restored.empty_assistant_count,
            response_avatar_count: restored.response_avatar_count,
            action_bar_count: restored.action_bar_count,
          },
          backend,
        };
      })()
    `,
    true,
  );
}

async function waitForBasicChatUiSend(
  win,
  { baseUrl, chatId, isolationChatId, firstText, secondText, timeoutMs },
) {
  await win.loadURL(`${baseUrl}/#/chat/${encodeURIComponent(chatId)}`);
  return win.webContents.executeJavaScript(
    `
      (async () => {
        const baseUrl = ${JSON.stringify(baseUrl)};
        const chatId = ${JSON.stringify(chatId)};
        const isolationChatId = ${JSON.stringify(isolationChatId)};
        const firstText = ${JSON.stringify(firstText)};
        const secondText = ${JSON.stringify(secondText)};
        const timeoutMs = ${JSON.stringify(timeoutMs)};
        const maxUserFeedbackMs = Math.min(3000, timeoutMs);
        const maxAssistantFeedbackMs = Math.min(15000, timeoutMs);
        const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
        const uniqueNodes = (...selectors) =>
          Array.from(
            new Set(
              selectors.flatMap((selector) =>
                Array.from(document.querySelectorAll(selector))
              )
            )
          );
        const listTexts = (selector) =>
          Array.from(document.querySelectorAll(selector)).map((node) =>
            (node.textContent || "").trim()
          );
        const snapshot = () => {
          const textarea = document.querySelector('[class*="sender"] textarea');
          const userTexts = listTexts(".lantai-bubble-list-item.lantai-bubble-user");
          const assistantTexts = listTexts(".lantai-bubble-list-item.lantai-bubble-assistant");
          const responseAvatars = uniqueNodes('[data-lantai-response-avatar="1"]');
          const actionBars = uniqueNodes('[data-lantai-response-actions="1"]', ".lantai-response-actions");
          const modalButtons = Array.from(document.querySelectorAll(".ant-modal button"));
          const submitButton = document.querySelector(".lantai-sender-actions-btn");
          const buttons = Array.from(document.querySelectorAll("button")).slice(0, 18);
          return {
            has_textarea: Boolean(textarea),
            textarea_value: textarea?.value || "",
            location_hash: window.location.hash,
            user_bubble_count: userTexts.length,
            assistant_bubble_count: assistantTexts.length,
            response_avatar_count: responseAvatars.length,
            action_bar_count: actionBars.length,
            empty_assistant_count: assistantTexts.filter((text) => !text).length,
            user_texts: userTexts,
            assistant_texts: assistantTexts,
            modal_open: Boolean(document.querySelector(".ant-modal")),
            modal_button_count: modalButtons.length,
            submit_button_present: Boolean(submitButton),
            submit_button_disabled: Boolean(submitButton?.disabled),
            submit_button_label:
              submitButton?.getAttribute("aria-label") ||
              (submitButton?.textContent || "").trim(),
            console_chat_post_count: fetchStats.console_chat_post_count,
            console_chat_post_statuses: fetchStats.console_chat_post_statuses,
            last_console_chat_body: fetchStats.last_console_chat_body,
            button_texts: buttons.map((node) => ({
              text: (node.textContent || "").trim(),
              aria: node.getAttribute("aria-label") || "",
              disabled: Boolean(node.disabled),
              class_name: node.className || "",
            })),
            body_text: document.body?.textContent || "",
          };
        };
        const waitFor = async (label, predicate, limitMs = timeoutMs) => {
          const deadline = Date.now() + limitMs;
          while (Date.now() < deadline) {
            const current = snapshot();
            if (predicate(current)) return current;
            await sleep(80);
          }
          throw new Error(
            label +
              " timed out; snapshot=" +
              JSON.stringify(snapshot()).slice(0, 2000)
          );
        };
        const waitForStableSnapshot = async (label, predicate, limitMs = 5000) => {
          const deadline = Date.now() + limitMs;
          let lastKey = "";
          let stableSamples = 0;
          while (Date.now() < deadline) {
            const current = snapshot();
            if (predicate(current)) {
              const key = JSON.stringify({
                location_hash: current.location_hash,
                textarea_value: current.textarea_value,
                user_bubble_count: current.user_bubble_count,
                assistant_bubble_count: current.assistant_bubble_count,
                response_avatar_count: current.response_avatar_count,
                action_bar_count: current.action_bar_count,
                empty_assistant_count: current.empty_assistant_count,
                user_texts: current.user_texts,
                assistant_texts: current.assistant_texts,
              });
              stableSamples = key === lastKey ? stableSamples + 1 : 1;
              lastKey = key;
              if (stableSamples >= 4) return current;
            } else {
              stableSamples = 0;
              lastKey = "";
            }
            await sleep(120);
          }
          throw new Error(
            label +
              " did not become stable; snapshot=" +
              JSON.stringify(snapshot()).slice(0, 1800)
          );
        };
        const textFromContent = (value) => {
          if (typeof value === "string") return value;
          if (Array.isArray(value)) {
            return value.map((item) => textFromContent(item)).filter(Boolean).join("\\n");
          }
          if (value && typeof value === "object") {
            return [
              value.text,
              value.content,
              value.output,
              value.value,
              value.message,
            ]
              .map((item) => textFromContent(item))
              .filter(Boolean)
              .join("\\n");
          }
          return "";
        };
        const backendSnapshot = async (targetChatId = chatId) => {
          const response = await fetch(
            \`\${baseUrl}/api/chats/\${encodeURIComponent(targetChatId)}\`,
          );
          if (!response.ok) {
            throw new Error(
              \`backend chat snapshot failed for \${targetChatId}: \${response.status}\`
            );
          }
          const payload = await response.json();
          const messages = Array.isArray(payload.messages) ? payload.messages : [];
          const roleOf = (message) => String(message?.role || message?.type || "");
          const messageText = (message) =>
            textFromContent(
              message?.content ??
                message?.text ??
                message?.message ??
                message?.output ??
                "",
            );
          const userMessages = messages.filter((message) => roleOf(message) === "user");
          const assistantMessages = messages.filter(
            (message) => roleOf(message) === "assistant",
          );
          return {
            chat_id: String(payload.id || targetChatId),
            status: String(payload.status || ""),
            pending_user_message:
              typeof payload.pending_user_message === "string"
                ? payload.pending_user_message
                : "",
            message_count: messages.length,
            user_message_count: userMessages.length,
            assistant_message_count: assistantMessages.length,
            user_texts: userMessages.map(messageText).filter(Boolean),
            assistant_texts: assistantMessages.map(messageText).filter(Boolean),
          };
        };
        const waitForBackend = async (
          label,
          predicate,
          limitMs = timeoutMs,
          targetChatId = chatId,
        ) => {
          const deadline = Date.now() + limitMs;
          let current = null;
          while (Date.now() < deadline) {
            current = await backendSnapshot(targetChatId);
            if (predicate(current)) return current;
            await sleep(100);
          }
          throw new Error(
            label +
              " timed out; backend=" +
              JSON.stringify(current).slice(0, 1800) +
              " dom=" +
              JSON.stringify(snapshot()).slice(0, 1200)
          );
        };
        const fetchStats = {
          console_chat_post_count: 0,
          console_chat_post_statuses: [],
          last_console_chat_body: "",
        };
        if (!window.__lantaiBasicExperienceFetchPatched) {
          const originalFetch = window.fetch.bind(window);
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
            let pathname = "";
            try {
              pathname = new URL(String(url), baseUrl).pathname;
            } catch {
              pathname = String(url);
            }
            const isConsoleChatPost =
              method === "POST" &&
              (pathname.endsWith("/api/console/chat") ||
                pathname.endsWith("/console/chat"));
            if (isConsoleChatPost) {
              fetchStats.console_chat_post_count += 1;
            }
            const response = await originalFetch(...args);
            if (isConsoleChatPost) {
              fetchStats.console_chat_post_statuses.push(response.status);
              void response
                .clone()
                .text()
                .then((body) => {
                  fetchStats.last_console_chat_body = body.slice(-12000);
                })
                .catch(() => {});
            }
            return response;
          };
          window.__lantaiBasicExperienceFetchPatched = true;
        }
        const setTextareaValue = (textarea, value) => {
          const previousValue = textarea.value;
          textarea.focus();
          textarea.select();
          const inserted =
            document.queryCommandSupported?.("insertText") &&
            document.execCommand("insertText", false, value) &&
            textarea.value === value;
          if (!inserted) {
            const descriptor = Object.getOwnPropertyDescriptor(
              window.HTMLTextAreaElement.prototype,
              "value",
            );
            if (descriptor?.set) {
              descriptor.set.call(textarea, value);
            } else {
              textarea.value = value;
            }
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
        const promptButtonScore = (node) => {
          const text = (node.textContent || "").trim();
          const compact = text.replace(/\\s+/g, "");
          const lower = text.toLowerCase();
          if (
            compact.includes("跳过") ||
            compact.includes("稍后") ||
            lower.includes("skip") ||
            lower.includes("later")
          ) {
            return 2;
          }
          if (
            compact.includes("配置模型") ||
            compact.includes("配置") ||
            lower.includes("configure")
          ) {
            return 1;
          }
          return 0;
        };
        const closeModelPromptIfOpen = async () => {
          const buttons = Array.from(document.querySelectorAll(".ant-modal button"));
          const button =
            buttons.find((node) => promptButtonScore(node) === 2) ||
            buttons.find((node) => promptButtonScore(node) === 1);
          if (button) {
            button.click();
            await sleep(120);
          }
        };
        await closeModelPromptIfOpen();
        const initial = await waitFor(
          "fresh basic chat initial empty state",
          (current) =>
            current.has_textarea &&
            current.location_hash.includes(encodeURIComponent(chatId)) &&
            current.user_bubble_count === 0 &&
            current.assistant_bubble_count === 0,
          8000,
        );
        const initialBackend = await waitForBackend(
          "fresh basic chat backend empty state",
          (current) =>
            current.message_count === 0 && current.pending_user_message === "",
          8000,
        );
        // A turn may have its normal answer plus one separately persisted
        // persona/inner-voice line.  The lifecycle probe must verify the two
        // requested answers, rather than treating that optional presentation
        // layer as a duplicate response.
        const submitOne = async (
          text,
          expectedUserCount,
          minAssistantCount,
          maxAssistantCount,
          expectedAnswerText,
        ) => {
          await closeModelPromptIfOpen();
          const ready = await waitFor(
            "chat textarea before submit " + text,
            (current) => current.has_textarea,
          );
          const textarea = document.querySelector('[class*="sender"] textarea');
          if (!textarea) throw new Error("chat textarea disappeared before submit");
          setTextareaValue(textarea, text);
          textarea.focus();
          await waitFor(
            "textarea value before submit " + text,
            (current) => current.textarea_value === text,
          );
          const startedAt = performance.now();
          const dispatchEnterSubmit = () =>
            textarea.dispatchEvent(
              new KeyboardEvent("keydown", {
                key: "Enter",
                code: "Enter",
                bubbles: true,
                cancelable: true,
              }),
            );
          dispatchEnterSubmit();
          await sleep(80);
          const afterEnter = snapshot();
          if (afterEnter.user_bubble_count < expectedUserCount) {
            const submitButton = document.querySelector(".lantai-sender-actions-btn");
            if (!submitButton) {
              throw new Error(
                "chat submit button disappeared before fallback click " +
                  text +
                  "; snapshot=" +
                  JSON.stringify(snapshot()).slice(0, 1800)
              );
            }
            if (submitButton.disabled) {
              throw new Error(
                "chat submit button was disabled before fallback click " +
                  text +
                  "; snapshot=" +
                  JSON.stringify(snapshot()).slice(0, 1800)
              );
            }
            const submitLabel =
              submitButton.getAttribute("aria-label") ||
              (submitButton.textContent || "").trim();
            // Clearing the draft while the sender still says "停止" means the
            // message was accepted into the FIFO queue. Clicking that control
            // would cancel the active turn instead of submitting the draft.
            if (
              afterEnter.textarea_value === text &&
              submitLabel.replace(/\\s+/g, "") === "发送"
            ) {
              submitButton.click();
            }
          }
          const userVisible = await waitFor(
            "user bubble visible after submit " + text,
            (current) =>
              current.user_bubble_count === expectedUserCount &&
              current.user_texts.some((item) => item.includes(text)) &&
              current.textarea_value !== text,
          );
          const userVisibleMs = Math.round(performance.now() - startedAt);
          const assistantVisible = await waitFor(
            "assistant feedback visible after submit " + text,
            (current) =>
              current.user_bubble_count === expectedUserCount &&
              current.assistant_bubble_count >= minAssistantCount &&
              current.assistant_bubble_count <= maxAssistantCount &&
              current.user_texts.some((item) => item.includes(text)) &&
              current.empty_assistant_count === 0 &&
              current.response_avatar_count <= current.assistant_bubble_count &&
              current.action_bar_count <= current.assistant_bubble_count &&
              current.assistant_texts.every((item) => item.length > 0) &&
              current.assistant_texts.some((item) =>
                item.includes(expectedAnswerText),
              ),
          );
          const assistantVisibleMs = Math.round(performance.now() - startedAt);
          if (userVisibleMs > maxUserFeedbackMs) {
            throw new Error(
              "user message feedback was too slow after submit " +
                text +
                ": " +
                userVisibleMs +
                "ms snapshot=" +
                JSON.stringify(assistantVisible).slice(0, 1800)
            );
          }
          if (assistantVisibleMs > maxAssistantFeedbackMs) {
            throw new Error(
              "assistant feedback was too slow after submit " +
                text +
                ": " +
                assistantVisibleMs +
                "ms snapshot=" +
                JSON.stringify(assistantVisible).slice(0, 1800)
            );
          }
          let backend = await backendSnapshot();
          const backendTurnObserved =
            fetchStats.console_chat_post_count >= expectedUserCount ||
            backend.user_message_count >= expectedUserCount ||
            backend.pending_user_message !== "";
          if (backendTurnObserved) {
            backend = await waitForBackend(
              "backend persisted after submit " + text,
              (current) =>
                current.user_message_count === expectedUserCount &&
                current.assistant_message_count >= minAssistantCount &&
                current.user_texts.some((item) => item.includes(text)) &&
                current.assistant_texts.every((item) => item.length > 0) &&
                current.assistant_texts.some((item) =>
                  item.includes(expectedAnswerText),
                ) &&
                current.pending_user_message === "",
            );
          }
          await waitFor(
            "sender ready after completed submit " + text,
            (current) =>
              current.submit_button_label.replace(/\\s+/g, "") === "发送" &&
              !current.submit_button_disabled &&
              current.textarea_value === "",
            5000,
          );
          await closeModelPromptIfOpen();
          return {
            user_visible_ms: userVisibleMs,
            assistant_visible_ms: assistantVisibleMs,
            user_bubble_count: assistantVisible.user_bubble_count,
            assistant_bubble_count: assistantVisible.assistant_bubble_count,
            response_avatar_count: assistantVisible.response_avatar_count,
            action_bar_count: assistantVisible.action_bar_count,
            empty_assistant_count: assistantVisible.empty_assistant_count,
            body_text_length: assistantVisible.body_text.length,
            before_submit_textarea_ready: ready.has_textarea,
            after_user_bubble_textarea_value: userVisible.textarea_value,
            backend,
            backend_turn_observed: backendTurnObserved,
            console_chat_post_count: fetchStats.console_chat_post_count,
          };
        };

        const firstAnswerText = "PACKAGED_MODEL_REPLY: " + firstText;
        const secondAnswerText = "PACKAGED_MODEL_REPLY: " + secondText;
        const first = await submitOne(firstText, 1, 1, 2, firstAnswerText);
        const second = await submitOne(secondText, 2, 2, 4, secondAnswerText);
        const final = snapshot();
        const answerOccurrenceCount = (needle) =>
          final.assistant_texts.reduce(
            (total, text) =>
              total + (needle ? text.split(needle).length - 1 : 0),
            0,
          );
        const firstAnswerOccurrenceCount = answerOccurrenceCount(firstAnswerText);
        const secondAnswerOccurrenceCount = answerOccurrenceCount(secondAnswerText);
        if (!final.user_texts.some((item) => item.includes(firstText))) {
          throw new Error("first submitted user text disappeared after second turn");
        }
        if (!final.user_texts.some((item) => item.includes(secondText))) {
          throw new Error("second submitted user text did not remain visible");
        }
        if (final.response_avatar_count > final.assistant_bubble_count) {
          throw new Error("response avatars outnumber assistant bubbles");
        }
        if (final.user_bubble_count !== 2) {
          throw new Error(
            "basic UI send should contain exactly two user bubbles; snapshot=" +
              JSON.stringify(final).slice(0, 1800)
          );
        }
        if (
          final.assistant_bubble_count < 2 ||
          final.assistant_bubble_count > 4 ||
          !final.assistant_texts.some((item) => item.includes(firstAnswerText)) ||
          !final.assistant_texts.some((item) => item.includes(secondAnswerText))
        ) {
          throw new Error(
            "basic UI send should preserve two primary answers and at most one optional persona line per turn; snapshot=" +
              JSON.stringify(final).slice(0, 1800)
          );
        }
        if (final.action_bar_count > final.assistant_bubble_count) {
          throw new Error("response action bars outnumber assistant bubbles");
        }
        if (final.empty_assistant_count !== 0) {
          throw new Error("empty assistant bubbles remained after basic send");
        }
        if (
          firstAnswerOccurrenceCount !== 1 ||
          secondAnswerOccurrenceCount !== 1
        ) {
          throw new Error(
            "each logical answer must render exactly once; snapshot=" +
              JSON.stringify(final).slice(0, 1800),
          );
        }
        const finalStable = await waitForStableSnapshot(
          "final two-turn basic chat state",
          (current) =>
            current.user_bubble_count === 2 &&
            current.assistant_bubble_count >= 2 &&
            current.assistant_bubble_count <= 4 &&
            current.empty_assistant_count === 0 &&
            current.textarea_value === "" &&
            current.user_texts.some((item) => item.includes(firstText)) &&
            current.user_texts.some((item) => item.includes(secondText)) &&
            current.assistant_texts.every((item) => item.length > 0) &&
            current.assistant_texts.some((item) => item.includes(firstAnswerText)) &&
            current.assistant_texts.some((item) => item.includes(secondAnswerText)),
        );
        const backendTurnObserved =
          first.backend_turn_observed ||
          second.backend_turn_observed ||
          fetchStats.console_chat_post_count > 0;
        const finalBackend = backendTurnObserved
          ? await waitForBackend(
              "final two-turn backend persisted state",
              (current) =>
                current.user_message_count === 2 &&
                current.assistant_message_count >= 2 &&
                current.pending_user_message === "" &&
                current.user_texts.some((item) => item.includes(firstText)) &&
                current.user_texts.some((item) => item.includes(secondText)) &&
                current.assistant_texts.every((item) => item.length > 0),
              8000,
            )
          : await waitForBackend(
              "final local preflight failure kept backend chat empty",
              (current) =>
                current.message_count === 0 && current.pending_user_message === "",
              2000,
            );
        let isolation = null;
        let restored = null;
        let isolationBackend = null;
        let restoredBackend = null;
        if (isolationChatId) {
          window.location.hash = "/chat/" + encodeURIComponent(isolationChatId);
          isolation = await waitFor(
            "empty isolated chat after route switch",
            (current) =>
              current.location_hash.includes(encodeURIComponent(isolationChatId)) &&
              current.user_bubble_count === 0 &&
              current.assistant_bubble_count === 0 &&
              !current.user_texts.some((item) => item.includes(firstText) || item.includes(secondText)) &&
              !current.assistant_texts.some((item) => item.includes(firstText) || item.includes(secondText)),
            8000,
          );
          isolationBackend = await waitForBackend(
            "empty isolated backend chat after route switch",
            (current) =>
              current.message_count === 0 && current.pending_user_message === "",
            8000,
            isolationChatId,
          );
          window.location.hash = "/chat/" + encodeURIComponent(chatId);
          restored = await waitFor(
            "original chat restored after isolated route switch",
            (current) =>
              current.location_hash.includes(encodeURIComponent(chatId)) &&
              current.user_bubble_count === 2 &&
              current.assistant_bubble_count >= 2 &&
              current.assistant_bubble_count <= 4 &&
              current.empty_assistant_count === 0 &&
              current.user_texts.some((item) => item.includes(firstText)) &&
              current.user_texts.some((item) => item.includes(secondText)) &&
              current.assistant_texts.some((item) => item.includes(firstAnswerText)) &&
              current.assistant_texts.some((item) => item.includes(secondAnswerText)),
            8000,
          );
          restoredBackend = backendTurnObserved
            ? await waitForBackend(
                "original backend chat restored after isolated route switch",
                (current) =>
                  current.user_message_count === 2 &&
                  current.assistant_message_count >= 2 &&
                  current.pending_user_message === "",
                8000,
              )
            : await waitForBackend(
                "original backend chat remained empty after local preflight failure",
                (current) =>
                  current.message_count === 0 && current.pending_user_message === "",
                2000,
              );
        }
        const experienceMatrix = [
          {
            id: "initial_empty_chat_dom",
            passed:
              initial.user_bubble_count === 0 && initial.assistant_bubble_count === 0,
          },
          {
            id: "initial_empty_chat_backend",
            passed:
              initialBackend.message_count === 0 &&
              initialBackend.pending_user_message === "",
          },
          {
            id: "first_user_feedback_within_budget",
            passed: first.user_visible_ms <= maxUserFeedbackMs,
          },
          {
            id: "first_assistant_feedback_within_budget",
            passed: first.assistant_visible_ms <= maxAssistantFeedbackMs,
          },
          {
            id: "second_user_feedback_within_budget",
            passed: second.user_visible_ms <= maxUserFeedbackMs,
          },
          {
            id: "second_assistant_feedback_within_budget",
            passed: second.assistant_visible_ms <= maxAssistantFeedbackMs,
          },
          {
            id: "two_primary_answers_with_optional_persona_lines",
            passed:
              finalStable.user_bubble_count === 2 &&
              finalStable.assistant_bubble_count >= 2 &&
              finalStable.assistant_bubble_count <= 4,
          },
          {
            id: "no_empty_assistant_bubbles",
            passed: finalStable.empty_assistant_count === 0,
          },
          {
            id: "no_duplicate_assistant_chrome",
            passed:
              finalStable.response_avatar_count <=
                finalStable.assistant_bubble_count &&
              finalStable.action_bar_count <= finalStable.assistant_bubble_count,
          },
          {
            id: "each_answer_rendered_exactly_once",
            passed:
              firstAnswerOccurrenceCount === 1 &&
              secondAnswerOccurrenceCount === 1,
          },
          {
            id: "textarea_cleared_after_submit",
            passed: finalStable.textarea_value === "",
          },
          {
            id: "backend_state_consistent_with_submission_mode",
            passed: backendTurnObserved
              ? finalBackend.user_message_count === 2 &&
                finalBackend.assistant_message_count >= 2
              : finalBackend.message_count === 0 &&
                fetchStats.console_chat_post_count === 0,
          },
          {
            id: "backend_pending_cleared",
            passed: finalBackend.pending_user_message === "",
          },
          {
            id: "route_isolation_empty_dom",
            passed:
              !isolation ||
              (isolation.user_bubble_count === 0 &&
                isolation.assistant_bubble_count === 0),
          },
          {
            id: "route_isolation_empty_backend",
            passed:
              !isolationBackend ||
              (isolationBackend.message_count === 0 &&
                isolationBackend.pending_user_message === ""),
          },
          {
            id: "route_restore_preserved_dom",
            passed:
              !restored ||
              (restored.user_bubble_count === 2 &&
                restored.assistant_bubble_count >= 2 &&
                restored.assistant_bubble_count <= 4),
          },
          {
            id: "route_restore_preserved_backend",
            passed:
              !restoredBackend ||
              (backendTurnObserved
                ? restoredBackend.user_message_count === 2 &&
                  restoredBackend.assistant_message_count >= 2
                : restoredBackend.message_count === 0),
          },
          {
            id: "final_dom_stable",
            passed:
              finalStable.user_bubble_count === final.user_bubble_count &&
              finalStable.assistant_bubble_count === final.assistant_bubble_count,
          },
        ];
        const failedExperienceChecks = experienceMatrix.filter((item) => !item.passed);
        if (failedExperienceChecks.length > 0) {
          throw new Error(
            "basic chat experience matrix failed: " +
              JSON.stringify(failedExperienceChecks)
          );
        }
        return {
          ok: true,
          chat_id: chatId,
          isolation_chat_id: isolationChatId || null,
          first_text: firstText,
          second_text: secondText,
          max_user_feedback_ms: maxUserFeedbackMs,
          max_assistant_feedback_ms: maxAssistantFeedbackMs,
          backend_turn_observed: backendTurnObserved,
          console_chat_post_count: fetchStats.console_chat_post_count,
          console_chat_post_statuses: fetchStats.console_chat_post_statuses,
          experience_matrix: experienceMatrix,
          initial: {
            user_bubble_count: initial.user_bubble_count,
            assistant_bubble_count: initial.assistant_bubble_count,
            response_avatar_count: initial.response_avatar_count,
            action_bar_count: initial.action_bar_count,
            empty_assistant_count: initial.empty_assistant_count,
            body_text_length: initial.body_text.length,
          },
          initial_backend: initialBackend,
          first,
          second,
          final: {
            user_bubble_count: final.user_bubble_count,
            assistant_bubble_count: final.assistant_bubble_count,
            response_avatar_count: final.response_avatar_count,
            action_bar_count: final.action_bar_count,
            empty_assistant_count: final.empty_assistant_count,
            body_text_length: final.body_text.length,
            user_texts: final.user_texts,
            assistant_texts: final.assistant_texts,
          },
          final_stable: {
            user_bubble_count: finalStable.user_bubble_count,
            assistant_bubble_count: finalStable.assistant_bubble_count,
            response_avatar_count: finalStable.response_avatar_count,
            action_bar_count: finalStable.action_bar_count,
            empty_assistant_count: finalStable.empty_assistant_count,
            textarea_value: finalStable.textarea_value,
            body_text_length: finalStable.body_text.length,
          },
          final_backend: finalBackend,
          isolation: isolation
            ? {
                chat_id: isolationChatId,
                user_bubble_count: isolation.user_bubble_count,
                assistant_bubble_count: isolation.assistant_bubble_count,
                response_avatar_count: isolation.response_avatar_count,
                action_bar_count: isolation.action_bar_count,
                empty_assistant_count: isolation.empty_assistant_count,
                body_text_length: isolation.body_text.length,
              }
            : null,
          isolation_backend: isolationBackend,
          restored: restored
            ? {
                chat_id: chatId,
                user_bubble_count: restored.user_bubble_count,
                assistant_bubble_count: restored.assistant_bubble_count,
                response_avatar_count: restored.response_avatar_count,
                action_bar_count: restored.action_bar_count,
                empty_assistant_count: restored.empty_assistant_count,
                body_text_length: restored.body_text.length,
              }
            : null,
          restored_backend: restoredBackend,
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
  const basicUiChatId = String(args["basic-ui-chat-id"] || "");
  const basicUiIsolationChatId = String(
    args["basic-ui-isolation-chat-id"] || "",
  );
  const basicUiFirstText = String(args["basic-ui-first-text"] || "");
  const basicUiSecondText = String(args["basic-ui-second-text"] || "");
  const providerStreamChatId = String(args["provider-stream-chat-id"] || "");
  const providerStreamPrompt = String(args["provider-stream-prompt"] || "");
  const providerStreamThinkingText = String(
    args["provider-stream-thinking-text"] || "",
  );
  const providerStreamPartialText = String(
    args["provider-stream-partial-text"] || "",
  );
  const providerStreamAnswerText = String(
    args["provider-stream-answer-text"] || "",
  );
  const providerStreamQueuedSecondText = String(
    args["provider-stream-queued-second-text"] || "",
  );
  const providerStreamQueuedThirdText = String(
    args["provider-stream-queued-third-text"] || "",
  );
  const providerStopChatId = String(args["provider-stop-chat-id"] || "");
  const providerStopPrompt = String(args["provider-stop-prompt"] || "");
  const providerStopAnswerText = String(
    args["provider-stop-answer-text"] || "",
  );
  const providerStopResendText = String(
    args["provider-stop-resend-text"] || "",
  );
  const providerStopResendAnswerText = String(
    args["provider-stop-resend-answer-text"] || "",
  );
  const providerSwitchChatId = String(args["provider-switch-chat-id"] || "");
  const providerSwitchIsolationChatId = String(
    args["provider-switch-isolation-chat-id"] || "",
  );
  const providerSwitchPrompt = String(args["provider-switch-prompt"] || "");
  const providerSwitchAnswerText = String(
    args["provider-switch-answer-text"] || "",
  );
  const expectedTerminalType = String(
    args["expected-terminal-type"] || "turn.failed",
  );
  const expectedTerminalStatuses = String(
    args["expected-terminal-statuses"] || "failed",
  )
    .split(",")
    .map((status) => status.trim())
    .filter(Boolean);
  const timeoutMs = Number(args["timeout-ms"] || 30000);
  if (!baseUrl || !desktopToken || !chatId) {
    throw new Error("--base-url, --desktop-token and --chat-id are required");
  }
  if (
    [
      basicUiChatId,
      basicUiIsolationChatId,
      basicUiFirstText,
      basicUiSecondText,
    ].some(Boolean) &&
    !(
      basicUiChatId &&
      basicUiIsolationChatId &&
      basicUiFirstText &&
      basicUiSecondText
    )
  ) {
    throw new Error(
      "--basic-ui-chat-id, --basic-ui-isolation-chat-id, --basic-ui-first-text and --basic-ui-second-text must be provided together",
    );
  }
  if (
    [
      providerStreamChatId,
      providerStreamPrompt,
      providerStreamThinkingText,
      providerStreamPartialText,
      providerStreamAnswerText,
      providerStreamQueuedSecondText,
      providerStreamQueuedThirdText,
    ].some(Boolean) &&
    !(
      providerStreamChatId &&
      providerStreamPrompt &&
      providerStreamThinkingText &&
      providerStreamPartialText &&
      providerStreamAnswerText &&
      providerStreamQueuedSecondText &&
      providerStreamQueuedThirdText
    )
  ) {
    throw new Error(
      "--provider-stream-chat-id, --provider-stream-prompt, --provider-stream-thinking-text, --provider-stream-partial-text, --provider-stream-answer-text, --provider-stream-queued-second-text and --provider-stream-queued-third-text must be provided together",
    );
  }
  if (
    [
      providerStopChatId,
      providerStopPrompt,
      providerStopAnswerText,
      providerStopResendText,
      providerStopResendAnswerText,
    ].some(Boolean) &&
    !(
      providerStopChatId &&
      providerStopPrompt &&
      providerStopAnswerText &&
      providerStopResendText &&
      providerStopResendAnswerText &&
      providerStreamThinkingText
    )
  ) {
    throw new Error(
      "--provider-stop-chat-id, --provider-stop-prompt, --provider-stop-answer-text, --provider-stop-resend-text, --provider-stop-resend-answer-text and --provider-stream-thinking-text must be provided together",
    );
  }
  if (
    [
      providerSwitchChatId,
      providerSwitchIsolationChatId,
      providerSwitchPrompt,
      providerSwitchAnswerText,
    ].some(Boolean) &&
    !(
      providerSwitchChatId &&
      providerSwitchIsolationChatId &&
      providerSwitchPrompt &&
      providerSwitchAnswerText &&
      providerStreamThinkingText
    )
  ) {
    throw new Error(
      "--provider-switch-chat-id, --provider-switch-isolation-chat-id, --provider-switch-prompt, --provider-switch-answer-text and --provider-stream-thinking-text must be provided together",
    );
  }

  const origin = new URL(baseUrl).origin;
  const streamUrls = [];
  session.defaultSession.webRequest.onBeforeSendHeaders(
    { urls: [`${origin}/*`] },
    (details, callback) => {
      const url = new URL(details.url);
      if (url.pathname.startsWith("/api/")) {
        details.requestHeaders["X-Lantai-Desktop-Token"] = desktopToken;
      }
      if (url.pathname.includes("/events/stream")) {
        streamUrls.push({ url: details.url, status: "sent" });
      }
      callback({ requestHeaders: details.requestHeaders });
    },
  );
  session.defaultSession.webRequest.onCompleted(
    { urls: [`${origin}/api/chats/*/events/stream*`] },
    (details) => {
      streamUrls.push({ url: details.url, status: details.statusCode });
    },
  );

  const visibleThinkingSettings = await enableVisibleThinking({
    baseUrl,
    desktopToken,
  });
  const trace = (stage) => {
    process.stderr.write(`[chat-runtime-smoke] ${stage}\n`);
  };

  // Each scenario has its own renderer document. A previous chat route keeps a
  // long-lived event stream open, so sharing one window made later route loads
  // intermittently stall before the target chat could subscribe.
  const runInFreshWindow = async (runScenario) => {
    const scenarioWindow = createWindow();
    try {
      return await runScenario(scenarioWindow);
    } finally {
      if (!scenarioWindow.isDestroyed()) {
        scenarioWindow.destroy();
      }
    }
  };

  const win = createWindow();
  try {
    trace("base-stream:start");
    await win.loadURL(`${baseUrl}/#/chat/${encodeURIComponent(chatId)}`);
    const payload = await win.webContents.executeJavaScript(
      `
        (async () => {
          const baseUrl = ${JSON.stringify(baseUrl)};
          const chatId = ${JSON.stringify(chatId)};
          const expectedTerminalType = ${JSON.stringify(expectedTerminalType)};
          const expectedTerminalStatuses = new Set(${JSON.stringify(expectedTerminalStatuses)});
          const timeoutMs = ${JSON.stringify(timeoutMs)};
          const deadline = Date.now() + timeoutMs;

          const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
          const parseFrames = (text, flush = false) => {
            const payloads = [];
            let rest = text;
            let separator = rest.indexOf("\\n\\n");
            while (separator !== -1) {
              const frame = rest.slice(0, separator);
              rest = rest.slice(separator + 2);
              const data = frame
                .split(/\\r?\\n/)
                .filter((line) => line.startsWith("data:"))
                .map((line) => line.slice(5).trimStart())
                .join("\\n")
                .trim();
              if (data) payloads.push(data);
              separator = rest.indexOf("\\n\\n");
            }
            if (flush && rest.trim()) {
              const data = rest
                .split(/\\r?\\n/)
                .filter((line) => line.startsWith("data:"))
                .map((line) => line.slice(5).trimStart())
                .join("\\n")
                .trim();
              if (data) payloads.push(data);
              rest = "";
            }
            return { payloads, rest };
          };
          const eventsFromPayloads = (payloads) => payloads
            .flatMap((payload) => Array.isArray(payload.events) ? payload.events : [])
            .sort((a, b) => Number(a.seq || 0) - Number(b.seq || 0));
          const isTerminal = (event) => {
            const type = String(event?.type || "");
            const status = String(event?.status || "");
            const finalTaskStatus = String(event?.payload?.final_task_status || "");
            return [
              "turn.completed",
              "turn.failed",
              "turn.cancelled",
              "turn.interrupted",
              "turn.stalled",
            ].includes(type) || [
              "completed",
              "failed",
              "finished",
              "cancelled",
              "interrupted",
              "stalled",
            ].includes(status) || [
              "completed",
              "failed",
              "finished",
              "cancelled",
              "interrupted",
              "stalled",
            ].includes(finalTaskStatus);
          };
          const latestCursor = (payloads) => payloads.reduce((cursor, payload) => {
            const value = Number(payload.next_after_seq ?? payload.latest_seq ?? 0);
            return Number.isFinite(value) ? Math.max(cursor, value) : cursor;
          }, 0);
          const readStream = async (afterSeq, stopWhen, streamEpoch = "") => {
            const controller = new AbortController();
            const timer = setTimeout(
              () => controller.abort(),
              Math.max(1, deadline - Date.now()),
            );
            try {
              const query = new URLSearchParams({ after_seq: String(afterSeq) });
              if (streamEpoch) query.set("stream_epoch", streamEpoch);
              const headers = streamEpoch
                ? { "Last-Event-ID": \`\${streamEpoch}:\${afterSeq}\` }
                : undefined;
              const response = await fetch(
                \`\${baseUrl}/api/chats/\${encodeURIComponent(chatId)}/events/stream?\${query.toString()}\`,
                { signal: controller.signal, headers },
              );
              if (!response.ok || !response.body) {
                throw new Error(\`stream failed: \${response.status}\`);
              }
              const reader = response.body.getReader();
              const decoder = new TextDecoder();
              let buffer = "";
              const payloads = [];
              while (Date.now() < deadline) {
                const { done, value } = await reader.read();
                if (done) {
                  buffer += decoder.decode();
                  const parsed = parseFrames(buffer, true);
                  buffer = parsed.rest;
                  payloads.push(...parsed.payloads.map((raw) => JSON.parse(raw)));
                  break;
                }
                buffer += decoder.decode(value, { stream: true });
                const parsed = parseFrames(buffer);
                buffer = parsed.rest;
                payloads.push(...parsed.payloads.map((raw) => JSON.parse(raw)));
                if (stopWhen(payloads)) {
                  controller.abort();
                  return payloads;
                }
              }
              controller.abort();
              throw new Error(\`timed out waiting for stream after_seq=\${afterSeq}\`);
            } catch (error) {
              if (error && error.name === "AbortError") {
                throw new Error(\`timed out waiting for stream after_seq=\${afterSeq}\`);
              }
              throw error;
            } finally {
              clearTimeout(timer);
            }
          };

          const firstPayloads = await readStream(0, (payloads) => eventsFromPayloads(payloads).length > 0);
          const firstEvents = eventsFromPayloads(firstPayloads);
          if (firstEvents.length === 0) {
            throw new Error("first renderer stream yielded no runtime events");
          }
          const streamEpochs = new Set(
            firstPayloads
              .map((payload) => String(payload.stream_epoch || ""))
              .filter(Boolean),
          );
          if (streamEpochs.size !== 1) {
            throw new Error(\`first renderer stream had invalid epochs: \${JSON.stringify(firstPayloads)}\`);
          }
          const streamEpoch = Array.from(streamEpochs)[0];
          const firstTerminal = [...firstEvents].reverse().find(isTerminal);
          const reconnectAfter = firstTerminal
            ? Math.max(0, Number(firstTerminal.seq || 0) - 1)
            : latestCursor(firstPayloads);
          await sleep(20);
          const reconnectPayloads = await readStream(
            reconnectAfter,
            (payloads) => eventsFromPayloads(payloads).some(isTerminal),
            streamEpoch,
          );
          if (
            reconnectPayloads.some(
              (payload) => String(payload.stream_epoch || "") !== streamEpoch,
            )
          ) {
            throw new Error(\`renderer reconnect changed stream epoch: \${JSON.stringify(reconnectPayloads)}\`);
          }
          const reconnectEvents = eventsFromPayloads(reconnectPayloads);
          const staleReplay = reconnectEvents.find((event) => Number(event.seq || 0) <= reconnectAfter);
          if (staleReplay) {
            throw new Error(\`renderer stream replayed stale seq \${staleReplay.seq} after \${reconnectAfter}\`);
          }
          const terminal = [...reconnectEvents].reverse().find(isTerminal);
          if (!terminal) {
            throw new Error("renderer reconnect did not recover a terminal runtime event");
          }
          if (
            terminal.type !== expectedTerminalType ||
            !expectedTerminalStatuses.has(String(terminal.status || ""))
          ) {
            throw new Error(
              \`unexpected terminal event expected=\${expectedTerminalType}/\${Array.from(expectedTerminalStatuses).join(",")} actual=\${JSON.stringify(terminal)}\`,
            );
          }
          const finalCursor = Math.max(latestCursor(firstPayloads), latestCursor(reconnectPayloads));
          const noReplay = await fetch(
            \`\${baseUrl}/api/chats/\${encodeURIComponent(chatId)}/events?after_seq=\${finalCursor}\`,
          ).then((response) => response.json());
          if (Array.isArray(noReplay.events) && noReplay.events.length > 0) {
            throw new Error(\`renderer latest cursor replayed duplicate events \${JSON.stringify(noReplay)}\`);
          }
          return {
            ok: true,
            chat_id: chatId,
            stream_epoch: streamEpoch,
            first_after_seq: 0,
            first_event_count: firstEvents.length,
            first_latest_seq: latestCursor(firstPayloads),
            reconnect_after_seq: reconnectAfter,
            reconnect_event_count: reconnectEvents.length,
            reconnect_latest_seq: latestCursor(reconnectPayloads),
            terminal_type: terminal.type,
            terminal_status: terminal.status,
            no_replay_after_seq: finalCursor,
            no_replay_event_count: Array.isArray(noReplay.events) ? noReplay.events.length : null,
          };
        })()
      `,
      true,
    );
    assert.equal(
      payload.ok,
      true,
      `Unexpected renderer stream payload: ${JSON.stringify(payload)}`,
    );
    assert.equal(
      payload.chat_id,
      chatId,
      `Renderer stream chat_id mismatch: ${JSON.stringify(payload)}`,
    );
    assert.equal(
      payload.terminal_type,
      expectedTerminalType,
      `Renderer stream terminal mismatch: ${JSON.stringify(payload)}`,
    );
    assert.equal(
      expectedTerminalStatuses.includes(payload.terminal_status),
      true,
      `Renderer stream terminal status mismatch: ${JSON.stringify(payload)}`,
    );
    assert.equal(
      payload.no_replay_event_count,
      0,
      `Renderer stream replayed latest cursor: ${JSON.stringify(payload)}`,
    );
    trace("base-stream:complete");
    // The base event/reconnect probe intentionally opens a long-lived stream.
    // It has no bearing on later UI scenarios, so close it before exercising
    // their independent documents and subscriptions.
    if (!win.isDestroyed()) win.destroy();

    let basicUiSend = null;
    if (
      basicUiChatId &&
      basicUiIsolationChatId &&
      basicUiFirstText &&
      basicUiSecondText
    ) {
      trace("basic-ui:start");
      basicUiSend = await runInFreshWindow((scenarioWindow) =>
        waitForBasicChatUiSend(scenarioWindow, {
          baseUrl,
          chatId: basicUiChatId,
          isolationChatId: basicUiIsolationChatId,
          firstText: basicUiFirstText,
          secondText: basicUiSecondText,
          timeoutMs,
        }),
      );
      assert.equal(
        basicUiSend.ok,
        true,
        `Basic chat UI send payload mismatch: ${JSON.stringify(basicUiSend)}`,
      );
      assert.equal(
        basicUiSend.chat_id,
        basicUiChatId,
        `Basic chat UI send chat_id mismatch: ${JSON.stringify(basicUiSend)}`,
      );
      assert.equal(
        basicUiSend.isolation_chat_id,
        basicUiIsolationChatId,
        `Basic chat UI isolation chat_id mismatch: ${JSON.stringify(basicUiSend)}`,
      );
      assert.equal(
        basicUiSend.final.empty_assistant_count,
        0,
        `Basic chat UI send left empty assistant bubbles: ${JSON.stringify(basicUiSend)}`,
      );
      assert.equal(
        basicUiSend.final.response_avatar_count <=
          basicUiSend.final.assistant_bubble_count,
        true,
        `Basic chat UI send duplicated assistant avatars: ${JSON.stringify(basicUiSend)}`,
      );
      assert.equal(
        basicUiSend.final.action_bar_count <=
          basicUiSend.final.assistant_bubble_count,
        true,
        `Basic chat UI send duplicated assistant action bars: ${JSON.stringify(basicUiSend)}`,
      );
      assert.equal(
        basicUiSend.isolation?.user_bubble_count,
        0,
        `Basic chat UI isolated session leaked user bubbles: ${JSON.stringify(basicUiSend)}`,
      );
      assert.equal(
        basicUiSend.isolation?.assistant_bubble_count,
        0,
        `Basic chat UI isolated session leaked assistant bubbles: ${JSON.stringify(basicUiSend)}`,
      );
      assert.equal(
        basicUiSend.restored?.user_bubble_count,
        2,
        `Basic chat UI original session did not restore user bubbles: ${JSON.stringify(basicUiSend)}`,
      );
      assert.equal(
        basicUiSend.restored?.assistant_bubble_count >= 2 &&
          basicUiSend.restored?.assistant_bubble_count <= 4,
        true,
        `Basic chat UI original session did not restore assistant bubbles: ${JSON.stringify(basicUiSend)}`,
      );
      trace("basic-ui:complete");
    }

    let providerStreamUi = null;
    if (
      providerStreamChatId &&
      providerStreamPrompt &&
      providerStreamThinkingText &&
      providerStreamPartialText &&
      providerStreamAnswerText &&
      providerStreamQueuedSecondText &&
      providerStreamQueuedThirdText
    ) {
      trace("provider-stream:start");
      providerStreamUi = await runInFreshWindow((scenarioWindow) =>
        waitForProviderStreamUiSend(scenarioWindow, {
          baseUrl,
          chatId: providerStreamChatId,
          promptText: providerStreamPrompt,
          thinkingText: providerStreamThinkingText,
          partialText: providerStreamPartialText,
          answerText: providerStreamAnswerText,
          queuedSecondText: providerStreamQueuedSecondText,
          queuedThirdText: providerStreamQueuedThirdText,
          timeoutMs,
        }),
      );
      assert.equal(
        providerStreamUi.ok,
        true,
        `Provider stream UI payload mismatch: ${JSON.stringify(providerStreamUi)}`,
      );
      assert.equal(
        providerStreamUi.chat_id,
        providerStreamChatId,
        `Provider stream UI chat_id mismatch: ${JSON.stringify(providerStreamUi)}`,
      );
      assert.deepEqual(
        providerStreamUi.input_method,
        {
          composition_enter_blocked: true,
          draft_preserved: true,
          post_composition_submission_count: 1,
        },
        `Provider stream UI did not keep IME Enter from submitting: ${JSON.stringify(providerStreamUi)}`,
      );
      assert.deepEqual(
        providerStreamUi.attachment_drag,
        {
          file_name: "steering-drag.png",
          media_type: "image/png",
          dragover_prevented: true,
          drop_prevented: true,
          uploaded_ready: true,
          duplicate_deduplicated: true,
          removed_cleanly: true,
          chat_submission_count: 0,
        },
        `Provider stream UI drag attachment contract failed: ${JSON.stringify(providerStreamUi)}`,
      );
      assert.equal(
        providerStreamUi.thinking.assistant_bubble_count,
        1,
        `Provider stream UI thinking split assistant block: ${JSON.stringify(providerStreamUi)}`,
      );
      assert.equal(
        providerStreamUi.partial.assistant_bubble_count,
        1,
        `Provider stream UI partial split assistant block: ${JSON.stringify(providerStreamUi)}`,
      );
      assert.equal(
        providerStreamUi.final.assistant_bubble_count,
        2,
        `Provider stream UI did not preserve the retired partial beside the redirected answer: ${JSON.stringify(providerStreamUi)}`,
      );
      assert.equal(
        providerStreamUi.final.redirected_answer_visible,
        true,
        `Provider stream UI did not render the redirected successor answer: ${JSON.stringify(providerStreamUi)}`,
      );
      assert.equal(
        providerStreamUi.final.old_full_answer_visible,
        false,
        `Provider stream UI leaked the retired epoch's full answer: ${JSON.stringify(providerStreamUi)}`,
      );
      assert.equal(
        providerStreamUi.rapid_queue?.queued_before_first_answer,
        true,
        `Provider stream UI did not queue before first answer: ${JSON.stringify(providerStreamUi)}`,
      );
      assert.equal(
        providerStreamUi.rapid_queue?.final?.user_bubble_count,
        3,
        `Provider stream UI lost a queued user message: ${JSON.stringify(providerStreamUi)}`,
      );
      assert.equal(
        providerStreamUi.rapid_queue?.final?.assistant_bubble_count,
        2,
        `Provider stream UI returned the wrong retired/successor assistant count: ${JSON.stringify(providerStreamUi)}`,
      );
      assert.deepEqual(
        providerStreamUi.rapid_queue?.submission_intents,
        ["supplement", "redirect"],
        `Provider stream UI did not keep default supplement and explicit redirect distinct: ${JSON.stringify(providerStreamUi)}`,
      );
      assert.equal(
        providerStreamUi.rapid_queue?.admitted_statuses?.supplement?.status,
        "admitted",
        `Provider stream UI did not expose supplement admission separately from application: ${JSON.stringify(providerStreamUi)}`,
      );
      assert.equal(
        providerStreamUi.rapid_queue?.admitted_statuses?.redirect?.status,
        "admitted",
        `Provider stream UI did not expose redirect admission separately from application: ${JSON.stringify(providerStreamUi)}`,
      );
      assert.equal(
        providerStreamUi.rapid_queue?.final?.supplement_disposition?.status,
        "superseded",
        `Provider stream UI did not dispose the overtaken supplement explicitly: ${JSON.stringify(providerStreamUi)}`,
      );
      assert.equal(
        providerStreamUi.rapid_queue?.final?.redirect_disposition?.status,
        "applied",
        `Provider stream UI did not confirm redirect application: ${JSON.stringify(providerStreamUi)}`,
      );
      assert.equal(
        providerStreamUi.rapid_queue?.final
          ?.conversation_items_preserve_fifo_order,
        true,
        `Provider stream UI did not preserve queued user or assistant FIFO order: ${JSON.stringify(providerStreamUi)}`,
      );
      assert.equal(
        providerStreamUi.rapid_queue?.final?.old_partial_preserved,
        true,
        `Provider stream UI lost already-visible old-epoch text: ${JSON.stringify(providerStreamUi)}`,
      );
      assert.equal(
        providerStreamUi.rapid_queue?.final?.old_full_answer_absent,
        true,
        `Provider stream UI rendered the retired epoch's unseen completion: ${JSON.stringify(providerStreamUi)}`,
      );
      assert.equal(
        providerStreamUi.rapid_queue?.final?.superseded_answer_absent,
        true,
        `Provider stream UI executed the superseded supplement: ${JSON.stringify(providerStreamUi)}`,
      );
      assert.equal(
        providerStreamUi.rapid_queue?.final?.redirect_answer_present,
        true,
        `Provider stream UI lost the applied redirect answer: ${JSON.stringify(providerStreamUi)}`,
      );
      assert.equal(
        providerStreamUi.thinking_visible_ms <
          providerStreamUi.partial_visible_ms,
        true,
        `Provider stream UI did not expose thinking before answer: ${JSON.stringify(providerStreamUi)}`,
      );
      assert.equal(
        providerStreamUi.partial_visible_ms < providerStreamUi.final_visible_ms,
        true,
        `Provider stream UI did not expose partial answer before final: ${JSON.stringify(providerStreamUi)}`,
      );
    }

    let providerStopResendUi = null;
    if (
      providerStopChatId &&
      providerStopPrompt &&
      providerStopAnswerText &&
      providerStopResendText &&
      providerStopResendAnswerText
    ) {
      providerStopResendUi = await runInFreshWindow((scenarioWindow) =>
        waitForProviderStopResendUi(scenarioWindow, {
          baseUrl,
          chatId: providerStopChatId,
          stopPromptText: providerStopPrompt,
          thinkingText: providerStreamThinkingText,
          stopAnswerText: providerStopAnswerText,
          resendText: providerStopResendText,
          resendAnswerText: providerStopResendAnswerText,
          timeoutMs,
        }),
      );
      assert.equal(
        providerStopResendUi.ok,
        true,
        `Provider stop-resend UI payload mismatch: ${JSON.stringify(providerStopResendUi)}`,
      );
      assert.equal(
        ["interrupted", "cancelled"].includes(
          providerStopResendUi.stop_terminal,
        ),
        true,
        `Provider stop-resend UI missed stop terminal: ${JSON.stringify(providerStopResendUi)}`,
      );
      assert.equal(
        providerStopResendUi.final.user_bubble_count,
        2,
        `Provider stop-resend UI lost resent user message: ${JSON.stringify(providerStopResendUi)}`,
      );
      assert.equal(
        providerStopResendUi.final.empty_assistant_count,
        0,
        `Provider stop-resend UI left empty assistant: ${JSON.stringify(providerStopResendUi)}`,
      );
    }

    let providerStreamRouteSwitchUi = null;
    if (
      providerSwitchChatId &&
      providerSwitchIsolationChatId &&
      providerSwitchPrompt &&
      providerSwitchAnswerText
    ) {
      providerStreamRouteSwitchUi = await runInFreshWindow((scenarioWindow) =>
        waitForProviderStreamRouteSwitchUi(scenarioWindow, {
          baseUrl,
          chatId: providerSwitchChatId,
          isolationChatId: providerSwitchIsolationChatId,
          promptText: providerSwitchPrompt,
          thinkingText: providerStreamThinkingText,
          answerText: providerSwitchAnswerText,
          timeoutMs,
        }),
      );
      assert.equal(
        providerStreamRouteSwitchUi.ok,
        true,
        `Provider stream route-switch UI payload mismatch: ${JSON.stringify(providerStreamRouteSwitchUi)}`,
      );
      assert.equal(
        providerStreamRouteSwitchUi.isolation.user_bubble_count,
        0,
        `Provider stream route-switch UI leaked user message: ${JSON.stringify(providerStreamRouteSwitchUi)}`,
      );
      assert.equal(
        providerStreamRouteSwitchUi.restored.assistant_bubble_count,
        1,
        `Provider stream route-switch UI duplicated assistant: ${JSON.stringify(providerStreamRouteSwitchUi)}`,
      );
    }

    console.log(
      JSON.stringify(
        {
          ...payload,
          basic_chat_ui_send: basicUiSend,
          provider_stream_ui: providerStreamUi,
          provider_stop_resend_ui: providerStopResendUi,
          provider_stream_route_switch_ui: providerStreamRouteSwitchUi,
          visible_thinking_settings: visibleThinkingSettings,
          observed_stream_requests: streamUrls,
          electron: process.versions.electron,
          chrome: process.versions.chrome,
        },
        null,
        2,
      ),
    );
  } finally {
    if (!win.isDestroyed()) {
      win.close();
    }
    app.quit();
  }
}

// This harness deliberately closes every scenario window before starting the
// next one. Keep Electron alive between those isolated scenarios; `main()`
// owns the explicit final `app.quit()` below.
app.on("window-all-closed", (event) => {
  event.preventDefault();
});

app.whenReady().then(() => {
  main().catch((error) => {
    console.error(error);
    app.exit(1);
  });
});

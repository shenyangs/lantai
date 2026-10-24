const assert = require("node:assert/strict");
const crypto = require("node:crypto");
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

function createWindow() {
  return new BrowserWindow({
    width: 960,
    height: 720,
    show: false,
    title: "Steering Control Gap Smoke",
    webPreferences: {
      sandbox: false,
      contextIsolation: true,
      nodeIntegration: false,
    },
  });
}

async function primeRuntimeIdentity(win, options) {
  const { baseUrl, chatId, promptText, timeoutMs } = options;
  await win.loadURL(`${baseUrl}/#/chat/${encodeURIComponent(chatId)}`);
  return win.webContents.executeJavaScript(
    `
      (async () => {
        const baseUrl = ${JSON.stringify(baseUrl)};
        const chatId = ${JSON.stringify(chatId)};
        const promptText = ${JSON.stringify(promptText)};
        const timeoutMs = ${JSON.stringify(timeoutMs)};
        const deadline = Date.now() + timeoutMs;
        const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
        const originalFetch = window.fetch.bind(window);
        const snapshot = () => {
          const textarea = document.querySelector('[class*="sender"] textarea');
          const stop = document.querySelector(".lantai-sender-stop-btn");
          return {
            hash: window.location.hash,
            textarea: textarea?.value || "",
            has_textarea: Boolean(textarea),
            stop_active: Boolean(stop) && !stop.disabled,
          };
        };
        const waitFor = async (label, predicate, limitMs = timeoutMs) => {
          const localDeadline = Math.min(deadline, Date.now() + limitMs);
          let current = snapshot();
          while (Date.now() < localDeadline) {
            current = snapshot();
            if (predicate(current)) return current;
            await sleep(60);
          }
          throw new Error(label + " timed out: " + JSON.stringify(current));
        };
        const closeModelPrompt = async () => {
          const button = [...document.querySelectorAll(".ant-modal button")].find((node) => {
            const text = (node.textContent || "").replace(/\\s+/g, "");
            return text.includes("跳过") || text.includes("稍后");
          });
          if (button) { button.click(); await sleep(100); }
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
          textarea.dispatchEvent(new InputEvent("input", {
            bubbles: true,
            data: value,
            inputType: "insertText",
          }));
          textarea.dispatchEvent(new Event("change", { bubbles: true }));
        };
        const enter = (textarea) => textarea.dispatchEvent(
          new KeyboardEvent("keydown", {
            key: "Enter",
            code: "Enter",
            bubbles: true,
            cancelable: true,
          }),
        );
        const backendRuntimeIdentity = async () => {
          const response = await originalFetch(
            baseUrl + "/api/chats/" + encodeURIComponent(chatId),
          );
          if (!response.ok) return null;
          const payload = await response.json();
          const state = payload?.runtime_state;
          const runId = String(state?.run_id || "").trim();
          const attemptId = String(state?.attempt_id || "").trim();
          return runId && attemptId
            ? { run_id: runId, attempt_id: attemptId, status: String(state?.status || "") }
            : null;
        };

        await closeModelPrompt();
        await waitFor(
          "primary chat ready",
          (current) => current.has_textarea && current.hash.includes(encodeURIComponent(chatId)),
          8000,
        );
        const textarea = document.querySelector('[class*="sender"] textarea');
        setTextareaValue(textarea, promptText);
        await waitFor("primary populated", (current) => current.textarea === promptText, 2000);
        enter(textarea);
        await waitFor("primary task active", (current) => current.stop_active, 8000);
        const identityDeadline = Math.min(deadline, Date.now() + 10000);
        let identity = null;
        while (Date.now() < identityDeadline) {
          identity = await backendRuntimeIdentity();
          if (identity) return identity;
          await sleep(80);
        }
        throw new Error("primary runtime identity timed out");
      })()
    `,
    true,
  );
}

async function rendererScenario(win, options) {
  const {
    baseUrl,
    chatId,
    promptText,
    controlText,
    injectedKind,
    intentLabel,
    timeoutMs,
  } = options;
  const backendIdentity = await primeRuntimeIdentity(win, {
    baseUrl,
    chatId,
    promptText,
    timeoutMs,
  });
  await win.loadURL(`${baseUrl}/#/chat/${encodeURIComponent(chatId)}`);
  return win.webContents.executeJavaScript(
    `
      (async () => {
        const baseUrl = ${JSON.stringify(baseUrl)};
        const chatId = ${JSON.stringify(chatId)};
        const controlText = ${JSON.stringify(controlText)};
        const injectedKind = ${JSON.stringify(injectedKind)};
        const intentLabel = ${JSON.stringify(intentLabel || "")};
        const backendIdentity = ${JSON.stringify(backendIdentity)};
        const timeoutMs = ${JSON.stringify(timeoutMs)};
        const deadline = Date.now() + timeoutMs;
        const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
        const transport = [];
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
          const rawBody = typeof args[1]?.body === "string" ? args[1].body : "";
          let parsedBody = null;
          try { parsedBody = rawBody ? JSON.parse(rawBody) : null; } catch {}
          const isControl =
            method === "POST" &&
            url.includes("/api/console/chat") &&
            parsedBody?.operation === "steer";
          if (isControl && rawBody.includes(controlText)) {
            transport.push({ method, url, injected: injectedKind, body: parsedBody });
            if (injectedKind === "http_400") {
              return new Response(
                JSON.stringify({
                  detail: {
                    code: "invalid_control_input",
                    message: "injected exact-candidate UI rejection",
                  },
                }),
                {
                  status: 400,
                  headers: { "Content-Type": "application/json" },
                },
              );
            }
            if (injectedKind === "network") {
              throw new TypeError("injected exact-candidate control disconnect");
            }
          }
          const response = await originalFetch(...args);
          if (
            url.includes("/api/console/chat/control-input") ||
            (method === "POST" && url.includes("/api/console/chat"))
          ) {
            const responseBody = await response.clone().text().catch(() => "");
            transport.push({
              method,
              url,
              status: response.status,
              body: parsedBody,
              response_body: responseBody.slice(0, 4000),
            });
          }
          return response;
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
          textarea.dispatchEvent(new InputEvent("input", {
            bubbles: true,
            data: value,
            inputType: "insertText",
          }));
          textarea.dispatchEvent(new Event("change", { bubbles: true }));
        };
        const enter = (textarea) => textarea.dispatchEvent(
          new KeyboardEvent("keydown", {
            key: "Enter",
            code: "Enter",
            bubbles: true,
            cancelable: true,
          }),
        );
        const visibleMenuItem = (label) =>
          [...document.querySelectorAll('[role="menuitem"]')]
            .filter((node) => {
              const rect = node.getBoundingClientRect();
              return rect.width > 0 && rect.height > 0;
            })
            .find((node) => (node.textContent || "").trim() === label);
        const buttonEvidence = (card) =>
          [...card.querySelectorAll("button")].map((node) => ({
            label: node.getAttribute("aria-label") || (node.textContent || "").trim(),
            text: (node.textContent || "").trim(),
            disabled: node.disabled,
          }));
        const snapshot = () => {
          const textarea = document.querySelector('[class*="sender"] textarea');
          const stop = document.querySelector(".lantai-sender-stop-btn");
          const card = [...document.querySelectorAll(".lantai-guidance-card")]
            .find((node) => (node.textContent || "").includes(controlText));
          const queueStatus = [...document.querySelectorAll(".lantai-user-message-queue-status")]
            .find((node) => node.closest(".lantai-bubble-user")?.textContent?.includes(controlText));
          return {
            hash: window.location.hash,
            textarea: textarea?.value || "",
            has_textarea: Boolean(textarea),
            stop_present: Boolean(stop),
            stop_disabled: Boolean(stop?.disabled),
            stop_label: stop?.getAttribute("aria-label") || "",
            guidance_status: card?.getAttribute("data-guidance-status") || "",
            guidance_text: (card?.textContent || "").trim(),
            guidance_label: card?.getAttribute("aria-label") || "",
            guidance_actions: card ? buttonEvidence(card) : [],
            status_role: card?.querySelector('[role="status"]')?.getAttribute("role") || "",
            status_live: card?.querySelector('[role="status"]')?.getAttribute("aria-live") || "",
            action_group_label: card?.querySelector('[role="group"]')?.getAttribute("aria-label") || "",
            queue_status: queueStatus?.getAttribute("data-status") || "",
            queue_status_text: (queueStatus?.textContent || "").trim(),
            body_text: document.body?.textContent || "",
          };
        };
        const waitFor = async (label, predicate, limitMs = timeoutMs) => {
          const localDeadline = Math.min(deadline, Date.now() + limitMs);
          let current = snapshot();
          while (Date.now() < localDeadline) {
            current = snapshot();
            if (predicate(current)) return current;
            await sleep(60);
          }
          throw new Error(label + " timed out: " + JSON.stringify(current).slice(0, 3000));
        };
        const closeModelPrompt = async () => {
          const button = [...document.querySelectorAll(".ant-modal button")].find((node) => {
            const text = (node.textContent || "").replace(/\\s+/g, "");
            return text.includes("跳过") || text.includes("稍后");
          });
          if (button) {
            button.click();
            await sleep(100);
          }
        };

        await closeModelPrompt();
        const active = await waitFor(
          "primary task restored with runtime identity",
          (current) => current.stop_present && !current.stop_disabled,
          8000,
        );

        let textarea = document.querySelector('[class*="sender"] textarea');
        setTextareaValue(textarea, controlText);
        await waitFor("control populated", (current) => current.textarea === controlText, 2000);
        if (intentLabel) {
          const intentButton = document.querySelector(".lantai-sender-control-intent-btn");
          if (!intentButton) throw new Error("control intent button missing");
          intentButton.click();
          const menuDeadline = Date.now() + 1500;
          let item = null;
          while (Date.now() < menuDeadline && !item) {
            item = visibleMenuItem(intentLabel);
            if (!item) await sleep(40);
          }
          if (!item) throw new Error("control intent item missing: " + intentLabel);
          item.click();
        } else {
          enter(textarea);
        }

        const expectedStatus = injectedKind === "http_400" ? "failed" : "uncertain";
        const expectedCopy = injectedKind === "http_400"
          ? "输入无法接收 · 请编辑后重试"
          : "接收待确认 · 可安全重试";
        const result = await waitFor(
          "control failure visible",
          (current) =>
            current.guidance_status === expectedStatus &&
            current.guidance_text.includes(expectedCopy),
          10000,
        );
        return {
          chat_id: chatId,
          injected_kind: injectedKind,
          control_text: controlText,
          expected_copy: expectedCopy,
          backend_identity: backendIdentity,
          primary_active_after_identity_hydration:
            active.stop_present && !active.stop_disabled,
          primary_active_before_control: active.stop_present && !active.stop_disabled,
          primary_active_after_control: result.stop_present && !result.stop_disabled,
          stop_label_after_control: result.stop_label,
          guidance: {
            status: result.guidance_status,
            text: result.guidance_text,
            label: result.guidance_label,
            status_role: result.status_role,
            status_live: result.status_live,
            action_group_label: result.action_group_label,
            actions: result.guidance_actions,
          },
          transport,
        };
      })()
    `,
    true,
  );
}

async function restoreScenario(win, options) {
  const { baseUrl, chatId, promptText, controlText, timeoutMs } = options;
  const backendIdentity = await primeRuntimeIdentity(win, {
    baseUrl,
    chatId,
    promptText,
    timeoutMs,
  });
  await win.loadURL(`${baseUrl}/#/chat/${encodeURIComponent(chatId)}`);
  return win.webContents.executeJavaScript(
    `
      (async () => {
        const baseUrl = ${JSON.stringify(baseUrl)};
        const chatId = ${JSON.stringify(chatId)};
        const controlText = ${JSON.stringify(controlText)};
        const backendIdentity = ${JSON.stringify(backendIdentity)};
        const timeoutMs = ${JSON.stringify(timeoutMs)};
        const deadline = Date.now() + timeoutMs;
        const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
        const statusRequests = [];
        const originalFetch = window.fetch.bind(window);
        window.fetch = async (...args) => {
          const rawInput = args[0];
          const url = typeof rawInput === "string" ? rawInput : rawInput?.url || "";
          const method = String(args[1]?.method || rawInput?.method || "GET").toUpperCase();
          const response = await originalFetch(...args);
          if (method === "GET" && url.includes("/api/console/chat/control-input?")) {
            statusRequests.push({
              url,
              status: response.status,
              body: (await response.clone().text().catch(() => "")).slice(0, 4000),
            });
          }
          return response;
        };
        const textFrom = (value) => {
          if (typeof value === "string") return value;
          if (Array.isArray(value)) return value.map(textFrom).filter(Boolean).join("\\n");
          if (value && typeof value === "object") {
            return [value.text, value.content, value.value, value.message]
              .map(textFrom).filter(Boolean).join("\\n");
          }
          return "";
        };
        const messageTime = (message) =>
          message?.createdAt ?? message?.created_at ?? message?.timestamp ??
          message?.cards?.[0]?.data?.created_at ?? null;
        const messageCommandId = (message) => {
          const metadata =
            message?.metadata && typeof message.metadata === "object"
              ? message.metadata
              : {};
          const nested =
            metadata?.metadata && typeof metadata.metadata === "object"
              ? metadata.metadata
              : {};
          return String(
            message?.cards?.[0]?.data?.runtime_control_command_id ||
              message?.cards?.[0]?.data?.client_turn_id ||
              metadata.runtime_control_command_id ||
              nested.runtime_control_command_id ||
              metadata.client_turn_id ||
              nested.client_turn_id ||
              "",
          );
        };
        const backendMessage = async () => {
          const response = await originalFetch(
            baseUrl + "/api/chats/" + encodeURIComponent(chatId),
          );
          if (!response.ok) throw new Error("backend chat failed: " + response.status);
          const payload = await response.json();
          const messages = Array.isArray(payload.messages) ? payload.messages : [];
          const message = messages.find((item) => textFrom(item).includes(controlText));
          return message ? {
            id: String(message.id || ""),
            command_id: messageCommandId(message),
            timestamp: messageTime(message),
            role: String(message.role || message.type || ""),
            keys: Object.keys(message).sort(),
          } : null;
        };
        const setTextareaValue = (textarea, value) => {
          const previousValue = textarea.value;
          const descriptor = Object.getOwnPropertyDescriptor(
            window.HTMLTextAreaElement.prototype, "value",
          );
          textarea.focus();
          if (descriptor?.set) descriptor.set.call(textarea, value);
          else textarea.value = value;
          textarea._valueTracker?.setValue(previousValue);
          textarea.dispatchEvent(new InputEvent("input", { bubbles: true, data: value, inputType: "insertText" }));
          textarea.dispatchEvent(new Event("change", { bubbles: true }));
        };
        const enter = (textarea) => textarea.dispatchEvent(new KeyboardEvent("keydown", {
          key: "Enter", code: "Enter", bubbles: true, cancelable: true,
        }));
        const snapshot = () => {
          const textarea = document.querySelector('[class*="sender"] textarea');
          const stop = document.querySelector(".lantai-sender-stop-btn");
          const user = [...document.querySelectorAll(".lantai-bubble-user")]
            .find((node) => (node.textContent || "").includes(controlText));
          const status = user?.querySelector(".lantai-user-message-queue-status");
          return {
            hash: window.location.hash,
            has_textarea: Boolean(textarea),
            textarea: textarea?.value || "",
            stop_active: Boolean(stop) && !stop.disabled,
            control_visible: Boolean(user),
            queue_status: status?.getAttribute("data-status") || "",
            queue_status_text: (status?.textContent || "").trim(),
          };
        };
        const waitFor = async (label, predicate, limitMs = timeoutMs) => {
          const localDeadline = Math.min(deadline, Date.now() + limitMs);
          let current = snapshot();
          while (Date.now() < localDeadline) {
            current = snapshot();
            if (predicate(current)) return current;
            await sleep(60);
          }
          throw new Error(label + " timed out: " + JSON.stringify(current));
        };
        const closeModelPrompt = async () => {
          const button = [...document.querySelectorAll(".ant-modal button")].find((node) => {
            const text = (node.textContent || "").replace(/\\s+/g, "");
            return text.includes("跳过") || text.includes("稍后");
          });
          if (button) { button.click(); await sleep(100); }
        };

        await closeModelPrompt();
        await waitFor("restore chat ready", (current) => current.has_textarea, 8000);
        await waitFor("restore primary active", (current) => current.stop_active, 8000);
        let textarea = document.querySelector('[class*="sender"] textarea');
        setTextareaValue(textarea, controlText);
        enter(textarea);
        const admitted = await waitFor(
          "admitted status visible",
          (current) =>
            current.queue_status === "admitted" &&
            current.queue_status_text === "已接收 · 等待当前任务安全边界",
          3000,
        );
        let before = null;
        while (Date.now() < deadline && !before) {
          before = await backendMessage();
          if (!before) await sleep(60);
        }
        if (!before) throw new Error("persisted control message missing before route change");
        const statusCountBefore = statusRequests.length;
        window.location.hash = "#/settings";
        await waitFor("settings route", (current) => current.hash.includes("/settings"), 3000);
        window.location.hash = "#/chat/" + encodeURIComponent(chatId);
        await waitFor(
          "control status restored",
          (current) => current.control_visible && Boolean(current.queue_status_text),
          8000,
        );
        const statusDeadline = Date.now() + 5000;
        while (Date.now() < statusDeadline && statusRequests.length <= statusCountBefore) {
          await sleep(80);
        }
        const after = await backendMessage();
        let serverStatus = "";
        const latestStatusResponse = statusRequests[statusRequests.length - 1];
        if (latestStatusResponse?.body) {
          try {
            const parsed = JSON.parse(latestStatusResponse.body);
            serverStatus = String(parsed.execution_status || parsed.status || parsed.receipt?.execution_status || "");
          } catch {}
        }
        const expectedUiStatus = ({
          pending: "admitted",
          admitted: "admitted",
          claimed: "claimed",
          bound: "bound",
          applied: "applied",
          steered: "steered",
          queued: "queued",
          cancelled: "cancelled",
          superseded: "superseded",
          failed: "failed",
        })[serverStatus.toLowerCase()] || serverStatus.toLowerCase();
        const restored = await waitFor(
          "control status reconciled",
          (current) =>
            current.control_visible &&
            Boolean(expectedUiStatus) &&
            current.queue_status === expectedUiStatus,
          5000,
        );
        return {
          chat_id: chatId,
          backend_identity: backendIdentity,
          admitted,
          restored,
          before,
          after,
          status_requests_before_return: statusCountBefore,
          status_requests_after_return: statusRequests.length,
          latest_server_status: serverStatus,
          status_requests: statusRequests.slice(-8),
        };
      })()
    `,
    true,
  );
}

async function collectAccessibility(win, controlText) {
  const dom = await win.webContents.executeJavaScript(
    `(() => {
      const controlText = ${JSON.stringify(controlText)};
      const card = [...document.querySelectorAll(".lantai-guidance-card")]
        .find((node) => (node.textContent || "").includes(controlText));
      if (!card) return null;
      const buttons = [...card.querySelectorAll("button")];
      const first = buttons[0];
      first?.focus();
      return {
        article_label: card.getAttribute("aria-label") || "",
        status_role: card.querySelector('[role="status"]')?.getAttribute("role") || "",
        status_live: card.querySelector('[role="status"]')?.getAttribute("aria-live") || "",
        group_label: card.querySelector('[role="group"]')?.getAttribute("aria-label") || "",
        button_labels: buttons.map((node) => node.getAttribute("aria-label") || ""),
        focus_before: document.activeElement?.getAttribute("aria-label") || "",
      };
    })()`,
    true,
  );
  win.webContents.sendInputEvent({ type: "keyDown", keyCode: "Tab" });
  win.webContents.sendInputEvent({ type: "keyUp", keyCode: "Tab" });
  await new Promise((resolve) => setTimeout(resolve, 100));
  const focusAfter = await win.webContents.executeJavaScript(
    `document.activeElement?.getAttribute("aria-label") || ""`,
    true,
  );

  let ax = [];
  try {
    win.webContents.debugger.attach("1.3");
    await win.webContents.debugger.sendCommand("Accessibility.enable");
    const tree = await win.webContents.debugger.sendCommand("Accessibility.getFullAXTree");
    ax = (tree.nodes || [])
      .map((node) => ({
        role: node.role?.value || "",
        name: node.name?.value || "",
      }))
      .filter((node) => node.name && node.name.includes(controlText));
  } finally {
    if (win.webContents.debugger.isAttached()) win.webContents.debugger.detach();
  }
  return { ...dom, focus_after: focusAfter, ax_nodes: ax };
}

async function collectVisualModes(win, controlText) {
  const modes = [];
  for (const theme of ["light", "dark"]) {
    await win.webContents.executeJavaScript(
      `(async () => {
        const theme = ${JSON.stringify(theme)};
        const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
        const toggleLabels = ["浅色", "深色", "跟随系统", "Light", "Dark", "System"];
        const targetLabels = theme === "light" ? ["浅色", "Light"] : ["深色", "Dark"];
        const toggle = [...document.querySelectorAll("button")].find((node) =>
          toggleLabels.includes(node.getAttribute("aria-label") || ""),
        );
        if (!toggle) throw new Error("theme toggle missing");
        toggle.click();
        const menuDeadline = Date.now() + 2000;
        let item = null;
        while (Date.now() < menuDeadline && !item) {
          item = [...document.querySelectorAll('[role="menuitem"]')]
            .filter((node) => {
              const rect = node.getBoundingClientRect();
              return rect.width > 0 && rect.height > 0;
            })
            .find((node) => targetLabels.includes((node.textContent || "").trim()));
          if (!item) await sleep(40);
        }
        if (!item) throw new Error("theme menu item missing: " + theme);
        item.click();
        const themeDeadline = Date.now() + 2000;
        while (Date.now() < themeDeadline) {
          if (document.documentElement.dataset.theme === theme) return true;
          await sleep(40);
        }
        throw new Error("theme did not apply: " + theme);
      })()`,
      true,
    );
    const deadline = Date.now() + 2000;
    let snapshot = null;
    while (Date.now() < deadline) {
      snapshot = await win.webContents.executeJavaScript(
        `(() => {
          const controlText = ${JSON.stringify(controlText)};
          const card = [...document.querySelectorAll(".lantai-guidance-card")]
            .find((node) => (node.textContent || "").includes(controlText));
          if (!card) return null;
          const bodyStyle = getComputedStyle(document.body);
          const cardStyle = getComputedStyle(card);
          return {
            html_class: document.documentElement.className,
            body_background: bodyStyle.backgroundColor,
            body_color: bodyStyle.color,
            card_background: cardStyle.backgroundColor,
            card_color: cardStyle.color,
            status_text: (card.querySelector('[role="status"]')?.textContent || "").trim(),
          };
        })()`,
        true,
      );
      if (snapshot) break;
      await new Promise((resolve) => setTimeout(resolve, 80));
    }
    if (!snapshot) throw new Error(`guidance card missing after ${theme} reload`);
    const image = await win.webContents.capturePage();
    modes.push({
      theme,
      ...snapshot,
      screenshot_sha256: crypto.createHash("sha256").update(image.toPNG()).digest("hex"),
    });
  }

  win.setSize(720, 760);
  await new Promise((resolve) => setTimeout(resolve, 120));
  const narrow = await win.webContents.executeJavaScript(
    `(() => {
      const controlText = ${JSON.stringify(controlText)};
      const card = [...document.querySelectorAll(".lantai-guidance-card")]
        .find((node) => (node.textContent || "").includes(controlText));
      const rect = card?.getBoundingClientRect();
      return {
        viewport_width: window.innerWidth,
        document_scroll_width: document.documentElement.scrollWidth,
        no_horizontal_overflow: document.documentElement.scrollWidth <= window.innerWidth + 1,
        card_in_viewport: Boolean(rect) && rect.left >= 0 && rect.right <= window.innerWidth + 1,
        status_text: (card?.querySelector('[role="status"]')?.textContent || "").trim(),
      };
    })()`,
    true,
  );
  return { modes, narrow };
}

async function main() {
  const args = parseArgs(process.argv);
  const baseUrl = String(args["base-url"] || "").replace(/\/$/, "");
  const desktopToken = String(args["desktop-token"] || "");
  const timeoutMs = Number(args["timeout-ms"] || 45000);
  const required = [
    "error-chat-id", "error-prompt", "error-control",
    "network-chat-id", "network-prompt", "network-control",
    "restore-chat-id", "restore-prompt", "restore-control",
  ];
  if (!baseUrl || !desktopToken || required.some((key) => !args[key])) {
    throw new Error("base URL, desktop token and all scenario arguments are required");
  }

  app.setAccessibilitySupportEnabled(true);
  const origin = new URL(baseUrl).origin;
  session.defaultSession.webRequest.onBeforeSendHeaders(
    { urls: [`${origin}/*`] },
    (details, callback) => {
      if (new URL(details.url).pathname.startsWith("/api/")) {
        details.requestHeaders["X-Lantai-Desktop-Token"] = desktopToken;
      }
      callback({ requestHeaders: details.requestHeaders });
    },
  );

  const errorWin = createWindow();
  const networkWin = createWindow();
  const restoreWin = createWindow();
  try {
    const deterministic = await rendererScenario(errorWin, {
      baseUrl,
      chatId: args["error-chat-id"],
      promptText: args["error-prompt"],
      controlText: args["error-control"],
      injectedKind: "http_400",
      intentLabel: "",
      timeoutMs,
    });
    const accessibility = await collectAccessibility(errorWin, args["error-control"]);
    const visuals = await collectVisualModes(errorWin, args["error-control"]);
    const unknown = await rendererScenario(networkWin, {
      baseUrl,
      chatId: args["network-chat-id"],
      promptText: args["network-prompt"],
      controlText: args["network-control"],
      injectedKind: "network",
      intentLabel: "立即调整方向",
      timeoutMs,
    });
    const restore = await restoreScenario(restoreWin, {
      baseUrl,
      chatId: args["restore-chat-id"],
      promptText: args["restore-prompt"],
      controlText: args["restore-control"],
      timeoutMs,
    });

    const payload = { ok: true, deterministic, unknown, restore, accessibility, visuals };
    process.stdout.write(`${JSON.stringify(payload)}\n`);
  } finally {
    for (const win of [errorWin, networkWin, restoreWin]) {
      if (!win.isDestroyed()) win.destroy();
    }
  }
}

app.whenReady()
  .then(main)
  .then(() => app.quit())
  .catch((error) => {
    process.stderr.write(`${error?.stack || error}\n`);
    app.exit(1);
  });

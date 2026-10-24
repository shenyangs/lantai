const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const path = require("node:path");
const { app, BrowserWindow, session } = require("electron");
const { normalizeConsoleMessage } = require("./consoleMessageDetails.cjs");

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

function countOccurrences(value, needle) {
  if (!needle) return 0;
  let count = 0;
  let cursor = 0;
  while (cursor <= value.length) {
    const found = value.indexOf(needle, cursor);
    if (found === -1) break;
    count += 1;
    cursor = found + needle.length;
  }
  return count;
}

function duplicateTokenCounts(value, pattern) {
  const counts = new Map();
  for (const token of String(value || "").match(pattern) || []) {
    counts.set(token, (counts.get(token) || 0) + 1);
  }
  return Object.fromEntries(
    [...counts.entries()].filter(([, count]) => count > 1),
  );
}

async function waitForSnapshot(win, expected, timeoutMs) {
  const startedAt = Date.now();
  const deadline = startedAt + timeoutMs;
  let latest = null;
  while (Date.now() < deadline) {
    latest = await win.webContents.executeJavaScript(
      `
        (() => {
          const userNodes = Array.from(document.querySelectorAll(
            ".lantai-bubble-list-item.lantai-bubble-user",
          ));
          const assistantNodes = Array.from(document.querySelectorAll(
            ".lantai-bubble-list-item.lantai-bubble-assistant",
          ));
          const answerNodes = Array.from(document.querySelectorAll(
            '[data-lantai-assistant-answer="1"]',
          ));
          const navigator = document.querySelector(
            '[aria-label="会话定位"]',
          );
          const markerCount = navigator?.querySelectorAll(
            ".lantai-conversation-navigator-marker",
          ).length || 0;
          const userText = userNodes.map((node) => node.textContent || "").join("\\n");
          const assistantText = answerNodes
            .map((node) => node.textContent || "")
            .join("\\n");
          const assistantAnswerIdentities = answerNodes.map((node) => {
            const fiberKey = Object.keys(node).find((key) =>
              key.startsWith("__reactFiber$"),
            );
            const data = fiberKey
              ? node[fiberKey]?.memoizedProps?.children?.props?.data
              : null;
            const metadata =
              data?.metadata && typeof data.metadata === "object"
                ? data.metadata
                : {};
            return {
              text: node.textContent || "",
              output_id: String(data?.id || ""),
              source_message_id: String(
                metadata.source_message_id ||
                  metadata.native_runtime_logical_source_id ||
                  metadata.native_runtime_source_id ||
                  "",
              ),
              client_turn_id: String(
                metadata.client_turn_id || metadata.native_runtime_turn_id || "",
              ),
              run_id: String(metadata.run_id || metadata.turn_id || ""),
              native_runtime_stream: metadata.native_runtime_stream === true,
              status: String(data?.status || ""),
            };
          });
          return {
            location_hash: window.location.hash,
            user_count: userNodes.length,
            assistant_count: assistantNodes.length,
            answer_count: answerNodes.length,
            empty_assistant_count: assistantNodes.filter(
              (node) => !(node.textContent || "").trim(),
            ).length,
            user_text: userText,
            assistant_text: assistantText,
            assistant_answer_texts: answerNodes.map(
              (node) => node.textContent || "",
            ),
            assistant_answer_identities: assistantAnswerIdentities,
            navigator_present: Boolean(navigator),
            navigator_visible: navigator?.getAttribute("data-visible") === "true",
            navigator_marker_count: markerCount,
            body_text: document.body?.innerText || "",
          };
        })()
      `,
      true,
    );
    if (
      latest.location_hash.includes(encodeURIComponent(expected.chatId)) &&
      latest.user_count === expected.turns &&
      latest.assistant_count === expected.turns &&
      latest.answer_count >= expected.turns &&
      latest.empty_assistant_count === 0 &&
      latest.navigator_present &&
      latest.navigator_visible &&
      latest.navigator_marker_count === expected.turns &&
      countOccurrences(latest.assistant_text, expected.answerPrefix) ===
        expected.turns &&
      countOccurrences(latest.assistant_text, expected.ququPrefix) ===
        expected.ququCount &&
      latest.user_text.includes(expected.firstPrompt) &&
      latest.user_text.includes(expected.lastPrompt) &&
      latest.assistant_text.includes(expected.firstAnswer) &&
      latest.assistant_text.includes(expected.lastAnswer)
    ) {
      return { ...latest, ready_ms: Date.now() - startedAt };
    }
    await new Promise((resolve) => setTimeout(resolve, 80));
  }
  throw new Error(
    `long-chat renderer did not settle: ${JSON.stringify({
      ...latest,
      answer_prefix_count: latest
        ? countOccurrences(latest.assistant_text, expected.answerPrefix)
        : 0,
      expected_answer_prefix_count: expected.turns,
      ququ_prefix_count: latest
        ? countOccurrences(latest.assistant_text, expected.ququPrefix)
        : 0,
      expected_ququ_prefix_count: expected.ququCount,
      duplicate_answer_tokens: latest
        ? duplicateTokenCounts(
            latest.assistant_text,
            /LANTAI_DEPTH_ANSWER_\d{3}_[a-z0-9]+/g,
          )
        : {},
      duplicate_ququ_tokens: latest
        ? duplicateTokenCounts(
            latest.assistant_text,
            /LANTAI_DEPTH_QUQU_[A-Za-z0-9_]+/g,
          )
        : {},
      duplicate_answer_node_samples: latest
        ? latest.assistant_answer_identities
            .filter((identity) =>
              Object.keys(
                duplicateTokenCounts(
                  latest.assistant_text,
                  /LANTAI_DEPTH_ANSWER_\d{3}_[a-z0-9]+/g,
                ),
              ).some((token) => identity.text.includes(token)),
            )
            .slice(0, 8)
        : [],
      duplicate_ququ_node_samples: latest
        ? latest.assistant_answer_identities
            .filter((identity) =>
              Object.keys(
                duplicateTokenCounts(
                  latest.assistant_text,
                  /LANTAI_DEPTH_QUQU_[A-Za-z0-9_]+/g,
                ),
              ).some((token) => identity.text.includes(token)),
            )
            .slice(0, 8)
        : [],
      user_text: undefined,
      assistant_text: undefined,
      assistant_answer_texts: undefined,
      assistant_answer_identities: undefined,
      body_text: undefined,
    })}`,
  );
}

async function waitForWorks(win, expected, timeoutMs) {
  const startedAt = Date.now();
  const deadline = startedAt + timeoutMs;
  let latest = null;
  await win.loadURL(`${expected.baseUrl}/#/works`);
  while (Date.now() < deadline) {
    latest = await win.webContents.executeJavaScript(
      `
        (() => ({
          location_hash: window.location.hash,
          body_text: document.body?.innerText || "",
        }))()
      `,
      true,
    );
    const titleCount = countOccurrences(latest.body_text, expected.title);
    if (
      latest.location_hash.includes("/works") &&
      titleCount === expected.versionCount &&
      ["V1", "V2", "V3"].every((version) => latest.body_text.includes(version))
    ) {
      return {
        ready_ms: Date.now() - startedAt,
        title_count: titleCount,
        versions_visible: ["V1", "V2", "V3"],
      };
    }
    await new Promise((resolve) => setTimeout(resolve, 80));
  }
  throw new Error(
    `Works renderer did not expose all versions: ${JSON.stringify({
      location_hash: latest?.location_hash,
      title_count: latest
        ? countOccurrences(latest.body_text, expected.title)
        : 0,
    })}`,
  );
}

async function waitForSemanticArtifacts(
  win,
  expected,
  timeoutMs,
  screenshotDir,
) {
  const startedAt = Date.now();
  const deadline = startedAt + timeoutMs;
  let latest = null;
  await win.loadURL(
    `${expected.baseUrl}/#/chat/${encodeURIComponent(expected.chatId)}`,
  );
  while (Date.now() < deadline) {
    latest = await win.webContents.executeJavaScript(
      `
        (() => ({
          location_hash: window.location.hash,
          cards: Array.from(document.querySelectorAll(
            '[data-lantai-artifact="text"]',
          )).map((card) => ({
            kind: card.getAttribute('data-lantai-artifact-kind') || '',
            title: card.querySelector('.lantai-artifact-title strong')?.textContent || '',
            text: card.textContent || '',
            copy_button: Array.from(card.querySelectorAll('button')).some(
              (button) => (button.getAttribute('aria-label') || '').startsWith('复制'),
            ),
            download_button: Array.from(card.querySelectorAll('button')).some(
              (button) => (button.getAttribute('aria-label') || '') === '下载文本',
            ),
          })),
        }))()
      `,
      true,
    );
    const kinds = latest.cards.map((card) => card.kind).sort();
    if (
      latest.location_hash.includes(encodeURIComponent(expected.chatId)) &&
      latest.cards.length === expected.count &&
      JSON.stringify(kinds) === JSON.stringify(expected.kinds) &&
      latest.cards.every(
        (card) =>
          card.title && card.text && card.copy_button && card.download_button,
      )
    ) {
      break;
    }
    await new Promise((resolve) => setTimeout(resolve, 80));
  }
  if (!latest || latest.cards.length !== expected.count) {
    throw new Error(
      `semantic artifact cards did not settle: ${JSON.stringify(latest)}`,
    );
  }

  await win.webContents.executeJavaScript(
    `
      (() => {
        window.__lantaiArtifactCopies = [];
        Object.defineProperty(navigator, 'clipboard', {
          configurable: true,
          value: {
            writeText: async (text) => {
              window.__lantaiArtifactCopies.push(String(text));
            },
          },
        });
      })()
    `,
    true,
  );

  const downloadRoot = path.join(screenshotDir, "semantic-artifact-downloads");
  await fs.rm(downloadRoot, { recursive: true, force: true });
  await fs.mkdir(downloadRoot, { recursive: true });
  const downloads = [];
  const onDownload = (_event, item) => {
    const record = {
      filename: item.getFilename(),
      path: path.join(downloadRoot, item.getFilename()),
      state: "pending",
    };
    downloads.push(record);
    item.setSavePath(record.path);
    item.once("done", (_doneEvent, state) => {
      record.state = state;
    });
  };
  session.defaultSession.on("will-download", onDownload);
  try {
    await win.webContents.executeJavaScript(
      `
        (() => {
          const cards = Array.from(document.querySelectorAll(
            '[data-lantai-artifact="text"]',
          ));
          for (const card of cards) {
            const buttons = Array.from(card.querySelectorAll('button'));
            buttons.find((button) =>
              (button.getAttribute('aria-label') || '').startsWith('复制'),
            )?.click();
            buttons.find((button) =>
              (button.getAttribute('aria-label') || '') === '下载文本',
            )?.click();
          }
        })()
      `,
      true,
    );
    while (
      Date.now() < deadline &&
      (downloads.length !== expected.count ||
        downloads.some((record) => record.state === "pending"))
    ) {
      await new Promise((resolve) => setTimeout(resolve, 80));
    }
  } finally {
    session.defaultSession.removeListener("will-download", onDownload);
  }

  const copies = await win.webContents.executeJavaScript(
    "window.__lantaiArtifactCopies || []",
    true,
  );
  assert.equal(copies.length, expected.count);
  assert.equal(downloads.length, expected.count);
  assert.equal(
    downloads.every((record) => record.state === "completed"),
    true,
  );
  assert.equal(
    copies.every((content) => content && !content.includes("~>[")),
    true,
  );
  const downloadedContents = [];
  for (const record of downloads) {
    assert.equal(record.filename.endsWith(".txt"), true);
    downloadedContents.push(await fs.readFile(record.path, "utf8"));
  }
  assert.deepEqual([...downloadedContents].sort(), [...copies].sort());
  if (screenshotDir) {
    const firstCard = await win.webContents.executeJavaScript(
      `
        (() => {
          const card = document.querySelector('[data-lantai-artifact="text"]');
          card?.scrollIntoView({ block: 'start' });
          return Boolean(card);
        })()
      `,
      true,
    );
    assert.equal(firstCard, true);
    const image = await win.webContents.capturePage();
    await fs.writeFile(
      path.join(screenshotDir, "semantic-artifacts.png"),
      image.toPNG(),
    );
  }
  return {
    ready_ms: Date.now() - startedAt,
    card_count: latest.cards.length,
    kinds: latest.cards.map((card) => card.kind).sort(),
    titles: latest.cards.map((card) => card.title),
    copy_action_count: copies.length,
    download_action_count: downloads.length,
    copy_download_contents_match: true,
    ququ_excluded_from_copy_download: true,
    downloaded_files: downloads.map((record) => record.filename).sort(),
  };
}

async function waitForResearchAdvisory(
  win,
  expected,
  timeoutMs,
  screenshotDir,
) {
  const startedAt = Date.now();
  const deadline = startedAt + timeoutMs;
  let latest = null;
  await win.loadURL(
    `${expected.baseUrl}/#/chat/${encodeURIComponent(expected.chatId)}`,
  );
  while (Date.now() < deadline) {
    latest = await win.webContents.executeJavaScript(
      `
        (() => {
          const notes = Array.from(document.querySelectorAll(
            '[data-lantai-research-advisory="1"]',
          ));
          const documents = Array.from(document.querySelectorAll(
            '[data-lantai-artifact="document"]',
          ));
          return {
            location_hash: window.location.hash,
            document_count: documents.length,
            advisory_count: notes.length,
            advisories: notes.map((note) => ({
              label: note.querySelector('strong')?.textContent || '',
              body: note.querySelector('p')?.textContent || '',
              outside_document: !documents.some((document) =>
                document.contains(note),
              ),
            })),
          };
        })()
      `,
      true,
    );
    if (
      latest.location_hash.includes(encodeURIComponent(expected.chatId)) &&
      latest.document_count === 1 &&
      latest.advisory_count === 1 &&
      latest.advisories[0]?.label === expected.label &&
      latest.advisories[0]?.body === expected.body &&
      latest.advisories[0]?.outside_document === true
    ) {
      break;
    }
    await new Promise((resolve) => setTimeout(resolve, 80));
  }
  if (
    !latest ||
    latest.document_count !== 1 ||
    latest.advisory_count !== 1 ||
    latest.advisories[0]?.body !== expected.body
  ) {
    throw new Error(
      `research advisory did not settle: ${JSON.stringify(latest)}`,
    );
  }
  if (screenshotDir) {
    const visible = await win.webContents.executeJavaScript(
      `
        (() => {
          const note = document.querySelector(
            '[data-lantai-research-advisory="1"]',
          );
          note?.scrollIntoView({ block: 'center' });
          return Boolean(note);
        })()
      `,
      true,
    );
    assert.equal(visible, true);
    const image = await win.webContents.capturePage();
    await fs.writeFile(
      path.join(screenshotDir, "research-advisory.png"),
      image.toPNG(),
    );
  }
  return {
    ready_ms: Date.now() - startedAt,
    document_count: latest.document_count,
    advisory_count: latest.advisory_count,
    label: latest.advisories[0].label,
    body: latest.advisories[0].body,
    outside_document: latest.advisories[0].outside_document,
    duplicate_count: Math.max(0, latest.advisory_count - 1),
  };
}

async function main() {
  const args = parseArgs(process.argv);
  const baseUrl = String(args["base-url"] || "").replace(/\/$/, "");
  const desktopToken = String(args["desktop-token"] || "");
  const chatId = String(args["chat-id"] || "");
  const turns = Number(args.turns || 0);
  const firstPrompt = String(args["first-prompt"] || "");
  const lastPrompt = String(args["last-prompt"] || "");
  const firstAnswer = String(args["first-answer"] || "");
  const lastAnswer = String(args["last-answer"] || "");
  const answerPrefix = String(args["answer-prefix"] || "");
  const ququPrefix = String(args["ququ-prefix"] || "");
  const expectedQuquCount = Number(args["expected-ququ-count"] || 0);
  const artifactChatId = String(args["artifact-chat-id"] || "");
  const artifactCount = Number(args["artifact-count"] || 0);
  const researchChatId = String(args["research-chat-id"] || "");
  const researchAdvisoryLabel = String(args["research-advisory-label"] || "");
  const researchAdvisoryBody = String(args["research-advisory-body"] || "");
  const workTitle = String(args["work-title"] || "");
  const workVersionCount = Number(args["work-version-count"] || 0);
  const timeoutMs = Number(args["timeout-ms"] || 90000);
  const screenshotDir = String(args["screenshot-dir"] || "");
  if (
    !baseUrl ||
    !desktopToken ||
    !chatId ||
    !Number.isInteger(turns) ||
    turns < 6 ||
    !firstPrompt ||
    !lastPrompt ||
    !firstAnswer ||
    !lastAnswer ||
    !answerPrefix ||
    !ququPrefix ||
    !artifactChatId ||
    artifactCount !== 4 ||
    !researchChatId ||
    !researchAdvisoryLabel ||
    !researchAdvisoryBody ||
    !workTitle ||
    workVersionCount !== 3
  ) {
    throw new Error("invalid companion depth renderer arguments");
  }

  const origin = new URL(baseUrl).origin;
  const consoleErrors = [];
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

  const win = new BrowserWindow({
    width: 1280,
    height: 800,
    show: false,
    title: "Companion Depth Renderer Smoke",
    webPreferences: {
      sandbox: false,
      contextIsolation: true,
      nodeIntegration: false,
    },
  });
  win.webContents.on("console-message", (event, ...legacy) => {
    const details = normalizeConsoleMessage(event, ...legacy);
    if (details.level >= 2) consoleErrors.push(details.message);
  });
  try {
    await win.loadURL(`${baseUrl}/#/chat/${encodeURIComponent(chatId)}`);
    const longChat = await waitForSnapshot(
      win,
      {
        chatId,
        turns,
        ququCount: expectedQuquCount,
        answerPrefix,
        ququPrefix,
        firstPrompt,
        lastPrompt,
        firstAnswer,
        lastAnswer,
      },
      timeoutMs,
    );
    assert.equal(countOccurrences(longChat.user_text, firstPrompt), 1);
    assert.equal(countOccurrences(longChat.user_text, lastPrompt), 1);
    assert.equal(countOccurrences(longChat.assistant_text, firstAnswer), 1);
    assert.equal(countOccurrences(longChat.assistant_text, lastAnswer), 1);
    assert.equal(
      countOccurrences(longChat.assistant_text, answerPrefix),
      turns,
    );
    assert.equal(
      countOccurrences(longChat.assistant_text, ququPrefix),
      expectedQuquCount,
    );
    if (screenshotDir) {
      await fs.mkdir(screenshotDir, { recursive: true });
      const image = await win.webContents.capturePage();
      await fs.writeFile(
        path.join(screenshotDir, "long-chat.png"),
        image.toPNG(),
      );
    }

    const semanticArtifacts = await waitForSemanticArtifacts(
      win,
      {
        baseUrl,
        chatId: artifactChatId,
        count: artifactCount,
        kinds: ["copy", "correspondence", "creative", "script"],
      },
      timeoutMs,
      screenshotDir,
    );

    const researchAdvisory = await waitForResearchAdvisory(
      win,
      {
        baseUrl,
        chatId: researchChatId,
        label: researchAdvisoryLabel,
        body: researchAdvisoryBody,
      },
      timeoutMs,
      screenshotDir,
    );

    const works = await waitForWorks(
      win,
      {
        baseUrl,
        title: workTitle,
        versionCount: workVersionCount,
      },
      timeoutMs,
    );
    if (screenshotDir) {
      const image = await win.webContents.capturePage();
      await fs.writeFile(
        path.join(screenshotDir, "works-lineage.png"),
        image.toPNG(),
      );
    }
    assert.deepEqual(consoleErrors, []);
    console.log(
      JSON.stringify(
        {
          ok: true,
          electron: process.versions.electron,
          chrome: process.versions.chrome,
          long_chat: {
            ready_ms: longChat.ready_ms,
            user_count: longChat.user_count,
            assistant_count: longChat.assistant_count,
            answer_count: longChat.answer_count,
            empty_assistant_count: longChat.empty_assistant_count,
            navigator_visible: longChat.navigator_visible,
            navigator_marker_count: longChat.navigator_marker_count,
            answer_prefix_occurrences: countOccurrences(
              longChat.assistant_text,
              answerPrefix,
            ),
            ququ_prefix_occurrences: countOccurrences(
              longChat.assistant_text,
              ququPrefix,
            ),
          },
          semantic_artifacts: semanticArtifacts,
          research_advisory: researchAdvisory,
          works,
          console_error_count: consoleErrors.length,
          screenshots: screenshotDir
            ? [
                "long-chat.png",
                "semantic-artifacts.png",
                "research-advisory.png",
                "works-lineage.png",
              ]
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

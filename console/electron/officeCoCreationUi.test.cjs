const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const vendorRoot = path.resolve(
  __dirname,
  "../vendor/lantai-docx-editor/apps",
);
const officeRoot = path.resolve(vendorRoot, "..");

test("all Office surfaces use the primary-chat Shen Xingzhi avatar", () => {
  const avatar = fs.readFileSync(path.join(officeRoot, "packages/ui/src/OfficeAgentAvatar.tsx"), "utf8");
  assert.match(avatar, /public\/lantai\.png/);
  assert.equal(fs.existsSync(path.resolve(officeRoot, "../../public/lantai.png")), true);

  const sources = [
    "docs/src/renderer/App.tsx",
    "docs/src/renderer/ai/AiPanel.tsx",
    "sheets/src/renderer/ExcelShell.tsx",
    "sheets/src/renderer/ai/AiChatPanel.tsx",
    "slides/src/renderer/components/Ribbon.tsx",
    "slides/src/renderer/ai/AiPanel.tsx",
    "pdf/src/renderer/App.tsx",
    "pdf/src/renderer/ai/AiPanel.tsx",
  ];
  for (const relativePath of sources) {
    const source = fs.readFileSync(path.join(vendorRoot, relativePath), "utf8");
    assert.match(source, /OfficeAgentAvatar/);
  }
});

test("panel capability shortcuts invoke existing real Agent execution paths", () => {
  const component = fs.readFileSync(
    path.join(officeRoot, "packages/ui/src/OfficeAgentCapabilities.tsx"),
    "utf8",
  );
  assert.match(component, /onClick=\{action\.onInvoke\}/);
  assert.match(component, /action\.available !== false/);
  assert.match(component, /data-office-agent-evidence=\{action\.evidence\}/);

  const expectations = [
    ["docs/src/renderer/ai/AiPanel.tsx", /runWith\(\s*t\(["']aiSummarizePrompt["']\),\s*t\(["']aiSummarizePrompt["']\),\s*["']ask["'],?\s*\)/, /runWith\(\s*t\(["']aiPolishPrompt["']\),\s*t\(["']aiPolishPrompt["']\),\s*["']suggest["'],?\s*\)/, /runWith\(\s*t\(["']aiTidyPrompt["']\),\s*t\(["']aiTidyPrompt["']\),\s*["']suggest["'],?\s*\)/],
    ["sheets/src/renderer/ai/AiChatPanel.tsx", /onSend\(t\(["']aiCheckPrompt["']\),\s*["']ask["']\)/, /onSend\(t\(["']aiAnalyzePrompt["']\),\s*["']ask["']\)/],
    ["slides/src/renderer/ai/AiPanel.tsx", /runWith\(t\(["']aiBeautifyPrompt["']\),\s*t\(["']aiBeautifyPrompt["']\),\s*\{[\s\S]*?slideShot:\s*true,[\s\S]*?mode:\s*["']execute["']/, /runWith\(t\(["']aiFactCheckPrompt["']\),\s*t\(["']aiFactCheckPrompt["']\),\s*\{\s*mode:\s*["']ask["'],?\s*\}\)/, /runWith\(t\(["']aiImagePrompt["']\),\s*t\(["']aiImagePrompt["']\),\s*\{\s*mode:\s*["']execute["'],?\s*\}\)/],
    ["pdf/src/renderer/ai/AiPanel.tsx", /hasDocument &&/, /send\(t\(["']aiQuickSummaryPrompt["']\),\s*["']ask["']\)/, /send\(t\(["']aiQuickKeyPointsPrompt["']\),\s*["']ask["']\)/],
  ];
  for (const [relativePath, ...patterns] of expectations) {
    const source = fs.readFileSync(path.join(vendorRoot, relativePath), "utf8");
    assert.match(source, /<OfficeAgentCapabilities/);
    for (const pattern of patterns) assert.match(source, pattern);
  }
});

test("all Office Agent panels show the same factual run receipt", () => {
  const receipt = fs.readFileSync(
    path.join(officeRoot, "packages/ui/src/OfficeAgentRunReceipt.tsx"),
    "utf8",
  );
  assert.match(receipt, /execution—not an unverified save or delivery claim/);
  assert.match(receipt, /receipt\.failedCount/);
  assert.match(receipt, /receipt\.mutationCount/);
  assert.match(receipt, /rollbackAvailable/);

  for (const relativePath of [
    "docs/src/renderer/ai/AiPanel.tsx",
    "sheets/src/renderer/ai/AiChatPanel.tsx",
    "slides/src/renderer/ai/AiPanel.tsx",
    "pdf/src/renderer/ai/AiPanel.tsx",
  ]) {
    const source = fs.readFileSync(path.join(vendorRoot, relativePath), "utf8");
    assert.match(source, /<OfficeAgentRunReceipt\s+receipt=\{entry\.receipt\}\s+rollbackAvailable\s*\/>/);
  }

  const styles = fs.readFileSync(
    path.join(officeRoot, "packages/ui/src/office-shell.css"),
    "utf8",
  );
  assert.match(styles, /\.lt-office-agent-receipt\s*\{/);
  assert.match(styles, /\.lt-office-agent-receipt\.warning/);
});

test("every Office Agent panel exposes the model bound to this editor window", () => {
  const binding = fs.readFileSync(
    path.join(officeRoot, "packages/ui/src/OfficeAgentModelBinding.tsx"),
    "utf8",
  );
  assert.match(binding, /lantaiModelBinding/);
  assert.match(binding, /lantaiModelOptions/);
  assert.match(binding, /切换当前编辑窗口模型/);
  assert.match(binding, /当前模型/);
  assert.match(binding, /data-office-model-binding/);

  for (const relativePath of [
    "docs/src/renderer/ai/AiPanel.tsx",
    "sheets/src/renderer/ai/AiChatPanel.tsx",
    "slides/src/renderer/ai/AiPanel.tsx",
    "pdf/src/renderer/ai/AiPanel.tsx",
  ]) {
    const source = fs.readFileSync(path.join(vendorRoot, relativePath), "utf8");
    assert.match(source, /data-office-agent-persona="沈行知">沈行知<\/span>/);
    assert.match(source, /OfficeAgentModelBinding/);
    assert.match(source, /onSelect=/);
  }

  const styles = fs.readFileSync(
    path.join(officeRoot, "packages/ui/src/office-shell.css"),
    "utf8",
  );
  assert.match(styles, /\.lt-office-agent-model-binding\s*\{/);
});

test("real Agent busy state reaches the persistent entry with elapsed feedback", () => {
  const entry = fs.readFileSync(
    path.join(officeRoot, "packages/ui/src/OfficeAgentEntry.tsx"),
    "utf8",
  );
  assert.match(entry, /elapsedSeconds/);
  assert.match(entry, /aria-busy=\{busy \|\| undefined\}/);

  const sources = [
    "docs/src/renderer/ai/AiPanel.tsx",
    "slides/src/renderer/ai/AiPanel.tsx",
    "pdf/src/renderer/ai/AiPanel.tsx",
  ];
  for (const relativePath of sources) {
    const source = fs.readFileSync(path.join(vendorRoot, relativePath), "utf8");
    assert.match(source, /onBusyChange\?\.\(busy\)/);
  }
  const sheets = fs.readFileSync(path.join(vendorRoot, "sheets/src/renderer/ExcelShell.tsx"), "utf8");
  assert.match(sheets, /<OfficeAgentEntry[\s\S]*busy=\{aiBusy\}/);
});

test("Office apps use one Lantai title-row entry and retire legacy Home AI groups", () => {
  const entries = [
    "docs/src/renderer/App.tsx",
    "sheets/src/renderer/ExcelShell.tsx",
    "slides/src/renderer/components/Ribbon.tsx",
    "pdf/src/renderer/App.tsx",
  ];
  for (const relativePath of entries) {
    const source = fs.readFileSync(path.join(vendorRoot, relativePath), "utf8");
    assert.match(source, /<OfficeAgentEntry/);
    assert.match(source, /收起沈行知/);
  }

  const retiredGroups = [
    ["docs/src/renderer/components/Ribbon.tsx", /LEGACY_RIBBON_AI_ENTRIES_ENABLED = false/],
    ["sheets/src/renderer/ExcelShell.tsx", /LEGACY_HOME_AI_GROUP_ENABLED = false/],
    ["slides/src/renderer/components/RibbonHomeTab.tsx", /LEGACY_HOME_AI_GROUP_ENABLED = false/],
  ];
  for (const [relativePath, pattern] of retiredGroups) {
    const source = fs.readFileSync(path.join(vendorRoot, relativePath), "utf8");
    assert.match(source, pattern);
  }

  const pdfSource = fs.readFileSync(
    path.join(vendorRoot, "pdf/src/renderer/App.tsx"),
    "utf8",
  );
  assert.doesNotMatch(pdfSource, /lt-office-legacy-ai-group|LantaiMark/);

  const styles = fs.readFileSync(
    path.join(vendorRoot, "slides/src/renderer/styles.css"),
    "utf8",
  );
  assert.match(styles, /\.stage-ai-bar\s*\{[^}]*display:\s*none;/s);
  assert.match(styles, /\.ai-msg-user\s*\{[^}]*background:\s*#536f90;/s);
});

test("Office Agent has one title-row launcher and no duplicate collapsed avatar rail", () => {
  const hiddenPanels = [
    "docs/src/renderer/ai/AiPanel.tsx",
    "sheets/src/renderer/ai/AiChatPanel.tsx",
    "slides/src/renderer/ai/AiPanel.tsx",
  ];
  for (const relativePath of hiddenPanels) {
    const source = fs.readFileSync(path.join(vendorRoot, relativePath), "utf8");
    assert.match(source, /if \(!(?:open|isOpen)\) return null/);
    assert.doesNotMatch(source, /className="(?:ai-rail|expand-copilot)"/);
  }

  for (const relativePath of [
    "docs/src/renderer/App.tsx",
    "slides/src/renderer/App.tsx",
  ]) {
    const source = fs.readFileSync(path.join(vendorRoot, relativePath), "utf8");
    assert.doesNotMatch(source, /className="ai-rail"/);
  }

  const dockContracts = [
    ["docs/src/renderer/styles.css", /\.ai-dock\.collapsed\s*\{[^}]*width:\s*0;/s],
    ["slides/src/renderer/styles.css", /\.ai-dock\.collapsed\s*\{[^}]*width:\s*0;/s],
    [
      "sheets/src/renderer/styles.css",
      /\.app-shell\.copilot-collapsed \.sheet-body\s*\{[^}]*grid-template-columns:\s*minmax\(0, 1fr\) 0;/s,
    ],
    ["pdf/src/renderer/styles.css", /\.ai-dock\.collapsed\s*\{[^}]*width:\s*0;/s],
  ];
  for (const [relativePath, pattern] of dockContracts) {
    const source = fs.readFileSync(path.join(vendorRoot, relativePath), "utf8");
    assert.match(source, pattern);
  }
});

test("all Office renderers opt into the shared Lantai shell contract", () => {
  for (const app of ["docs", "sheets", "slides", "pdf"]) {
    const rendererRoot = path.join(vendorRoot, app, "src/renderer");
    const main = fs.readFileSync(path.join(rendererRoot, "main.tsx"), "utf8");
    const html = fs.readFileSync(path.join(rendererRoot, "index.html"), "utf8");
    assert.match(main, /import '@genoffice\/ui\/office-shell\.css'/);
    assert.match(html, /data-lantai-office-shell="v1"/);
    assert.match(html, new RegExp(`data-office-app="${app}"`));
  }
});

test("the Office shell contract owns shared visual and accessibility states", () => {
  const styles = fs.readFileSync(
    path.join(officeRoot, "packages/ui/src/office-shell.css"),
    "utf8",
  );
  const uiPackage = JSON.parse(
    fs.readFileSync(path.join(officeRoot, "packages/ui/package.json"), "utf8"),
  );

  assert.equal(uiPackage.exports["./office-shell.css"], "./src/office-shell.css");
  assert.match(styles, /--lt-office-accent:\s*#536f90/);
  assert.match(styles, /\.lt-office-agent-entry\s*\{/);
  assert.match(styles, /\.lt-office-agent-capabilities\s*\{/);
  assert.match(styles, /\.lt-office-file-title\s*\{/);
  assert.match(styles, /--lt-office-ribbon-tab-row-height:\s*35px/);
  assert.match(styles, /--lt-office-ribbon-command-height:\s*80px/);
  assert.match(styles, /--lt-office-ribbon-tab-font-size:\s*13px/);
  assert.match(styles, /--lt-office-ribbon-command-font-size:\s*12px/);
  assert.match(
    styles,
    /\[data-office-app=["']sheets["']\]\s+\.excel-header\s*>\s*\.ribbon/,
  );
  assert.match(styles, /\.ai-msg-user\s*\{[^}]*background:\s*var\(--lt-office-accent\)/s);
  assert.match(styles, /\.file-menu\s*\{/);
  assert.match(styles, /\.app-toast\s*\{/);
  assert.match(styles, /\.lt-office-state\s*\{/);
  assert.match(styles, /:focus-visible/);
  assert.match(
    styles,
    /\.ai-input-box textarea:focus-visible\s*\{[^}]*outline:\s*none;/s,
  );
  assert.match(styles, /\.ai-composer\s*\{[^}]*flex:\s*0 0 auto;/s);
  assert.match(styles, /prefers-reduced-motion:\s*reduce/);
  assert.doesNotMatch(styles, /transition:\s*all/);
}
);

test("Sheets and Slides reserve a full-width status row below the Agent composer", () => {
  const sheetsSource = fs.readFileSync(
    path.join(vendorRoot, "sheets/src/renderer/ExcelShell.tsx"),
    "utf8",
  );
  const sheetsStyles = fs.readFileSync(
    path.join(vendorRoot, "sheets/src/renderer/styles.css"),
    "utf8",
  );
  assert.match(sheetsSource, /<div className="sheet-main">[\s\S]*<\/div>[\s\S]*<footer className="status-bar">/);
  assert.match(sheetsStyles, /\.sheet-body\s*\{[^}]*grid-template-rows:\s*minmax\(0,\s*1fr\) auto;/s);
  assert.match(sheetsStyles, /\.status-bar\s*\{[^}]*grid-column:\s*1 \/ -1;[^}]*grid-row:\s*2;/s);
  assert.match(sheetsStyles, /\.sheet-body > \.copilot\s*\{[^}]*grid-row:\s*1;/s);

  const slidesSource = fs.readFileSync(
    path.join(vendorRoot, "slides/src/renderer/App.tsx"),
    "utf8",
  );
  const slidesStyles = fs.readFileSync(
    path.join(vendorRoot, "slides/src/renderer/styles.css"),
    "utf8",
  );
  assert.match(slidesSource, /<div className="app-content">[\s\S]*<\/div>[\s\S]*<footer className="status-bar">/);
  assert.match(slidesStyles, /\.app-main\s*\{[^}]*grid-template-rows:\s*minmax\(0,\s*1fr\) auto;/s);
  assert.match(slidesStyles, /\.app-main > \.status-bar\s*\{[^}]*grid-column:\s*1 \/ -1;[^}]*grid-row:\s*2;/s);
  assert.match(slidesStyles, /\.ai-dock\s*\{[^}]*grid-row:\s*1;/s);
});

test("PDF uses the shared loading, empty and error state with live semantics", () => {
  const app = fs.readFileSync(path.join(vendorRoot, "pdf/src/renderer/App.tsx"), "utf8");
  const panel = fs.readFileSync(path.join(vendorRoot, "pdf/src/renderer/ai/AiPanel.tsx"), "utf8");
  const state = fs.readFileSync(
    path.join(officeRoot, "packages/ui/src/OfficeShellState.tsx"),
    "utf8",
  );
  assert.match(app, /<OfficeShellState title=\{stateTitle\} tone=\{stateTone\} \/>/);
  assert.match(state, /role=\{isError \? 'alert' : 'status'\}/);
  assert.match(state, /aria-busy=\{isLoading \|\| undefined\}/);
  assert.match(panel, /window\.innerWidth >= PANEL_WIDTH_MIN/);
  assert.match(panel, /Math\.max\(\s*PANEL_WIDTH_MIN,/);
});

test("Office co-creation protocol keeps ask and suggest inert and execute transactional", () => {
  const contract = fs.readFileSync(
    path.join(officeRoot, "packages/ui/src/OfficeCoCreationContract.ts"),
    "utf8",
  );
  assert.match(contract, /OFFICE_CO_CREATION_PROTOCOL_VERSION = ["']2\.0["']/);
  assert.match(contract, /mode === ["']execute["']/);
  assert.match(contract, /READ-ONLY MODE:[^\n]*do not call any mutating/s);
  assert.match(contract, /SUGGESTION MODE:[^\n]*Do not mutate/s);
  for (const stage of ["snapshot", "plan", "preview", "validate", "apply", "reopen", "verify", "commit"]) {
    assert.match(contract, new RegExp(`["']${stage}["']`));
  }
  assert.match(contract, /otherwise roll back visibly/);
  assert.match(contract, /idempotency key/);
  assert.match(contract, /work on a copy/);
});

test("Office suggestion mode distinguishes plain answers from executable proposals", () => {
  const contract = fs.readFileSync(
    path.join(officeRoot, "packages/ui/src/OfficeCoCreationContract.ts"),
    "utf8",
  );
  assert.match(contract, /OfficeCoCreationResponseKind/);
  assert.match(contract, /LANTAI_OFFICE_RESPONSE:ANSWER/);
  assert.match(contract, /LANTAI_OFFICE_RESPONSE:PROPOSAL/);
  assert.match(contract, /parseOfficeCoCreationResponse/);
  assert.match(contract, /:\s*["']answer["'],/);

  const sources = [
    "docs/src/renderer/ai/AiPanel.tsx",
    "sheets/src/renderer/ai/AiChatPanel.tsx",
    "sheets/src/renderer/App.tsx",
    "slides/src/renderer/ai/AiPanel.tsx",
    "pdf/src/renderer/ai/AiPanel.tsx",
  ];
  for (const relativePath of sources) {
    const source = fs.readFileSync(path.join(vendorRoot, relativePath), "utf8");
    assert.match(source, /responseKind/);
    if (!relativePath.endsWith("/AiChatPanel.tsx")) {
      assert.match(source, /parseOfficeCoCreationResponse/);
    }
  }

  for (const relativePath of sources.filter((source) => !source.endsWith("/App.tsx"))) {
    const source = fs.readFileSync(path.join(vendorRoot, relativePath), "utf8");
    assert.match(source, /entry\.responseKind === ["']proposal["']/);
  }
});

test("Office suite capability profiles define object scope and post-write validation", () => {
  const contract = fs.readFileSync(
    path.join(officeRoot, "packages/ui/src/OfficeCoCreationContract.ts"),
    "utf8",
  );
  const expectations = [
    ["docs", "selection", "revision"],
    ["sheets", "range", "recalculate"],
    ["slides", "element", "geometry"],
    ["pdf", "page-set", "page-count"],
    ["markdown", "block", "syntax"],
  ];
  for (const [suite, scope, validation] of expectations) {
    assert.match(contract, new RegExp(`${suite}: \\{[\\s\\S]*?${scope}[\\s\\S]*?${validation}`));
  }
});

test("all embedded Office Agent panels expose ask, suggest and execute through one shared contract", () => {
  const sources = [
    ["docs/src/renderer/ai/AiPanel.tsx", "docs"],
    ["sheets/src/renderer/ai/AiChatPanel.tsx", "sheets"],
    ["slides/src/renderer/ai/AiPanel.tsx", "slides"],
    ["pdf/src/renderer/ai/AiPanel.tsx", "pdf"],
  ];
  for (const [relativePath, suite] of sources) {
    const source = fs.readFileSync(path.join(vendorRoot, relativePath), "utf8");
    assert.match(source, /OfficeCoCreationModeBar/);
    assert.match(source, /OfficeSuggestionCard/);
    assert.match(source, new RegExp(`suite=[{]?['\"]${suite}['\"]`));
  }

  const sheetsApp = fs.readFileSync(path.join(vendorRoot, "sheets/src/renderer/App.tsx"), "utf8");
  assert.match(sheetsApp, /buildOfficeCoCreationInstruction\(instruction, mode, ["']sheets["']\)/);
  for (const [relativePath, suite] of sources.filter(([relativePath]) => !relativePath.startsWith("sheets/"))) {
    const source = fs.readFileSync(path.join(vendorRoot, relativePath), "utf8");
    assert.match(source, new RegExp(`buildOfficeCoCreationInstruction\\([^)]*['\"]${suite}['\"],?\\s*\\)`));
  }
});

test("Office suggestion cards are single-use review objects rather than implicit writes", () => {
  const card = fs.readFileSync(
    path.join(officeRoot, "packages/ui/src/OfficeSuggestionCard.tsx"),
    "utf8",
  );
  assert.match(card, /const actionable = localStatus === ["']pending["']/);
  assert.match(card, /transition\(["']refining["']\)/);
  assert.match(card, /transition\(["']rejected["']\)/);
  assert.match(card, /transition\(["']accepted["']\)/);
  assert.match(card, /确认并修改/);
  assert.match(card, /确认后只会按上方/);
  assert.match(card, /data-office-suggestion-id/);
  assert.match(card, /data-office-suggestion-status/);
  assert.match(card, /accepting a card must start a fresh execute-mode run/);
});

test("shared Office UI exports the co-creation mode and suggestion contracts", () => {
  const exports = fs.readFileSync(path.join(officeRoot, "packages/ui/src/index.ts"), "utf8");
  assert.match(exports, /OfficeCoCreationContract/);
  assert.match(exports, /OfficeCoCreationModeBar/);
  assert.match(exports, /OfficeSuggestionCard/);
  const styles = fs.readFileSync(path.join(officeRoot, "packages/ui/src/office-shell.css"), "utf8");
  assert.match(styles, /\.lt-office-co-creation-modebar\s*\{/);
  assert.match(styles, /\.lt-office-suggestion\s*\{/);
  assert.match(styles, /\.lt-office-suggestion-actions\s*\{/);
}
);

test("suite mutation engines retain conflict detection, undo and post-write verification", () => {
  const docsRewrite = fs.readFileSync(
    path.join(vendorRoot, "docs/src/renderer/lantai-double-write.ts"),
    "utf8",
  );
  const docsProposals = fs.readFileSync(
    path.join(vendorRoot, "docs/src/renderer/shen-rewrite-proposals.ts"),
    "utf8",
  );
  const docsSave = fs.readFileSync(
    path.join(vendorRoot, "docs/src/main/docs-main.ts"),
    "utf8",
  );
  assert.match(docsRewrite, /isDocxRewriteRangeCurrent/);
  assert.match(docsRewrite, /transaction\.mapping\.map/);
  assert.match(docsRewrite, /applyDocxRewriteDirectly/);
  assert.match(docsProposals, /hasShenRewriteOverlap/);
  assert.match(docsProposals, /phase:\s*mapped\.conflict \? 'conflict'/);
  assert.match(docsSave, /atomicWriteFile\(filePath, bytes\)/);

  const sheetsDomain = fs.readFileSync(
    path.join(vendorRoot, "sheets/src/domain/in-memory-workbook.ts"),
    "utf8",
  );
  const sheetsTools = fs.readFileSync(
    path.join(vendorRoot, "sheets/src/renderer/ai/tools.ts"),
    "utf8",
  );
  assert.match(sheetsDomain, /committedTransactions\.has\(plan\.transactionId\)/);
  assert.match(sheetsDomain, /undo\(\): CommitReceipt/);
  assert.match(sheetsDomain, /WorkbookConflictError/);
  assert.match(sheetsTools, /Read-back after write \(write → verify\)/);
  assert.match(sheetsTools, /undo via the side panel \[Undo\] button or ⌘Z/);

  const slidesSession = fs.readFileSync(
    path.join(vendorRoot, "slides/src/main/session-state.ts"),
    "utf8",
  );
  const slidesMain = fs.readFileSync(
    path.join(vendorRoot, "slides/src/main/slides-main.ts"),
    "utf8",
  );
  assert.match(slidesSession, /Begin a nestable transaction/);
  assert.match(slidesSession, /registerAiSnapshot/);
  assert.match(slidesSession, /restoreAiSnapshot/);
  assert.match(slidesMain, /slides:ai-snapshot-restore/);
  assert.match(slidesMain, /const rollback = \(\) =>/);

  const pdfSave = fs.readFileSync(
    path.join(vendorRoot, "pdf/src/main/save-pdf.ts"),
    "utf8",
  );
  assert.match(pdfSave, /atomically write the result to targetPath/);
  assert.match(pdfSave, /verifyContentEdits/);
  assert.match(pdfSave, /the file was not written/);
  assert.match(pdfSave, /await verifyContentEdits\(bytes, request, skips\)/);
});

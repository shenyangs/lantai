const editor = document.getElementById("editor");
const preview = document.getElementById("preview");
const workspace = document.getElementById("workspace");
const fileButton = document.getElementById("file-button");
const fileMenu = document.getElementById("file-menu");
const fileName = document.getElementById("file-name");
const status = document.getElementById("status");
const count = document.getElementById("count");
let currentPath = "";
let savedText = "";
let dirty = false;
let coCreationMode = "suggest";
let currentView = "preview";
let handoffContext = null;
const handoffButton = document.getElementById("handoff-button");
const handoffPanel = document.getElementById("handoff-panel");
const handoffConfirm = document.getElementById("handoff-confirm");

const escapeHtml = (value) =>
  value.replace(
    /[&<>"']/g,
    (char) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[
        char
      ],
  );
const inlineText = (value) =>
  escapeHtml(value)
    .replace(/\\\|/g, "|")
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/_([^_]+)_|\*([^*]+)\*/g, "<em>$1$2</em>")
    .replace(
      /\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g,
      '<a href="$2" rel="noreferrer">$1</a>',
    );

function inline(value) {
  // Protect code contents while allowing emphasis and links around code spans.
  let token = "\uE000code";
  while (value.includes(token)) token += "\uE000";
  const spans = [];
  const text = value.replace(/(`+)(.*?)\1/g, (_, ticks, contents) => {
    const index =
      spans.push(`<code>${escapeHtml(contents.replace(/\\\|/g, "|"))}</code>`) -
      1;
    return `${token}${index}\uE001`;
  });
  return inlineText(text).replace(
    new RegExp(`${token}(\\d+)\uE001`, "g"),
    (_, index) => spans[Number(index)],
  );
}

function tableCells(line) {
  const cells = [];
  let cell = "";
  let escaped = false;
  const text = line.trim();
  for (const char of text) {
    if (char === "|" && !escaped) {
      cells.push(cell.trim());
      cell = "";
    } else cell += char;
    escaped = char === "\\" && !escaped;
  }
  cells.push(cell.trim());
  if (text.startsWith("|")) cells.shift();
  if (cells.at(-1) === "" && text.endsWith("|")) cells.pop();
  return cells;
}

function renderMarkdown(source) {
  const lines = source.replace(/\r\n?/g, "\n").split("\n");
  const output = [];
  let inCode = false;
  let code = [];
  let list = null;
  const closeList = () => {
    if (list) output.push(`</${list}>`);
    list = null;
  };
  for (let index = 0; index < lines.length; index += 1) {
    const line = lines[index];
    if (/^```/.test(line)) {
      closeList();
      if (inCode) {
        output.push(`<pre><code>${escapeHtml(code.join("\n"))}</code></pre>`);
        code = [];
      }
      inCode = !inCode;
      continue;
    }
    if (inCode) {
      code.push(line);
      continue;
    }
    const separators = tableCells(lines[index + 1] || "");
    const headers = tableCells(line);
    if (
      line.includes("|") &&
      headers.length > 0 &&
      headers.length === separators.length &&
      separators.every((cell) => /^:?-{3,}:?$/.test(cell))
    ) {
      closeList();
      const alignment = separators.map((cell) =>
        cell.endsWith(":")
          ? cell.startsWith(":")
            ? "center"
            : "right"
          : "left",
      );
      const row = (cells, tag) =>
        `<tr>${headers
          .map(
            (_, column) =>
              `<${tag} class="align-${alignment[column]}">${inline(cells[column] || "")}</${tag}>`,
          )
          .join("")}</tr>`;
      output.push(
        '<div class="table-scroll" tabindex="0" role="region" aria-label="表格"><table><thead>',
      );
      output.push(row(headers, "th"), "</thead><tbody>");
      index += 1;
      while (
        index + 1 < lines.length &&
        lines[index + 1].trim() &&
        lines[index + 1].includes("|") &&
        !/^(?:#{1,6}\s|```|>\s?|\s*[-*+]\s|\s*\d+[.)]\s)/.test(lines[index + 1])
      ) {
        output.push(row(tableCells(lines[++index]), "td"));
      }
      output.push("</tbody></table></div>");
      continue;
    }
    const heading = /^(#{1,6})\s+(.+)$/.exec(line);
    if (heading) {
      closeList();
      const level = heading[1].length;
      output.push(`<h${level}>${inline(heading[2])}</h${level}>`);
      continue;
    }
    const unordered = /^\s*[-*+]\s+(.+)$/.exec(line);
    const ordered = /^\s*\d+[.)]\s+(.+)$/.exec(line);
    if (unordered || ordered) {
      const kind = unordered ? "ul" : "ol";
      if (list !== kind) {
        closeList();
        list = kind;
        output.push(`<${kind}>`);
      }
      output.push(`<li>${inline((unordered || ordered)[1])}</li>`);
      continue;
    }
    closeList();
    if (/^---+$/.test(line.trim())) output.push("<hr>");
    else if (/^>\s?/.test(line))
      output.push(
        `<blockquote>${inline(line.replace(/^>\s?/, ""))}</blockquote>`,
      );
    else if (line.trim()) output.push(`<p>${inline(line)}</p>`);
  }
  closeList();
  if (inCode)
    output.push(`<pre><code>${escapeHtml(code.join("\n"))}</code></pre>`);
  preview.innerHTML = output.join("\n");
}

function updateDirty(next) {
  dirty = next;
  window.markdownApi.setDirty(dirty);
  document.title = `${dirty ? "● " : ""}${currentPath.split(/[\\/]/).pop() || "澜台 Markdown"} — 澜台 Markdown`;
  status.textContent = dirty ? "尚未保存" : "已保存";
}

function loadDocument(payload) {
  setHandoffOpen(false);
  currentPath = payload.path;
  editor.value = payload.text;
  savedText = payload.text;
  fileName.textContent = currentPath.split(/[\\/]/).pop();
  fileName.title = currentPath;
  count.textContent = `${editor.value.length} 字符`;
  renderMarkdown(editor.value);
  updateDirty(false);
}

async function save(saveAs = false) {
  status.textContent = "正在保存…";
  const result = await window.markdownApi.save({ text: editor.value, saveAs });
  if (result?.success) {
    currentPath = result.path;
    savedText = editor.value;
    fileName.textContent = currentPath.split(/[\\/]/).pop();
    fileName.title = currentPath;
    updateDirty(false);
    return true;
  }
  if (!result?.canceled) status.textContent = result?.error || "保存失败";
  return false;
}

async function openDocument() {
  if (dirty && !(await save(false))) return;
  const payload = await window.markdownApi.chooseOpen();
  if (payload) loadDocument(payload);
}

function captureHandoff() {
  const start = editor.selectionStart;
  const end = editor.selectionEnd;
  const selection = window.getSelection();
  if (
    currentView !== "edit" &&
    selection &&
    !selection.isCollapsed &&
    preview.contains(selection.anchorNode) &&
    preview.contains(selection.focusNode)
  ) {
    return { text: selection.toString(), selectionOnly: true };
  }
  if (currentView !== "preview" && end > start) {
    return { text: editor.value.slice(start, end), selectionOnly: true };
  }
  return { text: editor.value, selectionOnly: false };
}

function setHandoffOpen(open, restoreFocus = false) {
  handoffPanel.hidden = !open;
  handoffButton.setAttribute("aria-expanded", String(open));
  if (open) {
    handoffContext = captureHandoff();
    document.getElementById("handoff-scope").textContent =
      handoffContext.selectionOnly
        ? `已选中 ${handoffContext.text.length} 字符`
        : "使用整份文稿";
    handoffPanel.querySelector("button.active").focus();
  } else if (restoreFocus) handoffButton.focus();
}

async function handoffToChat() {
  if (handoffConfirm.disabled) return;
  const context = handoffContext || captureHandoff();
  handoffConfirm.disabled = true;
  status.textContent = "正在移交给沈行知…";
  try {
    const result = await window.markdownApi.handoffToChat({
      mode: coCreationMode,
      ...context,
    });
    status.textContent = result?.success
      ? "已移交到主对话，请确认后发送"
      : result?.error || "移交失败，请重试";
    if (result?.success) setHandoffOpen(false, true);
  } catch {
    status.textContent = "移交失败，请重试";
  } finally {
    handoffConfirm.disabled = false;
  }
}

function setView(view) {
  if (!["edit", "preview", "split"].includes(view)) return;
  currentView = view;
  setHandoffOpen(false);
  workspace.className = `workspace view-${view}`;
  document.querySelectorAll(".view-button").forEach((button) => {
    const active = button.dataset.view === view;
    button.classList.toggle("active", active);
    button.setAttribute("aria-pressed", String(active));
  });
  if (view !== "edit") renderMarkdown(editor.value);
}

async function execute(command) {
  if (command === "open") await openDocument();
  else if (command === "save") await save(false);
  else if (command === "save-before-close")
    window.markdownApi.reportCloseSaved(await save(false));
  else if (command === "save-as") await save(true);
  else if (command.startsWith("view-")) setView(command.slice(5));
}

fileButton.addEventListener("click", () => {
  setHandoffOpen(false);
  const open = fileMenu.hidden;
  fileMenu.hidden = !open;
  fileButton.classList.toggle("open", open);
  fileButton.setAttribute("aria-expanded", String(open));
});
fileMenu.addEventListener("click", (event) => {
  const command = event.target.closest("button")?.dataset.command;
  if (!command) return;
  fileMenu.hidden = true;
  fileButton.classList.remove("open");
  fileButton.setAttribute("aria-expanded", "false");
  void execute(command);
});
document
  .getElementById("save-button")
  .addEventListener("click", () => void save(false));
document
  .querySelectorAll(".view-button")
  .forEach((button) =>
    button.addEventListener("click", () => setView(button.dataset.view)),
  );
document.querySelectorAll(".co-creation-mode button").forEach((button) => {
  button.addEventListener("click", () => {
    coCreationMode = button.dataset.mode || "suggest";
    document.querySelectorAll(".co-creation-mode button").forEach((item) => {
      const active = item === button;
      item.classList.toggle("active", active);
      item.setAttribute("aria-pressed", String(active));
    });
  });
});
document.getElementById("handoff-button").addEventListener("click", () => {
  closeFileMenu();
  setHandoffOpen(handoffPanel.hidden);
});
handoffConfirm.addEventListener("click", () => void handoffToChat());

function closeFileMenu() {
  fileMenu.hidden = true;
  fileButton.classList.remove("open");
  fileButton.setAttribute("aria-expanded", "false");
}

document.addEventListener("click", (event) => {
  if (!event.target.closest(".file-wrap")) closeFileMenu();
  if (!event.target.closest(".handoff-wrap")) setHandoffOpen(false);
});
document.addEventListener("keydown", (event) => {
  if (event.key !== "Escape") return;
  if (!handoffPanel.hidden) {
    setHandoffOpen(false, true);
    event.preventDefault();
  }
  if (!fileMenu.hidden) {
    closeFileMenu();
    fileButton.focus();
    event.preventDefault();
  }
});
editor.addEventListener("input", () => {
  count.textContent = `${editor.value.length} 字符`;
  renderMarkdown(editor.value);
  updateDirty(editor.value !== savedText);
});
editor.addEventListener("keydown", (event) => {
  if (event.key === "Tab") {
    event.preventDefault();
    const start = editor.selectionStart;
    editor.setRangeText("  ", start, editor.selectionEnd, "end");
    editor.dispatchEvent(new Event("input"));
  }
});
window.addEventListener("keydown", (event) => {
  if (!(event.metaKey || event.ctrlKey)) return;
  const key = event.key.toLowerCase();
  if (key === "s") {
    event.preventDefault();
    void save(event.shiftKey);
  } else if (key === "o") {
    event.preventDefault();
    void openDocument();
  }
});
setView("preview");
window.markdownApi.onCommand((command) => void execute(command));
window.markdownApi
  .consume()
  .then(loadDocument)
  .catch((error) => {
    status.textContent = error?.message || "打开失败";
  });

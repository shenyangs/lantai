const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { test } = require("node:test");
const { JSDOM } = require("jsdom");
const root = path.join(__dirname, "markdown-editor");

async function reader(text = "# 文稿\n\n正文") {
  const dom = new JSDOM(
    fs.readFileSync(path.join(root, "index.html"), "utf8"),
    { runScripts: "outside-only" },
  );
  const calls = { dirty: [], handoff: [], save: [], closed: [] };
  let command;
  dom.window.markdownApi = {
    setDirty: (value) => calls.dirty.push(value),
    consume: async () => ({ path: "/tmp/文稿.md", text }),
    save: async (value) => {
      calls.save.push(value);
      return { success: true, path: "/tmp/文稿.md" };
    },
    chooseOpen: async () => null,
    handoffToChat: async (value) => {
      calls.handoff.push(value);
      return { success: true };
    },
    reportCloseSaved: (value) => calls.closed.push(value),
    onCommand: (callback) => {
      command = callback;
    },
  };
  for (const script of dom.window.document.querySelectorAll("script[src]")) {
    dom.window.eval(
      fs.readFileSync(path.join(root, script.getAttribute("src")), "utf8"),
    );
  }
  await new Promise(setImmediate);
  return {
    dom,
    document: dom.window.document,
    calls,
    command,
    close: () => dom.window.close(),
  };
}

test("opens in reading view with exactly one matching pressed mode", async () => {
  const r = await reader();
  try {
    assert.equal(
      r.document.querySelector("#workspace").className,
      "workspace view-preview",
    );
    assert.equal(
      r.document.querySelectorAll('.view-button[aria-pressed="true"]').length,
      1,
    );
    assert.equal(
      r.document.querySelector(".view-button.active").dataset.view,
      "preview",
    );
    assert.equal(r.document.querySelector("#handoff-panel").hidden, true);
  } finally {
    r.close();
  }
});

test("renders GFM tables, alignment, escaped pipes and literal inline code safely", async () => {
  const r = await reader(
    "| 项目 | 内容 | 数值 |\n| :--- | :---: | ---: |\n| **状态** | `a_b*` | 42 |\n| a\\|b | <img src=x onerror=alert(1)> | 0 |\n\n结束",
  );
  try {
    const table = r.document.querySelector("#preview table");
    assert.ok(table, "valid table must render as a table");
    assert.equal(table.querySelectorAll("thead th").length, 3);
    assert.equal(table.querySelectorAll("tbody tr").length, 2);
    assert.equal(table.querySelector("code").textContent, "a_b*");
    assert.equal(
      table.querySelectorAll("tbody tr")[1].children[0].textContent,
      "a|b",
    );
    assert.equal(table.querySelector("th:last-child").className, "align-right");
    assert.equal(table.querySelector("img"), null);
  } finally {
    r.close();
  }
});

test("does not interpret fenced tables or malformed delimiters, and retains existing blocks", async () => {
  const r = await reader(
    "# 标题\n\n```md\n| a | b |\n|---|---|\n```\n\n| a | b |\n| nope | --- |\n\n- 一\n- 二\n\n> 引用\n\n---\n\n**粗体** [链接](https://example.com)",
  );
  try {
    const p = r.document.querySelector("#preview");
    assert.equal(p.querySelector("table"), null);
    assert.match(p.querySelector("pre").textContent, /\|---\|---\|/);
    assert.equal(p.querySelectorAll("li").length, 2);
    for (const tag of ["h1", "blockquote", "hr", "strong", "a"])
      assert.ok(p.querySelector(tag));
  } finally {
    r.close();
  }
});

test("view changes preserve unsaved source, native menu commands and save-before-close", async () => {
  const r = await reader();
  try {
    const editor = r.document.querySelector("#editor");
    editor.value = "# 修改\n\n未保存";
    editor.dispatchEvent(new r.dom.window.Event("input"));
    for (const mode of ["edit", "split", "preview"]) {
      r.command(`view-${mode}`);
      assert.equal(
        r.document.querySelector(".view-button.active").dataset.view,
        mode,
      );
      assert.equal(editor.value, "# 修改\n\n未保存");
    }
    assert.equal(r.calls.dirty.at(-1), true);
    r.command("save-before-close");
    await new Promise(setImmediate);
    assert.equal(r.calls.save[0].text, editor.value);
    assert.equal(r.calls.closed[0], true);
    assert.equal(r.calls.dirty.at(-1), false);
  } finally {
    r.close();
  }
});

test("handoff disclosure never sends on open, supports Escape, explicit mode and selection", async () => {
  const r = await reader("选中内容和其余正文");
  try {
    const doc = r.document;
    const trigger = doc.querySelector("#handoff-button");
    trigger.click();
    assert.equal(
      r.calls.handoff.length,
      0,
      "opening choices must not hand off",
    );
    assert.equal(doc.querySelector("#handoff-panel").hidden, false);
    doc.dispatchEvent(
      new r.dom.window.KeyboardEvent("keydown", {
        key: "Escape",
        bubbles: true,
      }),
    );
    assert.equal(doc.querySelector("#handoff-panel").hidden, true);
    assert.equal(doc.activeElement, trigger);
    doc.querySelector('[data-view="edit"]').click();
    doc.querySelector("#editor").setSelectionRange(0, 4);
    trigger.click();
    doc.querySelector('[data-mode="ask"]').click();
    doc.querySelector("#handoff-confirm").click();
    await new Promise(setImmediate);
    assert.equal(r.calls.handoff.length, 1);
    assert.equal(r.calls.handoff[0].mode, "ask");
    assert.equal(r.calls.handoff[0].text, "选中内容");
    assert.equal(r.calls.handoff[0].selectionOnly, true);
  } finally {
    r.close();
  }
});

test("reading handoff ignores stale hidden source selection", async () => {
  const r = await reader("完整的文稿内容");
  try {
    r.document.querySelector("#editor").setSelectionRange(0, 2);
    r.document.querySelector("#handoff-button").click();
    r.document.querySelector("#handoff-confirm").click();
    await new Promise(setImmediate);
    assert.equal(r.calls.handoff[0].text, "完整的文稿内容");
    assert.equal(r.calls.handoff[0].selectionOnly, false);
  } finally {
    r.close();
  }
});

test("tables stop before adjacent block content even when it contains pipes", async () => {
  const r = await reader(
    "| a | b |\n|---|---|\n| 1 | 2 |\n## 标题 | 说明\n\n| a | b |\n|---|---|\n```\nx | y\n```",
  );
  try {
    assert.equal(r.document.querySelectorAll("table").length, 2);
    assert.equal(r.document.querySelector("h2").textContent, "标题 | 说明");
    assert.equal(r.document.querySelector("pre").textContent, "x | y");
  } finally {
    r.close();
  }
});

test("reading selection is captured before disclosure moves keyboard focus", async () => {
  const r = await reader("选中正文与剩余内容");
  try {
    const range = r.document.createRange();
    const text = r.document.querySelector("#preview p").firstChild;
    range.setStart(text, 0);
    range.setEnd(text, 4);
    r.dom.window.getSelection().addRange(range);
    r.document.querySelector("#handoff-button").click();
    r.document.querySelector("#handoff-confirm").click();
    await new Promise(setImmediate);
    assert.equal(r.calls.handoff[0].text, "选中正文");
    assert.equal(r.calls.handoff[0].selectionOnly, true);
  } finally {
    r.close();
  }
});

test("literal code still nests within emphasis and link labels", async () => {
  const r = await reader("**使用 `a_b*`** [查看 `a_b`](https://example.com)");
  try {
    assert.equal(r.document.querySelector("strong code").textContent, "a_b*");
    assert.equal(r.document.querySelector("a code").textContent, "a_b");
    assert.equal(r.document.querySelector("a").href, "https://example.com/");
  } finally {
    r.close();
  }
});

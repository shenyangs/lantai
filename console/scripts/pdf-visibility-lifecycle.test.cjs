const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const ts = require("typescript");
const { JSDOM } = require("jsdom");
const React = require("react");
const { createRoot } = require("react-dom/client");

// Exercise the actual hook AND its App call sites without booting the PDF worker,
// IPC, and AI services. In particular, do not duplicate the enabled expressions.
const filename = path.resolve(
  __dirname,
  "../vendor/lantai-docx-editor/apps/pdf/src/renderer/App.tsx",
);
const source = fs.readFileSync(filename, "utf8");
const ast = ts.createSourceFile(
  filename,
  source,
  ts.ScriptTarget.Latest,
  true,
  ts.ScriptKind.TSX,
);
const hook = ast.statements.find(
  (n) => ts.isFunctionDeclaration(n) && n.name?.text === "useVisibleSet",
);
assert.ok(hook, "visibility hook is present");
const calls = [];
function visit(node) {
  if (
    ts.isVariableStatement(node) &&
    node.declarationList.declarations.some(
      (d) =>
        d.initializer &&
        ts.isCallExpression(d.initializer) &&
        d.initializer.expression.getText(ast) === "useVisibleSet",
    )
  ) {
    calls.push(node.getText(ast));
  }
  ts.forEachChild(node, visit);
}
visit(ast);
assert.equal(
  calls.length,
  2,
  "both page and thumbnail call sites are exercised",
);
const compiled = ts.transpileModule(
  `${hook.getText(ast)}
function useAppVisibility(scrollRef, thumbsRef, rows, pageCount, status, sidebar) {
  ${calls.join("\n")}
  return { visibleRows, visibleThumbs, setRowRef, setThumbRef };
}`,
  { compilerOptions: { target: ts.ScriptTarget.ES2022 } },
).outputText;
const useAppVisibility = new Function(
  "useState",
  "useRef",
  "useEffect",
  `${compiled}; return useAppVisibility;`,
)(React.useState, React.useRef, React.useEffect);

test("PDF visibility follows ready, reload and thumbnail-root lifecycles", async () => {
  const dom = new JSDOM('<div id="root"></div>');
  const saved = new Map(
    [
      "window",
      "document",
      "HTMLElement",
      "IntersectionObserver",
      "IS_REACT_ACT_ENVIRONMENT",
    ].map((k) => [k, Object.getOwnPropertyDescriptor(globalThis, k)]),
  );
  const observers = [];
  Object.assign(globalThis, {
    window: dom.window,
    document: dom.window.document,
    HTMLElement: dom.window.HTMLElement,
    IS_REACT_ACT_ENVIRONMENT: true,
  });
  globalThis.IntersectionObserver = class {
    constructor(callback, options) {
      this.callback = callback;
      this.root = options.root;
      this.targets = new Set();
      this.disconnected = false;
      observers.push(this);
    }
    observe(target) {
      this.targets.add(target);
    }
    disconnect() {
      this.disconnected = true;
      this.targets.clear();
    }
  };
  function Fixture({ count, status, sidebar = "thumbs" }) {
    const scrollRef = React.useRef(null),
      thumbsRef = React.useRef(null);
    const v = useAppVisibility(
      scrollRef,
      thumbsRef,
      Array(count).fill(0),
      count,
      status,
      sidebar,
    );
    if (status !== "ready") return React.createElement("p", null, status);
    const items = (kind, ref, visible) =>
      Array.from({ length: count }, (_, i) =>
        React.createElement(
          "div",
          {
            key: i,
            ref: ref(i),
            "data-idx": i,
            "data-kind": kind,
            "data-visible": visible.has(i),
          },
          String(i),
        ),
      );
    return React.createElement(
      "main",
      null,
      React.createElement(
        "section",
        { ref: scrollRef, id: "pages" },
        items("page", v.setRowRef, v.visibleRows),
      ),
      sidebar === "thumbs" &&
        React.createElement(
          "aside",
          { ref: thumbsRef, id: "thumbs" },
          items("thumb", v.setThumbRef, v.visibleThumbs),
        ),
    );
  }
  const root = createRoot(document.getElementById("root"));
  const render = (props) =>
    React.act(async () => root.render(React.createElement(Fixture, props)));
  const active = () => observers.filter((o) => !o.disconnected);
  const emit = async (intersecting) =>
    React.act(async () => {
      for (const observer of active())
        observer.callback(
          [...observer.targets].map((target) => ({
            target,
            isIntersecting: intersecting,
          })),
        );
    });
  try {
    await render({ count: 0, status: "loading" });
    await render({ count: 2, status: "loading" });
    await render({ count: 2, status: "ready" });
    assert.equal(
      active().length,
      2,
      "late-mounted pages and thumbnails both have observers",
    );
    assert.deepEqual(
      active().map((o) => o.targets.size),
      [2, 2],
    );
    await emit(true);
    assert.equal(document.querySelectorAll('[data-visible="true"]').length, 4);
    await emit(false);
    assert.equal(
      document.querySelectorAll('[data-visible="true"]').length,
      0,
      "off-screen items remain lazy",
    );
    const originalRoots = active().map((o) => o.root);
    await render({ count: 2, status: "loading" });
    assert.equal(active().length, 0, "reload releases old roots");
    await render({ count: 2, status: "ready" });
    assert.equal(
      active().length,
      2,
      "same-page-count reopen observes new roots",
    );
    assert.ok(active().every((o) => !originalRoots.includes(o.root)));
    await render({ count: 2, status: "ready", sidebar: "outline" });
    assert.deepEqual(
      active().map((o) => o.root.id),
      ["pages"],
    );
    await render({ count: 2, status: "ready" });
    assert.equal(active().length, 2, "reopened thumbnail sidebar is observed");
    await emit(true);
    assert.equal(document.querySelectorAll('[data-visible="true"]').length, 4);
  } finally {
    await React.act(async () => root.unmount());
    assert.equal(active().length, 0, "unmount disconnects observers");
    dom.window.close();
    for (const [key, descriptor] of saved) {
      if (descriptor) Object.defineProperty(globalThis, key, descriptor);
      else delete globalThis[key];
    }
  }
});

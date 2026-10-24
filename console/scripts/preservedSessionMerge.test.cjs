const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const assert = require("node:assert/strict");
const test = require("node:test");
const ts = require("typescript");

const root = path.resolve(__dirname, "../..");
function declarations(relative, names) {
  const filename = path.join(root, relative);
  const source = ts.createSourceFile(
    filename,
    fs.readFileSync(filename, "utf8"),
    ts.ScriptTarget.Latest,
    true,
  );
  const found = source.statements.filter(
    (node) => ts.isFunctionDeclaration(node) && names.includes(node.name?.text),
  );
  for (const name of names)
    assert.ok(
      found.some((node) => node.name.text === name),
      `source function missing: ${name}`,
    );
  return found
    .map((node) => node.getText(source).replace(/^export\s+/, ""))
    .join("\n");
}
const source = [
  'const ROLE_USER = "user"; const ROLE_ASSISTANT = "assistant";',
  'const LANTAI_REQUEST_CARD_CODE = "LantaiRuntimeRequestCard"; const LANTAI_RESPONSE_CARD_CODE = "LantaiRuntimeResponseCard";',
  declarations("console/src/vendor/lantaiTranscriptReconciler.ts", [
    "userMessageInputSignature",
    "userMessageClientTurnIdentity",
    "userMessageCommandId",
    "userMessageSubmissionIdentity",
  ]),
  declarations("console/src/pages/Chat/sessionApi/index.ts", [
    "isRecord",
    "stringField",
    "preservedMessageIdentity",
    "canonicalIdentityValue",
    "preservedMessageFallbackIdentity",
    "preservedMessagesMatch",
    "mergePreservedSessionMessages",
    "messageClientTurnId",
  ]),
  "module.exports = { mergePreservedSessionMessages };",
].join("\n");
const moduleContext = { exports: {} };
vm.runInNewContext(
  ts.transpileModule(source, {
    compilerOptions: {
      target: ts.ScriptTarget.ES2022,
      module: ts.ModuleKind.CommonJS,
    },
  }).outputText,
  { module: moduleContext, exports: moduleContext.exports },
  { filename: "actual-sessionApi-functions.js" },
);
const merge = moduleContext.exports.mergePreservedSessionMessages;
const user = (id, content, turnId) => ({
  ...(id ? { id } : {}),
  role: "user",
  ...(turnId ? { metadata: { client_turn_id: turnId } } : {}),
  cards: [
    {
      code: "LantaiRuntimeRequestCard",
      data: { input: [{ role: "user", content }] },
    },
  ],
});

function commandUser(id, content, commandId, location = "metadata") {
  const message = user(id, content);
  const data = message.cards[0].data;
  if (location === "metadata") message.metadata = { command_id: commandId };
  if (location === "request_data") data.runtime_control_command_id = commandId;
  if (location === "input_metadata") {
    data.input[0].metadata = { command_id: commandId };
  }
  return message;
}

for (const [kind, content] of [
  ["text", [{ type: "text", text: "repeat command submission" }]],
  ["image", [{ type: "image", image_url: "file:///synthetic/shared.png" }]],
  ["file", [{ type: "file", file_url: "file:///synthetic/shared.docx" }]],
]) {
  for (const location of ["metadata", "request_data", "input_metadata"]) {
    test(`command-only ${location} preserves distinct ${kind} submissions without row IDs`, () => {
      const first = commandUser("", content, "command-A", location);
      const second = commandUser("", content, "command-B", location);
      assert.deepEqual(Array.from(merge([first], [second])), [first, second]);
    });
    test(`command-only ${location} reconciles the same ${kind} submission once without row IDs`, () => {
      const first = commandUser("", content, "command-A", location);
      const replay = commandUser("", content, "command-A", location);
      assert.deepEqual(Array.from(merge([first], [replay])), [replay]);
    });
  }
  test(`different commands outrank a reused ${kind} row ID`, () => {
    const first = commandUser("same-row", content, "command-A");
    const second = commandUser("same-row", content, "command-B");
    assert.deepEqual(Array.from(merge([first], [second])), [first, second]);
  });
  test(`the same command reconciles changed ${kind} row IDs`, () => {
    const first = commandUser("optimistic-row", content, "command-A");
    const replay = commandUser("durable-row", content, "command-A");
    assert.deepEqual(Array.from(merge([first], [replay])), [replay]);
  });
}

for (const [kind, content] of [
  ["text", [{ type: "text", text: "repeat submission" }]],
  ["image", [{ type: "image", image_url: "file:///tmp/shared.png" }]],
  [
    "file",
    [
      {
        type: "file",
        file_url: "file:///tmp/report.docx",
        file_name: "report.docx",
      },
    ],
  ],
  ["audio", [{ type: "audio", audio_url: "file:///tmp/shared.wav" }]],
  ["video", [{ type: "video", video_url: "file:///tmp/shared.mp4" }]],
]) {
  test(`different stable message IDs preserve two real ${kind} submissions`, () => {
    const merged = merge(
      [user("message-A", content, "turn-A")],
      [user("message-B", content, "turn-B")],
    );
    assert.equal(merged.length, 2);
    assert.deepEqual(
      Array.from(merged, (message) => message.id),
      ["message-A", "message-B"],
    );
  });
}

test("different explicit client turns with missing message IDs remain distinct", () => {
  const content = [{ type: "text", text: "same text" }];
  assert.equal(
    merge([user("", content, "turn-A")], [user("", content, "turn-B")]).length,
    2,
  );
});

test("same stable ID replay remains a single message", () => {
  const content = [{ type: "text", text: "same text" }];
  assert.equal(
    merge(
      [user("message-A", content, "turn-A")],
      [user("message-A", content, "turn-A")],
    ).length,
    1,
  );
});

test("legacy messages without stable IDs still reconcile once", () => {
  const content = [{ type: "text", text: "legacy text" }];
  assert.equal(merge([user("", content)], [user("", content)]).length, 1);
});

test("conflicting message IDs without client turns remain distinct", () => {
  const content = [{ type: "text", text: "same text" }];
  assert.equal(
    merge([user("message-A", content)], [user("message-B", content)]).length,
    2,
  );
});

test("a stable identity cannot be replaced by an unidentified content match", () => {
  const content = [{ type: "text", text: "same text" }];
  assert.equal(
    merge([user("message-A", content)], [user("", content)]).length,
    2,
  );
  assert.equal(
    merge([user("", content)], [user("message-A", content)]).length,
    2,
  );
});

test("the explicit client turn has priority over a reused card ID", () => {
  const content = [{ type: "text", text: "same text" }];
  assert.equal(
    merge(
      [user("same-card", content, "turn-A")],
      [user("same-card", content, "turn-B")],
    ).length,
    2,
  );
});

test("optimistic and durable IDs reconcile through the same explicit turn", () => {
  const content = [{ type: "text", text: "same text" }];
  const durable = user("durable-id", content, "turn-A");
  assert.deepEqual(
    Array.from(merge([user("optimistic-id", content, "turn-A")], [durable])),
    [durable],
  );
});

test("a shared turn identity cannot consume a message with another role", () => {
  const content = [{ type: "text", text: "same text" }];
  const assistant = { id: "shared-id", role: "assistant", cards: [] };
  assert.deepEqual(
    Array.from(
      merge([user("shared-id", content)], [assistant]),
      (message) => message.role,
    ),
    ["user", "assistant"],
  );
});

test("inserting cached users cannot consume one legacy server entry twice", () => {
  const y = user("", [{ type: "text", text: "legacy-Y" }]);
  const z = user("", [{ type: "text", text: "legacy-Z" }]);
  const x = user("message-X", [{ type: "text", text: "new-X" }]);
  const merged = merge([z, y, x, z], [y, z]);
  assert.equal(merged.length, 4);
  assert.equal(Array.from(merged).filter((message) => message === z).length, 2);
});

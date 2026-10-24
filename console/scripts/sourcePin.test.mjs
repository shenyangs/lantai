import assert from "node:assert/strict";
import test from "node:test";

import { parseGenOfficeSourcePin } from "./sourcePin.mjs";

const commit = "8f523289d6c34f940cd691472ee56b2013d148c8";

test("parses both supported exact GenOffice source-pin labels", () => {
  assert.equal(parseGenOfficeSourcePin(`- 固定提交：\`${commit}\``), commit);
  assert.equal(parseGenOfficeSourcePin(`固定提交仍为 \`${commit}\``), commit);
});

test("rejects a source pin that is not exactly 40 hexadecimal characters", () => {
  assert.equal(parseGenOfficeSourcePin("固定提交仍为 `8f52328`"), undefined);
});

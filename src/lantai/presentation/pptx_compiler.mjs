// Generic command boundary for native PPTX composition.
//
// It owns input validation, historical DeckIR migration and execution
// evidence. Backend-specific API calls are isolated in pptxgenjs_backend.mjs.

import fs from "node:fs";
import { normalizeHistoricalDeckIR } from "./legacy_deckir_adapter.mjs";
import { compileWithPptxGenJS } from "./pptxgenjs_backend.mjs";

const [specPath, outputPath, backendModulePath] = process.argv.slice(2);
if (!specPath || !outputPath || !backendModulePath) {
  throw new Error(
    "usage: pptx_compiler.mjs <deck.json> <output.pptx> <backend-module>",
  );
}

const rawDeck = JSON.parse(fs.readFileSync(specPath, "utf8"));
if (!rawDeck?.id || !Array.isArray(rawDeck.slides) || rawDeck.slides.length === 0) {
  throw new Error("native PPTX compilation requires a non-empty DeckIR");
}

const normalized = normalizeHistoricalDeckIR(rawDeck);
const result = await compileWithPptxGenJS({
  deck: normalized.deck,
  outputPath,
  modulePath: backendModulePath,
});

process.stdout.write(
  JSON.stringify({
    engine: "native_pptx",
    backend: result.backend,
    editable_object_count: result.editableObjectCount,
    degradations: [...normalized.degradations, ...result.degradations],
  }),
);

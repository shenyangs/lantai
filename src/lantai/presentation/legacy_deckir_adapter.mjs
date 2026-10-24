// Compatibility geometry for historical DeckIR checkpoints only.
//
// Current Presentation Design Intelligence must emit explicit object frames.
// This adapter is deliberately isolated from the compiler backend and refuses
// to infer geometry for decks that already carry a PresentationDesignContext.

const SLIDE_WIDTH = 13.333;
const SLIDE_HEIGHT = 7.5;

function validFrame(value) {
  if (!value) return false;
  const x = Number(value.x);
  const y = Number(value.y);
  const w = Number(value.w);
  const h = Number(value.h);
  return (
    [x, y, w, h].every(Number.isFinite) &&
    x >= 0 &&
    y >= 0 &&
    w > 0 &&
    h >= 0 &&
    x + w <= SLIDE_WIDTH + 0.001 &&
    y + h <= SLIDE_HEIGHT + 0.001
  );
}

function frameForHistoricalSlot(slotName, ordinal, total) {
  const slot = String(slotName || "body");
  if (slot === "title") return { x: 0.75, y: 0.48, w: 11.83, h: 0.72 };
  if (slot === "subtitle") return { x: 0.82, y: 3.35, w: 7.0, h: 0.72 };
  if (slot === "meta") return { x: 0.82, y: 4.25, w: 6.4, h: 0.42 };
  if (slot === "body" || slot === "quote") {
    return { x: 0.82, y: 1.55, w: 11.7, h: 4.95 };
  }
  if (slot === "attribution") return { x: 7.1, y: 5.65, w: 5.0, h: 0.45 };
  if (slot === "left") return { x: 0.75, y: 1.55, w: 5.72, h: 5.05 };
  if (slot === "right") return { x: 6.85, y: 1.55, w: 5.72, h: 5.05 };
  if (slot === "root") return { x: 1.8, y: 1.55, w: 9.73, h: 1.1 };
  if (slot === "chart") return { x: 0.75, y: 1.52, w: 8.1, h: 4.82 };
  if (slot === "table") return { x: 0.75, y: 1.48, w: 11.83, h: 4.85 };
  if (["hero", "cover_visual", "quote_visual"].includes(slot)) {
    return { x: 7.9, y: 0.72, w: 4.68, h: 5.95 };
  }
  if (["caption", "conclusion", "contact"].includes(slot)) {
    return { x: 0.75, y: 6.45, w: 11.83, h: 0.42 };
  }

  const numbered = /^(?:card|cell|step|node|metric)_(\d+)$/.exec(slot);
  if (numbered) {
    const index = Math.max(0, Number.parseInt(numbered[1], 10) - 1);
    if (slot.startsWith("cell_") || slot.startsWith("node_")) {
      const column = index % 2;
      const row = Math.floor(index / 2);
      return {
        x: 0.75 + column * 6.1,
        y: 1.58 + row * 2.55,
        w: 5.72,
        h: 2.25,
      };
    }
    if (slot.startsWith("metric_")) {
      const count = Math.max(total, index + 1, 1);
      const top = 1.62;
      const height = 1.28;
      const bottom = 0.18;
      const pitch =
        count <= 1
          ? 0
          : Math.min(
              1.55,
              (SLIDE_HEIGHT - bottom - top - height) / (count - 1),
            );
      return { x: 9.18, y: top + index * pitch, w: 3.1, h: height };
    }
    const count = Math.max(total, index + 1, 1);
    const gap = 0.24;
    const width = (11.83 - gap * (count - 1)) / count;
    return { x: 0.75 + index * (width + gap), y: 1.78, w: width, h: 4.72 };
  }

  const count = Math.max(total, 1);
  const index = Math.max(0, Math.min(ordinal, count - 1));
  const height = Math.max(0.42, 4.95 / count - 0.12);
  return { x: 0.82, y: 1.55 + index * (height + 0.12), w: 11.7, h: height };
}

export function normalizeHistoricalDeckIR(rawDeck) {
  const deck = structuredClone(rawDeck);
  const degradations = [];
  for (const slide of deck.slides || []) {
    const objects = slide.objects || [];
    const numberedFamilyCounts = new Map();
    for (const object of objects) {
      const numbered = /^(card|cell|step|node|metric)_(\d+)$/.exec(
        String(object.slot || ""),
      );
      if (!numbered) continue;
      numberedFamilyCounts.set(
        numbered[1],
        (numberedFamilyCounts.get(numbered[1]) || 0) + 1,
      );
    }
    objects.forEach((object, ordinal) => {
      if (validFrame(object.frame)) return;
      if (deck.design_context) {
        throw new Error(
          `${slide.id}:${object.id}: designed deck is missing an explicit valid frame`,
        );
      }
      const numbered = /^(card|cell|step|node|metric)_(\d+)$/.exec(
        String(object.slot || ""),
      );
      const familyTotal = numbered
        ? numberedFamilyCounts.get(numbered[1]) || objects.length
        : objects.length;
      object.frame = frameForHistoricalSlot(object.slot, ordinal, familyTotal);
      if (!validFrame(object.frame)) {
        throw new Error(
          `${slide.id}:${object.id}: historical DeckIR frame adapter produced invalid geometry`,
        );
      }
      degradations.push(
        `${slide.id}:${object.id}: historical DeckIR frame adapter used`,
      );
    });
  }
  return { deck, degradations };
}

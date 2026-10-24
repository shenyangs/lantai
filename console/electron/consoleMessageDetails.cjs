const LEVEL_NUMBERS = Object.freeze({
  debug: 0,
  info: 1,
  warning: 2,
  error: 3,
});

function normalizeConsoleMessage(
  event,
  legacyLevel,
  legacyMessage,
  legacyLine,
  legacySourceId,
) {
  if (event && typeof event.message === "string") {
    const levelName = String(event.level || "info").toLowerCase();
    return {
      level: LEVEL_NUMBERS[levelName] ?? 1,
      levelName,
      message: event.message,
      line: Number(event.lineNumber || 0),
      sourceId: String(event.sourceId || ""),
    };
  }
  const numericLevel = Number(legacyLevel || 0);
  return {
    level: numericLevel,
    levelName:
      Object.keys(LEVEL_NUMBERS).find(
        (name) => LEVEL_NUMBERS[name] === numericLevel,
      ) || "info",
    message: String(legacyMessage || ""),
    line: Number(legacyLine || 0),
    sourceId: String(legacySourceId || ""),
  };
}

module.exports = { normalizeConsoleMessage };

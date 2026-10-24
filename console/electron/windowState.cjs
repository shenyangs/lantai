const fs = require("fs");
const path = require("path");

const DEFAULT_WINDOW_BOUNDS = { width: 1280, height: 820 };
const MIN_WINDOW_BOUNDS = { width: 900, height: 600 };
const WINDOW_STATE_FILE_NAME = "window-state.json";

function sanitizeWindowBounds(bounds, minBounds = MIN_WINDOW_BOUNDS) {
  if (!bounds || typeof bounds !== "object") {
    return null;
  }
  const width = Number(bounds.width);
  const height = Number(bounds.height);
  if (!Number.isFinite(width) || !Number.isFinite(height)) {
    return null;
  }
  const normalized = {
    width: Math.max(Math.round(width), minBounds.width),
    height: Math.max(Math.round(height), minBounds.height),
  };
  const x = Number(bounds.x);
  const y = Number(bounds.y);
  if (Number.isFinite(x) && Number.isFinite(y)) {
    normalized.x = Math.round(x);
    normalized.y = Math.round(y);
  }
  return normalized;
}

function isWindowBoundsVisible(bounds, displays) {
  if (!Number.isFinite(bounds.x) || !Number.isFinite(bounds.y)) {
    return true;
  }
  return displays.some((display) => {
    const area = display.workArea;
    return (
      bounds.x < area.x + area.width &&
      bounds.x + bounds.width > area.x &&
      bounds.y < area.y + area.height &&
      bounds.y + bounds.height > area.y
    );
  });
}

function loadWindowBoundsFromFile({
  filePath,
  displays,
  defaultBounds = DEFAULT_WINDOW_BOUNDS,
  minBounds = MIN_WINDOW_BOUNDS,
  onError,
}) {
  try {
    const parsed = JSON.parse(fs.readFileSync(filePath, "utf8"));
    const bounds = sanitizeWindowBounds(parsed.bounds, minBounds);
    if (bounds && isWindowBoundsVisible(bounds, displays)) {
      return bounds;
    }
  } catch (err) {
    if (err.code !== "ENOENT" && typeof onError === "function") {
      onError(err);
    }
  }
  return { ...defaultBounds };
}

function saveWindowBoundsToFile({
  filePath,
  bounds,
  minBounds = MIN_WINDOW_BOUNDS,
  onError,
}) {
  const normalized = sanitizeWindowBounds(bounds, minBounds);
  if (!normalized) {
    return false;
  }
  try {
    fs.mkdirSync(path.dirname(filePath), { recursive: true });
    fs.writeFileSync(
      filePath,
      JSON.stringify(
        { bounds: normalized, updatedAt: new Date().toISOString() },
        null,
        2,
      ),
    );
    return true;
  } catch (err) {
    if (typeof onError === "function") {
      onError(err);
    }
    return false;
  }
}

module.exports = {
  DEFAULT_WINDOW_BOUNDS,
  MIN_WINDOW_BOUNDS,
  WINDOW_STATE_FILE_NAME,
  isWindowBoundsVisible,
  loadWindowBoundsFromFile,
  sanitizeWindowBounds,
  saveWindowBoundsToFile,
};

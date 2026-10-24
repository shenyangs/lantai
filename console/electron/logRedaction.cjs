const crypto = require("crypto");
const os = require("os");

function escapeRegExp(value) {
  return String(value || "").replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function redactDiagnosticText(value, { limit = 1000 } = {}) {
  let text = String(value || "");
  const homeDir = os.homedir();
  if (homeDir && homeDir !== "/") {
    const escapedHome = escapeRegExp(homeDir);
    text = text.replace(
      new RegExp(`${escapedHome}(?:[/\\\\][^\\s:'")]+)?`, "g"),
      "[redacted-path]",
    );
  }
  text = text
    .replace(/\bsk-[A-Za-z0-9_\-]{16,}\b/g, "[redacted-key]")
    .replace(/Bearer\s+[A-Za-z0-9._~+/-]+=*/gi, "Bearer [redacted]")
    .replace(
      /\b(Authorization\s*[:=]\s*)(?:Bearer|Basic|Token|ApiKey|Api-Key)?\s*[A-Za-z0-9._~+/\-=:]+/gi,
      "$1[redacted]",
    )
    .replace(
      /([?&](?:token|access_token|refresh_token|api_key|key|secret)=)[^&\s'")]+/gi,
      "$1[redacted]",
    )
    .replace(
      /("(?:authorization|token|access_token|refresh_token|api_key|secret|password)"\s*:\s*")[^"]+(")/gi,
      "$1[redacted]$2",
    )
    .replace(/\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b/gi, "[redacted-email]")
    .replace(/\b[A-Za-z0-9_\-]{32,}\b/g, "[redacted-id]");
  if (text.length > limit) {
    return `${text.slice(0, limit)}...[truncated]`;
  }
  return text;
}

function stableDiagnosticHash(value) {
  return crypto
    .createHash("sha256")
    .update(String(value || ""))
    .digest("hex")
    .slice(0, 16);
}

function describeErrorForDiagnostics(err, fallbackMessage = "Unknown error") {
  const rawMessage = err?.message || String(err || fallbackMessage);
  const message = redactDiagnosticText(rawMessage);
  const errorType = err?.name || "Error";
  return {
    error_type: errorType,
    message,
    error_hash: stableDiagnosticHash(`${errorType}:${message}`),
  };
}

module.exports = {
  describeErrorForDiagnostics,
  redactDiagnosticText,
  stableDiagnosticHash,
};

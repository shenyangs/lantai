export type QuquPlacement = "lead" | "echo";

export type QuquBlock =
  | { type: "markdown"; text: string }
  | { type: "ququ"; text: string; placement: QuquPlacement };

const QUQU_LINE_RE = /^~>\s*(?:\[(lead|echo)\]\s*)?(.+)$/i;
const NESTED_QUQU_PREFIX_RE = /^(?:~>\s*(?:\[(?:lead|echo)\]\s*)?)+/i;
const FENCE_LINE_RE = /^\s*(`{3,}|~{3,})/;

function getFenceMarker(line: string): string | null {
  const match = line.match(FENCE_LINE_RE);
  return match ? match[1] : null;
}

export function parseQuquBlocks(content: string): QuquBlock[] {
  const lines = content.split("\n");
  const blocks: QuquBlock[] = [];
  let markdownBuffer: string[] = [];
  let fenceMarker: string | null = null;

  const flushMarkdown = () => {
    if (markdownBuffer.length > 0) {
      blocks.push({ type: "markdown", text: markdownBuffer.join("\n") });
      markdownBuffer = [];
    }
  };

  for (const line of lines) {
    const match = fenceMarker ? null : line.match(QUQU_LINE_RE);
    if (match) {
      flushMarkdown();
      blocks.push({
        type: "ququ",
        placement: match[1]?.toLowerCase() === "lead" ? "lead" : "echo",
        // Read-old protection: older persisted reflections can contain a
        // canonical wrapper around an untyped provider marker. Protocol
        // syntax is never user copy, so remove every nested prefix here.
        text: match[2].replace(NESTED_QUQU_PREFIX_RE, "").trim(),
      });
      continue;
    }

    markdownBuffer.push(line);

    const marker = getFenceMarker(line);
    if (!marker) continue;
    if (!fenceMarker) {
      fenceMarker = marker;
    } else if (
      marker[0] === fenceMarker[0] &&
      marker.length >= fenceMarker.length
    ) {
      fenceMarker = null;
    }
  }

  flushMarkdown();
  return blocks;
}

/** Return only user-deliverable content, keeping ququ outside copy/export. */
export function stripQuquFromArtifactContent(content: string): string {
  return parseQuquBlocks(content)
    .flatMap((block) => (block.type === "markdown" ? [block.text] : []))
    .join("\n")
    .replace(/\n{3,}/g, "\n\n")
    .trim();
}

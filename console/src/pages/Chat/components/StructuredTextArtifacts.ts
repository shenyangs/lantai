import type { UtilityClassifiedArtifact } from "@/api/types";

export type TextArtifactKind =
  | "creative"
  | "correspondence"
  | "copy"
  | "script";

export type StructuredOutputSegment =
  | {
      type: "markdown";
      content: string;
    }
  | {
      type: "artifact";
      kind: TextArtifactKind;
      label: string;
      title: string;
      body: string;
      copyContent: string;
      fileExtension: "txt";
    };

const ARTIFACT_LABELS: Record<TextArtifactKind, string> = {
  creative: "作品",
  correspondence: "信函",
  copy: "文案",
  script: "脚本",
};

function trimArtifactTitleFromBody(source: string, title: string): string {
  const escapedTitle = title.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const titlePatterns = [
    new RegExp(`^\\s*《${escapedTitle}》[：:]?\\s*\\n+`),
    new RegExp(`^\\s*#{1,3}\\s+${escapedTitle}\\s*\\n+`),
    new RegExp(`^\\s*(?:主题|标题)[：:]\\s*${escapedTitle}\\s*\\n+`),
  ];
  for (const pattern of titlePatterns) {
    if (pattern.test(source)) return source.replace(pattern, "").trim();
  }
  return source.trim();
}

function validatedArtifacts(
  content: string,
  artifacts: UtilityClassifiedArtifact[],
): UtilityClassifiedArtifact[] {
  const accepted: UtilityClassifiedArtifact[] = [];
  const occupied: Array<[number, number]> = [];

  for (const artifact of artifacts) {
    const source = artifact.source_text;
    if (
      !source?.trim() ||
      !artifact.title?.trim() ||
      !(artifact.kind in ARTIFACT_LABELS) ||
      content.slice(artifact.start, artifact.end) !== source ||
      content.indexOf(source) !== artifact.start ||
      content.indexOf(source, artifact.start + 1) !== -1
    ) {
      continue;
    }
    if (
      occupied.some(
        ([usedStart, usedEnd]) =>
          artifact.start < usedEnd && artifact.end > usedStart,
      )
    ) {
      continue;
    }
    occupied.push([artifact.start, artifact.end]);
    accepted.push(artifact);
  }

  return accepted.sort((left, right) => left.start - right.start);
}

function escapedPattern(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function stripTrailingArtifactBoundary(
  markdown: string,
  artifactTitle: string,
): string {
  const title = escapedPattern(artifactTitle.trim());
  const duplicateTitle = new RegExp(
    `(?:^|\\n)[ \\t]*(?:#{1,3}[ \\t]+${title}|\\*\\*${title}\\*\\*|__${title}__|《${title}》)[ \\t]*$`,
  );
  const withoutDuplicateTitle = markdown.trimEnd().replace(duplicateTitle, "");
  return withoutDuplicateTitle
    .trimEnd()
    .replace(/(?:^|\n)[ \t]*(?:-{3,}|\*{3,}|_{3,})[ \t]*$/, "")
    .trim();
}

function stripLeadingArtifactBoundary(markdown: string): string {
  return markdown
    .trimStart()
    .replace(/^(?:-{3,}|\*{3,}|_{3,})[ \t]*(?:\n|$)/, "")
    .trim();
}

function pushMarkdown(
  segments: StructuredOutputSegment[],
  content: string,
  start: number,
  end: number,
  options: {
    artifactTitle?: string;
    followsArtifact?: boolean;
  } = {},
) {
  let markdown = content.slice(start, end).trim();
  if (options.artifactTitle) {
    markdown = stripTrailingArtifactBoundary(markdown, options.artifactTitle);
  }
  if (options.followsArtifact) {
    markdown = stripLeadingArtifactBoundary(markdown);
  }
  if (markdown) segments.push({ type: "markdown", content: markdown });
}

export function splitStandaloneTextArtifacts(
  content: string,
  classifiedArtifacts: UtilityClassifiedArtifact[] = [],
): StructuredOutputSegment[] {
  if (!content.trim()) return [];

  const artifacts = validatedArtifacts(content, classifiedArtifacts);
  if (!artifacts.length) return [{ type: "markdown", content }];

  const segments: StructuredOutputSegment[] = [];
  let cursor = 0;
  for (const [index, artifact] of artifacts.entries()) {
    pushMarkdown(segments, content, cursor, artifact.start, {
      artifactTitle: artifact.title,
      followsArtifact: index > 0,
    });
    segments.push({
      type: "artifact",
      kind: artifact.kind,
      label: ARTIFACT_LABELS[artifact.kind],
      title: artifact.title.trim(),
      body: trimArtifactTitleFromBody(
        artifact.source_text,
        artifact.title.trim(),
      ),
      copyContent: artifact.source_text,
      fileExtension: "txt",
    });
    cursor = artifact.end;
  }
  pushMarkdown(segments, content, cursor, content.length, {
    followsArtifact: true,
  });
  return segments;
}

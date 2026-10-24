export interface DreamDiaryCandidate {
  score: string;
  content: string;
}

export interface DreamDiaryEntry {
  completedAt: string;
  trigger: string;
  mode: string;
  remStatus: string;
  reviewed: number;
  eligible: number;
  candidates: DreamDiaryCandidate[];
  reflection: string;
}

const FIELD_PATTERN = /^-\s*([^：:]+)[：:]\s*(.*)$/;
const CANDIDATE_PATTERN = /^-\s*(?:\[([^\]]+)\]\s*)?(.*)$/;

function parseCount(value: string) {
  const matched = value.match(/\d+/);
  return matched ? Number(matched[0]) : 0;
}

function toTriggerLabel(value: string) {
  const normalized = value.trim().toLowerCase();
  if (normalized === "scheduled") return "定时整理";
  if (normalized === "manual") return "手动整理";
  if (normalized === "startup") return "启动时整理";
  return value.trim() || "自动整理";
}

function toModeLabel(value: string) {
  const normalized = value.trim();
  if (normalized === "受治理整理") return "已整理";
  if (normalized === "影子整理" || normalized === "影子预览") {
    return "仅预览（未保存）";
  }
  return normalized || "已整理";
}

function toRemStatusLabel(value: string) {
  const normalized = value.trim();
  if (normalized === "已生成反思摘要") return "已生成整理说明";
  if (normalized === "已关闭") return "未生成整理说明";
  return normalized || "未生成整理说明";
}

function cleanReflection(lines: string[]) {
  return lines
    .map((line) => line.trim())
    .filter(
      (line) =>
        line &&
        !line.startsWith("> 以下内容只供用户浏览") &&
        !line.startsWith("> 这是供用户复核"),
    )
    .join("\n");
}

export function parseDreamDiaryExcerpt(source: string): DreamDiaryEntry[] {
  const lines = (source || "").replace(/\r\n?/g, "\n").split("\n");
  const headingIndexes = lines.reduce<number[]>((indexes, line, index) => {
    if (/^##\s+/.test(line) && !/^###\s+/.test(line)) indexes.push(index);
    return indexes;
  }, []);

  const entries = headingIndexes.map((start, headingIndex) => {
    const end = headingIndexes[headingIndex + 1] ?? lines.length;
    const heading = lines[start].replace(/^##\s+/, "").trim();
    const completedAt = heading.split(/\s+·\s+/)[0]?.trim() || "";
    const fields = new Map<string, string>();
    const candidates: DreamDiaryCandidate[] = [];
    const reflectionLines: string[] = [];
    let section: "meta" | "candidates" | "reflection" = "meta";

    for (const rawLine of lines.slice(start + 1, end)) {
      const line = rawLine.trim();
      if (/^###\s+候选/.test(line)) {
        section = "candidates";
        continue;
      }
      if (/^###\s+REM\s+反思/.test(line)) {
        section = "reflection";
        continue;
      }
      if (!line) continue;

      if (section === "meta") {
        const field = line.match(FIELD_PATTERN);
        if (field) fields.set(field[1].trim(), field[2].trim());
        continue;
      }

      if (section === "candidates") {
        const candidate = line.match(CANDIDATE_PATTERN);
        if (!candidate) continue;
        const content = candidate[2].trim();
        if (!content || content.includes("没有达到晋升门槛")) continue;
        candidates.push({ score: candidate[1]?.trim() || "", content });
        continue;
      }

      reflectionLines.push(rawLine);
    }

    return {
      completedAt,
      trigger: toTriggerLabel(fields.get("触发") || ""),
      mode: toModeLabel(fields.get("模式") || ""),
      remStatus: toRemStatusLabel(fields.get("REM") || ""),
      reviewed: parseCount(fields.get("审阅") || ""),
      eligible: parseCount(fields.get("达标") || ""),
      candidates,
      reflection: cleanReflection(reflectionLines),
    };
  });

  return entries.filter((entry) => entry.completedAt).reverse();
}

import type {
  LayeredMemorySnapshot,
  MemoryDreamingStatus,
  UserMemoryEntry,
  UserMemorySnapshot,
  UserMemoryType,
  UserUnderstandingSnapshot,
} from "../../../api/types";
import { toDisplaySentence } from "./presentation";

export type MemoryTopicKind = "profile" | "theme" | "project" | "experience";

export interface MemoryTopic {
  id: string;
  kind: MemoryTopicKind;
  correctionType: UserMemoryType;
  title: string;
  summary: string;
  details: string[];
  updatedAt: string;
  evidenceCount: number;
  entries: UserMemoryEntry[];
}

export interface MemoryChange {
  id: string;
  verb: string;
  title: string;
  detail: string;
  occurredAt: string;
  topicId?: string;
  reason: string;
  impact: string;
}

const PROFILE_TOPICS: Array<{ type: UserMemoryType; title: string }> = [
  { type: "identity", title: "关于你" },
  { type: "workflow", title: "你的做事方式" },
  { type: "style", title: "你的表达偏好" },
  { type: "instruction", title: "长期约定" },
];

const TOPIC_CORRECTION_REASON_PREFIX = "用户纠正主题:";
const PERSONAL_MODELS: Record<string, UserMemoryType> = {
  user_profile: "identity",
  workflow_model: "workflow",
  communication_model: "style",
  standing_rules: "instruction",
};
const GENERIC_SCENES = new Set([
  "user-profile",
  "workflow-and-projects",
  "communication-style",
  "standing-instructions",
  "task-timeline",
  "general",
]);

function isPersonalEntry(entry: UserMemoryEntry) {
  return (
    entry.confirmed !== false &&
    (entry.subject || "user") === "user" &&
    (entry.scope || "global") === "global"
  );
}

export function memoryTopicCorrectionReason(topic: MemoryTopic) {
  return `${TOPIC_CORRECTION_REASON_PREFIX}${topic.id}|${topic.title}`;
}

function topicCorrectionTarget(entry: UserMemoryEntry) {
  const reason = String(entry.reason || "");
  if (!reason.startsWith(TOPIC_CORRECTION_REASON_PREFIX)) return null;
  const payload = reason.slice(TOPIC_CORRECTION_REASON_PREFIX.length);
  const separator = payload.indexOf("|");
  const topicId = (
    separator >= 0 ? payload.slice(0, separator) : payload
  ).trim();
  const title = (separator >= 0 ? payload.slice(separator + 1) : "").trim();
  if (!topicId) return null;
  return { topicId, title };
}

function topicKindFromId(topicId: string): MemoryTopicKind {
  const prefix = topicId.split(":", 1)[0];
  if (prefix === "project" || prefix === "experience" || prefix === "profile") {
    return prefix;
  }
  return "theme";
}

function system2CorrectionType(key: string): UserMemoryType {
  if (key === "user_profile") return "identity";
  if (key === "communication_model") return "style";
  if (key === "standing_rules") return "instruction";
  return "workflow";
}

function validDate(value: string | undefined) {
  const timestamp = value ? new Date(value).getTime() : 0;
  return Number.isFinite(timestamp) ? timestamp : 0;
}

function newest(values: Array<string | undefined>) {
  return (
    values
      .filter((value): value is string => Boolean(value))
      .sort((a, b) => validDate(b) - validDate(a))[0] || ""
  );
}

function uniqueLines(values: Array<string | undefined>) {
  const seen = new Set<string>();
  return values
    .map((value) => String(value || "").trim())
    .filter((value) => {
      if (!value || seen.has(value)) return false;
      seen.add(value);
      return true;
    });
}

function entrySentence(entry: UserMemoryEntry) {
  return toDisplaySentence(entry.content, entry.type) || entry.content;
}

export function buildMemoryTopics(
  snapshot: UserMemorySnapshot,
  layered: LayeredMemorySnapshot,
  understanding: UserUnderstandingSnapshot,
): MemoryTopic[] {
  const topics: MemoryTopic[] = [];
  const sceneAliases = new Map<string, Set<string>>();
  const registerSceneAlias = (sceneName: string, topicId: string) => {
    for (const kind of ["project", "theme", "experience"]) {
      const alias = `${kind}:${sceneName}`;
      const targets = sceneAliases.get(alias) || new Set<string>();
      targets.add(topicId);
      sceneAliases.set(alias, targets);
    }
  };
  const seenFacts = new Set<string>();
  const activeEntries = [...snapshot.entries]
    .sort((a, b) => validDate(b.updated_at) - validDate(a.updated_at))
    .filter((entry) => {
      if (
        entry.status !== "active" ||
        entry.evolution_status === "superseded" ||
        entry.superseded_by_id
      )
        return false;
      const identity = entry.canonical_fact_id || entry.id;
      if (seenFacts.has(identity)) return false;
      seenFacts.add(identity);
      return true;
    });
  const correctionEntries = activeEntries.filter(topicCorrectionTarget);
  const ordinaryEntries = activeEntries.filter(
    (entry) => !topicCorrectionTarget(entry),
  );

  PROFILE_TOPICS.forEach(({ type, title }) => {
    const entries = ordinaryEntries.filter(
      (entry) => entry.type === type && isPersonalEntry(entry),
    );
    if (!entries.length) return;
    const details = uniqueLines(entries.map(entrySentence));
    topics.push({
      id: `profile:${type}`,
      kind: "profile",
      correctionType: type,
      title,
      summary: details[0] || "",
      details,
      updatedAt: newest(entries.map((entry) => entry.updated_at)),
      evidenceCount: entries.length,
      entries,
    });
  });

  ordinaryEntries
    .filter((entry) => !isPersonalEntry(entry))
    .forEach((entry) => {
      const isProject =
        entry.subject === "project" || entry.scope === "project";
      const kind: MemoryTopicKind =
        entry.scope === "thread"
          ? "experience"
          : isProject
            ? "project"
            : "theme";
      const matchingScene = layered.scenes.find(
        (scene) =>
          !GENERIC_SCENES.has(scene.scene_name) &&
          layered.atoms.some(
            (atom) =>
              (entry.canonical_atom_ids || []).includes(atom.id) &&
              atom.scene_name === scene.scene_name,
          ),
      );
      const name = entry.scope_id || matchingScene?.scene_name;
      const scopeTitle =
        entry.scope_id &&
        !entry.scope_id.startsWith("workspace:") &&
        !/^[0-9a-f-]{24,}$/i.test(entry.scope_id)
          ? entry.scope_id
          : "";
      const title =
        matchingScene?.title ||
        scopeTitle ||
        (kind === "project"
          ? "项目资料"
          : kind === "experience"
            ? "会话事项"
            : entry.subject === "third_party"
              ? "相关人物与资料"
              : "待核对的资料");
      const details = [entrySentence(entry)];
      topics.push({
        id: `${kind}:${name || (kind === "project" ? "unassigned" : kind === "experience" ? "thread-context" : "reference")}`,
        kind,
        correctionType: entry.type,
        title,
        summary: details[0],
        details,
        updatedAt: entry.updated_at,
        evidenceCount: 1,
        entries: [entry],
      });
      if (matchingScene)
        registerSceneAlias(
          matchingScene.scene_name,
          topics[topics.length - 1].id,
        );
    });

  (layered.system2?.models || []).forEach((model) => {
    const personalType = PERSONAL_MODELS[model.key];
    const existing = personalType
      ? topics.find((topic) => topic.id === `profile:${personalType}`)
      : undefined;
    const details = uniqueLines(
      model.sample?.length ? model.sample : [model.summary],
    );
    if (existing) {
      existing.details = uniqueLines([
        ...existing.details,
        ...details.map((line) => toDisplaySentence(line, personalType) || line),
      ]);
      existing.evidenceCount = existing.details.length;
      existing.updatedAt = newest([existing.updatedAt, model.updated_at]);
      return;
    }
    if (!model.title || !details.length) return;
    topics.push({
      id: `theme:${model.key}`,
      kind: personalType ? "profile" : "theme",
      correctionType: system2CorrectionType(model.key),
      title: model.title,
      summary:
        model.summary === model.sample?.slice(0, 3).join("；")
          ? details[0]
          : model.summary || details[0],
      details,
      updatedAt: model.updated_at || layered.updated_at || "",
      evidenceCount: model.evidence_count || details.length,
      entries: [],
    });
  });

  layered.scenes.forEach((scene) => {
    if (GENERIC_SCENES.has(scene.scene_name)) return;
    const sceneAtoms = layered.atoms.filter(
      (atom) =>
        atom.scene_name === scene.scene_name && atom.status === "active",
    );
    const ownership = sceneAtoms.map(
      (atom) =>
        ordinaryEntries.find((entry) =>
          entry.canonical_atom_ids?.includes(atom.id),
        ) ||
        atom.metadata ||
        {},
    );
    const isProject = ownership.some(
      (owner) => owner.subject === "project" || owner.scope === "project",
    );
    const isThread =
      ownership.length > 0 &&
      ownership.every((owner) => owner.scope === "thread");
    const kind: MemoryTopicKind = isThread
      ? "experience"
      : sceneAtoms.length
        ? isProject
          ? "project"
          : "theme"
        : "project";
    const details = uniqueLines(scene.sample || []);
    const scopedNames = [
      ...new Set(ownership.map((owner) => owner.scope_id).filter(Boolean)),
    ];
    const sceneTopicId = `${kind}:${scopedNames.length === 1 ? scopedNames[0] : scene.scene_name}`;
    // A scene spanning explicit scopes is only navigation, not a new owner.
    if (scopedNames.length > 1) return;
    registerSceneAlias(scene.scene_name, sceneTopicId);
    const existingScene = topics.find((topic) => topic.id === sceneTopicId);
    if (existingScene) {
      existingScene.details = uniqueLines([
        ...existingScene.details,
        ...details.map(
          (line) =>
            toDisplaySentence(line, existingScene.correctionType) || line,
        ),
      ]);
      existingScene.evidenceCount = existingScene.details.length;
      return;
    }
    if (!scene.title || !details.length) return;
    topics.push({
      id: sceneTopicId,
      kind,
      correctionType: "workflow",
      title: scene.title,
      summary: details[0],
      details,
      updatedAt: scene.updated_at || layered.updated_at || "",
      evidenceCount: scene.atom_count || details.length,
      entries: [],
    });
  });

  const openItems = uniqueLines([
    ...understanding.open_loops.map((item) => item.statement),
    ...(layered.system2?.open_intentions || []).map((item) => item.content),
  ]);
  if (openItems.length) {
    topics.push({
      id: "experience:open-items",
      kind: "experience",
      correctionType: "workflow",
      title: "正在推进的事",
      summary: openItems[0],
      details: openItems,
      updatedAt: newest([
        ...understanding.open_loops.map((item) => item.updated_at),
        ...(layered.system2?.open_intentions || []).map(
          (item) => item.updated_at,
        ),
      ]),
      evidenceCount: openItems.length,
      entries: [],
    });
  }

  const byIdentity = new Map<string, MemoryTopic>();
  topics.forEach((topic) => {
    const identity = topic.id;
    const previous = byIdentity.get(identity);
    if (!previous) {
      byIdentity.set(identity, topic);
      return;
    }
    previous.details = uniqueLines([...previous.details, ...topic.details]);
    previous.summary = previous.summary || topic.summary;
    previous.updatedAt = newest([previous.updatedAt, topic.updatedAt]);
    previous.evidenceCount += topic.evidenceCount;
    previous.entries = [...previous.entries, ...topic.entries];
  });

  correctionEntries
    .sort((a, b) => validDate(a.updated_at) - validDate(b.updated_at))
    .forEach((entry) => {
      const target = topicCorrectionTarget(entry);
      if (!target) return;
      const correction = entrySentence(entry);
      if (!correction) return;
      const aliases = sceneAliases.get(target.topicId);
      const resolvedTargetId =
        aliases?.size === 1 ? [...aliases][0] : target.topicId;
      const topic = [...byIdentity.values()].find(
        (item) =>
          item.id === resolvedTargetId ||
          (PERSONAL_MODELS[target.topicId.replace(/^theme:/, "")] &&
            item.id ===
              `profile:${PERSONAL_MODELS[target.topicId.replace(/^theme:/, "")]}`),
      );
      if (topic) {
        topic.summary = correction;
        topic.details = [correction];
        topic.updatedAt = entry.updated_at || entry.created_at;
        topic.evidenceCount = 1;
        topic.entries = [entry];
        return;
      }
      const kind = topicKindFromId(target.topicId);
      byIdentity.set(
        `${kind}:${(target.title || correction).toLocaleLowerCase()}`,
        {
          id: target.topicId,
          kind,
          correctionType: entry.type,
          title: target.title || "已纠正的理解",
          summary: correction,
          details: [correction],
          updatedAt: entry.updated_at || entry.created_at,
          evidenceCount: 1,
          entries: [entry],
        },
      );
    });

  return [...byIdentity.values()].sort(
    (a, b) => validDate(b.updatedAt) - validDate(a.updatedAt),
  );
}

export function filterMemoryTopics(
  topics: MemoryTopic[],
  query: string,
  kind: MemoryTopicKind | "all",
) {
  const normalizedQuery = query.trim().toLocaleLowerCase();
  return topics.filter((topic) => {
    if (kind !== "all" && topic.kind !== kind) return false;
    if (!normalizedQuery) return true;
    return [topic.title, topic.summary, ...topic.details].some((value) =>
      value.toLocaleLowerCase().includes(normalizedQuery),
    );
  });
}

export function memoryTopicKindLabel(kind: MemoryTopicKind) {
  if (kind === "profile") return "关于你";
  if (kind === "project") return "项目";
  if (kind === "experience") return "事项";
  return "理解主题";
}

function factContent(value: unknown) {
  if (!value || typeof value !== "object") return "";
  return String((value as Record<string, unknown>).content || "").trim();
}

export function buildMemoryChanges(
  snapshot: UserMemorySnapshot,
  dreaming: MemoryDreamingStatus,
  topics: MemoryTopic[] = [],
): MemoryChange[] {
  const changes: MemoryChange[] = [];
  const run = dreaming.latest_run;

  (run?.mutations || []).forEach((mutation, index) => {
    const content = factContent(mutation.after) || factContent(mutation.before);
    if (!content) return;
    changes.push({
      id: `dream:${run?.run_id || "latest"}:${index}`,
      verb: "送入核对",
      title: "自动整理发现一项长期理解",
      detail: content,
      occurredAt: run?.completed_at || run?.started_at || "",
      reason: "自动整理发现了可能影响长期协作的新内容",
      impact: "核对前不会直接替换已经确认的记忆",
    });
  });

  snapshot.entries.forEach((entry) => {
    const detail = entrySentence(entry);
    if (!detail) return;
    const correctionTarget = topicCorrectionTarget(entry);
    const linkedTopic = topics.find((topic) =>
      topic.entries.some((item) => item.id === entry.id),
    );
    let verb = "更新了";
    if (entry.status === "archived") verb = "停止使用";
    else if (correctionTarget) verb = "纠正了";
    else if (entry.supersedes_id) verb = "修正了";
    else if (entry.source_kind === "import") verb = "带入了";
    else if (entry.source === "manual") verb = "你补充了";
    else if (entry.created_at === entry.updated_at) verb = "形成了";
    changes.push({
      id: `entry:${entry.id}:${entry.updated_at}`,
      verb,
      title:
        linkedTopic?.title ||
        correctionTarget?.title ||
        (isPersonalEntry(entry)
          ? CATEGORY_LABEL_BY_TYPE[entry.type]
          : entry.scope === "thread"
            ? "会话事项"
            : entry.subject === "project" || entry.scope === "project"
              ? "项目资料"
              : "待核对的资料"),
      detail,
      occurredAt: entry.updated_at || entry.created_at,
      topicId:
        linkedTopic?.id ||
        correctionTarget?.topicId ||
        (isPersonalEntry(entry) ? `profile:${entry.type}` : undefined),
      reason:
        entry.status === "archived"
          ? entry.superseded_by_id || entry.evolution_status === "superseded"
            ? "已有更新说法替代这条旧内容"
            : "你或系统已明确停止使用这条旧说法"
          : correctionTarget
            ? "这是你对原有理解的明确纠正"
            : entry.supersedes_id
              ? "新信息修正了此前说法"
              : entry.source_kind === "import"
                ? "来自你主动导入的记忆资料"
                : entry.source === "manual"
                  ? "来自你主动补充的内容"
                  : "由近期可追溯内容形成",
      impact:
        entry.status === "archived"
          ? entry.superseded_by_id || entry.evolution_status === "superseded"
            ? "以后优先使用更新说法；旧内容和来源仍可追溯"
            : "以后回答不会主动采用；原始聊天和来源仍然保留"
          : "以后遇到相关话题时会参与理解和协作",
    });
  });

  if (
    run &&
    run.status === "completed" &&
    !(run.mutations || []).length &&
    (run.counts?.reviewed || 0) > 0
  ) {
    if ((run.counts?.staged || 0) > 0) {
      changes.push({
        id: `dream:${run.run_id}:staged`,
        verb: "送入核对",
        title: `${run.counts?.staged || 0} 条变化`,
        detail: "这份历史整理记录没有包含可展示的变化详情。",
        occurredAt: run.completed_at || run.started_at || "",
        reason: "自动整理留下了需要你确认的候选内容",
        impact: "确认前不会作为稳定长期记忆使用",
      });
    } else {
      changes.push({
        id: `dream:${run.run_id}:checked`,
        verb: "检查了",
        title: `${run.counts?.reviewed || 0} 条候选记忆`,
        detail: "没有内容达到改变长期记忆或送入核对的条件。",
        occurredAt: run.completed_at || run.started_at || "",
        reason: "候选内容没有达到长期保留条件",
        impact: "现有长期记忆没有改变",
      });
    }
  }

  return changes.sort(
    (a, b) => validDate(b.occurredAt) - validDate(a.occurredAt),
  );
}

const CATEGORY_LABEL_BY_TYPE: Record<UserMemoryType, string> = {
  identity: "关于你的信息",
  workflow: "做事方式",
  style: "表达偏好",
  instruction: "长期约定",
};

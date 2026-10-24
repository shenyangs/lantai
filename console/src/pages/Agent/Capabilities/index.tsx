import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  Button,
  Checkbox,
  Empty,
  Input,
  InputNumber,
  Modal,
  Popconfirm,
  Select,
  Space,
  Spin,
  Switch,
  Tabs,
  Tag,
} from "antd";
import {
  CalendarOutlined,
  CopyOutlined,
  DeleteOutlined,
  EditOutlined,
  PlusOutlined,
  PlayCircleOutlined,
  ReloadOutlined,
  SaveOutlined,
} from "@ant-design/icons";
import {
  DndContext,
  PointerSensor,
  closestCenter,
  useSensor,
  useSensors,
} from "@dnd-kit/core";
import {
  SortableContext,
  arrayMove,
  useSortable,
  verticalListSortingStrategy,
} from "@dnd-kit/sortable";
import { CSS } from "@dnd-kit/utilities";
import {
  Boxes,
  Brain,
  FileText,
  GitBranch,
  Globe2,
  MousePointer2,
  Timer,
} from "lucide-react";
import dayjs from "dayjs";
import { useLocation, useNavigate } from "react-router-dom";
import { capabilityApi } from "../../../api/modules/capability";
import { cronJobApi } from "../../../api/modules/cronjob";
import { invalidateSkillCache, skillApi } from "../../../api/modules/skill";
import type {
  Capability,
  CapabilityDraft,
  CapabilityParameter,
  CapabilityPolicy,
  CapabilityStep,
  Pipeline,
  PipelineEdge,
  PipelineNode,
  EvolutionProposal,
  EvolutionProposalStatus,
  SuggestedSkillDraft,
  TaskRun,
  TaskStep,
} from "../../../api/types";
import {
  CapabilityCard,
  CapabilityChip,
  CapabilityPage,
} from "../../../components/CapabilityLayout";
import { useAppMessage } from "../../../hooks/useAppMessage";
import { useAgentStore } from "../../../stores/agentStore";
import {
  createAsyncStateScope,
  createLatestRequestGate,
} from "../../../utils/latestRequest";
import { attachSuggestedSkillToCapability } from "./suggestedSkills";
import styles from "./index.module.less";

const { TextArea } = Input;

function newId() {
  return globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random()}`;
}

function nowIso() {
  return new Date().toISOString();
}

function errorWithCause(message: string, cause: unknown): Error {
  const wrapped = new Error(message) as Error & { cause?: unknown };
  wrapped.cause = cause;
  return wrapped;
}

function defaultParameter(): CapabilityParameter {
  return {
    name: "task_goal",
    label: "任务目标",
    type: "text",
    required: true,
    default: "",
    description: "",
    options: [],
  };
}

function defaultCapability(): Capability {
  const now = nowIso();
  return {
    id: newId(),
    name: "新的能力",
    description: "把一个可复用任务流程参数化保存下来。",
    version: 1,
    enabled: true,
    tags: [],
    parameters: [defaultParameter()],
    steps: [
      {
        id: newId(),
        index: 0,
        title: "执行任务",
        mode: "dynamic",
        kind: "agent_prompt",
        instruction_template: "{{task_goal}}",
        required_parameters: ["task_goal"],
        risk_level: "low",
      },
      {
        id: newId(),
        index: 1,
        title: "交付结果",
        mode: "fixed",
        kind: "delivery",
        instruction_template: "把本次执行结果交付给用户。",
        required_parameters: [],
        risk_level: "low",
      },
    ],
    delivery_defaults: [{ type: "chat" }],
    risk_policy: { require_approval_for: ["high"] },
    created_at: now,
    updated_at: now,
  };
}

function defaultPipeline(capability?: Capability): Pipeline {
  const now = nowIso();
  return {
    id: newId(),
    name: "新的 Pipeline",
    description: "把多个能力串联或并行编排成一个复合流程。",
    version: 1,
    enabled: true,
    tags: [],
    parameters: [defaultParameter()],
    nodes: capability
      ? [
          {
            id: newId(),
            index: 0,
            title: capability.name,
            capability_id: capability.id,
            parameters: { task_goal: "{{task_goal}}" },
            position: { x: 40, y: 40 },
          },
        ]
      : [],
    edges: [],
    delivery_defaults: [{ type: "chat" }],
    runtime: { max_parallelism: 3 },
    created_at: now,
    updated_at: now,
  };
}

function formatTime(value?: string | null) {
  if (!value) return "暂无记录";
  const parsed = dayjs(value);
  return parsed.isValid() ? parsed.format("MM/DD HH:mm") : "暂无记录";
}

function evolutionActionLabel(proposal: EvolutionProposal) {
  if (proposal.matched_capability_id) return "补充已有能力";
  if (proposal.recommended_form === "automation") return "保存为定时流程";
  return "保存为可复用流程";
}

function evolutionDescription(proposal: EvolutionProposal) {
  if (proposal.matched_capability_id) {
    return "系统发现它和一项已有能力相似。查看后可以在原能力上补充，避免重复创建。";
  }
  if (proposal.recommended_form === "automation") {
    return "这些定时任务的目标相似，可能值得整理成一个固定流程。";
  }
  return "这些任务的目标相似，可能值得整理成下次可直接复用的流程。";
}

function taskStatusTone(status: TaskRun["status"]) {
  if (status === "dispatched" || status === "acked" || status === "running") {
    return "info";
  }
  if (status === "succeeded") return "success";
  if (
    status === "failed" ||
    status === "timed_out" ||
    status === "stalled" ||
    status === "blocked" ||
    status === "cancelled"
  ) {
    return "danger";
  }
  return "muted";
}

function evaluationTone(summary?: TaskRun["evaluation_summary"] | null) {
  if (!summary) return "muted";
  if (summary.conclusion === "pass") return "success";
  if (summary.conclusion === "blocked") return "danger";
  return "warning";
}

function evaluationLabel(summary?: TaskRun["evaluation_summary"] | null) {
  if (!summary) return "NO_REPORT";
  if (summary.conclusion === "pass") return "PASS";
  if (summary.conclusion === "blocked") return "BLOCKED";
  return "NEEDS_REVISION";
}

function isActiveTaskStatus(status: TaskRun["status"]) {
  return status === "dispatched" || status === "acked" || status === "running";
}

function canGenerateDraftFromTaskRun(taskRun: TaskRun) {
  return (
    !isActiveTaskStatus(taskRun.status) &&
    taskRun.status !== "blocked" &&
    taskRun.status !== "stalled" &&
    taskRun.status !== "timed_out"
  );
}

function truncate(value: string, max = 96) {
  const text = value.replace(/\s+/g, " ").trim();
  if (text.length <= max) return text;
  return `${text.slice(0, max).trim()}...`;
}

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
}

function asText(value: unknown): string {
  return typeof value === "string" ? value : value == null ? "" : String(value);
}

function asNumber(value: unknown): number {
  return typeof value === "number" && Number.isFinite(value) ? value : 0;
}

function browserTaskEvidence(step: TaskStep) {
  const metadata = asRecord(step.metadata);
  const runId = asText(metadata.browser_task_run_id);
  if (!runId) return null;
  const plan = asRecord(metadata.browser_task_plan);
  const artifacts = asRecord(metadata.browser_task_artifacts);
  return {
    runId,
    status: asText(metadata.browser_task_status) || "queued",
    errorCode: asText(metadata.browser_task_error_code),
    waitResult: asText(metadata.browser_task_wait_result),
    adapterId: asText(plan.adapter_id) || "generic",
    strategy: asText(plan.strategy) || "planner",
    eventCount: asNumber(artifacts.event_count),
    screenshotCount: asNumber(artifacts.screenshot_count),
    latestScreenshot: asText(artifacts.latest_screenshot),
  };
}

function hasBrowserTaskEvidence(taskRun: TaskRun) {
  return taskRun.steps.some((step) => Boolean(browserTaskEvidence(step)));
}

function hasGuiSteps(capability: Capability | null | undefined) {
  return Boolean(capability?.steps.some((step) => step.kind === "gui_prompt"));
}

function hasGuiPipeline(
  pipeline: Pipeline | null | undefined,
  capabilities: Capability[],
) {
  if (!pipeline) return false;
  const byId = new Map(capabilities.map((item) => [item.id, item]));
  return pipeline.nodes.some((node) =>
    hasGuiSteps(byId.get(node.capability_id)),
  );
}

function confirmHighRisk(title: string, detail: string) {
  return new Promise<boolean>((resolve) => {
    Modal.confirm({
      title,
      content: detail,
      okText: "确认手动运行",
      cancelText: "取消",
      onOk: () => resolve(true),
      onCancel: () => resolve(false),
    });
  });
}

function confirmEnabledEvolutionSave(isExisting: boolean) {
  return new Promise<boolean>((resolve) => {
    Modal.confirm({
      title: isExisting ? "确认更新这项已启用能力？" : "确认保存并启用？",
      content: isExisting
        ? "这项能力当前已启用。保存后，新版本会供下次运行使用，但不会立即运行。"
        : "这份草稿已被设为启用。保存后它会进入能力列表，但不会立即运行。",
      okText: isExisting ? "确认更新" : "保存并启用",
      cancelText: "返回检查",
      onOk: () => resolve(true),
      onCancel: () => resolve(false),
    });
  });
}

type FlowItem = {
  id: string;
  title: string;
  subtitle: string;
  tone?: "default" | "risk" | "muted";
};

function SortableFlowCard({
  item,
  selected,
  onSelect,
}: {
  item: FlowItem;
  selected: boolean;
  onSelect: () => void;
}) {
  const { attributes, listeners, setNodeRef, transform, transition } =
    useSortable({
      id: item.id,
    });
  return (
    <button
      ref={setNodeRef}
      type="button"
      className={`${styles.flowNode} ${selected ? styles.flowNodeSelected : ""} ${
        item.tone === "risk" ? styles.flowNodeRisk : ""
      }`}
      style={{ transform: CSS.Transform.toString(transform), transition }}
      onClick={onSelect}
      {...attributes}
      {...listeners}
    >
      <strong>{item.title}</strong>
      <span>{item.subtitle}</span>
    </button>
  );
}

function FlowCanvas({
  items,
  edges,
  selectedId,
  onSelect,
  onReorder,
  empty,
}: {
  items: FlowItem[];
  edges?: PipelineEdge[];
  selectedId?: string | null;
  onSelect: (id: string) => void;
  onReorder?: (from: number, to: number) => void;
  empty: string;
}) {
  const sensors = useSensors(useSensor(PointerSensor));
  const sequentialEdges = edges?.length
    ? edges
    : items.slice(1).map((item, index) => ({
        id: `${items[index].id}:${item.id}`,
        source: items[index].id,
        target: item.id,
      }));
  const indexById = new Map(items.map((item, index) => [item.id, index]));
  return (
    <div className={styles.flowCanvas}>
      {items.length === 0 ? (
        <Empty description={empty} />
      ) : (
        <>
          <svg className={styles.flowSvg} aria-hidden="true">
            {sequentialEdges.map((edge) => {
              const source = indexById.get(edge.source);
              const target = indexById.get(edge.target);
              if (source === undefined || target === undefined) return null;
              const y1 = 50 + source * 86;
              const y2 = 50 + target * 86;
              return (
                <path
                  key={edge.id}
                  d={`M 36 ${y1} C 70 ${y1}, 70 ${y2}, 104 ${y2}`}
                  className={styles.flowEdge}
                />
              );
            })}
          </svg>
          <DndContext
            sensors={sensors}
            collisionDetection={closestCenter}
            onDragEnd={(event) => {
              if (
                !onReorder ||
                !event.over ||
                event.active.id === event.over.id
              )
                return;
              const from = items.findIndex(
                (item) => item.id === event.active.id,
              );
              const to = items.findIndex((item) => item.id === event.over?.id);
              if (from >= 0 && to >= 0) onReorder(from, to);
            }}
          >
            <SortableContext
              items={items.map((item) => item.id)}
              strategy={verticalListSortingStrategy}
            >
              <div className={styles.flowList}>
                {items.map((item) => (
                  <SortableFlowCard
                    key={item.id}
                    item={item}
                    selected={selectedId === item.id}
                    onSelect={() => onSelect(item.id)}
                  />
                ))}
              </div>
            </SortableContext>
          </DndContext>
        </>
      )}
    </div>
  );
}

export default function CapabilitiesPage() {
  const location = useLocation();
  const navigate = useNavigate();
  const { message } = useAppMessage();
  const { selectedAgent } = useAgentStore();
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [policy, setPolicy] = useState<CapabilityPolicy | null>(null);
  const [capabilities, setCapabilities] = useState<Capability[]>([]);
  const [pipelines, setPipelines] = useState<Pipeline[]>([]);
  const [taskRuns, setTaskRuns] = useState<TaskRun[]>([]);
  const [evolutionProposals, setEvolutionProposals] = useState<
    EvolutionProposal[]
  >([]);
  const [editorOpen, setEditorOpen] = useState(false);
  const [editorCapability, setEditorCapability] = useState<Capability | null>(
    null,
  );
  const [selectedStepId, setSelectedStepId] = useState<string | null>(null);
  const [draftMeta, setDraftMeta] = useState<CapabilityDraft | null>(null);
  const [selectedSuggestedSkills, setSelectedSuggestedSkills] = useState<
    Set<string>
  >(new Set());
  const [pipelineEditorOpen, setPipelineEditorOpen] = useState(false);
  const [editorPipeline, setEditorPipeline] = useState<Pipeline | null>(null);
  const [selectedPipelineNodeId, setSelectedPipelineNodeId] = useState<
    string | null
  >(null);
  const [edgeDraft, setEdgeDraft] = useState({ source: "", target: "" });
  const [saving, setSaving] = useState(false);
  const [runTarget, setRunTarget] = useState<Capability | null>(null);
  const [pipelineRunTarget, setPipelineRunTarget] = useState<Pipeline | null>(
    null,
  );
  const [runValues, setRunValues] = useState<Record<string, unknown>>({});
  const [running, setRunning] = useState(false);
  const [scheduleTarget, setScheduleTarget] = useState<Capability | null>(null);
  const [schedulePipelineTarget, setSchedulePipelineTarget] =
    useState<Pipeline | null>(null);
  const [scheduleCron, setScheduleCron] = useState("0 9 * * *");
  const [scheduleSaving, setScheduleSaving] = useState(false);
  const [handledTaskRunId, setHandledTaskRunId] = useState<string | null>(null);
  const loadGateRef = useRef(createLatestRequestGate());
  const taskDraftGateRef = useRef(createLatestRequestGate());
  const mutationScopeRef = useRef(createAsyncStateScope());
  const handledCollaborationDraftRef = useRef<CapabilityDraft | null>(null);

  const loadData = useCallback(async () => {
    const ownsState = loadGateRef.current.begin();
    setRefreshing(true);
    try {
      const [caps, pipes, runs, nextPolicy, proposals] = await Promise.all([
        capabilityApi.listCapabilities(),
        capabilityApi.listPipelines(),
        capabilityApi.listTaskRuns(),
        capabilityApi.getPolicy(),
        capabilityApi.listEvolutionProposals(),
      ]);
      if (!ownsState()) return;
      setCapabilities(Array.isArray(caps) ? caps : []);
      setPipelines(Array.isArray(pipes) ? pipes : []);
      setTaskRuns(Array.isArray(runs) ? runs : []);
      setPolicy(nextPolicy);
      setEvolutionProposals(Array.isArray(proposals) ? proposals : []);
    } catch (error) {
      if (!ownsState()) return;
      console.error("Failed to load capabilities:", error);
      message.error("加载能力中心失败");
    } finally {
      if (ownsState()) {
        setLoading(false);
        setRefreshing(false);
      }
    }
  }, [message]);

  useEffect(() => {
    setLoading(true);
    setRefreshing(false);
    setPolicy(null);
    setCapabilities([]);
    setPipelines([]);
    setTaskRuns([]);
    setEvolutionProposals([]);
    setEditorOpen(false);
    setEditorCapability(null);
    setPipelineEditorOpen(false);
    setEditorPipeline(null);
    setRunTarget(null);
    setPipelineRunTarget(null);
    setScheduleTarget(null);
    setSchedulePipelineTarget(null);
    setSaving(false);
    setRunning(false);
    setScheduleSaving(false);
    setHandledTaskRunId(null);
    handledCollaborationDraftRef.current = null;
    void loadData();
    return () => {
      loadGateRef.current.invalidate();
      taskDraftGateRef.current.invalidate();
      mutationScopeRef.current.invalidate();
    };
  }, [loadData, selectedAgent]);

  const enabledCount = useMemo(
    () => capabilities.filter((item) => item.enabled).length,
    [capabilities],
  );
  const activeRuns = useMemo(
    () =>
      taskRuns.filter(
        (run) => run.source === "capability" || run.source === "pipeline",
      ),
    [taskRuns],
  );
  const suggestedSkillDrafts: SuggestedSkillDraft[] =
    draftMeta?.suggested_skills || [];
  const pendingEvolutionCount = evolutionProposals.filter(
    (proposal) => proposal.status === "pending",
  ).length;
  const visibleEvolutionProposals = evolutionProposals.filter(
    (proposal) =>
      proposal.status === "pending" || proposal.status === "accepted",
  );

  const openEditor = useCallback(
    (capability: Capability, draft?: CapabilityDraft | null) => {
      setEditorCapability(capability);
      setSelectedStepId(capability.steps[0]?.id || null);
      setDraftMeta(draft || null);
      setSelectedSuggestedSkills(
        draft?.primary_asset === "skill" || draft?.primary_asset === "both"
          ? new Set((draft.suggested_skills || []).map((skill) => skill.name))
          : new Set(),
      );
      setEditorOpen(true);
    },
    [],
  );

  const openPipelineEditor = useCallback((pipeline: Pipeline) => {
    setEditorPipeline(pipeline);
    setSelectedPipelineNodeId(pipeline.nodes[0]?.id || null);
    setEdgeDraft({
      source: pipeline.nodes[0]?.id || "",
      target: pipeline.nodes[1]?.id || "",
    });
    setPipelineEditorOpen(true);
  }, []);

  useEffect(() => {
    const taskRunId = new URLSearchParams(location.search).get("taskRun");
    if (!taskRunId || taskRunId === handledTaskRunId) return;
    const ownsState = taskDraftGateRef.current.begin();
    setHandledTaskRunId(taskRunId);
    capabilityApi
      .draftCapabilityFromTask(taskRunId)
      .then((draft) => {
        if (ownsState()) openEditor(draft.capability, draft);
      })
      .catch((error) => {
        if (!ownsState()) return;
        console.error("Failed to open task draft:", error);
        message.error("打开任务草稿失败");
      });
  }, [handledTaskRunId, location.search, message, openEditor]);

  useEffect(() => {
    const state = location.state as {
      collaborationDraft?: CapabilityDraft;
    } | null;
    const draft = state?.collaborationDraft;
    if (!draft || handledCollaborationDraftRef.current === draft) return;
    handledCollaborationDraftRef.current = draft;
    openEditor(draft.capability, draft);
    navigate(`${location.pathname}${location.search}`, {
      replace: true,
      state: null,
    });
  }, [
    location.pathname,
    location.search,
    location.state,
    navigate,
    openEditor,
  ]);

  const patchCapability = useCallback((patch: Partial<Capability>) => {
    setEditorCapability((current) =>
      current ? { ...current, ...patch } : current,
    );
  }, []);

  const updateStep = useCallback(
    (stepId: string, patch: Partial<CapabilityStep>) => {
      setEditorCapability((current) => {
        if (!current) return current;
        return {
          ...current,
          steps: current.steps.map((step) =>
            step.id === stepId ? { ...step, ...patch } : step,
          ),
        };
      });
    },
    [],
  );

  const addStep = useCallback(
    (kind: CapabilityStep["kind"]) => {
      if (kind === "gui_prompt" && policy?.gui_steps_enabled === false) return;
      const step: CapabilityStep = {
        id: newId(),
        index: editorCapability?.steps.length || 0,
        title:
          kind === "agent_prompt"
            ? "Agent 步骤"
            : kind === "skill_prompt"
              ? "Skill 步骤"
              : kind === "browser_prompt"
                ? "浏览器步骤"
                : kind === "gui_prompt"
                  ? "GUI 操作步骤"
                  : "交付步骤",
        mode: kind === "delivery" ? "fixed" : "dynamic",
        kind,
        instruction_template:
          kind === "delivery"
            ? "把本次执行结果交付给用户。"
            : kind === "browser_prompt"
              ? "请在澜台浏览器工作台中完成：{{task_goal}}"
              : kind === "gui_prompt"
                ? "请打开目标界面并完成：{{task_goal}}"
                : "{{task_goal}}",
        required_parameters: kind === "delivery" ? [] : ["task_goal"],
        risk_level:
          kind === "gui_prompt"
            ? "high"
            : kind === "browser_prompt"
              ? "medium"
              : "low",
        profile_id: kind === "browser_prompt" ? "default" : null,
        target_domains: [],
        access: kind === "browser_prompt" ? "read" : null,
        requires_manual_confirmation: kind === "browser_prompt" ? false : false,
        gui_target: kind === "gui_prompt" ? "browser" : null,
        replay_policy:
          kind === "gui_prompt" || kind === "browser_prompt" ? "full" : null,
      };
      setEditorCapability((current) => {
        if (!current) return current;
        const steps = [...current.steps, step].map((item, index) => ({
          ...item,
          index,
        }));
        return { ...current, steps };
      });
      setSelectedStepId(step.id);
    },
    [editorCapability?.steps.length, policy?.gui_steps_enabled],
  );

  const toggleSuggestedSkill = useCallback((skillName: string) => {
    setSelectedSuggestedSkills((current) => {
      const next = new Set(current);
      if (next.has(skillName)) next.delete(skillName);
      else next.add(skillName);
      return next;
    });
  }, []);

  const saveSelectedSuggestedSkills = useCallback(
    async (capability: Capability): Promise<Capability> => {
      const ownsState = mutationScopeRef.current.capture();
      const suggestedSkills = draftMeta?.suggested_skills || [];
      if (!suggestedSkills.length || selectedSuggestedSkills.size === 0) {
        return capability;
      }

      let nextCapability = capability;
      for (const skill of suggestedSkills) {
        if (!selectedSuggestedSkills.has(skill.name)) continue;
        try {
          const result = await skillApi.createSkill(
            skill.name,
            skill.content,
            undefined,
            true,
          );
          if (!ownsState()) return nextCapability;
          const actualName = result.name || skill.name;
          const sideUpdates: Promise<unknown>[] = [
            skillApi.updateSkillChannels(actualName, ["all"]),
          ];
          if (skill.tags.length) {
            sideUpdates.push(skillApi.updateSkillTags(actualName, skill.tags));
          }
          await Promise.all(sideUpdates);
          if (!ownsState()) return nextCapability;
          invalidateSkillCache();
          nextCapability = attachSuggestedSkillToCapability(nextCapability, {
            name: actualName,
            attach_to_step_index: skill.attach_to_step_index,
          });
        } catch (error) {
          throw errorWithCause(
            `保存技能片段“${skill.name}”失败：${
              error instanceof Error ? error.message : "未知错误"
            }`,
            error,
          );
        }
      }
      return nextCapability;
    },
    [draftMeta?.suggested_skills, selectedSuggestedSkills],
  );

  const saveSkillsOnly = useCallback(async () => {
    if (!editorCapability || selectedSuggestedSkills.size === 0) {
      message.warning("请先选择至少一个 Skill 草稿。");
      return;
    }
    const ownsState = mutationScopeRef.current.capture();
    setSaving(true);
    try {
      await saveSelectedSuggestedSkills(editorCapability);
      if (!ownsState()) return;
      setEditorOpen(false);
      setEditorCapability(null);
      setDraftMeta(null);
      setSelectedSuggestedSkills(new Set());
      message.success("Skill 已保存到当前智能体。");
    } catch (error) {
      if (!ownsState()) return;
      console.error("Failed to save distilled skills:", error);
      message.error(error instanceof Error ? error.message : "保存 Skill 失败");
    } finally {
      if (ownsState()) setSaving(false);
    }
  }, [
    editorCapability,
    message,
    saveSelectedSuggestedSkills,
    selectedSuggestedSkills.size,
  ]);

  const saveCapability = useCallback(async () => {
    if (!editorCapability) return;
    const ownsState = mutationScopeRef.current.capture();
    if (draftMeta?.evolution_proposal_id && editorCapability.enabled) {
      const confirmed = await confirmEnabledEvolutionSave(
        Boolean(draftMeta.matched_capability_id),
      );
      if (!confirmed || !ownsState()) return;
    }
    setSaving(true);
    try {
      const capabilityWithSkills = await saveSelectedSuggestedSkills({
        ...editorCapability,
        steps: editorCapability.steps.map((step, index) => ({
          ...step,
          index,
        })),
        updated_at: nowIso(),
      });
      const capability: Capability = {
        ...capabilityWithSkills,
        steps: capabilityWithSkills.steps.map((step, index) => ({
          ...step,
          index,
        })),
      };
      const exists = capabilities.some((item) => item.id === capability.id);
      const saved = exists
        ? await capabilityApi.updateCapability(capability.id, capability)
        : await capabilityApi.createCapability(capability);
      if (!ownsState()) return;
      setEditorOpen(false);
      setEditorCapability(null);
      setDraftMeta(null);
      setSelectedSuggestedSkills(new Set());
      message.success(exists ? "能力已更新" : "能力已保存");
      setCapabilities((current) => [
        saved,
        ...current.filter((item) => item.id !== saved.id),
      ]);
      if (draftMeta?.evolution_proposal_id) {
        try {
          const reviewed = await capabilityApi.reviewEvolutionProposal(
            draftMeta.evolution_proposal_id,
            { status: "accepted", capability_id: saved.id },
          );
          if (ownsState()) {
            setEvolutionProposals((current) =>
              current.map((item) =>
                item.id === reviewed.id ? reviewed : item,
              ),
            );
          }
        } catch (reviewError) {
          console.error(
            "Failed to mark evolution proposal accepted:",
            reviewError,
          );
          if (ownsState()) message.warning("能力已保存；建议状态暂未同步");
        }
      }
      void loadData();
    } catch (error) {
      if (!ownsState()) return;
      console.error("Failed to save capability:", error);
      message.error(error instanceof Error ? error.message : "保存失败");
    } finally {
      if (ownsState()) setSaving(false);
    }
  }, [
    capabilities,
    draftMeta?.evolution_proposal_id,
    draftMeta?.matched_capability_id,
    editorCapability,
    loadData,
    message,
    saveSelectedSuggestedSkills,
  ]);

  const openEvolutionDraft = useCallback(
    async (proposal: EvolutionProposal) => {
      const ownsState = mutationScopeRef.current.capture();
      try {
        const draft = await capabilityApi.draftCapabilityFromEvolutionProposal(
          proposal.id,
        );
        if (ownsState()) openEditor(draft.capability, draft);
      } catch (error) {
        if (!ownsState()) return;
        console.error("Failed to build evolution draft:", error);
        message.error("暂时无法打开草稿，请重试");
      }
    },
    [message, openEditor],
  );

  const reviewEvolutionProposal = useCallback(
    async (proposal: EvolutionProposal, status: EvolutionProposalStatus) => {
      const ownsState = mutationScopeRef.current.capture();
      try {
        const reviewed = await capabilityApi.reviewEvolutionProposal(
          proposal.id,
          { status },
        );
        if (!ownsState()) return;
        setEvolutionProposals((current) =>
          current.map((item) => (item.id === reviewed.id ? reviewed : item)),
        );
        if (status === "rolled_back") {
          message.success("已停用关联能力并保留审核证据");
          void loadData();
        } else if (status === "dismissed") {
          message.success("已隐藏这条建议；任务记录没有删除");
        } else {
          message.success("已记录本次审核决定");
        }
      } catch (error) {
        if (!ownsState()) return;
        console.error("Failed to review evolution proposal:", error);
        message.error("操作没有完成，请重试");
      }
    },
    [loadData, message],
  );

  const savePipeline = useCallback(async () => {
    if (!editorPipeline) return;
    const ownsState = mutationScopeRef.current.capture();
    setSaving(true);
    try {
      const pipeline: Pipeline = {
        ...editorPipeline,
        nodes: editorPipeline.nodes.map((node, index) => ({ ...node, index })),
        updated_at: nowIso(),
      };
      const exists = pipelines.some((item) => item.id === pipeline.id);
      const saved = exists
        ? await capabilityApi.updatePipeline(pipeline.id, pipeline)
        : await capabilityApi.createPipeline(pipeline);
      if (!ownsState()) return;
      setPipelineEditorOpen(false);
      setEditorPipeline(null);
      message.success(exists ? "Pipeline 已更新" : "Pipeline 已保存");
      setPipelines((current) => [
        saved,
        ...current.filter((item) => item.id !== saved.id),
      ]);
      void loadData();
    } catch (error) {
      if (!ownsState()) return;
      console.error("Failed to save pipeline:", error);
      message.error(error instanceof Error ? error.message : "保存失败");
    } finally {
      if (ownsState()) setSaving(false);
    }
  }, [editorPipeline, loadData, message, pipelines]);

  const generateDraft = useCallback(
    async (taskRun: TaskRun) => {
      const ownsState = mutationScopeRef.current.capture();
      try {
        const draft = await capabilityApi.draftCapabilityFromTask(taskRun.id);
        if (ownsState()) openEditor(draft.capability, draft);
      } catch (error) {
        if (!ownsState()) return;
        console.error("Failed to generate draft:", error);
        message.error("生成能力草稿失败");
      }
    },
    [message, openEditor],
  );

  const duplicateCapability = useCallback(
    (capability: Capability) => {
      openEditor({
        ...capability,
        id: newId(),
        name: `${capability.name} 副本`,
        version: 1,
        enabled: false,
        created_at: nowIso(),
        updated_at: nowIso(),
      });
    },
    [openEditor],
  );

  const deleteCapability = useCallback(
    async (capability: Capability) => {
      const ownsState = mutationScopeRef.current.capture();
      try {
        await capabilityApi.deleteCapability(capability.id);
        if (!ownsState()) return;
        setCapabilities((current) =>
          current.filter((item) => item.id !== capability.id),
        );
        message.success("能力已删除");
      } catch (error) {
        if (!ownsState()) return;
        console.error("Failed to delete capability:", error);
        message.error("删除能力失败");
      }
    },
    [message],
  );

  const deletePipeline = useCallback(
    async (pipeline: Pipeline) => {
      const ownsState = mutationScopeRef.current.capture();
      try {
        await capabilityApi.deletePipeline(pipeline.id);
        if (!ownsState()) return;
        setPipelines((current) =>
          current.filter((item) => item.id !== pipeline.id),
        );
        message.success("Pipeline 已删除");
      } catch (error) {
        if (!ownsState()) return;
        console.error("Failed to delete pipeline:", error);
        message.error("删除 Pipeline 失败");
      }
    },
    [message],
  );

  const openRunModal = useCallback(
    (target: Capability | Pipeline, kind: "capability" | "pipeline") => {
      const defaults = Object.fromEntries(
        target.parameters.map((parameter) => [
          parameter.name,
          parameter.default ?? "",
        ]),
      );
      setRunValues(defaults);
      if (kind === "capability") setRunTarget(target as Capability);
      else setPipelineRunTarget(target as Pipeline);
    },
    [],
  );

  const runCapability = useCallback(async () => {
    if (!runTarget) return;
    const ownsState = mutationScopeRef.current.capture();
    if (hasGuiSteps(runTarget)) {
      const ok = await confirmHighRisk(
        "确认执行 GUI/电脑操作能力",
        "这个能力会通过现有 Agent 工具守卫操作浏览器或桌面界面。运行会记录结构化回放，请确认你正在手动触发。",
      );
      if (!ok || !ownsState()) return;
    }
    setRunning(true);
    try {
      const run = await capabilityApi.runCapability(runTarget.id, {
        parameters: runValues,
        channel: "console",
        manual_gui_ack: hasGuiSteps(runTarget),
        approved_risks: hasGuiSteps(runTarget) ? ["high"] : [],
      });
      if (!ownsState()) return;
      message.success(
        run.status === "succeeded"
          ? "能力运行完成"
          : `能力运行已记录：${run.status}`,
      );
      setRunTarget(null);
      void loadData();
    } catch (error) {
      if (!ownsState()) return;
      console.error("Failed to run capability:", error);
      message.error(error instanceof Error ? error.message : "运行失败");
    } finally {
      if (ownsState()) setRunning(false);
    }
  }, [loadData, message, runTarget, runValues]);

  const runPipeline = useCallback(async () => {
    if (!pipelineRunTarget) return;
    const ownsState = mutationScopeRef.current.capture();
    const gui = hasGuiPipeline(pipelineRunTarget, capabilities);
    if (gui) {
      const ok = await confirmHighRisk(
        "确认执行含 GUI/电脑操作的 Pipeline",
        "Pipeline 中包含高风险 GUI 步骤，只允许手动运行，定时任务会被后端拒绝。",
      );
      if (!ok || !ownsState()) return;
    }
    setRunning(true);
    try {
      const run = await capabilityApi.runPipeline(pipelineRunTarget.id, {
        parameters: runValues,
        channel: "console",
        manual_gui_ack: gui,
        approved_risks: gui ? ["high"] : [],
      });
      if (!ownsState()) return;
      message.success(
        run.status === "succeeded"
          ? "Pipeline 运行完成"
          : `Pipeline 已记录：${run.status}`,
      );
      setPipelineRunTarget(null);
      void loadData();
    } catch (error) {
      if (!ownsState()) return;
      console.error("Failed to run pipeline:", error);
      message.error(error instanceof Error ? error.message : "运行失败");
    } finally {
      if (ownsState()) setRunning(false);
    }
  }, [capabilities, loadData, message, pipelineRunTarget, runValues]);

  const createScheduledCapability = useCallback(async () => {
    const target = scheduleTarget || schedulePipelineTarget;
    if (!target) return;
    const ownsState = mutationScopeRef.current.capture();
    setScheduleSaving(true);
    try {
      const parameters = Object.fromEntries(
        target.parameters.map((parameter) => [
          parameter.name,
          parameter.default ?? "",
        ]),
      );
      const isPipeline = Boolean(schedulePipelineTarget);
      await cronJobApi.createCronJob({
        name: `${target.name} 定时运行`,
        enabled: true,
        schedule: {
          type: "cron",
          cron: scheduleCron,
          timezone: Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC",
        },
        task_type: isPipeline ? "pipeline" : "capability",
        capability_request: !isPipeline
          ? { capability_id: target.id, parameters }
          : undefined,
        pipeline_request: isPipeline
          ? { pipeline_id: target.id, parameters }
          : undefined,
        dispatch: {
          type: "channel",
          channel: "console",
          target: { user_id: "capability", session_id: "console:capability" },
          mode: "stream",
          meta: { source: isPipeline ? "pipeline" : "capability" },
        },
      });
      if (!ownsState()) return;
      message.success(
        isPipeline ? "已创建定时 Pipeline" : "已创建定时能力任务",
      );
      setScheduleTarget(null);
      setSchedulePipelineTarget(null);
      void loadData();
    } catch (error) {
      if (!ownsState()) return;
      console.error("Failed to schedule capability:", error);
      message.error(
        error instanceof Error ? error.message : "创建定时任务失败",
      );
    } finally {
      if (ownsState()) setScheduleSaving(false);
    }
  }, [loadData, message, scheduleCron, schedulePipelineTarget, scheduleTarget]);

  const selectedStep =
    editorCapability?.steps.find((step) => step.id === selectedStepId) || null;
  const selectedPipelineNode =
    editorPipeline?.nodes.find((node) => node.id === selectedPipelineNodeId) ||
    null;
  const selectedNodeCapability = capabilities.find(
    (item) => item.id === selectedPipelineNode?.capability_id,
  );

  const capabilityItems =
    editorCapability?.steps.map((step) => ({
      id: step.id,
      title: step.title,
      subtitle: `${step.kind} · ${step.risk_level}`,
      tone:
        step.kind === "gui_prompt" ? ("risk" as const) : ("default" as const),
    })) || [];
  const pipelineItems =
    editorPipeline?.nodes.map((node) => ({
      id: node.id,
      title: node.title,
      subtitle:
        capabilities.find((item) => item.id === node.capability_id)?.name ||
        "未选择能力",
      tone: hasGuiSteps(
        capabilities.find((item) => item.id === node.capability_id),
      )
        ? ("risk" as const)
        : ("default" as const),
    })) || [];

  const renderParameterInputs = (target: Capability | Pipeline | null) => (
    <div className={styles.runForm}>
      {target?.parameters.length ? (
        target.parameters.map((parameter) => (
          <label key={parameter.name}>
            <span>{parameter.label || parameter.name}</span>
            {parameter.type === "boolean" ? (
              <Switch
                checked={Boolean(runValues[parameter.name])}
                onChange={(checked) =>
                  setRunValues({ ...runValues, [parameter.name]: checked })
                }
              />
            ) : parameter.type === "number" ? (
              <InputNumber
                value={Number(runValues[parameter.name] || 0)}
                onChange={(value) =>
                  setRunValues({ ...runValues, [parameter.name]: value })
                }
              />
            ) : (
              <Input
                value={String(runValues[parameter.name] ?? "")}
                onChange={(event) =>
                  setRunValues({
                    ...runValues,
                    [parameter.name]: event.target.value,
                  })
                }
              />
            )}
          </label>
        ))
      ) : (
        <Empty description="没有参数，可以直接运行" />
      )}
    </div>
  );

  return (
    <CapabilityPage
      title="能力中心"
      description="把成功任务沉淀成可编辑、可复用、可定时运行的能力与 Pipeline。"
      actions={
        <>
          <Button
            icon={<ReloadOutlined spin={refreshing} />}
            onClick={() => void loadData()}
          >
            刷新
          </Button>
          <Button
            type="primary"
            icon={<PlusOutlined />}
            onClick={() => openEditor(defaultCapability())}
          >
            创建能力
          </Button>
        </>
      }
      stats={[
        { label: "能力", value: capabilities.length },
        { label: "Pipeline", value: pipelines.length, tone: "info" },
        { label: "已启用", value: enabledCount, tone: "success" },
        { label: "任务记录", value: taskRuns.length, tone: "info" },
        {
          label: "待复核建议",
          value: pendingEvolutionCount,
          tone: pendingEvolutionCount ? "warning" : "muted",
        },
      ]}
      className={styles.page}
    >
      {loading ? (
        <div className={styles.loading}>
          <Spin />
        </div>
      ) : (
        <Tabs
          className={styles.tabs}
          items={[
            {
              key: "capabilities",
              label: "能力",
              children: (
                <section className={styles.section}>
                  <div className={styles.sectionHeader}>
                    <div>
                      <h2>能力库</h2>
                      <p>手动创建或从任务草稿保存后的可复用流程。</p>
                    </div>
                  </div>
                  {capabilities.length === 0 ? (
                    <Empty description="还没有能力，可以先从最近任务生成一个草稿" />
                  ) : (
                    <div className={styles.cardGrid}>
                      {capabilities.map((capability) => (
                        <CapabilityCard
                          key={capability.id}
                          icon={
                            hasGuiSteps(capability) ? (
                              <MousePointer2 size={18} />
                            ) : (
                              <Boxes size={18} />
                            )
                          }
                          title={capability.name}
                          subtitle={`v${capability.version} · 更新 ${formatTime(capability.updated_at)}`}
                          status={capability.enabled ? "已启用" : "草稿"}
                          statusTone={capability.enabled ? "success" : "muted"}
                          description={capability.description || "暂无描述"}
                          chips={
                            <>
                              <CapabilityChip tone="info">
                                {capability.parameters.length} 参数
                              </CapabilityChip>
                              <CapabilityChip tone="default">
                                {capability.steps.length} 步骤
                              </CapabilityChip>
                              {hasGuiSteps(capability) ? (
                                <CapabilityChip tone="danger">
                                  GUI 手动
                                </CapabilityChip>
                              ) : null}
                              {capability.tags.slice(0, 3).map((tag) => (
                                <CapabilityChip key={tag} tone="muted">
                                  {tag}
                                </CapabilityChip>
                              ))}
                            </>
                          }
                          footer={
                            <Space wrap>
                              <Button
                                size="small"
                                type="primary"
                                icon={<PlayCircleOutlined />}
                                disabled={!capability.enabled}
                                onClick={() =>
                                  openRunModal(capability, "capability")
                                }
                              >
                                运行
                              </Button>
                              <Button
                                size="small"
                                icon={<EditOutlined />}
                                onClick={() => openEditor(capability)}
                              >
                                编辑
                              </Button>
                              <Button
                                size="small"
                                icon={<CopyOutlined />}
                                onClick={() => duplicateCapability(capability)}
                              >
                                复制
                              </Button>
                              <Button
                                size="small"
                                icon={<CalendarOutlined />}
                                disabled={
                                  !capability.enabled || hasGuiSteps(capability)
                                }
                                onClick={() => {
                                  setScheduleCron("0 9 * * *");
                                  setScheduleTarget(capability);
                                }}
                              >
                                定时
                              </Button>
                              <Popconfirm
                                title="删除这个能力？"
                                onConfirm={() =>
                                  void deleteCapability(capability)
                                }
                              >
                                <Button
                                  size="small"
                                  danger
                                  icon={<DeleteOutlined />}
                                >
                                  删除
                                </Button>
                              </Popconfirm>
                            </Space>
                          }
                        />
                      ))}
                    </div>
                  )}
                </section>
              ),
            },
            {
              key: "pipelines",
              label: "Pipeline",
              children: (
                <section className={styles.section}>
                  <div className={styles.sectionHeader}>
                    <div>
                      <h2>多能力编排</h2>
                      <p>
                        支持串行、并行分支、依赖结果传参，失败即停并保留已完成产物。
                      </p>
                    </div>
                    <Button
                      icon={<PlusOutlined />}
                      onClick={() =>
                        openPipelineEditor(defaultPipeline(capabilities[0]))
                      }
                    >
                      创建 Pipeline
                    </Button>
                  </div>
                  {pipelines.length === 0 ? (
                    <Empty description="还没有 Pipeline" />
                  ) : (
                    <div className={styles.cardGrid}>
                      {pipelines.map((pipeline) => (
                        <CapabilityCard
                          key={pipeline.id}
                          icon={<GitBranch size={18} />}
                          title={pipeline.name}
                          subtitle={`v${pipeline.version} · 更新 ${formatTime(pipeline.updated_at)}`}
                          status={pipeline.enabled ? "已启用" : "草稿"}
                          statusTone={pipeline.enabled ? "success" : "muted"}
                          description={pipeline.description || "暂无描述"}
                          chips={
                            <>
                              <CapabilityChip tone="info">
                                {pipeline.nodes.length} 节点
                              </CapabilityChip>
                              <CapabilityChip tone="default">
                                {pipeline.edges.length} 连线
                              </CapabilityChip>
                              {hasGuiPipeline(pipeline, capabilities) ? (
                                <CapabilityChip tone="danger">
                                  含 GUI
                                </CapabilityChip>
                              ) : null}
                            </>
                          }
                          footer={
                            <Space wrap>
                              <Button
                                size="small"
                                type="primary"
                                icon={<PlayCircleOutlined />}
                                disabled={!pipeline.enabled}
                                onClick={() =>
                                  openRunModal(pipeline, "pipeline")
                                }
                              >
                                运行
                              </Button>
                              <Button
                                size="small"
                                icon={<EditOutlined />}
                                onClick={() => openPipelineEditor(pipeline)}
                              >
                                编辑
                              </Button>
                              <Button
                                size="small"
                                icon={<CalendarOutlined />}
                                disabled={
                                  !pipeline.enabled ||
                                  hasGuiPipeline(pipeline, capabilities)
                                }
                                onClick={() => {
                                  setScheduleCron("0 9 * * *");
                                  setSchedulePipelineTarget(pipeline);
                                }}
                              >
                                定时
                              </Button>
                              <Popconfirm
                                title="删除这个 Pipeline？"
                                onConfirm={() => void deletePipeline(pipeline)}
                              >
                                <Button
                                  size="small"
                                  danger
                                  icon={<DeleteOutlined />}
                                >
                                  删除
                                </Button>
                              </Popconfirm>
                            </Space>
                          }
                        />
                      ))}
                    </div>
                  )}
                </section>
              ),
            },
            {
              key: "evolution",
              label: `可复用任务建议${pendingEvolutionCount ? ` ${pendingEvolutionCount}` : ""}`,
              children: (
                <section className={styles.section}>
                  <div className={styles.sectionHeader}>
                    <div>
                      <h2>这些任务可能值得保存</h2>
                      <p>系统只会提醒你可能重复做过的事，由你决定是否保存。</p>
                    </div>
                  </div>
                  <div className={styles.evolutionSafetyGuide}>
                    <div>
                      <strong>这里不会自动创建或运行任何东西</strong>
                      <span>
                        点击“查看并编辑”只会打开草稿，不会保存或运行；新建流程默认不启用。
                      </span>
                    </div>
                    <ol aria-label="处理建议的三个步骤">
                      <li>看来源记录</li>
                      <li>检查并修改步骤</li>
                      <li>确认后才保存</li>
                    </ol>
                  </div>
                  {visibleEvolutionProposals.length === 0 ? (
                    <Empty description="暂时没有发现值得保存的重复任务" />
                  ) : (
                    <div className={styles.cardGrid}>
                      {visibleEvolutionProposals.map((proposal) => (
                        <CapabilityCard
                          key={proposal.id}
                          icon={<Brain size={18} />}
                          title={proposal.title}
                          subtitle={`根据最近 ${proposal.repeat_count} 次成功任务生成`}
                          status={
                            proposal.status === "pending"
                              ? "等你确认"
                              : proposal.status === "accepted"
                                ? "已采纳"
                                : proposal.status === "dismissed"
                                  ? "已忽略"
                                  : "已回滚"
                          }
                          statusTone={
                            proposal.status === "pending"
                              ? "warning"
                              : proposal.status === "accepted"
                                ? "success"
                                : "muted"
                          }
                          description={evolutionDescription(proposal)}
                          chips={
                            <>
                              <CapabilityChip tone="info">
                                建议：{evolutionActionLabel(proposal)}
                              </CapabilityChip>
                              {proposal.evidence
                                .slice(0, 3)
                                .map((evidence, index) => (
                                  <CapabilityChip
                                    key={evidence.run_id}
                                    tone="default"
                                    title={evidence.title}
                                  >
                                    第 {index + 1} 次 ·{" "}
                                    {formatTime(evidence.occurred_at)}
                                  </CapabilityChip>
                                ))}
                            </>
                          }
                          footer={
                            <div className={styles.evolutionCardFooter}>
                              {proposal.status === "pending" ? (
                                <Space wrap>
                                  <Button
                                    size="small"
                                    type="primary"
                                    onClick={() =>
                                      void openEvolutionDraft(proposal)
                                    }
                                  >
                                    查看并编辑
                                  </Button>
                                  <Popconfirm
                                    title="不再显示这条建议？任务记录不会被删除。"
                                    okText="不再提示"
                                    cancelText="取消"
                                    onConfirm={() =>
                                      void reviewEvolutionProposal(
                                        proposal,
                                        "dismissed",
                                      )
                                    }
                                  >
                                    <Button size="small">不再提示</Button>
                                  </Popconfirm>
                                </Space>
                              ) : null}
                              {proposal.status === "accepted" &&
                              proposal.capability_id ? (
                                <Popconfirm
                                  title="停用关联能力并回滚这条建议？"
                                  onConfirm={() =>
                                    void reviewEvolutionProposal(
                                      proposal,
                                      "rolled_back",
                                    )
                                  }
                                >
                                  <Button size="small" danger>
                                    停用并回滚
                                  </Button>
                                </Popconfirm>
                              ) : null}
                              {proposal.status === "pending" ? (
                                <span>打开不会保存，新建内容默认不启用</span>
                              ) : null}
                            </div>
                          }
                        />
                      ))}
                    </div>
                  )}
                </section>
              ),
            },
            {
              key: "tasks",
              label: "任务沉淀",
              children: (
                <section className={styles.section}>
                  <div className={styles.sectionHeader}>
                    <div>
                      <h2>最近任务</h2>
                      <p>
                        聊天、定时、能力和 Pipeline
                        运行都可以复盘步骤并生成草稿。
                      </p>
                    </div>
                  </div>
                  {taskRuns.length === 0 ? (
                    <Empty description="还没有任务记录" />
                  ) : (
                    <div className={styles.taskList}>
                      {taskRuns.slice(0, 40).map((taskRun) => (
                        <article key={taskRun.id} className={styles.taskItem}>
                          <span className={styles.taskIcon}>
                            {taskRun.source === "cron" ? (
                              <Timer size={16} />
                            ) : (
                              <FileText size={16} />
                            )}
                          </span>
                          <div className={styles.taskCopy}>
                            <strong>{taskRun.title}</strong>
                            <span>
                              {truncate(
                                taskRun.user_intent || `${taskRun.source} 任务`,
                                140,
                              )}
                            </span>
                            <div className={styles.taskChips}>
                              <CapabilityChip
                                tone={taskStatusTone(taskRun.status)}
                              >
                                {taskRun.status}
                              </CapabilityChip>
                              {taskRun.evaluation_summary ? (
                                <CapabilityChip
                                  tone={evaluationTone(
                                    taskRun.evaluation_summary,
                                  )}
                                >
                                  {evaluationLabel(taskRun.evaluation_summary)}
                                </CapabilityChip>
                              ) : null}
                              <CapabilityChip tone="muted">
                                {taskRun.source}
                              </CapabilityChip>
                              <CapabilityChip tone="default">
                                {taskRun.steps.length} 步骤
                              </CapabilityChip>
                              <CapabilityChip tone="default">
                                {taskRun.artifacts.length} 产物
                              </CapabilityChip>
                              {hasBrowserTaskEvidence(taskRun) ? (
                                <CapabilityChip tone="info">
                                  浏览器任务
                                </CapabilityChip>
                              ) : null}
                              <CapabilityChip tone="default">
                                {formatTime(taskRun.started_at)}
                              </CapabilityChip>
                            </div>
                            {taskRun.steps.length ? (
                              <div className={styles.timelinePreview}>
                                {taskRun.steps.slice(0, 4).map((step) => (
                                  <span key={step.id}>{step.title}</span>
                                ))}
                              </div>
                            ) : null}
                          </div>
                          <Button
                            size="small"
                            disabled={!canGenerateDraftFromTaskRun(taskRun)}
                            onClick={() => void generateDraft(taskRun)}
                          >
                            生成能力
                          </Button>
                        </article>
                      ))}
                    </div>
                  )}
                </section>
              ),
            },
            {
              key: "runs",
              label: "运行记录",
              children: (
                <section className={styles.section}>
                  <div className={styles.sectionHeader}>
                    <div>
                      <h2>能力运行记录</h2>
                      <p>
                        查看能力和 Pipeline 的步骤、产物、错误和 GUI replay
                        路径。
                      </p>
                    </div>
                  </div>
                  {activeRuns.length === 0 ? (
                    <Empty description="还没有能力运行记录" />
                  ) : (
                    <div className={styles.taskList}>
                      {activeRuns.slice(0, 40).map((run) => (
                        <article key={run.id} className={styles.runItem}>
                          <div className={styles.taskCopy}>
                            <strong>{run.title}</strong>
                            <div className={styles.taskChips}>
                              <CapabilityChip tone={taskStatusTone(run.status)}>
                                {run.status}
                              </CapabilityChip>
                              {run.evaluation_summary ? (
                                <CapabilityChip
                                  tone={evaluationTone(run.evaluation_summary)}
                                >
                                  {evaluationLabel(run.evaluation_summary)}
                                </CapabilityChip>
                              ) : null}
                              <CapabilityChip tone="muted">
                                {run.source}
                              </CapabilityChip>
                              <CapabilityChip tone="default">
                                {formatTime(run.started_at)}
                              </CapabilityChip>
                            </div>
                            <div className={styles.runTimeline}>
                              {run.steps.map((step) => {
                                const browserTask = browserTaskEvidence(step);
                                return (
                                  <div key={step.id} className={styles.runStep}>
                                    <span>{step.title}</span>
                                    <em>{step.status}</em>
                                    {browserTask ? (
                                      <section
                                        className={styles.browserTaskEvidence}
                                      >
                                        <div>
                                          <strong>Browser Task</strong>
                                          <small>
                                            {browserTask.status} ·{" "}
                                            {browserTask.strategy} /{" "}
                                            {browserTask.adapterId}
                                          </small>
                                        </div>
                                        <div
                                          className={styles.browserTaskChips}
                                        >
                                          <CapabilityChip tone="info">
                                            {browserTask.runId}
                                          </CapabilityChip>
                                          {browserTask.waitResult ? (
                                            <CapabilityChip tone="default">
                                              {browserTask.waitResult}
                                            </CapabilityChip>
                                          ) : null}
                                          {browserTask.eventCount ? (
                                            <CapabilityChip tone="default">
                                              {browserTask.eventCount} events
                                            </CapabilityChip>
                                          ) : null}
                                          {browserTask.screenshotCount ? (
                                            <CapabilityChip tone="default">
                                              {browserTask.screenshotCount}{" "}
                                              shots
                                            </CapabilityChip>
                                          ) : null}
                                          {browserTask.errorCode ? (
                                            <CapabilityChip tone="danger">
                                              {browserTask.errorCode}
                                            </CapabilityChip>
                                          ) : null}
                                          <Button
                                            size="small"
                                            href={`/chat?browser_task=${encodeURIComponent(browserTask.runId)}`}
                                          >
                                            查看任务
                                          </Button>
                                        </div>
                                        {browserTask.latestScreenshot ? (
                                          <code>
                                            {browserTask.latestScreenshot}
                                          </code>
                                        ) : null}
                                      </section>
                                    ) : null}
                                    {step.metadata?.replay_path ? (
                                      <code>
                                        {String(step.metadata.replay_path)}
                                      </code>
                                    ) : null}
                                  </div>
                                );
                              })}
                            </div>
                          </div>
                        </article>
                      ))}
                    </div>
                  )}
                </section>
              ),
            },
          ]}
        />
      )}

      <Modal
        title={
          draftMeta?.scope
            ? "复核这段协作的沉淀"
            : draftMeta?.evolution_proposal_id
              ? "查看并编辑可复用任务草稿"
              : draftMeta
                ? "从任务生成能力"
                : "能力可视编辑器"
        }
        open={editorOpen}
        width={1180}
        destroyOnClose
        onCancel={() => {
          setEditorOpen(false);
          setSelectedSuggestedSkills(new Set());
        }}
        onOk={() =>
          void (draftMeta?.primary_asset === "skill"
            ? saveSkillsOnly()
            : saveCapability())
        }
        okText={
          draftMeta?.primary_asset === "skill"
            ? "保存 Skill"
            : draftMeta?.primary_asset === "both"
              ? "保存 Skill + 能力"
              : "保存能力"
        }
        confirmLoading={saving}
        okButtonProps={{ icon: <SaveOutlined /> }}
        footer={
          draftMeta?.scope && suggestedSkillDrafts.length
            ? (_originNode, { OkBtn, CancelBtn }) => (
                <Space>
                  <CancelBtn />
                  <Button
                    disabled={saving}
                    onClick={() =>
                      void (draftMeta.primary_asset === "skill"
                        ? saveCapability()
                        : saveSkillsOnly())
                    }
                  >
                    {draftMeta.primary_asset === "skill"
                      ? "同时保存为能力"
                      : "只保存 Skill"}
                  </Button>
                  <OkBtn />
                </Space>
              )
            : undefined
        }
      >
        {editorCapability ? (
          <div
            className={styles.visualEditor}
            data-primary-asset={draftMeta?.primary_asset || "capability"}
          >
            {draftMeta?.evolution_proposal_id ? (
              <div className={styles.evolutionDraftNotice}>
                <strong>尚未保存</strong>
                <span>
                  {draftMeta.matched_capability_id
                    ? "这份草稿会更新已有能力。请先检查名称、参数和每个步骤；保存后会产生新版本，但不会立即运行。"
                    : "请先检查名称、参数和每个步骤。点击底部“保存能力”后才会创建，且不会立即运行。"}
                </span>
              </div>
            ) : null}
            <section className={styles.editorBasics}>
              <div className={styles.formGrid}>
                <label>
                  <span>名称</span>
                  <Input
                    value={editorCapability.name}
                    onChange={(event) =>
                      patchCapability({ name: event.target.value })
                    }
                  />
                </label>
                <label>
                  <span>标签</span>
                  <Input
                    value={editorCapability.tags.join(", ")}
                    onChange={(event) =>
                      patchCapability({
                        tags: event.target.value
                          .split(",")
                          .map((item) => item.trim())
                          .filter(Boolean),
                      })
                    }
                  />
                </label>
              </div>
              <label className={styles.fullField}>
                <span>描述</span>
                <TextArea
                  rows={2}
                  value={editorCapability.description}
                  onChange={(event) =>
                    patchCapability({ description: event.target.value })
                  }
                />
              </label>
              <div className={styles.switchLine}>
                <span>启用状态</span>
                <Switch
                  checked={editorCapability.enabled}
                  checkedChildren="启用"
                  unCheckedChildren="草稿"
                  onChange={(enabled) => patchCapability({ enabled })}
                />
              </div>
            </section>

            <section className={styles.parameterPanel}>
              <div className={styles.panelHeader}>
                <strong>参数</strong>
                <Button
                  size="small"
                  icon={<PlusOutlined />}
                  onClick={() =>
                    patchCapability({
                      parameters: [
                        ...editorCapability.parameters,
                        {
                          ...defaultParameter(),
                          name: `param_${editorCapability.parameters.length + 1}`,
                        },
                      ],
                    })
                  }
                >
                  添加
                </Button>
              </div>
              {editorCapability.parameters.map((parameter, index) => (
                <div
                  key={parameter.name || index}
                  className={styles.parameterRow}
                >
                  <Input
                    value={parameter.name}
                    onChange={(event) => {
                      const parameters = [...editorCapability.parameters];
                      parameters[index] = {
                        ...parameter,
                        name: event.target.value,
                      };
                      patchCapability({ parameters });
                    }}
                  />
                  <Input
                    value={parameter.label || ""}
                    placeholder="显示名"
                    onChange={(event) => {
                      const parameters = [...editorCapability.parameters];
                      parameters[index] = {
                        ...parameter,
                        label: event.target.value,
                      };
                      patchCapability({ parameters });
                    }}
                  />
                  <Select
                    value={parameter.type}
                    options={[
                      { value: "text", label: "文本" },
                      { value: "number", label: "数字" },
                      { value: "boolean", label: "开关" },
                      { value: "select", label: "选项" },
                    ]}
                    onChange={(type) => {
                      const parameters = [...editorCapability.parameters];
                      parameters[index] = { ...parameter, type };
                      patchCapability({ parameters });
                    }}
                  />
                  <Popconfirm
                    title="删除参数？"
                    onConfirm={() =>
                      patchCapability({
                        parameters: editorCapability.parameters.filter(
                          (_, itemIndex) => itemIndex !== index,
                        ),
                      })
                    }
                  >
                    <Button size="small" danger icon={<DeleteOutlined />} />
                  </Popconfirm>
                </div>
              ))}
            </section>

            <div className={styles.flowEditorGrid}>
              <aside className={styles.nodeLibrary}>
                <strong>节点库</strong>
                <Button
                  icon={<Brain size={15} />}
                  onClick={() => addStep("agent_prompt")}
                >
                  Agent 步骤
                </Button>
                <Button
                  icon={<Boxes size={15} />}
                  onClick={() => addStep("skill_prompt")}
                >
                  Skill 步骤
                </Button>
                {policy?.gui_steps_enabled !== false ? (
                  <Button
                    icon={<Globe2 size={15} />}
                    onClick={() => addStep("browser_prompt")}
                  >
                    浏览器步骤
                  </Button>
                ) : null}
                <Button
                  icon={<FileText size={15} />}
                  onClick={() => addStep("delivery")}
                >
                  交付步骤
                </Button>
                {policy?.gui_steps_enabled !== false ? (
                  <Button
                    icon={<MousePointer2 size={15} />}
                    onClick={() => addStep("gui_prompt")}
                  >
                    GUI 步骤
                  </Button>
                ) : null}
              </aside>
              <FlowCanvas
                items={capabilityItems}
                selectedId={selectedStepId}
                onSelect={setSelectedStepId}
                onReorder={(from, to) => {
                  const steps = arrayMove(editorCapability.steps, from, to).map(
                    (step, index) => ({ ...step, index }),
                  );
                  patchCapability({ steps });
                }}
                empty="从左侧添加一个步骤"
              />
              <aside className={styles.propertyPanel}>
                <strong>步骤属性</strong>
                {selectedStep ? (
                  <>
                    <label>
                      <span>标题</span>
                      <Input
                        value={selectedStep.title}
                        onChange={(event) =>
                          updateStep(selectedStep.id, {
                            title: event.target.value,
                          })
                        }
                      />
                    </label>
                    <label>
                      <span>类型</span>
                      <Select
                        value={selectedStep.kind}
                        options={[
                          { value: "agent_prompt", label: "Agent 步骤" },
                          { value: "skill_prompt", label: "Skill 步骤" },
                          ...(policy?.gui_steps_enabled === false
                            ? []
                            : [
                                {
                                  value: "browser_prompt",
                                  label: "浏览器步骤",
                                },
                              ]),
                          ...(policy?.gui_steps_enabled === false
                            ? []
                            : [{ value: "gui_prompt", label: "GUI 步骤" }]),
                          { value: "delivery", label: "交付步骤" },
                        ]}
                        onChange={(kind) =>
                          updateStep(selectedStep.id, {
                            kind,
                            risk_level:
                              kind === "gui_prompt"
                                ? "high"
                                : kind === "browser_prompt"
                                  ? "medium"
                                  : selectedStep.risk_level,
                          })
                        }
                      />
                    </label>
                    <label>
                      <span>模式</span>
                      <Select
                        value={selectedStep.mode}
                        options={[
                          { value: "fixed", label: "固定" },
                          { value: "dynamic", label: "动态" },
                        ]}
                        onChange={(mode) =>
                          updateStep(selectedStep.id, { mode })
                        }
                      />
                    </label>
                    <label>
                      <span>风险等级</span>
                      <Select
                        value={selectedStep.risk_level}
                        options={[
                          { value: "low", label: "低" },
                          { value: "medium", label: "中" },
                          { value: "high", label: "高" },
                        ]}
                        onChange={(risk_level) =>
                          updateStep(selectedStep.id, { risk_level })
                        }
                      />
                    </label>
                    {selectedStep.kind === "skill_prompt" ? (
                      <label>
                        <span>Skill 名称</span>
                        <Input
                          value={selectedStep.skill_name || ""}
                          onChange={(event) =>
                            updateStep(selectedStep.id, {
                              skill_name: event.target.value,
                            })
                          }
                        />
                      </label>
                    ) : null}
                    {selectedStep.kind === "gui_prompt" ? (
                      <>
                        <label>
                          <span>GUI 目标</span>
                          <Select
                            value={selectedStep.gui_target || "browser"}
                            options={[
                              { value: "browser", label: "浏览器" },
                              { value: "desktop", label: "桌面" },
                            ]}
                            onChange={(gui_target) =>
                              updateStep(selectedStep.id, {
                                gui_target,
                                replay_policy: "full",
                                risk_level: "high",
                              })
                            }
                          />
                        </label>
                        <Tag color="orange">手动运行 · 完整结构化 replay</Tag>
                      </>
                    ) : null}
                    {selectedStep.kind === "browser_prompt" ? (
                      <>
                        <label>
                          <span>Profile</span>
                          <Input
                            value={selectedStep.profile_id || "default"}
                            onChange={(event) =>
                              updateStep(selectedStep.id, {
                                profile_id: event.target.value || "default",
                              })
                            }
                          />
                        </label>
                        <label>
                          <span>目标域名</span>
                          <Select
                            mode="tags"
                            value={selectedStep.target_domains || []}
                            options={(selectedStep.target_domains || []).map(
                              (domain) => ({ value: domain, label: domain }),
                            )}
                            onChange={(target_domains) =>
                              updateStep(selectedStep.id, { target_domains })
                            }
                          />
                        </label>
                        <label>
                          <span>访问级别</span>
                          <Select
                            value={selectedStep.access || "read"}
                            options={[
                              { value: "read", label: "读取/观察" },
                              { value: "write", label: "写入/操作" },
                              { value: "sensitive", label: "敏感/发布" },
                            ]}
                            onChange={(access) =>
                              updateStep(selectedStep.id, {
                                access,
                                risk_level:
                                  access === "read"
                                    ? selectedStep.risk_level
                                    : "high",
                                requires_manual_confirmation: access !== "read",
                              })
                            }
                          />
                        </label>
                        <label>
                          <span>强制确认</span>
                          <Switch
                            checked={Boolean(
                              selectedStep.requires_manual_confirmation,
                            )}
                            onChange={(requires_manual_confirmation) =>
                              updateStep(selectedStep.id, {
                                requires_manual_confirmation,
                              })
                            }
                          />
                        </label>
                        <Tag color="blue">
                          Browser Workbench · Broker replay
                        </Tag>
                      </>
                    ) : null}
                    <label>
                      <span>超时秒数</span>
                      <InputNumber
                        min={1}
                        value={selectedStep.timeout_seconds || undefined}
                        onChange={(timeout_seconds) =>
                          updateStep(selectedStep.id, { timeout_seconds })
                        }
                      />
                    </label>
                    <label>
                      <span>指令模板</span>
                      <TextArea
                        rows={7}
                        value={selectedStep.instruction_template}
                        onChange={(event) =>
                          updateStep(selectedStep.id, {
                            instruction_template: event.target.value,
                          })
                        }
                      />
                    </label>
                    <label>
                      <span>所需参数</span>
                      <Select
                        mode="tags"
                        value={selectedStep.required_parameters}
                        options={editorCapability.parameters.map((item) => ({
                          value: item.name,
                          label: item.name,
                        }))}
                        onChange={(required_parameters) =>
                          updateStep(selectedStep.id, { required_parameters })
                        }
                      />
                    </label>
                    <Popconfirm
                      title="删除这个步骤？"
                      onConfirm={() => {
                        const steps = editorCapability.steps
                          .filter((step) => step.id !== selectedStep.id)
                          .map((step, index) => ({ ...step, index }));
                        patchCapability({ steps });
                        setSelectedStepId(steps[0]?.id || null);
                      }}
                    >
                      <Button danger icon={<DeleteOutlined />}>
                        删除步骤
                      </Button>
                    </Popconfirm>
                  </>
                ) : (
                  <Empty description="选择一个步骤" />
                )}
              </aside>
            </div>

            {suggestedSkillDrafts.length ? (
              <section className={styles.suggestedSkillPanel}>
                <div className={styles.panelHeader}>
                  <strong>从这段协作提炼的 Skill</strong>
                  <Tag color="blue">
                    {draftMeta?.primary_asset === "skill" ||
                    draftMeta?.primary_asset === "both"
                      ? "已默认选中"
                      : "可选"}
                  </Tag>
                </div>
                <p>
                  它吸收了截至所点回复的完整可见上下文，但只保留稳定方法、
                  用户纠正、失败模式和交付标准。取消勾选就不会保存。
                </p>
                <div className={styles.suggestedSkillList}>
                  {suggestedSkillDrafts.map((skill) => (
                    <label
                      key={skill.name}
                      className={styles.suggestedSkillItem}
                    >
                      <Checkbox
                        checked={selectedSuggestedSkills.has(skill.name)}
                        onChange={() => toggleSuggestedSkill(skill.name)}
                      />
                      <span className={styles.suggestedSkillCopy}>
                        <strong>{skill.name}</strong>
                        <em>{skill.description}</em>
                        <small>{skill.reason}</small>
                      </span>
                    </label>
                  ))}
                </div>
              </section>
            ) : null}

            {draftMeta ? (
              <section className={styles.reviewBox}>
                <strong>复核依据</strong>
                <div>
                  {draftMeta.warnings.map((warning) => (
                    <Tag key={warning} color="gold">
                      {warning}
                    </Tag>
                  ))}
                </div>
                <ul>
                  {draftMeta.evidence.map((item) => (
                    <li key={item}>{item}</li>
                  ))}
                </ul>
                {draftMeta.insights?.corrections.length ? (
                  <>
                    <strong>识别到的用户纠正</strong>
                    <ul>
                      {draftMeta.insights.corrections.map((item) => (
                        <li key={item}>{item}</li>
                      ))}
                    </ul>
                  </>
                ) : null}
              </section>
            ) : null}
          </div>
        ) : null}
      </Modal>

      <Modal
        title="Pipeline 可视编辑器"
        open={pipelineEditorOpen}
        width={1180}
        destroyOnClose
        onCancel={() => setPipelineEditorOpen(false)}
        onOk={() => void savePipeline()}
        okText="保存 Pipeline"
        confirmLoading={saving}
        okButtonProps={{ icon: <SaveOutlined /> }}
      >
        {editorPipeline ? (
          <div className={styles.visualEditor}>
            <section className={styles.editorBasics}>
              <div className={styles.formGrid}>
                <label>
                  <span>名称</span>
                  <Input
                    value={editorPipeline.name}
                    onChange={(event) =>
                      setEditorPipeline({
                        ...editorPipeline,
                        name: event.target.value,
                      })
                    }
                  />
                </label>
                <label>
                  <span>并行上限</span>
                  <InputNumber
                    min={1}
                    max={8}
                    value={editorPipeline.runtime.max_parallelism}
                    onChange={(max_parallelism) =>
                      setEditorPipeline({
                        ...editorPipeline,
                        runtime: {
                          ...editorPipeline.runtime,
                          max_parallelism: Number(max_parallelism || 1),
                        },
                      })
                    }
                  />
                </label>
              </div>
              <label className={styles.fullField}>
                <span>描述</span>
                <TextArea
                  rows={2}
                  value={editorPipeline.description}
                  onChange={(event) =>
                    setEditorPipeline({
                      ...editorPipeline,
                      description: event.target.value,
                    })
                  }
                />
              </label>
              <div className={styles.switchLine}>
                <span>启用状态</span>
                <Switch
                  checked={editorPipeline.enabled}
                  checkedChildren="启用"
                  unCheckedChildren="草稿"
                  onChange={(enabled) =>
                    setEditorPipeline({ ...editorPipeline, enabled })
                  }
                />
              </div>
            </section>
            <div className={styles.flowEditorGrid}>
              <aside className={styles.nodeLibrary}>
                <strong>能力节点</strong>
                <Select
                  placeholder="选择能力并添加"
                  options={capabilities.map((item) => ({
                    value: item.id,
                    label: item.name,
                  }))}
                  onChange={(capabilityId) => {
                    const cap = capabilities.find(
                      (item) => item.id === capabilityId,
                    );
                    if (!cap) return;
                    const node: PipelineNode = {
                      id: newId(),
                      index: editorPipeline.nodes.length,
                      title: cap.name,
                      capability_id: cap.id,
                      parameters: Object.fromEntries(
                        cap.parameters.map((param) => [
                          param.name,
                          param.default ?? `{{pipeline.${param.name}}}`,
                        ]),
                      ),
                      position: {
                        x: 40,
                        y: 40 + editorPipeline.nodes.length * 88,
                      },
                    };
                    setEditorPipeline({
                      ...editorPipeline,
                      nodes: [...editorPipeline.nodes, node],
                    });
                    setSelectedPipelineNodeId(node.id);
                  }}
                />
                <strong>连线</strong>
                <Select
                  value={edgeDraft.source || undefined}
                  placeholder="上游"
                  options={editorPipeline.nodes.map((node) => ({
                    value: node.id,
                    label: node.title,
                  }))}
                  onChange={(source) => setEdgeDraft({ ...edgeDraft, source })}
                />
                <Select
                  value={edgeDraft.target || undefined}
                  placeholder="下游"
                  options={editorPipeline.nodes.map((node) => ({
                    value: node.id,
                    label: node.title,
                  }))}
                  onChange={(target) => setEdgeDraft({ ...edgeDraft, target })}
                />
                <Button
                  onClick={() => {
                    if (
                      !edgeDraft.source ||
                      !edgeDraft.target ||
                      edgeDraft.source === edgeDraft.target
                    )
                      return;
                    const edge: PipelineEdge = {
                      id: newId(),
                      source: edgeDraft.source,
                      target: edgeDraft.target,
                    };
                    setEditorPipeline({
                      ...editorPipeline,
                      edges: [...editorPipeline.edges, edge],
                    });
                  }}
                >
                  连接节点
                </Button>
                {editorPipeline.edges.map((edge) => (
                  <Tag
                    key={edge.id}
                    closable
                    onClose={() =>
                      setEditorPipeline({
                        ...editorPipeline,
                        edges: editorPipeline.edges.filter(
                          (item) => item.id !== edge.id,
                        ),
                      })
                    }
                  >
                    {editorPipeline.nodes.find(
                      (node) => node.id === edge.source,
                    )?.title || "上游"}{" "}
                    →{" "}
                    {editorPipeline.nodes.find(
                      (node) => node.id === edge.target,
                    )?.title || "下游"}
                  </Tag>
                ))}
              </aside>
              <FlowCanvas
                items={pipelineItems}
                edges={editorPipeline.edges}
                selectedId={selectedPipelineNodeId}
                onSelect={setSelectedPipelineNodeId}
                onReorder={(from, to) => {
                  const nodes = arrayMove(editorPipeline.nodes, from, to).map(
                    (node, index) => ({ ...node, index }),
                  );
                  setEditorPipeline({ ...editorPipeline, nodes });
                }}
                empty="从左侧添加能力节点"
              />
              <aside className={styles.propertyPanel}>
                <strong>节点属性</strong>
                {selectedPipelineNode ? (
                  <>
                    <label>
                      <span>标题</span>
                      <Input
                        value={selectedPipelineNode.title}
                        onChange={(event) =>
                          setEditorPipeline({
                            ...editorPipeline,
                            nodes: editorPipeline.nodes.map((node) =>
                              node.id === selectedPipelineNode.id
                                ? { ...node, title: event.target.value }
                                : node,
                            ),
                          })
                        }
                      />
                    </label>
                    <label>
                      <span>能力</span>
                      <Select
                        value={selectedPipelineNode.capability_id}
                        options={capabilities.map((item) => ({
                          value: item.id,
                          label: item.name,
                        }))}
                        onChange={(capability_id) =>
                          setEditorPipeline({
                            ...editorPipeline,
                            nodes: editorPipeline.nodes.map((node) =>
                              node.id === selectedPipelineNode.id
                                ? { ...node, capability_id }
                                : node,
                            ),
                          })
                        }
                      />
                    </label>
                    {selectedNodeCapability?.parameters.map((param) => (
                      <label key={param.name}>
                        <span>{param.label || param.name}</span>
                        <Input
                          value={String(
                            selectedPipelineNode.parameters[param.name] ?? "",
                          )}
                          onChange={(event) =>
                            setEditorPipeline({
                              ...editorPipeline,
                              nodes: editorPipeline.nodes.map((node) =>
                                node.id === selectedPipelineNode.id
                                  ? {
                                      ...node,
                                      parameters: {
                                        ...node.parameters,
                                        [param.name]: event.target.value,
                                      },
                                    }
                                  : node,
                              ),
                            })
                          }
                        />
                      </label>
                    ))}
                    <Popconfirm
                      title="删除这个节点？"
                      onConfirm={() => {
                        setEditorPipeline({
                          ...editorPipeline,
                          nodes: editorPipeline.nodes
                            .filter(
                              (node) => node.id !== selectedPipelineNode.id,
                            )
                            .map((node, index) => ({ ...node, index })),
                          edges: editorPipeline.edges.filter(
                            (edge) =>
                              edge.source !== selectedPipelineNode.id &&
                              edge.target !== selectedPipelineNode.id,
                          ),
                        });
                        setSelectedPipelineNodeId(
                          editorPipeline.nodes[0]?.id || null,
                        );
                      }}
                    >
                      <Button danger icon={<DeleteOutlined />}>
                        删除节点
                      </Button>
                    </Popconfirm>
                  </>
                ) : (
                  <Empty description="选择一个节点" />
                )}
              </aside>
            </div>
          </div>
        ) : null}
      </Modal>

      <Modal
        title={runTarget ? `运行能力：${runTarget.name}` : "运行能力"}
        open={Boolean(runTarget)}
        onCancel={() => setRunTarget(null)}
        onOk={() => void runCapability()}
        okText="开始运行"
        confirmLoading={running}
        okButtonProps={{ icon: <PlayCircleOutlined /> }}
      >
        {renderParameterInputs(runTarget)}
      </Modal>

      <Modal
        title={
          pipelineRunTarget
            ? `运行 Pipeline：${pipelineRunTarget.name}`
            : "运行 Pipeline"
        }
        open={Boolean(pipelineRunTarget)}
        onCancel={() => setPipelineRunTarget(null)}
        onOk={() => void runPipeline()}
        okText="开始运行"
        confirmLoading={running}
        okButtonProps={{ icon: <PlayCircleOutlined /> }}
      >
        {renderParameterInputs(pipelineRunTarget)}
      </Modal>

      <Modal
        title={
          scheduleTarget || schedulePipelineTarget
            ? `设为定时运行：${(scheduleTarget || schedulePipelineTarget)?.name}`
            : "设为定时运行"
        }
        open={Boolean(scheduleTarget || schedulePipelineTarget)}
        onCancel={() => {
          setScheduleTarget(null);
          setSchedulePipelineTarget(null);
        }}
        onOk={() => void createScheduledCapability()}
        okText="创建定时任务"
        confirmLoading={scheduleSaving}
        okButtonProps={{ icon: <CalendarOutlined /> }}
      >
        <div className={styles.runForm}>
          <label>
            <span>Cron 表达式</span>
            <Input
              value={scheduleCron}
              onChange={(event) => setScheduleCron(event.target.value)}
            />
          </label>
          <div className={styles.scheduleHint}>
            <Tag>默认每天 09:00</Tag>
            <Tag>参数使用当前默认值</Tag>
            <Tag>含 GUI 的流程会被拒绝定时执行</Tag>
          </div>
        </div>
      </Modal>
    </CapabilityPage>
  );
}

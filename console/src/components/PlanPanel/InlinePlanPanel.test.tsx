import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import InlinePlanPanel from "./InlinePlanPanel";
import type { UsePlanStateResult } from "./usePlanState";

const mockUsePlanState = vi.hoisted(() => vi.fn());

vi.mock("react-i18next", () => ({
  useTranslation: () => ({
    t: (key: string, fallback?: string) =>
      ({
        "plan.executionPlan": "执行计划",
        "plan.completed": "已完成",
        "plan.currentStepPrefix": "正在第",
        "plan.currentStepSuffix": " 步",
        "plan.allStepsCompleted": "全部步骤已完成",
        "plan.planAbandoned": "计划已终止",
        "plan.step": "第",
        "plan.remainingStepsPrefix": "另有 ",
        "plan.remainingStepsSuffix": "步",
        "plan.viewFullPlan": "查看完整计划",
        "plan.adjustPlan": "调整计划",
        "plan.addValidation": "补充验收",
        "plan.validationReady": "计划包含验收步骤",
        "plan.validationMissing": "尚未看到明确的验收步骤",
        "plan.outcomeArrow": "结果",
      })[key] ||
      fallback ||
      key,
  }),
}));

vi.mock("./usePlanState", () => ({
  usePlanState: mockUsePlanState,
}));

vi.mock("./index", () => ({
  default: ({ open }: { open: boolean }) =>
    open ? (
      <div role="dialog" aria-label="完整计划抽屉">
        完整计划抽屉
      </div>
    ) : null,
}));

function planState(
  overrides: Partial<UsePlanStateResult> = {},
): UsePlanStateResult {
  return {
    loading: false,
    doneCount: 1,
    totalCount: 3,
    percent: 33,
    refresh: vi.fn(),
    plan: {
      id: "plan-1",
      name: "算账出方案",
      description: "拆解截图中的商业逻辑",
      expected_outcome: "完整方案",
      state: "in_progress",
      created_at: null,
      finished_at: null,
      outcome: null,
      subtasks: [
        {
          name: "拆解截图中的商业逻辑",
          description: "明确核心假设",
          expected_outcome: "",
          outcome: "核心逻辑已拆解",
          state: "done",
          created_at: null,
          finished_at: null,
        },
        {
          name: "建立成本模型",
          description: "送半年/全年成本与传统稿费对比",
          expected_outcome: "",
          outcome: null,
          state: "in_progress",
          created_at: null,
          finished_at: null,
        },
        {
          name: "输出完整方案文档",
          description: "",
          expected_outcome: "",
          outcome: null,
          state: "todo",
          created_at: null,
          finished_at: null,
        },
      ],
    },
    ...overrides,
  };
}

describe("InlinePlanPanel", () => {
  beforeEach(() => {
    mockUsePlanState.mockReturnValue(planState());
  });

  it("does not render when there is no active plan", () => {
    mockUsePlanState.mockReturnValue(
      planState({
        plan: null,
        doneCount: 0,
        totalCount: 0,
        percent: 0,
      }),
    );

    const { container } = render(<InlinePlanPanel active />);

    expect(container).toBeEmptyDOMElement();
  });

  it("does not present a plan-fetch request as an active goal", () => {
    mockUsePlanState.mockReturnValue(
      planState({
        plan: null,
        loading: true,
        doneCount: 0,
        totalCount: 0,
        percent: 0,
      }),
    );

    const { container } = render(<InlinePlanPanel active />);

    expect(container).toBeEmptyDOMElement();
    expect(screen.queryByText("进行中的目标")).not.toBeInTheDocument();
  });

  it("states completed work and the current step without conflating them", () => {
    render(<InlinePlanPanel active />);

    const toggle = screen.getByRole("button", { name: /执行计划/ });
    expect(toggle).toHaveTextContent("已完成 1/3");
    expect(toggle).toHaveTextContent("正在第 2 步");
    expect(toggle).toHaveTextContent("建立成本模型");
    expect(screen.queryByText("输出完整方案文档")).not.toBeInTheDocument();
  });

  it("does not present an abandoned plan as actively running a step", () => {
    mockUsePlanState.mockReturnValue(
      planState({
        plan: {
          ...planState().plan!,
          state: "abandoned",
          subtasks: planState().plan!.subtasks.map((task) => ({
            ...task,
            state: task.state === "done" ? "done" : "abandoned",
          })),
        },
      }),
    );

    render(<InlinePlanPanel active />);

    const toggle = screen.getByRole("button", { name: /执行计划/ });
    expect(toggle).toHaveTextContent("计划已终止");
    expect(toggle).not.toHaveTextContent("正在第");
  });

  it("keeps the expanded preview lightweight and opens the complete plan on demand", () => {
    mockUsePlanState.mockReturnValue(
      planState({
        doneCount: 2,
        totalCount: 6,
        percent: 33,
        plan: {
          ...planState().plan!,
          subtasks: [
            {
              ...planState().plan!.subtasks[0],
              name: "整理素材",
            },
            {
              ...planState().plan!.subtasks[0],
              name: "核对口径",
            },
            {
              ...planState().plan!.subtasks[1],
              name: "建立成本模型",
            },
            {
              ...planState().plan!.subtasks[2],
              name: "形成初稿",
            },
            {
              ...planState().plan!.subtasks[2],
              name: "复核数据",
            },
            {
              ...planState().plan!.subtasks[2],
              name: "交付报告",
            },
          ],
        },
      }),
    );

    render(<InlinePlanPanel active />);

    fireEvent.click(screen.getByRole("button", { name: /执行计划/ }));

    expect(screen.queryByText("整理素材")).not.toBeInTheDocument();
    expect(screen.getByText("核对口径")).toBeInTheDocument();
    expect(screen.getAllByText("建立成本模型").length).toBeGreaterThanOrEqual(
      2,
    );
    expect(screen.getByText("形成初稿")).toBeInTheDocument();
    expect(screen.queryByText("交付报告")).not.toBeInTheDocument();
    expect(screen.getByText("另有 3 步")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "查看完整计划" }));
    expect(
      screen.getByRole("dialog", { name: "完整计划抽屉" }),
    ).toBeInTheDocument();
  });

  it("retains the plan adjustment and validation affordances", () => {
    const onRequestReplan = vi.fn();
    const onRequestValidation = vi.fn();

    render(
      <InlinePlanPanel
        active
        onRequestReplan={onRequestReplan}
        onRequestValidation={onRequestValidation}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: /执行计划/ }));

    fireEvent.click(screen.getByRole("button", { name: "调整计划" }));
    fireEvent.click(screen.getByRole("button", { name: "补充验收" }));
    expect(onRequestReplan).toHaveBeenCalledTimes(1);
    expect(onRequestValidation).toHaveBeenCalledTimes(1);
  });
});

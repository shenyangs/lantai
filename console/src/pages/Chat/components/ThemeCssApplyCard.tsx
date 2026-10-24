import { Button } from "antd";
import { Check, Palette } from "lucide-react";
import { useMemo, useState } from "react";
import {
  customizationApi,
  notifyCustomizationChanged,
} from "../../../api/modules/customization";
import { useAppMessage } from "../../../hooks/useAppMessage";
import type { ThemeCssCandidate } from "../themeCss";
import styles from "./ThemeCssApplyCard.module.less";

interface ThemeCssApplyCardProps {
  candidate: ThemeCssCandidate;
}

export default function ThemeCssApplyCard({
  candidate,
}: ThemeCssApplyCardProps) {
  const { message } = useAppMessage();
  const [state, setState] = useState<"idle" | "applying" | "done">("idle");
  const variableSummary = useMemo(() => {
    const preview = candidate.variables.slice(0, 4).join(", ");
    const rest = candidate.variables.length - 4;
    return rest > 0
      ? `${preview} 等 ${candidate.variables.length} 个变量`
      : preview;
  }, [candidate.variables]);

  const applyTheme = async () => {
    if (state !== "idle") return;
    setState("applying");
    try {
      const tokens = Object.fromEntries(
        Array.from(
          candidate.css.matchAll(/(--lt-[\w-]+)\s*:\s*([^;{}]+);/g),
        ).map((match) => [match[1], match[2].trim()]),
      );
      const [customizationState, profiles] = await Promise.all([
        customizationApi.getState(),
        customizationApi.listProfiles(),
      ]);
      let profile = profiles.find(
        (item) => item.id === customizationState.active_profile_id,
      );
      if (!profile) {
        profile = await customizationApi.createProfile({ name: "对话主题" });
      }
      const preview = await customizationApi.previewPlan({
        target_profile_id: profile.id,
        base_revision: profile.revision,
        intent: "应用主对话生成的设计令牌",
        operations: [
          { op: "set", path: "appearance.design_tokens", value: tokens },
          { op: "set", path: "appearance.palette", value: "custom" },
        ],
      });
      const result = await customizationApi.applyPlan(preview.plan.id!);
      if (result.status !== "committed") {
        throw new Error("主题健康检查失败，已回滚");
      }
      await customizationApi.activateProfile(profile.id);
      notifyCustomizationChanged();
      setState("done");
      message.success("已通过版本化配置事务应用主题");
    } catch (cause) {
      setState("idle");
      message.error(cause instanceof Error ? cause.message : "应用主题失败");
    }
  };

  return (
    <div className={styles.card} data-theme-css-apply-card="1">
      <span className={styles.icon} aria-hidden="true">
        <Palette size={16} strokeWidth={2} />
      </span>
      <div className={styles.body}>
        <strong>检测到可应用的主题 CSS</strong>
        <p title={variableSummary}>{variableSummary}</p>
      </div>
      <Button
        className={styles.action}
        disabled={state !== "idle"}
        icon={<Check size={14} strokeWidth={2} />}
        onClick={() => void applyTheme()}
        size="small"
        type="primary"
      >
        {state === "done" ? "已应用" : state === "applying" ? "应用中" : "应用主题"}
      </Button>
    </div>
  );
}

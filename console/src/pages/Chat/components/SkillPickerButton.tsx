import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Button, Empty, Input, Popover, Spin, Tag, Tooltip } from "antd";
import { SettingOutlined } from "@ant-design/icons";
import { IconButton } from "@/vendor/lantaiDesign";
import { Puzzle } from "lucide-react";
import { useTranslation } from "react-i18next";
import { useNavigate } from "react-router-dom";
import { skillApi } from "@/api/modules/skill";
import type { SkillIndexEntry } from "@/api/types";
import { getLocalizedSkillDescription } from "@/utils/displayDescriptions";
import { isSkillBuiltin } from "@/utils/skill";
import { SkillVisual } from "@/components/SkillVisual";
import { createLatestRequestGate } from "@/utils/latestRequest";
import styles from "./SkillPickerButton.module.less";

interface SkillPickerButtonProps {
  selectedAgent: string;
  selectedSkillName: string | null;
  onPickSkill: (skillName: string) => void;
  onClearSkill: () => void;
}

function skillSupportsConsole(skill: SkillIndexEntry): boolean {
  const channels = skill.channels?.length ? skill.channels : ["all"];
  return channels.includes("all") || channels.includes("console");
}

function getFallbackLabels(language?: string) {
  const isZh = (language || "").toLowerCase().startsWith("zh");
  return {
    tooltip: isZh ? "选择技能" : "Select skill",
    selectedTooltip: isZh ? "已选择技能：{{name}}" : "Selected skill: {{name}}",
    title: isZh ? "选择技能" : "Select skill",
    searchPlaceholder: isZh ? "搜索技能..." : "Search skills...",
    empty: isZh ? "没有已启用的聊天技能" : "No enabled chat skills",
    manage: isZh ? "管理技能" : "Manage skills",
    selected: isZh ? "本轮使用" : "Using",
    builtin: isZh ? "内置" : "Built-in",
    custom: isZh ? "自定义" : "Custom",
  };
}

export default function SkillPickerButton({
  selectedAgent,
  selectedSkillName,
  onPickSkill,
  onClearSkill,
}: SkillPickerButtonProps) {
  const { t, i18n } = useTranslation();
  const navigate = useNavigate();
  const labels = getFallbackLabels(i18n.resolvedLanguage || i18n.language);
  const [open, setOpen] = useState(false);
  const [search, setSearch] = useState("");
  const [skills, setSkills] = useState<SkillIndexEntry[]>([]);
  const [loading, setLoading] = useState(false);
  const [loadFailed, setLoadFailed] = useState(false);
  const loadGateRef = useRef(createLatestRequestGate());

  const loadSkills = useCallback(async () => {
    const ownsState = loadGateRef.current.begin();
    setLoading(true);
    setLoadFailed(false);
    try {
      const data = await skillApi.listSkillIndex(selectedAgent);
      if (ownsState()) setSkills(Array.isArray(data) ? data : []);
    } catch (error) {
      if (!ownsState()) return;
      console.error("Failed to load chat skills:", error);
      setLoadFailed(true);
      setSkills([]);
    } finally {
      if (ownsState()) setLoading(false);
    }
  }, [selectedAgent]);

  useEffect(() => {
    if (open) {
      void loadSkills();
    }
    return () => loadGateRef.current.invalidate();
  }, [loadSkills, open]);

  useEffect(() => {
    loadGateRef.current.invalidate();
    setOpen(false);
    setSearch("");
    setSkills([]);
  }, [selectedAgent]);

  const availableSkills = useMemo(
    () =>
      skills.filter(
        (skill) => skill.enabled !== false && skillSupportsConsole(skill),
      ),
    [skills],
  );

  const visibleSkills = useMemo(() => {
    const query = search.trim().toLowerCase();
    if (!query) return availableSkills;

    return availableSkills.filter((skill) => {
      const description = getLocalizedSkillDescription(skill, t);
      const haystack = [skill.name, description, ...(skill.tags ?? [])]
        .join(" ")
        .toLowerCase();
      return haystack.includes(query);
    });
  }, [availableSkills, search, t]);

  const handlePick = useCallback(
    (skill: SkillIndexEntry) => {
      onPickSkill(skill.name);
      setOpen(false);
      setSearch("");
    },
    [onPickSkill],
  );

  const handleManage = useCallback(() => {
    setOpen(false);
    navigate("/skills");
  }, [navigate]);

  const tooltipTitle = selectedSkillName
    ? t("chat.skillPicker.selectedTooltip", {
        defaultValue: labels.selectedTooltip,
        name: selectedSkillName,
      })
    : t("chat.skillPicker.tooltip", { defaultValue: labels.tooltip });

  const content = (
    <div className={styles.popover}>
      <div className={styles.header}>
        <span className={styles.title}>
          {t("chat.skillPicker.title", { defaultValue: labels.title })}
        </span>
        <span className={styles.count}>{availableSkills.length}</span>
      </div>
      <Input
        className={styles.search}
        allowClear
        size="middle"
        value={search}
        placeholder={t("chat.skillPicker.searchPlaceholder", {
          defaultValue: labels.searchPlaceholder,
        })}
        onChange={(event) => setSearch(event.target.value)}
      />
      <div className={styles.body}>
        {loading ? (
          <div className={styles.loading}>
            <Spin />
          </div>
        ) : visibleSkills.length > 0 ? (
          <div className={styles.list}>
            {visibleSkills.map((skill) => {
              const isActive = skill.name === selectedSkillName;
              const isBuiltin = isSkillBuiltin(skill.source);
              return (
                <button
                  key={skill.name}
                  type="button"
                  className={`${styles.item} ${
                    isActive ? styles.itemActive : ""
                  }`}
                  onClick={() => handlePick(skill)}
                >
                  <span className={styles.icon}>
                    <SkillVisual name={skill.name} emoji={skill.emoji} />
                  </span>
                  <span className={styles.info}>
                    <span className={styles.nameRow}>
                      <span className={styles.name}>{skill.name}</span>
                      <Tag
                        className={styles.tag}
                        color={isBuiltin ? "blue" : "default"}
                      >
                        {isBuiltin
                          ? t("skills.builtin", {
                              defaultValue: labels.builtin,
                            })
                          : t("skills.custom", {
                              defaultValue: labels.custom,
                            })}
                      </Tag>
                    </span>
                    <span className={styles.description}>
                      {getLocalizedSkillDescription(skill, t)}
                    </span>
                  </span>
                </button>
              );
            })}
          </div>
        ) : (
          <Empty
            image={Empty.PRESENTED_IMAGE_SIMPLE}
            description={
              loadFailed
                ? t("skills.loadFailed", { defaultValue: labels.empty })
                : t("chat.skillPicker.empty", { defaultValue: labels.empty })
            }
          />
        )}
      </div>
      <div className={styles.footer}>
        <span className={styles.selected}>
          {selectedSkillName ? (
            <>
              {t("chat.skillPicker.selected", {
                defaultValue: labels.selected,
              })}
              : <span className={styles.selectedName}>{selectedSkillName}</span>
              <Button type="link" size="small" onClick={onClearSkill}>
                {t("common.clear")}
              </Button>
            </>
          ) : null}
        </span>
        <Button
          type="text"
          size="small"
          icon={<SettingOutlined />}
          onClick={handleManage}
        >
          {t("chat.skillPicker.manage", { defaultValue: labels.manage })}
        </Button>
      </div>
    </div>
  );

  return (
    <Popover
      open={open}
      placement="topLeft"
      trigger="click"
      content={content}
      onOpenChange={setOpen}
    >
      <Tooltip title={tooltipTitle} mouseEnterDelay={0.5}>
        <IconButton
          aria-label={tooltipTitle}
          bordered={false}
          icon={<Puzzle size={16} strokeWidth={2} />}
          data-chat-skill-trigger="1"
          className={`${styles.skillButton} ${
            selectedSkillName ? styles.skillButtonActive : ""
          }`}
        />
      </Tooltip>
    </Popover>
  );
}

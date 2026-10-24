import { Button, Checkbox, Switch } from "@/vendor/lantaiDesign";
import { useTranslation } from "react-i18next";
import type { SkillSpec } from "../../../../api/types";
import { isSkillBuiltin } from "@/utils/skill";
import { getLocalizedSkillDescription } from "@/utils/displayDescriptions";
import { getSkillVisual } from "./SkillCard";
import { getSkillDisplayName, getSkillSlugLabel } from "../skillPresentation";
import dayjs from "dayjs";
import relativeTime from "dayjs/plugin/relativeTime";
import styles from "../index.module.less";

dayjs.extend(relativeTime);

interface SkillListItemProps {
  skill: SkillSpec;
  batchModeEnabled: boolean;
  isSelected: boolean;
  onSelect: () => void;
  onClick: () => void;
  onToggleEnabled: () => Promise<void>;
  onDelete: () => void;
}

export function SkillListItem({
  skill,
  batchModeEnabled,
  isSelected,
  onSelect,
  onClick,
  onToggleEnabled,
  onDelete,
}: SkillListItemProps) {
  const { t } = useTranslation();
  const isBuiltin = isSkillBuiltin(skill.source);
  const channels = (skill.channels || ["all"])
    .map((ch) => (ch === "all" ? t("skills.allChannels") : ch))
    .join(", ");
  const description = getLocalizedSkillDescription(skill, t);
  const displayName = getSkillDisplayName(skill.name, description);

  return (
    <div
      className={`${styles.skillListItem} ${
        isSelected ? styles.selectedListItem : ""
      }`}
      role="button"
      tabIndex={0}
      aria-pressed={batchModeEnabled ? isSelected : undefined}
      onClick={() => {
        if (batchModeEnabled) onSelect();
        else onClick();
      }}
      onKeyDown={(event) => {
        if (event.currentTarget !== event.target) return;
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          if (batchModeEnabled) onSelect();
          else onClick();
        }
      }}
    >
      {batchModeEnabled && (
        <Checkbox
          checked={isSelected}
          onClick={(e) => {
            e.stopPropagation();
            onSelect();
          }}
        />
      )}
      <div className={styles.listItemLeft}>
        <span className={styles.fileIcon}>{getSkillVisual(skill.name)}</span>
        <div className={styles.listItemInfo}>
          <div className={styles.listItemHeader}>
            <span
              className={styles.skillTitle}
              title={getSkillSlugLabel(skill.name)}
            >
              {displayName}
            </span>
            <span className={styles.skillSlug}>{skill.name}</span>
            <span className={styles.typeBadge}>
              {isBuiltin ? t("skills.builtin") : t("skills.custom")}
            </span>
            <span className={styles.channelBadge}>{channels}</span>
            {skill.last_updated && (
              <span className={styles.listItemTime}>
                {t("skills.lastUpdated")} {dayjs(skill.last_updated).fromNow()}
              </span>
            )}
          </div>
          <p className={styles.listItemDesc}>{description}</p>
          {!!skill.tags?.length && (
            <div className={styles.listItemTags}>
              {skill.tags.map((tag) => (
                <span key={tag} className={styles.tagChip}>
                  {tag}
                </span>
              ))}
            </div>
          )}
        </div>
      </div>
      <div className={styles.listItemRight}>
        <span data-event-boundary onClick={(e) => e.stopPropagation()}>
          <Switch
            checked={skill.enabled}
            disabled={batchModeEnabled}
            onChange={onToggleEnabled}
            aria-label={`${
              skill.enabled ? t("common.disable") : t("common.enable")
            } ${displayName}`}
          />
        </span>
        <Button
          danger
          disabled={batchModeEnabled}
          onClick={(e) => {
            e.stopPropagation();
            onDelete();
          }}
        >
          {t("common.delete")}
        </Button>
      </div>
    </div>
  );
}

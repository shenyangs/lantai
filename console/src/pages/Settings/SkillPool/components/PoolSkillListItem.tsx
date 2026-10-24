import { Button, Checkbox } from "@/vendor/lantaiDesign";
import { useTranslation } from "react-i18next";
import type { PoolSkillSpec } from "../../../../api/types";
import {
  getPoolBuiltinStatusLabel,
  getPoolBuiltinStatusTone,
  isSkillBuiltin,
} from "@/utils/skill";
import { getLocalizedSkillDescription } from "@/utils/displayDescriptions";
import { getSkillVisual } from "../../../Agent/Skills/components";
import {
  getSkillDisplayName,
  getSkillSlugLabel,
} from "../../../Agent/Skills/skillPresentation";
import { SkillTagChips } from "./SkillMeta";
import styles from "../index.module.less";

interface PoolSkillListItemProps {
  skill: PoolSkillSpec;
  isSelected: boolean;
  batchModeEnabled: boolean;
  onToggleSelect: (name: string) => void;
  onEdit: (skill: PoolSkillSpec) => void;
  onBroadcast: (skill: PoolSkillSpec) => void;
  onDelete: (skill: PoolSkillSpec) => void;
}

export function PoolSkillListItem({
  skill,
  isSelected,
  batchModeEnabled,
  onToggleSelect,
  onEdit,
  onBroadcast,
  onDelete,
}: PoolSkillListItemProps) {
  const { t } = useTranslation();
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
        if (batchModeEnabled) {
          onToggleSelect(skill.name);
        } else {
          onEdit(skill);
        }
      }}
      onKeyDown={(event) => {
        if (event.currentTarget !== event.target) return;
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          if (batchModeEnabled) onToggleSelect(skill.name);
          else onEdit(skill);
        }
      }}
    >
      {batchModeEnabled && (
        <Checkbox
          checked={isSelected}
          onClick={(e) => {
            e.stopPropagation();
            onToggleSelect(skill.name);
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
            {isSkillBuiltin(skill.source) && (
              <span className={styles.typeBadge}>{t("skillPool.builtin")}</span>
            )}
            <span
              className={`${styles.statusValue} ${
                styles[getPoolBuiltinStatusTone(skill.sync_status)]
              }`}
            >
              {getPoolBuiltinStatusLabel(skill.sync_status, t)}
            </span>
          </div>
          <p className={styles.listItemDesc}>{description}</p>
          <SkillTagChips tags={skill.tags} />
        </div>
      </div>
      <div className={styles.listItemRight}>
        <Button
          className={styles.actionButton}
          disabled={batchModeEnabled}
          onClick={(e) => {
            e.stopPropagation();
            onBroadcast(skill);
          }}
        >
          {t("skillPool.broadcast")}
        </Button>
        <Button
          danger
          className={styles.deleteButton}
          disabled={batchModeEnabled}
          onClick={(e) => {
            e.stopPropagation();
            void onDelete(skill);
          }}
        >
          {t("skillPool.delete")}
        </Button>
      </div>
    </div>
  );
}

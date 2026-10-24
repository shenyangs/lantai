import { Button, Checkbox } from "@/vendor/lantaiDesign";
import { useTranslation } from "react-i18next";
import type { PoolSkillSpec } from "../../../../api/types";
import {
  getPoolBuiltinStatusLabel,
  getPoolBuiltinStatusTone,
} from "@/utils/skill";
import { getLocalizedSkillDescription } from "@/utils/displayDescriptions";
import { getSkillVisual } from "../../../Agent/Skills/components";
import styles from "../index.module.less";

interface SkillPoolListItemProps {
  skill: PoolSkillSpec;
  isSelected: boolean;
  batchMode: boolean;
  onToggleSelect: (name: string) => void;
  onEdit: (skill: PoolSkillSpec) => void;
  onBroadcast: (skill: PoolSkillSpec) => void;
  onDelete: (skill: PoolSkillSpec) => void;
}

export function SkillPoolListItem({
  skill,
  isSelected,
  batchMode,
  onToggleSelect,
  onEdit,
  onBroadcast,
  onDelete,
}: SkillPoolListItemProps) {
  const { t } = useTranslation();
  const description = getLocalizedSkillDescription(skill, t);

  const handleClick = () => {
    if (batchMode) {
      onToggleSelect(skill.name);
    } else {
      onEdit(skill);
    }
  };

  const handleSelectClick = (e: React.MouseEvent) => {
    e.stopPropagation();
    onToggleSelect(skill.name);
  };

  return (
    <div
      className={`${styles.skillListItem} ${
        isSelected ? styles.selectedListItem : ""
      }`}
      role="button"
      tabIndex={0}
      aria-pressed={batchMode ? isSelected : undefined}
      onClick={handleClick}
      onKeyDown={(event) => {
        if (event.currentTarget !== event.target) return;
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          handleClick();
        }
      }}
    >
      {batchMode && (
        <Checkbox checked={isSelected} onClick={handleSelectClick} />
      )}
      <div className={styles.listItemLeft}>
        <span className={styles.fileIcon}>{getSkillVisual(skill.name)}</span>
        <div className={styles.listItemInfo}>
          <div className={styles.listItemHeader}>
            <span className={styles.skillTitle}>{skill.name}</span>
            <span
              className={`${styles.statusValue} ${
                styles[getPoolBuiltinStatusTone(skill.sync_status)]
              }`}
            >
              {getPoolBuiltinStatusLabel(skill.sync_status, t)}
            </span>
          </div>
          <p className={styles.listItemDesc}>{description}</p>
        </div>
      </div>
      <div className={styles.listItemRight}>
        <Button
          className={styles.actionButton}
          disabled={batchMode}
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
          disabled={batchMode}
          onClick={(e) => {
            e.stopPropagation();
            onDelete(skill);
          }}
        >
          {t("skillPool.delete")}
        </Button>
      </div>
    </div>
  );
}

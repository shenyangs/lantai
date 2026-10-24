import { useMemo, useState } from "react";
import { Popover } from "antd";
import {
  Check,
  ChevronDown,
  Settings2,
  ShieldAlert,
  ShieldCheck,
  Zap,
} from "lucide-react";
import type { ChatPermissionPreset } from "../permissionMode";
import styles from "./PermissionModeSelector.module.less";

interface PermissionOption {
  id: ChatPermissionPreset;
  label: string;
  shortLabel: string;
  description: string;
  icon: typeof ShieldCheck;
}

const OPTIONS: PermissionOption[] = [
  {
    id: "request",
    label: "请求权限",
    shortLabel: "请求权限",
    description: "当前对话中，所有可能产生影响的操作都先问你",
    icon: ShieldCheck,
  },
  {
    id: "risk",
    label: "仅风险操作询问",
    shortLabel: "风险询问",
    description: "当前对话中，常规操作直接执行；有风险或超出工作区时再问你",
    icon: ShieldAlert,
  },
  {
    id: "full",
    label: "完全访问",
    shortLabel: "完全访问",
    description: "当前对话中尽量直接执行；系统硬性限制仍不会放行",
    icon: Zap,
  },
];

export interface PermissionModeSelectorProps {
  value: ChatPermissionPreset;
  allowFullAccess?: boolean;
  onChange: (value: ChatPermissionPreset) => void;
  onOpenCustom: () => void;
}

export default function PermissionModeSelector({
  value,
  allowFullAccess = true,
  onChange,
  onOpenCustom,
}: PermissionModeSelectorProps) {
  const [open, setOpen] = useState(false);
  const options = useMemo(
    () => OPTIONS.filter((option) => allowFullAccess || option.id !== "full"),
    [allowFullAccess],
  );
  const selected =
    options.find((option) => option.id === value) ??
    options.find((option) => option.id === "risk") ??
    options[0];
  const TriggerIcon = selected?.icon ?? ShieldAlert;

  const content = (
    <div className={styles.menu} role="menu" aria-label="权限模式">
      <div className={styles.scopeNotice} role="note">
        <span className={styles.scopeBadge}>只影响当前对话</span>
        <span>
          其他对话和后台任务的默认权限不会改变，系统硬性限制始终有效。
        </span>
      </div>
      {options.map((option) => {
        const Icon = option.icon;
        const isSelected = option.id === selected?.id;
        return (
          <button
            key={option.id}
            type="button"
            role="menuitemradio"
            aria-checked={isSelected}
            className={`${styles.option} ${
              isSelected ? styles.optionSelected : ""
            }`}
            onClick={() => {
              onChange(option.id);
              setOpen(false);
            }}
          >
            <span className={styles.optionIcon}>
              <Icon size={18} strokeWidth={1.9} />
            </span>
            <span className={styles.optionCopy}>
              <span className={styles.optionLabel}>{option.label}</span>
              <span className={styles.optionDescription}>
                {option.description}
              </span>
            </span>
            <span className={styles.checkSlot}>
              {isSelected ? <Check size={17} strokeWidth={2.1} /> : null}
            </span>
          </button>
        );
      })}
      <div className={styles.divider} />
      <button
        type="button"
        role="menuitem"
        className={styles.option}
        onClick={() => {
          setOpen(false);
          onOpenCustom();
        }}
      >
        <span className={styles.optionIcon}>
          <Settings2 size={18} strokeWidth={1.9} />
        </span>
        <span className={styles.optionCopy}>
          <span className={styles.optionLabel}>设置后台任务的默认权限</span>
          <span className={styles.optionDescription}>
            管理未单独选择权限的场景、备用文件夹和更细的规则
          </span>
        </span>
        <span className={styles.checkSlot} />
      </button>
    </div>
  );

  return (
    <Popover
      content={content}
      trigger="click"
      placement="topLeft"
      arrow={false}
      open={open}
      onOpenChange={setOpen}
      rootClassName={styles.popover}
    >
      <button
        type="button"
        data-chat-permission-trigger="1"
        className={styles.trigger}
        aria-label={`权限模式：${selected?.label ?? "仅风险操作询问"}`}
        aria-haspopup="menu"
        aria-expanded={open}
      >
        <TriggerIcon size={16} strokeWidth={1.9} />
        <span>{selected?.shortLabel ?? "风险询问"}</span>
        <ChevronDown size={13} strokeWidth={1.9} />
      </button>
    </Popover>
  );
}

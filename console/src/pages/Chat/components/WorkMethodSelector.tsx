import { useMemo, useState } from "react";
import { Popover } from "antd";
import {
  Check,
  ChevronDown,
  GitBranch,
  Microscope,
  Repeat2,
  Route,
} from "lucide-react";

import type { WorkMethod } from "../../../api/types/workMethod";
import styles from "./WorkMethodSelector.module.less";

const ICONS = {
  standard: Route,
  deep_research: Microscope,
  sustained_execution: Repeat2,
  collaboration: GitBranch,
} as const;

export interface WorkMethodSelectorProps {
  methods: WorkMethod[];
  value: string;
  onChange: (methodId: string) => void;
}

export default function WorkMethodSelector({
  methods,
  value,
  onChange,
}: WorkMethodSelectorProps) {
  const [open, setOpen] = useState(false);
  const selected = useMemo(
    () => methods.find((method) => method.id === value) ?? methods[0],
    [methods, value],
  );
  const TriggerIcon = ICONS[selected?.id as keyof typeof ICONS] ?? GitBranch;

  const content = (
    <div className={styles.menu} role="menu" aria-label="工作法">
      <div className={styles.menuHeader}>
        <span>工作法</span>
        <span>按任务形态选择推进方式，不会改变权限</span>
      </div>
      {methods.map((method) => {
        const Icon = ICONS[method.id as keyof typeof ICONS] ?? GitBranch;
        const isSelected = method.id === selected?.id;
        return (
          <button
            key={method.id}
            type="button"
            role="menuitemradio"
            aria-checked={isSelected}
            className={`${styles.option} ${
              isSelected ? styles.optionSelected : ""
            }`}
            onClick={() => {
              onChange(method.id);
              setOpen(false);
            }}
          >
            <span className={styles.optionIcon}>
              <Icon size={18} strokeWidth={1.9} />
            </span>
            <span className={styles.optionCopy}>
              <span className={styles.optionLabel}>
                {method.name}
                {!method.built_in ? (
                  <span className={styles.customBadge}>自定义</span>
                ) : null}
              </span>
              <span className={styles.optionDescription}>{method.summary}</span>
            </span>
            <span className={styles.checkSlot}>
              {isSelected ? <Check size={17} strokeWidth={2.1} /> : null}
            </span>
          </button>
        );
      })}
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
        data-chat-work-method-trigger="1"
        className={styles.trigger}
        aria-label={`工作法：${selected?.name ?? "标准"}`}
        aria-haspopup="menu"
        aria-expanded={open}
      >
        <TriggerIcon size={16} strokeWidth={1.9} />
        <span>{selected?.name ?? "标准"}</span>
        <ChevronDown size={13} strokeWidth={1.9} />
      </button>
    </Popover>
  );
}

import type { ReactNode } from "react";
import { BarChartOutlined } from "@ant-design/icons";
import styles from "./index.module.less";

interface SettingsEmptyStateProps {
  title: ReactNode;
  description?: ReactNode;
  icon?: ReactNode;
  className?: string;
}

export function SettingsEmptyState({
  title,
  description,
  icon = <BarChartOutlined />,
  className,
}: SettingsEmptyStateProps) {
  return (
    <section
      className={[styles.emptyState, className].filter(Boolean).join(" ")}
      role="status"
    >
      <span className={styles.icon} aria-hidden="true">
        {icon}
      </span>
      <div>
        <strong>{title}</strong>
        {description ? <p>{description}</p> : null}
      </div>
    </section>
  );
}

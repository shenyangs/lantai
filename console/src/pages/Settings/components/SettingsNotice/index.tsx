import type { ReactNode } from "react";
import {
  CheckCircleOutlined,
  CloseCircleOutlined,
  InfoCircleOutlined,
  WarningOutlined,
} from "@ant-design/icons";
import styles from "./index.module.less";

export type SettingsNoticeTone = "info" | "success" | "warning" | "error";

interface SettingsNoticeProps {
  title: ReactNode;
  children?: ReactNode;
  tone?: SettingsNoticeTone;
  className?: string;
}

const icons: Record<SettingsNoticeTone, ReactNode> = {
  info: <InfoCircleOutlined />,
  success: <CheckCircleOutlined />,
  warning: <WarningOutlined />,
  error: <CloseCircleOutlined />,
};

export function SettingsNotice({
  title,
  children,
  tone = "info",
  className,
}: SettingsNoticeProps) {
  return (
    <aside
      className={[styles.notice, styles[tone], className]
        .filter(Boolean)
        .join(" ")}
      role={tone === "error" ? "alert" : "note"}
    >
      <span className={styles.icon} aria-hidden="true">
        {icons[tone]}
      </span>
      <div>
        <strong>{title}</strong>
        {children ? <div className={styles.description}>{children}</div> : null}
      </div>
    </aside>
  );
}

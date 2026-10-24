import type { ReactNode } from "react";
import styles from "./index.module.less";

type AppStateTone = "loading" | "empty" | "error";

interface AppStateProps {
  tone?: AppStateTone;
  title: ReactNode;
  description?: ReactNode;
  actions?: ReactNode;
  compact?: boolean;
}

export function AppState({
  tone = "empty",
  title,
  description,
  actions,
  compact = false,
}: AppStateProps) {
  return (
    <section
      className={`${styles.state} ${compact ? styles.compact : ""}`.trim()}
      data-tone={tone}
      role={tone === "error" ? "alert" : "status"}
      aria-live={tone === "error" ? "assertive" : "polite"}
      aria-busy={tone === "loading" || undefined}
    >
      <span className={styles.mark} aria-hidden="true">
        {tone === "loading" ? (
          <span className={styles.progress} />
        ) : tone === "error" ? (
          "!"
        ) : (
          "·"
        )}
      </span>
      <div className={styles.copy}>
        <strong>{title}</strong>
        {description ? <p>{description}</p> : null}
      </div>
      {actions ? <div className={styles.actions}>{actions}</div> : null}
    </section>
  );
}

export default AppState;

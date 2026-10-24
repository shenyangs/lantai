import type { ReactNode } from "react";
import { useLocation } from "react-router-dom";
import { getDetailNavigation } from "../../navigation/detailNavigation";
import { DetailBackButton } from "../DetailBackButton";
import styles from "./index.module.less";

/**
 * Shared semantic colour vocabulary for capability pages.
 *
 * Keep meaning stable across pages: success is ready/enabled/completed,
 * warning needs attention, danger is failed/blocked, and muted is inactive.
 */
export type CapabilityTone =
  | "default"
  | "info"
  | "success"
  | "warning"
  | "danger"
  | "muted";

interface CapabilityStat {
  label: ReactNode;
  value: ReactNode;
  tone?: CapabilityTone;
}

interface CapabilityPageProps {
  title: ReactNode;
  section?: ReactNode;
  description?: ReactNode;
  actions?: ReactNode;
  stats?: CapabilityStat[];
  toolbar?: ReactNode;
  children: ReactNode;
  className?: string;
  compact?: boolean;
}

interface CapabilityCardBaseProps {
  icon: ReactNode;
  title: ReactNode;
  subtitle?: ReactNode;
  status?: ReactNode;
  statusTone?: CapabilityTone;
  description?: ReactNode;
  chips?: ReactNode;
  selected?: boolean;
  muted?: boolean;
  className?: string;
}

/**
 * A whole card may be interactive only when it has one unambiguous action.
 * Cards with local controls use an explicit footer and remain passive regions.
 */
type CapabilityCardProps = CapabilityCardBaseProps &
  (
    | {
        footer?: ReactNode;
        onClick?: never;
        interactiveLabel?: never;
      }
    | {
        footer?: never;
        onClick: () => void;
        interactiveLabel: string;
      }
  );

interface CapabilityChipProps {
  children: ReactNode;
  tone?: CapabilityTone;
  title?: string;
  className?: string;
}

export function CapabilityChip({
  children,
  tone = "default",
  title,
  className,
}: CapabilityChipProps) {
  return (
    <span
      className={`${styles.chip} ${className ?? ""}`.trim()}
      data-tone={tone}
      title={title}
    >
      {children}
    </span>
  );
}

export function CapabilityPage({
  title,
  section = "能力",
  description,
  actions,
  stats = [],
  toolbar,
  children,
  className,
  compact = false,
}: CapabilityPageProps) {
  const location = useLocation();
  const detailNavigation = getDetailNavigation(location.pathname);

  return (
    <main
      className={`${styles.page} ${compact ? styles.pageCompact : ""} ${
        className ?? ""
      }`.trim()}
    >
      <section className={styles.header}>
        <div className={styles.headerMain}>
          <div className={styles.headingGroup}>
            {detailNavigation ? (
              <DetailBackButton {...detailNavigation} />
            ) : null}
            <div className={styles.heading}>
              <div className={styles.breadcrumb}>
                <span>{section}</span>
                <span className={styles.breadcrumbSeparator}>/</span>
                <strong>{title}</strong>
              </div>
              <h1>{title}</h1>
              {description ? <p>{description}</p> : null}
            </div>
          </div>
          {actions ? <div className={styles.actions}>{actions}</div> : null}
        </div>

        {stats.length > 0 ? (
          <div className={styles.stats}>
            {stats.map((stat, index) => (
              <span key={index} className={styles.stat} data-tone={stat.tone}>
                <span>{stat.label}</span>
                <strong>{stat.value}</strong>
              </span>
            ))}
          </div>
        ) : null}
      </section>

      {toolbar ? <section className={styles.toolbar}>{toolbar}</section> : null}

      <section className={styles.content}>{children}</section>
    </main>
  );
}

export function CapabilityCard({
  icon,
  title,
  subtitle,
  status,
  statusTone = "default",
  description,
  chips,
  footer,
  selected = false,
  muted = false,
  onClick,
  interactiveLabel,
  className,
}: CapabilityCardProps) {
  return (
    <article
      className={`${styles.card} ${selected ? styles.cardSelected : ""} ${
        muted ? styles.cardMuted : ""
      } ${onClick ? styles.cardClickable : ""} ${className ?? ""}`.trim()}
      onClick={onClick}
      role={onClick ? "button" : undefined}
      tabIndex={onClick ? 0 : undefined}
      aria-label={onClick ? interactiveLabel : undefined}
      onKeyDown={(event) => {
        if (!onClick) return;
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          onClick();
        }
      }}
    >
      <div className={styles.cardTop}>
        <div className={styles.cardIdentity}>
          <span className={styles.cardIcon}>{icon}</span>
          <span className={styles.cardTitleBlock}>
            <strong>{title}</strong>
            {subtitle ? <em>{subtitle}</em> : null}
          </span>
        </div>
        {status ? (
          <span className={styles.status} data-tone={statusTone}>
            {status}
          </span>
        ) : null}
      </div>

      {description ? (
        <p className={styles.cardDescription}>{description}</p>
      ) : null}

      {chips ? <div className={styles.cardChips}>{chips}</div> : null}

      {footer ? <div className={styles.cardFooter}>{footer}</div> : null}
    </article>
  );
}

export default CapabilityPage;

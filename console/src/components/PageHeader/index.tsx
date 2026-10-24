import { Fragment, type ReactNode } from "react";
import { useLocation } from "react-router-dom";
import { getDetailNavigation } from "../../navigation/detailNavigation";
import { DetailBackButton } from "../DetailBackButton";
import styles from "./index.module.less";

export type PageHeaderBreadcrumbItem = {
  title: ReactNode;
};

export interface PageHeaderProps {
  /** When omitted, `parent` and `current` build the trail. */
  items?: PageHeaderBreadcrumbItem[];
  parent?: ReactNode;
  current?: ReactNode;
  center?: ReactNode;
  extra?: ReactNode;
  description?: ReactNode;
  /** Same row as the breadcrumb (e.g. workspace path chip). */
  afterBreadcrumb?: ReactNode;
  subRow?: ReactNode;
  className?: string;
}

function buildItemsFromParentCurrent(
  parent: ReactNode | undefined,
  current: ReactNode | undefined,
): PageHeaderBreadcrumbItem[] {
  const out: PageHeaderBreadcrumbItem[] = [];
  if (parent != null && parent !== "") out.push({ title: parent });
  if (current != null && current !== "") out.push({ title: current });
  return out;
}

export function PageHeader({
  items: itemsProp,
  parent,
  current,
  center,
  extra,
  description,
  afterBreadcrumb,
  subRow,
  className,
}: PageHeaderProps) {
  const location = useLocation();
  const items =
    itemsProp !== undefined
      ? itemsProp
      : buildItemsFromParentCurrent(parent, current);
  const detailNavigation = getDetailNavigation(location.pathname);

  return (
    <div className={`${styles.pageHeader} ${className ?? ""}`.trim()}>
      <div className={styles.headerMain}>
        <div className={styles.leading}>
          <div className={styles.leadingTop}>
            {detailNavigation ? (
              <DetailBackButton {...detailNavigation} />
            ) : null}
            <div className={styles.breadcrumbHeader}>
              {items.map((item, index) => (
                <Fragment key={index}>
                  {index > 0 ? (
                    <span className={styles.breadcrumbSeparator}>/</span>
                  ) : null}
                  {index === items.length - 1 ? (
                    <h1 className={styles.breadcrumbCurrent}>{item.title}</h1>
                  ) : (
                    <span className={styles.breadcrumbParent}>
                      {item.title}
                    </span>
                  )}
                </Fragment>
              ))}
              {afterBreadcrumb}
            </div>
          </div>
        </div>
        {center ? <div className={styles.center}>{center}</div> : null}
        {extra ? <div className={styles.extra}>{extra}</div> : null}
      </div>
      {description || subRow ? (
        <div className={styles.subContent}>
          {description ? (
            <p className={styles.description}>{description}</p>
          ) : null}
          {subRow}
        </div>
      ) : null}
    </div>
  );
}

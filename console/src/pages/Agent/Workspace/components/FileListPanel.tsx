import React from "react";
import { Button, Card } from "@/vendor/lantaiDesign";
import { ReloadOutlined } from "@ant-design/icons";
import type {
  WikiPageSummary,
  WikiQueueSummary,
  WikiSourceInfo,
} from "../../../../api/types";
import { FileItem } from "./FileItem";
import { useTranslation } from "react-i18next";
import styles from "../index.module.less";

interface FileListPanelProps {
  files: WikiPageSummary[];
  sources?: WikiSourceInfo[];
  queueSummary?: WikiQueueSummary;
  selectedFile: WikiPageSummary | null;
  stats?: Record<string, number>;
  onRefresh: () => void;
  onFileClick: (file: WikiPageSummary) => void;
}

export const FileListPanel: React.FC<FileListPanelProps> = ({
  files,
  sources = [],
  queueSummary,
  selectedFile,
  stats,
  onRefresh,
  onFileClick,
}) => {
  const { t } = useTranslation();
  const groupedFiles = files.reduce<Record<string, WikiPageSummary[]>>(
    (groups, file) => {
      const key = file.category || "root";
      groups[key] = groups[key] ?? [];
      groups[key].push(file);
      return groups;
    },
    {},
  );
  const orderedGroups = Object.entries(groupedFiles).sort(([a], [b]) =>
    a.localeCompare(b),
  );

  return (
    <div className={styles.fileListPanel}>
      <Card
        styles={{
          body: {
            padding: 16,
            display: "flex",
            flexDirection: "column",
            height: "100%",
            overflow: "auto",
          },
        }}
        style={{ flex: 1, minHeight: 0 }}
      >
        <div className={styles.headerRow}>
          <div>
            <h3 className={styles.sectionTitle}>{t("workspace.coreFiles")}</h3>
            <p className={styles.infoText}>{t("workspace.coreFilesDesc")}</p>
          </div>
          <Button
            size="small"
            onClick={onRefresh}
            icon={<ReloadOutlined />}
            aria-label={t("common.refresh")}
          />
        </div>

        <div className={styles.wikiStats}>
          <div>
            <strong>{files.length}</strong>
            <span>{t("workspace.pageCount")}</span>
          </div>
          <div>
            <strong>{stats?.links ?? 0}</strong>
            <span>{t("workspace.linkCount")}</span>
          </div>
          <div>
            <strong>{stats?.backlinks ?? 0}</strong>
            <span>{t("workspace.backlinkCount")}</span>
          </div>
          <div>
            <strong>{stats?.broken_links ?? 0}</strong>
            <span>{t("workspace.brokenLinkCount")}</span>
          </div>
        </div>

        <div className={styles.divider} />

        <div className={styles.queuePanel}>
          <div>
            <strong>{t("workspace.queueStatus")}</strong>
            <span>
              {t("workspace.queueSummary", {
                pending: queueSummary?.pending ?? 0,
                failed: queueSummary?.failed ?? 0,
                total: queueSummary?.total ?? 0,
              })}
            </span>
          </div>
        </div>

        <div className={styles.scrollContainer}>
          {files.length > 0 ? (
            orderedGroups.map(([category, groupFiles]) => (
              <section className={styles.fileGroup} key={category}>
                <h4>
                  {t(`workspace.wikiCategory.${category}`, {
                    defaultValue: category,
                  })}
                </h4>
                {groupFiles.map((file) => (
                  <FileItem
                    key={file.filename}
                    file={file}
                    selectedFile={selectedFile}
                    onFileClick={onFileClick}
                  />
                ))}
              </section>
            ))
          ) : (
            <div className={styles.emptyState}>{t("workspace.noFiles")}</div>
          )}

          {sources.length > 0 ? (
            <section className={styles.sourceMiniTree}>
              <h4>{t("workspace.sourceTreeTitle")}</h4>
              {sources.slice(0, 12).map((source) => (
                <div className={styles.sourceMiniItem} key={source.path}>
                  <strong>{source.filename}</strong>
                  <span>{source.status}</span>
                </div>
              ))}
            </section>
          ) : null}
        </div>
      </Card>
    </div>
  );
};

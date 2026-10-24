import React from "react";
import { FileMarkdownOutlined } from "@ant-design/icons";
import type { WikiPageSummary } from "../../../../api/types";
import prettyBytes from "pretty-bytes";
import { formatTimeAgo } from "./utils";
import styles from "../index.module.less";

interface FileItemProps {
  file: WikiPageSummary;
  selectedFile: WikiPageSummary | null;
  onFileClick: (file: WikiPageSummary) => void;
}

export const FileItem: React.FC<FileItemProps> = ({
  file,
  selectedFile,
  onFileClick,
}) => {
  const isSelected = selectedFile?.filename === file.filename;

  return (
    <div
      onClick={() => onFileClick(file)}
      className={`${styles.fileItem} ${isSelected ? styles.selected : ""}`}
      role="button"
      tabIndex={0}
      aria-current={isSelected ? "true" : undefined}
      onKeyDown={(event) => {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          onFileClick(file);
        }
      }}
    >
      <div className={styles.fileItemHeader}>
        <div className={styles.fileIcon}>
          <FileMarkdownOutlined />
        </div>
        <div className={styles.fileInfo}>
          <div className={styles.fileItemName}>{file.title}</div>
          <div className={styles.fileItemMeta}>
            {prettyBytes(file.size)} · {formatTimeAgo(file.modified_time)}
          </div>
          {file.summary && (
            <div className={styles.fileItemSummary}>{file.summary}</div>
          )}
          {(file.links.length > 0 || file.backlinks.length > 0) && (
            <div className={styles.fileItemLinks}>
              {file.links.length > 0 && <span>→ {file.links.length}</span>}
              {file.backlinks.length > 0 && (
                <span>← {file.backlinks.length}</span>
              )}
            </div>
          )}
        </div>
      </div>
    </div>
  );
};

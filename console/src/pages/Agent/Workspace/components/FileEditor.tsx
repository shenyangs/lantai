import React, { useState, useMemo } from "react";
import { Button, Input, Switch } from "@/vendor/lantaiDesign";
import {
  CopyOutlined,
  DeleteOutlined,
  UndoOutlined,
  SaveOutlined,
} from "@ant-design/icons";
import type { MarkdownFile } from "../../../../api/types";
import { XMarkdown } from "@ant-design/x-markdown";
import { useTranslation } from "react-i18next";
import { useAppMessage } from "../../../../hooks/useAppMessage";
import { stripFrontmatter } from "../../../../utils/markdown";
import styles from "../index.module.less";
import { Tooltip } from "antd";

interface FileEditorProps {
  selectedFile: MarkdownFile | null;
  fileContent: string;
  loading: boolean;
  hasChanges: boolean;
  onContentChange: (content: string) => void;
  onSave: () => void;
  onReset: () => void;
  onDelete?: () => void;
  deleting?: boolean;
}

export const FileEditor: React.FC<FileEditorProps> = ({
  selectedFile,
  fileContent,
  loading,
  hasChanges,
  onContentChange,
  onSave,
  onReset,
  onDelete,
  deleting = false,
}) => {
  const { t } = useTranslation();
  const { message } = useAppMessage();
  const [showMarkdown, setShowMarkdown] = useState(true);

  const isMarkdownFile = selectedFile?.filename.endsWith(".md") || false;
  const markdownContent = useMemo(
    () => stripFrontmatter(fileContent || ""),
    [fileContent],
  );

  const copyToClipboard = async () => {
    try {
      if (navigator.clipboard && window.isSecureContext) {
        await navigator.clipboard.writeText(fileContent);
        message.success(t("common.copied"));
      } else {
        const textArea = document.createElement("textarea");
        textArea.value = fileContent;
        textArea.style.position = "fixed";
        textArea.style.left = "-999999px";
        textArea.style.top = "-999999px";
        document.body.appendChild(textArea);
        textArea.focus();
        textArea.select();
        document.execCommand("copy");
        textArea.remove();
        message.success(t("common.copied"));
      }
    } catch (err) {
      console.error("Failed to copy text: ", err);
      message.error(t("common.copyFailed"));
    }
  };

  return (
    <div className={styles.fileEditor}>
      <section
        className={styles.editorCard}
        aria-label={selectedFile ? `${selectedFile.filename}详情` : "知识详情"}
      >
        {selectedFile ? (
          <>
            <div className={styles.editorHeader}>
              <div className={styles.editorTitle}>
                <div className={styles.fileName}>{selectedFile.filename}</div>
                <div className={styles.filePath}>{selectedFile.path}</div>
              </div>
              <div className={styles.buttonGroup}>
                {onDelete ? (
                  <Tooltip title={t("workspace.deletePageAction")}>
                    <Button
                      danger
                      size="small"
                      onClick={onDelete}
                      loading={deleting}
                      icon={<DeleteOutlined />}
                    >
                      {t("common.delete")}
                    </Button>
                  </Tooltip>
                ) : null}
                <Button
                  size="small"
                  onClick={onReset}
                  disabled={!hasChanges}
                  icon={<UndoOutlined />}
                >
                  {t("common.reset")}
                </Button>
                <Button
                  type="primary"
                  size="small"
                  onClick={onSave}
                  disabled={!hasChanges}
                  loading={loading}
                  icon={<SaveOutlined />}
                >
                  {t("common.save")}
                </Button>
              </div>
            </div>

            <div className={styles.editorContent}>
              <div className={styles.contentLabel}>
                <div>{t("common.content")}</div>
                {isMarkdownFile && (
                  <div className={styles.buttonGroup}>
                    <div className={styles.markdownToggle}>
                      <span className={styles.toggleLabel}>
                        {t("common.preview")}
                      </span>
                      <Switch
                        checked={showMarkdown}
                        onChange={setShowMarkdown}
                        size="small"
                        aria-label={`${selectedFile.filename} ${t(
                          "common.preview",
                        )}`}
                      />
                    </div>
                    <Button
                      icon={<CopyOutlined />}
                      type="text"
                      onClick={copyToClipboard}
                      className={styles.copyButton}
                      aria-label={`${t("common.copy")} ${selectedFile.filename}`}
                    />
                  </div>
                )}
              </div>
              {showMarkdown && isMarkdownFile ? (
                <XMarkdown
                  content={markdownContent}
                  className={styles.markdownViewer}
                  dompurifyConfig={{
                    ADD_TAGS: ["pre", "code"],
                    ADD_ATTR: [
                      "data-block",
                      "data-state",
                      "data-lang",
                      "class",
                    ],
                  }}
                />
              ) : (
                <Input.TextArea
                  value={fileContent}
                  onChange={(e) => onContentChange(e.target.value)}
                  className={styles.textarea}
                  placeholder={t("workspace.fileContent")}
                />
              )}
            </div>
          </>
        ) : (
          <div className={styles.emptyState}>{t("workspace.selectFile")}</div>
        )}
        <p className={styles.attribution}>{t("workspace.attribution")}</p>
      </section>
    </div>
  );
};

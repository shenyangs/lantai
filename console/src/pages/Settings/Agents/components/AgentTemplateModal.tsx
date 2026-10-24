import { useEffect, useMemo, useState } from "react";
import {
  Button,
  Empty,
  Input,
  Modal,
  Space,
  Spin,
  Tag,
  Typography,
} from "antd";
import { CheckCircleOutlined, ImportOutlined } from "@ant-design/icons";
import { agentsApi } from "@/api/modules/agents";
import type {
  RoleTemplateDetail,
  RoleTemplateSummary,
} from "@/api/types/agents";
import styles from "../index.module.less";

const { Text } = Typography;

export interface AgentTemplateSelection {
  name: string;
  description: string;
  suggested_agent_id: string;
  role_template_id?: string;
  role_template_markdown?: string;
  role_template_source_name?: string;
  recommended_skills?: string[];
}

interface AgentTemplateModalProps {
  open: boolean;
  onCancel: () => void;
  onUseTemplate: (selection: AgentTemplateSelection) => void;
}

function summarizeChecks(template: RoleTemplateSummary | RoleTemplateDetail) {
  const errors = template.checks.filter(
    (check) => check.status === "error",
  ).length;
  const warnings = template.checks.filter(
    (check) => check.status === "warning",
  ).length;
  if (errors > 0) return `${errors} 个错误`;
  if (warnings > 0) return `${warnings} 个提醒`;
  return "结构完整";
}

export function AgentTemplateModal({
  open,
  onCancel,
  onUseTemplate,
}: AgentTemplateModalProps) {
  const [templates, setTemplates] = useState<RoleTemplateSummary[]>([]);
  const [loading, setLoading] = useState(false);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [importText, setImportText] = useState("");
  const [preview, setPreview] = useState<RoleTemplateDetail | null>(null);
  const [previewLoading, setPreviewLoading] = useState(false);
  const [previewError, setPreviewError] = useState<string | null>(null);

  useEffect(() => {
    if (!open) return;

    setLoading(true);
    agentsApi
      .listRoleTemplates()
      .then((data) => {
        setTemplates(data.templates ?? []);
        setSelectedId((data.templates ?? [])[0]?.id ?? null);
      })
      .finally(() => setLoading(false));
  }, [open]);

  useEffect(() => {
    if (!open) {
      setImportText("");
      setPreview(null);
      setPreviewError(null);
    }
  }, [open]);

  const selectedTemplate = useMemo(
    () => templates.find((template) => template.id === selectedId) ?? null,
    [selectedId, templates],
  );

  const handlePreviewImport = async () => {
    const content = importText.trim();
    if (!content) {
      setPreview(null);
      setPreviewError("请先粘贴 Markdown 模板内容。");
      return;
    }

    setPreviewLoading(true);
    setPreviewError(null);
    try {
      const result = await agentsApi.previewRoleTemplate({
        content,
        source_name: "粘贴导入模板.md",
      });
      setPreview(result);
    } catch (error: any) {
      setPreview(null);
      setPreviewError(error.message || "模板解析失败，请检查 Markdown 内容。");
    } finally {
      setPreviewLoading(false);
    }
  };

  const useBuiltinTemplate = () => {
    if (!selectedTemplate) return;
    onUseTemplate({
      name: selectedTemplate.name,
      description: selectedTemplate.description,
      suggested_agent_id: selectedTemplate.suggested_agent_id,
      role_template_id: selectedTemplate.id,
      recommended_skills: selectedTemplate.recommended_skills,
    });
  };

  const useImportedTemplate = () => {
    if (!preview) return;
    onUseTemplate({
      name: preview.name,
      description: preview.description,
      suggested_agent_id: preview.suggested_agent_id,
      role_template_markdown: preview.markdown || importText,
      role_template_source_name: "粘贴导入模板.md",
      recommended_skills: preview.recommended_skills,
    });
  };

  return (
    <Modal
      title={
        <div className={styles.modalTitle}>
          <span className={styles.modalTitleMain}>从模板创建智能体</span>
          <span className={styles.modalTitleSub}>
            先选一个专业角色，再进入创建弹窗配置模型、工作区和技能。
          </span>
        </div>
      }
      open={open}
      onCancel={onCancel}
      footer={null}
      width={980}
    >
      <div className={styles.templateModalBody}>
        <section className={styles.templatePanel}>
          <div className={styles.templatePanelHeader}>
            <div>
              <strong>内置精选模板</strong>
              <Text type="secondary">参考 agency-agents 的分类方式重写。</Text>
            </div>
            <Button
              type="primary"
              onClick={useBuiltinTemplate}
              disabled={!selectedTemplate}
            >
              使用所选模板
            </Button>
          </div>

          {loading ? (
            <div className={styles.templateLoading}>
              <Spin size="small" />
            </div>
          ) : templates.length === 0 ? (
            <Empty
              image={Empty.PRESENTED_IMAGE_SIMPLE}
              description="暂无模板"
            />
          ) : (
            <div className={styles.templateGrid}>
              {templates.map((template) => {
                const selected = template.id === selectedId;
                return (
                  <button
                    key={template.id}
                    type="button"
                    className={`${styles.templateCard} ${
                      selected ? styles.templateCardSelected : ""
                    }`}
                    onClick={() => setSelectedId(template.id)}
                  >
                    <span
                      className={styles.templateSwatch}
                      style={{ backgroundColor: template.accent_color }}
                    />
                    <span className={styles.templateCardTitle}>
                      {template.name}
                    </span>
                    <span className={styles.templateCardDesc}>
                      {template.description}
                    </span>
                    <span className={styles.templateCardMeta}>
                      <Tag>{template.category}</Tag>
                      <span>
                        {template.recommended_skills.length
                          ? `${template.recommended_skills.length} 个推荐技能 · `
                          : ""}
                        {summarizeChecks(template)}
                      </span>
                    </span>
                  </button>
                );
              })}
            </div>
          )}
        </section>

        <section className={styles.templatePanel}>
          <div className={styles.templatePanelHeader}>
            <div>
              <strong>导入 Markdown 模板</strong>
              <Text type="secondary">
                支持 YAML frontmatter、标题、描述和标签。
              </Text>
            </div>
            <Space>
              <Button icon={<ImportOutlined />} onClick={handlePreviewImport}>
                预览
              </Button>
              <Button
                type="primary"
                onClick={useImportedTemplate}
                disabled={!preview}
              >
                使用导入模板
              </Button>
            </Space>
          </div>

          <Input.TextArea
            className={styles.templateImportInput}
            value={importText}
            onChange={(event) => setImportText(event.target.value)}
            placeholder={
              "---\nname: 代码审查员\ndescription: 审查代码风险\n---\n# 代码审查员\n\n## 使命\n..."
            }
            rows={8}
          />

          <div className={styles.templatePreviewBox}>
            {previewLoading ? (
              <Spin size="small" />
            ) : preview ? (
              <>
                <div className={styles.templatePreviewTitle}>
                  <CheckCircleOutlined />
                  <strong>{preview.name}</strong>
                  <Tag>{preview.category}</Tag>
                </div>
                <p>{preview.description}</p>
                <div className={styles.templateCheckList}>
                  {preview.checks.map((check) => (
                    <span key={check.key} data-status={check.status}>
                      {check.label}：{check.detail}
                    </span>
                  ))}
                </div>
              </>
            ) : previewError ? (
              <Text type="danger">{previewError}</Text>
            ) : (
              <Text type="secondary">
                粘贴模板后点击预览，确认结构再创建智能体。
              </Text>
            )}
          </div>
        </section>
      </div>
    </Modal>
  );
}

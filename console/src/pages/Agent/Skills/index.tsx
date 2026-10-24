import dayjs from "dayjs";
import { useMemo } from "react";
import { useTranslation } from "react-i18next";
import {
  Button,
  Checkbox,
  Dropdown,
  Empty,
  Input,
  Select,
  Tooltip,
} from "@/vendor/lantaiDesign";
import type { MenuProps } from "@/vendor/lantaiDesign";
import {
  CheckCircleOutlined,
  ClockCircleOutlined,
  CloseOutlined,
  DeleteOutlined,
  DownOutlined,
  DownloadOutlined,
  EditOutlined,
  EyeInvisibleOutlined,
  EyeOutlined,
  ImportOutlined,
  PlusOutlined,
  ReloadOutlined,
  SwapOutlined,
  UploadOutlined,
} from "@ant-design/icons";
import type { SkillSpec } from "../../../api/types";
import {
  CapabilityCard,
  CapabilityChip,
  CapabilityPage,
} from "../../../components/CapabilityLayout";
import { getLocalizedSkillDescription } from "@/utils/displayDescriptions";
import { useAgentStore } from "../../../stores/agentStore";
import {
  getFileIcon,
  ImportHubModal,
  PoolTransferModal,
  SkillDrawer,
  SkillFilterDropdown,
  SkillLibraryNav,
} from "./components";
import { getSkillDisplayName } from "./skillPresentation";
import { useSkillsPage } from "./useSkillsPage";
import { describeSkillChannelScope } from "./scope";
import styles from "./index.module.less";

function isBuiltinSkill(skill: SkillSpec) {
  return (
    skill.source === "builtin" ||
    skill.source?.startsWith("builtin:") ||
    skill.source === "system"
  );
}

function SkillIcon({ skill }: { skill: SkillSpec }) {
  return (
    <span className={styles.capabilityFileIcon}>{getFileIcon(skill.name)}</span>
  );
}

function SkillsPage() {
  const { t } = useTranslation();
  const { selectedAgent, agents } = useAgentStore();
  const {
    skills,
    visibleSkills,
    hasMore,
    sentinelRef,
    poolSkills,
    allTags,
    sortedSkills,
    conflictRenameModal,
    loading,
    uploading,
    importing,
    restoringBuiltins,
    drawerOpen,
    importModalOpen,
    setImportModalOpen,
    editingSkill,
    form,
    fileInputRef,
    poolModal,
    setPoolModal,
    selectedSkills,
    batchModeEnabled,
    filterOpen,
    setFilterOpen,
    searchQuery,
    setSearchQuery,
    searchTags,
    setSearchTags,
    handleCreate,
    handleEdit,
    handleToggleEnabled,
    handleDelete,
    handleDrawerClose,
    handleSubmit,
    handleUploadToPool,
    handleDownloadFromPool,
    handleBatchEnable,
    handleBatchDisable,
    handleBatchDelete,
    handleUploadClick,
    handleFileChange,
    handleConfirmImport,
    restoreRecommendedSkills,
    closeImportModal,
    closePoolModal,
    toggleSelect,
    selectAll,
    clearSelection,
    toggleBatchMode,
    hardRefresh,
    cancelImport,
  } = useSkillsPage();

  const enabledCount = useMemo(
    () => skills.filter((skill) => skill.enabled).length,
    [skills],
  );
  const builtinCount = useMemo(
    () => skills.filter((skill) => isBuiltinSkill(skill)).length,
    [skills],
  );
  const selectedCount = selectedSkills.size;
  const currentAgent = agents.find((agent) => agent.id === selectedAgent);
  const currentAgentLabel =
    currentAgent?.name ||
    currentAgent?.description ||
    selectedAgent ||
    "默认智能体";
  const importMenuItems: MenuProps["items"] = [
    {
      key: "download-pool",
      icon: <DownloadOutlined />,
      label: t("skills.downloadFromPool"),
    },
    {
      key: "upload-pool",
      icon: <SwapOutlined />,
      label: t("skills.uploadToPool"),
    },
    {
      key: "upload-zip",
      icon: <UploadOutlined />,
      label: t("skills.uploadZip"),
      disabled: uploading,
    },
    {
      key: "import-hub",
      icon: <ImportOutlined />,
      label: t("skills.importHub"),
    },
  ];

  const handleImportMenuClick: MenuProps["onClick"] = ({ key }) => {
    if (key === "download-pool") setPoolModal("download");
    if (key === "upload-pool") setPoolModal("upload");
    if (key === "upload-zip") handleUploadClick();
    if (key === "import-hub") setImportModalOpen(true);
  };

  const renderChannels = (skill: SkillSpec) =>
    describeSkillChannelScope(skill.channels);

  const renderSource = (skill: SkillSpec) =>
    isBuiltinSkill(skill) ? t("skills.builtin") : t("skills.custom");

  return (
    <CapabilityPage
      title="技能"
      description="管理当前智能体可调用的技能，按任务需要启用、配置、同步或导入。"
      actions={
        <div className={styles.capabilityActions}>
          <input
            type="file"
            accept=".zip"
            ref={fileInputRef}
            onChange={handleFileChange}
            style={{ display: "none" }}
          />
          {batchModeEnabled ? (
            <>
              <span className={styles.selectionCount}>
                {t("skills.selectedCount", { count: selectedCount })}
              </span>
              <Button type="default" onClick={selectAll}>
                {t("skills.selectAll")}
              </Button>
              <Button
                type="default"
                icon={<CloseOutlined />}
                onClick={clearSelection}
              >
                {t("skills.clearSelection")}
              </Button>
              <Button
                type="default"
                icon={<SwapOutlined />}
                disabled={selectedCount === 0}
                onClick={() => {
                  const names = Array.from(selectedSkills);
                  if (names.length === 0) return;
                  clearSelection();
                  void handleUploadToPool(names);
                }}
              >
                {t("skills.uploadToPool")}
              </Button>
              <Button
                type="default"
                icon={<EyeOutlined />}
                disabled={selectedCount === 0}
                onClick={handleBatchEnable}
              >
                {t("skills.batchEnable")}
              </Button>
              <Button
                danger
                icon={<EyeInvisibleOutlined />}
                disabled={selectedCount === 0}
                onClick={handleBatchDisable}
              >
                {t("skills.batchDisable")}
              </Button>
              <Button
                danger
                icon={<DeleteOutlined />}
                disabled={selectedCount === 0}
                onClick={handleBatchDelete}
              >
                {t("common.delete")}
              </Button>
              <Button type="primary" onClick={toggleBatchMode}>
                {t("skills.exitBatch")}
              </Button>
            </>
          ) : (
            <>
              <Tooltip title={t("skills.refreshHint")}>
                <Button
                  type="default"
                  icon={<ReloadOutlined spin={loading} />}
                  onClick={hardRefresh}
                  disabled={loading}
                  aria-label={t("skills.refreshHint")}
                />
              </Tooltip>
              <Dropdown
                menu={{
                  items: importMenuItems,
                  onClick: handleImportMenuClick,
                }}
                trigger={["click"]}
              >
                <Button type="default" icon={<ImportOutlined />}>
                  导入与同步
                  <DownOutlined className={styles.menuChevron} />
                </Button>
              </Dropdown>
              <Button type="default" onClick={toggleBatchMode}>
                {t("skills.batchOperation")}
              </Button>
              <Button
                type="primary"
                icon={<PlusOutlined />}
                onClick={handleCreate}
              >
                {t("skills.createSkill")}
              </Button>
            </>
          )}
        </div>
      }
      stats={[
        { label: "全部", value: skills.length },
        { label: "启用", value: enabledCount, tone: "success" },
        { label: "停用", value: skills.length - enabledCount, tone: "muted" },
        { label: "内置", value: builtinCount, tone: "info" },
        { label: "标签", value: allTags.length },
      ]}
      toolbar={
        <div className={styles.capabilityToolbar}>
          <SkillLibraryNav active="agent" />
          <div className={styles.capabilityFilters}>
            <Input
              className={styles.capabilitySearch}
              placeholder={t("skills.searchPlaceholder")}
              value={searchQuery}
              onChange={(event) => setSearchQuery(event.target.value)}
            />
            <Select
              mode="multiple"
              className={styles.capabilityTagSelect}
              placeholder={t("skills.filterByTag")}
              value={searchTags}
              onChange={setSearchTags}
              open={filterOpen}
              onOpenChange={setFilterOpen}
              allowClear
              maxTagCount="responsive"
              notFoundContent={<></>}
              popupRender={() =>
                allTags.length > 0 ? (
                  <SkillFilterDropdown
                    allTags={allTags}
                    searchTags={searchTags}
                    setSearchTags={setSearchTags}
                    styles={styles}
                  />
                ) : (
                  <div className={styles.tagSelectEmpty}>
                    {t("skills.noTags")}
                  </div>
                )
              }
            />
          </div>
          <span className={styles.toolbarSummary}>
            {batchModeEnabled
              ? `已选 ${selectedCount} 个`
              : `当前显示 ${sortedSkills.length} 个`}
          </span>
        </div>
      }
    >
      <section className={styles.scopeNotice} aria-label="当前技能范围">
        <span>
          <strong>当前管理：{currentAgentLabel}</strong>
          这里的启用状态只属于这个智能体。技能池是可分发的总库；“所有消息渠道”不代表所有智能体都已启用。
        </span>
      </section>
      <ImportHubModal
        open={importModalOpen}
        importing={importing}
        onCancel={closeImportModal}
        onConfirm={handleConfirmImport}
        cancelImport={cancelImport}
        hint={t("skillPool.externalHubHint")}
      />

      {loading ? (
        <div className={styles.loading}>
          <span className={styles.loadingText}>{t("common.loading")}</span>
        </div>
      ) : skills.length === 0 ? (
        <div className={styles.capabilityEmptyState}>
          <Empty description={t("skills.emptyStateText")} />
          <div className={styles.emptyStateActions}>
            <Button
              loading={restoringBuiltins}
              onClick={() => restoreRecommendedSkills(false)}
            >
              恢复内置技能
            </Button>
            <Button
              type="primary"
              icon={<PlusOutlined />}
              onClick={handleCreate}
            >
              {t("skills.emptyStateCreate")}
            </Button>
          </div>
        </div>
      ) : sortedSkills.length === 0 ? (
        <div className={styles.capabilityEmptyState}>
          <Empty description={t("skills.noSearchResults")} />
        </div>
      ) : (
        <div className={styles.capabilityGrid}>
          {visibleSkills.map((skill) => {
            const selected = selectedSkills.has(skill.name);
            const description =
              getLocalizedSkillDescription(skill, t) || "暂无描述。";
            const displayName = getSkillDisplayName(skill.name, description);
            const tags = skill.tags || [];

            return (
              <CapabilityCard
                key={skill.name}
                icon={<SkillIcon skill={skill} />}
                title={
                  <Tooltip title={`技术名称：${skill.name}`}>
                    <span className={styles.cardTitleText}>{displayName}</span>
                  </Tooltip>
                }
                subtitle={`${renderSource(skill)} · ${skill.name}`}
                status={
                  <>
                    {skill.enabled ? (
                      <CheckCircleOutlined />
                    ) : (
                      <EyeInvisibleOutlined />
                    )}
                    {skill.enabled ? t("common.enabled") : t("common.disabled")}
                  </>
                }
                statusTone={skill.enabled ? "success" : "muted"}
                description={description}
                muted={!skill.enabled}
                selected={batchModeEnabled && selected}
                chips={
                  <>
                    <CapabilityChip
                      tone={isBuiltinSkill(skill) ? "muted" : "warning"}
                    >
                      {renderSource(skill)}
                    </CapabilityChip>
                    <CapabilityChip title={renderChannels(skill)}>
                      <span className={styles.chipText}>
                        {renderChannels(skill)}
                      </span>
                    </CapabilityChip>
                    {skill.last_updated ? (
                      <CapabilityChip>
                        <ClockCircleOutlined />
                        {dayjs(skill.last_updated).fromNow()}
                      </CapabilityChip>
                    ) : null}
                    {tags.slice(0, 3).map((tag) => (
                      <CapabilityChip key={tag} tone="info" title={tag}>
                        <span className={styles.chipText}>{tag}</span>
                      </CapabilityChip>
                    ))}
                    {tags.length > 3 ? (
                      <CapabilityChip tone="muted">
                        +{tags.length - 3}
                      </CapabilityChip>
                    ) : null}
                  </>
                }
                footer={
                  <>
                    <div className={styles.cardSelectSide}>
                      {batchModeEnabled ? (
                        <Checkbox
                          checked={selected}
                          onClick={() => toggleSelect(skill.name)}
                        >
                          选择
                        </Checkbox>
                      ) : null}
                    </div>
                    <div className={styles.cardActionGroup}>
                      <Button
                        type="default"
                        size="small"
                        disabled={batchModeEnabled}
                        icon={<EditOutlined />}
                        aria-label={`编辑 ${displayName}`}
                        onClick={() => handleEdit(skill)}
                      >
                        编辑
                      </Button>
                      <Button
                        type="default"
                        size="small"
                        disabled={batchModeEnabled}
                        icon={
                          skill.enabled ? (
                            <EyeInvisibleOutlined />
                          ) : (
                            <EyeOutlined />
                          )
                        }
                        onClick={(event) => handleToggleEnabled(skill, event)}
                      >
                        {skill.enabled
                          ? t("common.disable")
                          : t("common.enable")}
                      </Button>
                      <Tooltip title={t("common.delete")}>
                        <Button
                          type="text"
                          size="small"
                          className={styles.cardDeleteButton}
                          disabled={batchModeEnabled}
                          icon={<DeleteOutlined />}
                          aria-label={`${t("common.delete")} ${skill.name}`}
                          onClick={(event) => handleDelete(skill, event)}
                        />
                      </Tooltip>
                    </div>
                  </>
                }
              />
            );
          })}
          {hasMore && (
            <div ref={sentinelRef} className={styles.scrollSentinel} />
          )}
        </div>
      )}

      <PoolTransferModal
        mode={poolModal}
        skills={skills}
        poolSkills={poolSkills}
        onCancel={closePoolModal}
        onUpload={handleUploadToPool}
        onDownload={handleDownloadFromPool}
      />

      {conflictRenameModal}

      <SkillDrawer
        open={drawerOpen}
        editingSkill={editingSkill}
        form={form}
        availableTags={allTags}
        onClose={handleDrawerClose}
        onSubmit={handleSubmit}
      />
    </CapabilityPage>
  );
}

export default SkillsPage;

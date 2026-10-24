import { Button, Form, Tabs } from "@/vendor/lantaiDesign";
import { useTranslation } from "react-i18next";
import { useNavigate } from "react-router-dom";
import { useSecurityPage } from "./useSecurityPage";
import {
  ToolGuardTab,
  RuleModal,
  PreviewModal,
  SkillScannerSection,
  FileGuardSection,
  AllowNoAuthHostsTab,
} from "./components";
import { PageHeader } from "@/components/PageHeader";
import styles from "./index.module.less";

function SecurityPage() {
  const { t } = useTranslation();
  const navigate = useNavigate();

  const {
    activeTab,
    setActiveTab,
    form,
    config,
    enabled,
    setEnabled,
    toolOptions,
    mergedRules,
    builtinRules,
    customRules,
    commandRules,
    setCommandRules,
    showFullAutoMode,
    rememberedApprovals,
    rememberedApprovalsLoading,
    handleRefreshRememberedApprovals,
    handleDeleteRememberedApproval,
    handleClearRememberedApprovals,
    toggleRule,
    toggleAutoDeny,
    deleteCustomRule,
    openAddRule,
    openEditRule,
    shellEvasionChecks,
    toggleShellEvasionCheck,
    editModal,
    setEditModal,
    editingRule,
    editForm,
    handleEditSave,
    previewRule,
    setPreviewRule,
    fileGuardHandlers,
    onFileGuardHandlersReady,
    allowNoAuthHostsHandlers,
    onAllowNoAuthHostsHandlersReady,
    saving,
    handleSave,
    handleReset,
    loading,
    error,
    fetchAll,
  } = useSecurityPage();

  // Loading state
  if (loading) {
    return (
      <div className={styles.securityPage}>
        <div className={styles.centerState}>
          <span className={styles.stateText}>{t("common.loading")}</span>
        </div>
      </div>
    );
  }

  // Error state
  if (error) {
    return (
      <div className={styles.securityPage}>
        <div className={styles.centerState}>
          <span className={styles.stateTextError}>{error}</span>
          <Button size="small" onClick={fetchAll} style={{ marginTop: 12 }}>
            {t("environments.retry")}
          </Button>
        </div>
      </div>
    );
  }

  return (
    <div className={styles.securityPage}>
      <PageHeader
        parent={t("security.parent")}
        current={t("security.security")}
        description={t("security.description")}
      />

      <div className={styles.content}>
        <Tabs
          className={styles.mainTabs}
          activeKey={activeTab}
          onChange={setActiveTab}
          items={[
            {
              key: "toolGuard",
              label: (
                <span className={styles.tabLabel}>
                  {t("security.toolGuardTitle")}
                </span>
              ),
              children: (
                <ToolGuardTab
                  form={form}
                  config={config}
                  enabled={enabled}
                  setEnabled={setEnabled}
                  toolOptions={toolOptions}
                  commandRules={commandRules}
                  setCommandRules={setCommandRules}
                  showFullAutoMode={showFullAutoMode}
                  mergedRules={mergedRules}
                  toggleRule={toggleRule}
                  toggleAutoDeny={toggleAutoDeny}
                  onPreviewRule={setPreviewRule}
                  onEditRule={openEditRule}
                  onDeleteRule={deleteCustomRule}
                  openAddRule={openAddRule}
                  rememberedApprovals={rememberedApprovals}
                  rememberedApprovalsLoading={rememberedApprovalsLoading}
                  onRefreshRememberedApprovals={
                    handleRefreshRememberedApprovals
                  }
                  onDeleteRememberedApproval={handleDeleteRememberedApproval}
                  onClearRememberedApprovals={handleClearRememberedApprovals}
                  shellEvasionChecks={shellEvasionChecks}
                  toggleShellEvasionCheck={toggleShellEvasionCheck}
                  onOpenPermissions={() => navigate("/permissions")}
                />
              ),
            },
            {
              key: "fileGuard",
              label: (
                <span className={styles.tabLabel}>
                  {t("security.fileGuard.title")}
                </span>
              ),
              children: (
                <div className={styles.tabContent}>
                  <div className={styles.sectionFileGuardContainer}>
                    <p className={styles.tabDescription}>
                      {t("security.fileGuard.description")}
                    </p>
                    <FileGuardSection onSave={onFileGuardHandlersReady} />
                  </div>
                </div>
              ),
            },
            {
              key: "skillScanner",
              label: (
                <span className={styles.tabLabel}>
                  {t("security.skillScanner.title")}
                </span>
              ),
              children: (
                <div className={styles.tabContent}>
                  <div className={styles.sectionSkillScannerContainer}>
                    <p className={styles.tabDescription}>
                      {t("security.skillScanner.description")}
                    </p>
                    <SkillScannerSection />
                  </div>
                </div>
              ),
            },
            {
              key: "allowNoAuthHosts",
              label: (
                <span className={styles.tabLabel}>
                  {t("security.allowNoAuthHosts.title")}
                </span>
              ),
              children: (
                <AllowNoAuthHostsTab onSave={onAllowNoAuthHostsHandlersReady} />
              ),
            },
          ]}
        />
      </div>

      <Form form={form} component={false} />

      {activeTab === "toolGuard" && (
        <div className={styles.footerButtons} aria-label="检测规则修改">
          <Button onClick={handleReset} disabled={saving}>
            取消规则修改
          </Button>
          <Button
            type="primary"
            onClick={() => void handleSave()}
            loading={saving}
          >
            保存检测规则
          </Button>
        </div>
      )}

      {activeTab === "fileGuard" && fileGuardHandlers && (
        <div className={styles.footerButtons}>
          <Button
            onClick={fileGuardHandlers.reset}
            disabled={fileGuardHandlers.saving}
            style={{ marginRight: 8 }}
          >
            {t("common.reset")}
          </Button>
          <Button
            type="primary"
            onClick={fileGuardHandlers.save}
            loading={fileGuardHandlers.saving}
          >
            {t("common.save")}
          </Button>
        </div>
      )}

      {activeTab === "allowNoAuthHosts" && allowNoAuthHostsHandlers && (
        <div className={styles.footerButtons}>
          <Button
            onClick={allowNoAuthHostsHandlers.reset}
            disabled={allowNoAuthHostsHandlers.saving}
            style={{ marginRight: 8 }}
          >
            {t("common.reset")}
          </Button>
          <Button
            type="primary"
            onClick={allowNoAuthHostsHandlers.save}
            loading={allowNoAuthHostsHandlers.saving}
          >
            {t("common.save")}
          </Button>
        </div>
      )}

      <RuleModal
        open={editModal}
        editingRule={editingRule}
        existingRuleIds={[
          ...builtinRules.map((r) => r.id),
          ...customRules.map((r) => r.id),
        ]}
        onOk={handleEditSave}
        onCancel={() => setEditModal(false)}
        form={editForm}
      />

      <PreviewModal rule={previewRule} onClose={() => setPreviewRule(null)} />
    </div>
  );
}

export default SecurityPage;

import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type RefObject,
} from "react";
import { Button, Input } from "@/vendor/lantaiDesign";
import {
  ApiOutlined,
  PlusOutlined,
  SearchOutlined,
  SyncOutlined,
} from "@ant-design/icons";
import { useProviders } from "./useProviders";
import {
  LoadingState,
  ProviderCard,
  CustomProviderModal,
  OpenAICompatibleProviderModal,
  ModelsSection,
  UtilityIntelligenceSection,
  WebSearchSection,
  ImageGenerationSection,
  VideoGenerationSection,
  ModelSetupGuide,
} from "./components";
import { PageHeader } from "@/components/PageHeader";
import { useTranslation } from "react-i18next";
import type { ProviderInfo } from "../../../api/types/provider";
import styles from "./index.module.less";
import {
  isTokenDanceCredentialConnected,
  partitionProviderSections,
} from "./providerSections";
import { TokenDanceAccountPanel } from "./components/sections/TokenDanceAccountPanel";
import { useNavigate, useSearchParams } from "react-router-dom";
import {
  ensureFirstModelTaskDraft,
  normalizeModelSetupReturnTo,
  parseModelSetupMode,
} from "../../../utils/modelSetupFlow";
import api from "../../../api";
import { deriveModelSetupReadiness } from "./modelSetupReadiness";

/* ------------------------------------------------------------------ */
/* Main Page                                                           */
/* ------------------------------------------------------------------ */

function ModelsPage() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  const setupMode = parseModelSetupMode(searchParams.get("setup"));
  const setupReturnTo = normalizeModelSetupReturnTo(
    searchParams.get("returnTo"),
  );
  const { providers, activeModels, loading, error, fetchAll } = useProviders();
  const [addProviderOpen, setAddProviderOpen] = useState(false);
  const [openAICompatibleOpen, setOpenAICompatibleOpen] = useState(
    setupMode === "own",
  );
  const [tokendanceSettingsOpen, setTokendanceSettingsOpen] = useState(false);
  const [searchQuery, setSearchQuery] = useState("");
  const [setupProviderId, setSetupProviderId] = useState<string | null>(null);
  const [verifyingModel, setVerifyingModel] = useState(false);
  const [verificationError, setVerificationError] = useState<string | null>(
    null,
  );
  const primaryModelRef = useRef<HTMLDivElement>(null);
  const localProvidersRef = useRef<HTMLDivElement>(null);

  const refreshProvidersSilently = useCallback(() => {
    void fetchAll(false);
  }, [fetchAll]);

  const { tokendanceProvider, regularProviders, localProviders } = useMemo(
    () => partitionProviderSections(providers, searchQuery),
    [providers, searchQuery],
  );

  const setupReadiness = useMemo(
    () =>
      setupMode
        ? deriveModelSetupReadiness({
            mode: setupMode,
            providers,
            activeModels,
            targetProviderId: setupProviderId,
          })
        : null,
    [activeModels, providers, setupMode, setupProviderId],
  );

  const scrollTo = useCallback((target: RefObject<HTMLDivElement | null>) => {
    target.current?.scrollIntoView?.({ behavior: "smooth", block: "center" });
  }, []);

  useEffect(() => {
    if (loading || !setupMode) return;
    if (setupMode === "local" && !setupReadiness?.providerConfigured) {
      scrollTo(localProvidersRef);
    } else if (
      setupReadiness?.providerConfigured &&
      !setupReadiness.modelSelected
    ) {
      scrollTo(primaryModelRef);
    }
  }, [loading, scrollTo, setupMode, setupReadiness]);

  const verifyCurrentModel = useCallback(async () => {
    const provider = setupReadiness?.provider;
    const modelId = setupReadiness?.modelId;
    if (!provider || !modelId) return;

    setVerifyingModel(true);
    setVerificationError(null);
    try {
      const result = await api.testModelConnection(provider.id, {
        model_id: modelId,
      });
      if (!result.success) {
        throw new Error(result.message || "当前模型验证失败");
      }
      if (!result.receipt_recorded) {
        throw new Error(
          "调用已成功，但结果未绑定到当前配置版本；请刷新后再核实，暂不能标记为已验证。",
        );
      }
      await fetchAll(false);
    } catch (verificationFailure) {
      setVerificationError(
        verificationFailure instanceof Error
          ? verificationFailure.message
          : "当前模型验证失败，请检查配置后再试。",
      );
    } finally {
      setVerifyingModel(false);
    }
  }, [fetchAll, setupReadiness]);

  const renderProviderCards = (list: ProviderInfo[]) =>
    list.map((provider) => (
      <ProviderCard
        key={provider.id}
        provider={provider}
        activeModels={activeModels}
        onSaved={refreshProvidersSilently}
      />
    ));

  return (
    <div className={styles.settingsPage}>
      {loading ? (
        <LoadingState message={t("models.loading")} />
      ) : error ? (
        <LoadingState message={error} error onRetry={fetchAll} />
      ) : (
        <>
          {/* ---- LLM Section (top) ---- */}
          <PageHeader
            parent={t("nav.settings")}
            current={t("models.llmTitle")}
            description={t("models.pageDescription")}
          />
          {/* ---- Scrollable Content ---- */}
          <div className={styles.content}>
            {setupMode && setupReadiness ? (
              <ModelSetupGuide
                mode={setupMode}
                readiness={setupReadiness}
                verifying={verifyingModel}
                verificationError={verificationError}
                onConnect={() => {
                  if (setupMode === "watcha") {
                    setTokendanceSettingsOpen(true);
                  } else if (setupMode === "own") {
                    setOpenAICompatibleOpen(true);
                  } else {
                    scrollTo(localProvidersRef);
                  }
                }}
                onSelectModel={() => scrollTo(primaryModelRef)}
                onVerifyModel={() => void verifyCurrentModel()}
                onFinish={() => {
                  ensureFirstModelTaskDraft(setupReturnTo);
                  navigate(setupReturnTo);
                }}
                onDismiss={() => navigate(setupReturnTo, { replace: true })}
              />
            ) : null}
            <div ref={primaryModelRef} data-testid="primary-model-selection">
              <ModelsSection
                providers={providers}
                activeModels={activeModels}
                onSaved={fetchAll}
              />
            </div>
            {/* ---- Providers Section ---- */}
            <div className={styles.providersBlock}>
              <div className={styles.sectionHeaderRow}>
                <div className={styles.providersHeading}>
                  <h2>{t("models.providersTitle")}</h2>
                  <p>{t("models.providersDescription")}</p>
                </div>
                <div className={styles.headerRight}>
                  {/* ---- Search ---- */}
                  <div className={styles.searchRow}>
                    <Input
                      placeholder={t("models.searchPlaceholder")}
                      value={searchQuery}
                      onChange={(e) => setSearchQuery(e.target.value)}
                      className={styles.searchInput}
                      prefix={<SearchOutlined />}
                      allowClear
                    />
                    <Button
                      icon={<SyncOutlined />}
                      onClick={() => fetchAll()}
                      className={styles.searchBtn}
                      title={t("common.refresh")}
                    />
                  </div>
                  <Button
                    type="primary"
                    icon={<PlusOutlined />}
                    onClick={() => setAddProviderOpen(true)}
                    className={styles.addProviderBtn}
                  >
                    {t("models.addProvider")}
                  </Button>
                </div>
              </div>

              <div className={styles.gatewayEntry}>
                <div className={styles.gatewayEntryIcon}>
                  <ApiOutlined />
                </div>
                <div className={styles.gatewayEntryCopy}>
                  <div className={styles.gatewayEntryTitleRow}>
                    <h3>{t("models.openAICompatibleTitle")}</h3>
                    <span>{t("models.openAICompatibleBadge")}</span>
                  </div>
                  <p>{t("models.openAICompatibleEntryDescription")}</p>
                </div>
                <Button
                  onClick={() => setOpenAICompatibleOpen(true)}
                  className={styles.gatewayEntryButton}
                >
                  {t("models.openAICompatibleConnect")}
                </Button>
              </div>

              {tokendanceProvider && (
                <section className={styles.tokendanceProviderBlock}>
                  <div className={styles.tokendanceProviderHeading}>
                    <h3>观猹账号模型</h3>
                    <p>
                      登录后统一使用实时目录，模型按聊天、生图和生视频能力分开呈现。
                    </p>
                  </div>
                  <div
                    className={`${styles.providerCards} ${styles.tokendanceProviderCards}`}
                  >
                    <ProviderCard
                      provider={tokendanceProvider}
                      activeModels={activeModels}
                      onSaved={refreshProvidersSilently}
                      featured
                      featuredDescription="由观猹账号统一授权；目录保持实时更新，具体能力由模型自身决定。"
                      settingsOpen={tokendanceSettingsOpen}
                      onSettingsOpenChange={setTokendanceSettingsOpen}
                    />
                  </div>
                  <TokenDanceAccountPanel
                    provider={tokendanceProvider}
                    connected={isTokenDanceCredentialConnected(
                      tokendanceProvider,
                    )}
                    onSaved={refreshProvidersSilently}
                  />
                </section>
              )}

              {localProviders.length > 0 && (
                <div className={styles.providerGroup} ref={localProvidersRef}>
                  <div className={styles.providerGroupHeading}>
                    <h3 className={styles.providerGroupTitle}>
                      {t("models.localProvidersTitle")}
                    </h3>
                    <span>{localProviders.length}</span>
                  </div>
                  <div
                    className={`${styles.providerCards} ${styles.localProviderCards}`}
                  >
                    {renderProviderCards(localProviders)}
                  </div>
                </div>
              )}

              {regularProviders.length > 0 && (
                <div className={styles.providerGroup}>
                  <div className={styles.providerGroupHeading}>
                    <h3 className={styles.providerGroupTitle}>
                      {t("models.remoteProvidersTitle")}
                    </h3>
                    <span>{regularProviders.length}</span>
                  </div>
                  <div className={styles.providerCards}>
                    {renderProviderCards(regularProviders)}
                  </div>
                </div>
              )}
            </div>

            <details className={styles.advancedModelSettings}>
              <summary>
                <span>
                  <strong>后台、联网与媒体生成</strong>
                  <small>
                    后台整理默认跟随主模型；联网、生图和生视频按任务单独设置
                  </small>
                </span>
                <span aria-hidden="true">展开</span>
              </summary>
              <div className={styles.advancedModelSettingsBody}>
                <UtilityIntelligenceSection providers={providers} />
                <WebSearchSection />
                <ImageGenerationSection providers={providers} />
                <VideoGenerationSection providers={providers} />
              </div>
            </details>

            <CustomProviderModal
              open={addProviderOpen}
              onClose={() => setAddProviderOpen(false)}
              onSaved={fetchAll}
            />
            <OpenAICompatibleProviderModal
              open={openAICompatibleOpen}
              onClose={() => setOpenAICompatibleOpen(false)}
              onSaved={async (providerId) => {
                setSetupProviderId(providerId);
                setVerificationError(null);
                await fetchAll(false);
              }}
            />
          </div>
        </>
      )}
    </div>
  );
}

export default ModelsPage;

import {
  ApiOutlined,
  CheckOutlined,
  CloudOutlined,
  LaptopOutlined,
  RightOutlined,
} from "@ant-design/icons";
import { Button } from "@/vendor/lantaiDesign";
import type { ModelSetupMode } from "@/utils/modelSetupFlow";
import type { ReactNode } from "react";
import styles from "../../index.module.less";
import type { ModelSetupReadiness } from "../../modelSetupReadiness";

const MODE_COPY: Record<
  ModelSetupMode,
  { icon: ReactNode; title: string; description: string }
> = {
  watcha: {
    icon: <CloudOutlined />,
    title: "使用观猹账号",
    description:
      "观猹负责登录，TokenDance 负责模型消费。两次授权相互独立，账号、额度和充值都会在澜台里呈现。",
  },
  own: {
    icon: <ApiOutlined />,
    title: "使用自有 API",
    description: "连接 OpenAI 兼容地址与密钥，通过连接测试后再选择主模型。",
  },
  local: {
    icon: <LaptopOutlined />,
    title: "使用本地模型",
    description:
      "确保 Ollama 或 LM Studio 正在运行，然后刷新模型目录并选择主模型。",
  },
};

interface ModelSetupGuideProps {
  mode: ModelSetupMode;
  readiness: ModelSetupReadiness;
  verifying: boolean;
  verificationError: string | null;
  onConnect: () => void;
  onSelectModel: () => void;
  onVerifyModel: () => void;
  onFinish: () => void;
  onDismiss: () => void;
}

export function ModelSetupGuide({
  mode,
  readiness,
  verifying,
  verificationError,
  onConnect,
  onSelectModel,
  onVerifyModel,
  onFinish,
  onDismiss,
}: ModelSetupGuideProps) {
  const copy = MODE_COPY[mode];
  const currentStep = !readiness.providerConfigured
    ? 1
    : !readiness.modelSelected
      ? 2
      : !readiness.modelVerified
        ? 3
        : 4;
  const providerLabel = readiness.provider?.name ?? "当前提供方";
  const modelLabel = readiness.modelId ?? "当前主模型";

  return (
    <section
      className={styles.modelSetupGuide}
      aria-labelledby="model-setup-title"
    >
      <div className={styles.modelSetupHeader}>
        <span className={styles.modelSetupIcon} aria-hidden="true">
          {copy.icon}
        </span>
        <div>
          <small>快速接入</small>
          <h2 id="model-setup-title">{copy.title}</h2>
          <p>{copy.description}</p>
        </div>
      </div>

      <ol className={styles.modelSetupSteps} aria-label="模型接入进度">
        {["配置提供方", "选择主模型", "验证当前模型", "确认一个小任务"].map(
          (label, index) => {
            const step = index + 1;
            const complete = step < currentStep;
            return (
              <li
                key={label}
                data-current={step === currentStep ? "true" : "false"}
                data-complete={complete ? "true" : "false"}
              >
                <span>{complete ? <CheckOutlined /> : step}</span>
                <strong>{label}</strong>
              </li>
            );
          },
        )}
      </ol>

      <div
        className={styles.modelSetupStatus}
        data-status={readiness.status}
        role={verificationError ? "alert" : "status"}
      >
        {verificationError ? (
          <p>{verificationError}</p>
        ) : readiness.status === "verified" ? (
          <p>
            已验证 {providerLabel} / {modelLabel}；验证结果绑定当前配置版本。
          </p>
        ) : readiness.status === "verification_stale" ? (
          <p>配置或主模型已变化，旧验证不能证明当前版本可用。</p>
        ) : readiness.status === "configured_unverified" ? (
          <p>
            配置已保存，但 {providerLabel} / {modelLabel}
            尚未通过当前版本的实际调用验证。
          </p>
        ) : readiness.status === "model_not_selected" ? (
          <p>提供方配置已保存；选择主模型后再做实际调用验证。</p>
        ) : (
          <p>填写并保存提供方配置后，再选择和验证主模型。</p>
        )}
      </div>

      <div className={styles.modelSetupActions}>
        <Button onClick={onDismiss}>退出引导</Button>
        {!readiness.providerConfigured ? (
          <Button type="primary" onClick={onConnect} icon={<RightOutlined />}>
            {mode === "watcha"
              ? "连接 TokenDance"
              : mode === "own"
                ? "填写 API 信息"
                : "查看本地模型"}
          </Button>
        ) : !readiness.modelSelected ? (
          <Button
            type="primary"
            onClick={onSelectModel}
            icon={<RightOutlined />}
          >
            选择主模型
          </Button>
        ) : !readiness.modelVerified ? (
          <Button
            type="primary"
            loading={verifying}
            onClick={onVerifyModel}
            icon={<RightOutlined />}
          >
            {readiness.status === "verification_stale"
              ? "重新验证当前模型"
              : "验证当前模型"}
          </Button>
        ) : (
          <Button type="primary" onClick={onFinish} icon={<CheckOutlined />}>
            带一个小任务回到对话
          </Button>
        )}
      </div>
    </section>
  );
}

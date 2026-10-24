import { useTranslation } from "react-i18next";
import { CodeOutlined } from "@ant-design/icons";
import { SettingsEmptyState } from "../../components/SettingsEmptyState";

interface EmptyStateProps {
  className?: string;
}

export function EmptyState({ className }: EmptyStateProps) {
  const { t } = useTranslation();

  return (
    <SettingsEmptyState
      className={className}
      icon={<CodeOutlined />}
      title={t("environments.noVariables")}
      description={t(
        "environments.noVariablesHint",
        "没有变量也是正常状态。需要代理、数据库连接或自定义工具令牌时再添加，敏感值会以掩码显示。",
      )}
    />
  );
}

import { useTranslation } from "react-i18next";
import { SettingsEmptyState } from "../../components/SettingsEmptyState";

interface EmptyStateProps {
  message: string;
  className?: string;
}

export function EmptyState({ message, className }: EmptyStateProps) {
  const { t } = useTranslation();

  return (
    <SettingsEmptyState
      className={className}
      title={message}
      description={t(
        "tokenUsage.noDataHint",
        "调整时间范围后再查看模型调用和 Token 趋势。",
      )}
    />
  );
}

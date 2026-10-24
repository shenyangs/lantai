import { SparkPlusLine } from "@/vendor/lantaiIcons";
import { useTranslation } from "react-i18next";
import styles from "../index.module.less";

interface AddButtonProps {
  onClick: () => void;
  className?: string;
}

export function AddButton({ onClick, className }: AddButtonProps) {
  const { t } = useTranslation();

  return (
    <div className={`${styles.addBar} ${className || ""}`}>
      <button
        type="button"
        className={styles.addBtn}
        onClick={onClick}
        title={t("environments.addVariable")}
      >
        <SparkPlusLine />
        <span>{t("environments.addVariable")}</span>
      </button>
    </div>
  );
}

import { LeftOutlined } from "@ant-design/icons";
import { useNavigate } from "react-router-dom";
import styles from "./index.module.less";

interface DetailBackButtonProps {
  to: string;
  label: string;
  className?: string;
}

export function DetailBackButton({
  to,
  label,
  className,
}: DetailBackButtonProps) {
  const navigate = useNavigate();

  return (
    <button
      type="button"
      className={`${styles.button} ${className ?? ""}`.trim()}
      onClick={() => navigate(to)}
      aria-label={label}
      title={label}
    >
      <LeftOutlined aria-hidden="true" />
    </button>
  );
}

export default DetailBackButton;

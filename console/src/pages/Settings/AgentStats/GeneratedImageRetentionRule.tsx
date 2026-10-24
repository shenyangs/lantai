import styles from "./index.module.less";

interface GeneratedImageRetentionRuleProps {
  days: number;
  disabled: boolean;
  onChange: (days: number) => void;
}

export function GeneratedImageRetentionRule({
  days,
  disabled,
  onChange,
}: GeneratedImageRetentionRuleProps) {
  return (
    <label className={styles.retentionRule}>
      <span>AI 生成图片临时文件</span>
      <select
        aria-label="AI 生成图片临时文件"
        value={days}
        disabled={disabled}
        onChange={(event) => onChange(Number(event.target.value))}
      >
        <option value={3}>保留 3 天</option>
        <option value={7}>保留 7 天</option>
        <option value={15}>保留 15 天</option>
        <option value={30}>保留 1 个月</option>
        <option value={0}>永久保留</option>
      </select>
      <small>不会清理你已下载到“下载”文件夹的副本。</small>
    </label>
  );
}

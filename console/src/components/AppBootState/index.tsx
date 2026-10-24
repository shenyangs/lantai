import styles from "./index.module.less";

export function AppBootState() {
  return (
    <section
      className={styles.state}
      role="status"
      aria-live="polite"
      aria-busy="true"
    >
      <strong className={styles.wordmark}>澜台</strong>
      <span className={styles.progress} aria-hidden="true">
        <span className={styles.glint} />
      </span>
      <span className={styles.description}>正在准备工作台</span>
    </section>
  );
}

export default AppBootState;

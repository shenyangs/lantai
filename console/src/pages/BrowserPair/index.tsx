import { useCallback, useMemo, useState } from "react";
import type { FormEvent } from "react";
import { useSearchParams } from "react-router-dom";
import { browserPairingApi } from "../../api/modules/browserPairing";
import styles from "./index.module.less";

function normalizeCode(value: string): string {
  const raw = value.toUpperCase().replace(/[^A-Z0-9]/g, "");
  if (raw.length <= 4) return raw;
  return `${raw.slice(0, 4)}-${raw.slice(4, 8)}`;
}

export function browserPairingErrorMessage(error: unknown): string {
  const message = error instanceof Error ? error.message : String(error || "");
  if (/expired|过期/i.test(message)) {
    return "配对码已过期。已输入的代码会保留，请在客户端生成新码后替换。";
  }
  if (/invalid|not found|无效|不存在/i.test(message)) {
    return "配对码不正确。请核对 8 位代码，或在客户端重新生成。";
  }
  if (/network|fetch|连接|offline/i.test(message)) {
    return "暂时无法连接本机客户端。请确认客户端仍在运行，然后重试。";
  }
  return message || "配对失败，已完成的输入仍然保留。";
}

export default function BrowserPairPage() {
  const [params] = useSearchParams();
  const [code, setCode] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const redirect = useMemo(() => params.get("redirect") || "#/", [params]);

  const handleSubmit = useCallback(
    async (event: FormEvent) => {
      event.preventDefault();
      setError("");
      setBusy(true);
      try {
        await browserPairingApi.completeBrowserPairing(code);
        window.location.hash = redirect.startsWith("#") ? redirect : "#/";
      } catch (err) {
        setError(browserPairingErrorMessage(err));
      } finally {
        setBusy(false);
      }
    },
    [code, redirect],
  );

  return (
    <main className={styles.pairPage}>
      <section className={styles.panel}>
        <div className={styles.header}>
          <h1>连接本机客户端</h1>
          <p>
            在澜台桌面客户端生成一次性代码，即可让这个浏览器使用同一套本机工作空间。
          </p>
        </div>

        <ol className={styles.steps} aria-label="连接步骤">
          <li>
            <span>1</span>
            <div>
              <strong>打开桌面客户端</strong>
              <small>进入“设置 → 浏览器连接”，生成配对码。</small>
            </div>
          </li>
          <li>
            <span>2</span>
            <div>
              <strong>输入 8 位代码</strong>
              <small>代码 5 分钟内有效，只能使用一次。</small>
            </div>
          </li>
          <li>
            <span>3</span>
            <div>
              <strong>继续当前工作</strong>
              <small>连接成功后会自动返回，不需要重新配置模型。</small>
            </div>
          </li>
        </ol>

        <form className={styles.form} onSubmit={handleSubmit}>
          <input
            className={styles.codeInput}
            value={code}
            onChange={(event) => setCode(normalizeCode(event.target.value))}
            placeholder="ABCD-2345"
            aria-label="8 位浏览器配对码"
            autoFocus
            autoComplete="one-time-code"
            inputMode="text"
            maxLength={9}
          />
          <button
            className={styles.primaryButton}
            type="submit"
            disabled={busy || code.replace("-", "").length < 8}
          >
            {busy ? "正在连接…" : "连接此浏览器"}
          </button>
        </form>

        {error && <p className={styles.error}>{error}</p>}
        <p className={styles.hint}>
          连接只授权当前浏览器访问这台电脑上的澜台；清除浏览器数据或在客户端退出后需要重新配对。
        </p>
      </section>
    </main>
  );
}

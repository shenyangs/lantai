import { useEffect, useRef, useState } from "react";
import { Alert, Input, Progress } from "antd";
import { Button } from "@/vendor/lantaiDesign";
import {
  readingApi,
  type ReadingChannel,
  type ReadingLocalStatus,
} from "@/api/modules/reading";
import styles from "./channels.module.less";

export default function LocalReadingService({
  channel,
  onStatus,
  busy,
}: {
  channel: ReadingChannel | null;
  onStatus: (status: ReadingLocalStatus) => void;
  busy: boolean;
}) {
  const [status, setStatus] = useState<ReadingLocalStatus | null>(null);
  const [error, setError] = useState("");
  const [working, setWorking] = useState(false);
  const [values, setValues] = useState<Record<string, string>>({});
  const [configMessage, setConfigMessage] = useState("");
  const mounted = useRef(true);
  const callback = useRef(onStatus);
  callback.current = onStatus;
  const apply = (value: ReadingLocalStatus) => {
    if (!mounted.current) return;
    setStatus(value);
    callback.current(value);
  };
  useEffect(() => {
    mounted.current = true;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const poll = async () => {
      let delay = 10000;
      try {
        const result = await readingApi.localStatus();
        if (!mounted.current) return;
        apply(result);
        delay = result.state === "preparing" ? 2000 : 10000;
      } catch (reason) {
        if (mounted.current)
          setError(
            reason instanceof Error ? reason.message : "无法读取服务状态",
          );
      }
      if (mounted.current) timer = setTimeout(() => void poll(), delay);
    };
    void poll();
    return () => {
      mounted.current = false;
      if (timer) clearTimeout(timer);
    };
  }, []);
  useEffect(() => {
    setValues({});
    setConfigMessage("");
  }, [channel?.id]);
  const action = async (name: "prepare" | "browser" | "start" | "stop") => {
    setWorking(true);
    setError("");
    try {
      apply(await readingApi.localAction(name));
    } catch (reason) {
      if (mounted.current)
        setError(reason instanceof Error ? reason.message : "服务操作失败");
    } finally {
      if (mounted.current) setWorking(false);
    }
  };
  const configs = Array.isArray(channel?.features.requireConfig)
    ? channel.features.requireConfig
    : [];
  const basicGithub = channel?.path.startsWith("/github/trending/");
  const preparing = status?.state === "preparing";
  const save = async (data: Record<string, string>) => {
    setWorking(true);
    setError("");
    try {
      await readingApi.localConfiguration(data);
      if (!mounted.current) return;
      setValues({});
      setConfigMessage("配置已加密保存，下一次读取会使用新配置。");
      apply(await readingApi.localStatus());
    } catch (reason) {
      if (mounted.current)
        setError(reason instanceof Error ? reason.message : "配置保存失败");
    } finally {
      if (mounted.current) setWorking(false);
    }
  };
  return (
    <section className={styles.localService} aria-label="本机阅读服务">
      <div className={styles.localStatus}>
        <div>
          <strong>
            本机阅读服务 ·{" "}
            {preparing
              ? "正在准备"
              : status?.running
                ? "运行中"
                : status?.installed
                  ? "已准备"
                  : "尚未准备"}
          </strong>
          <p>
            {status?.running && status.state !== "preparing"
              ? "本机服务正在运行，文章和收藏保存在本机。"
              : status?.installed && status.state === "idle"
                ? "组件已准备，首次读取会启动本机服务。"
                : status?.message || "正在检查本机组件…"}
          </p>
        </div>
        <div className={styles.actions}>
          {!status?.installed && (
            <Button
              type="primary"
              loading={working || preparing}
              disabled={busy}
              onClick={() => void action("prepare")}
            >
              准备本机服务
            </Button>
          )}
          {status?.installed && !status.running && (
            <Button
              disabled={busy || working || preparing}
              onClick={() => void action("start")}
            >
              启动服务
            </Button>
          )}
          {status?.installed && !status.browser_available && (
            <Button
              disabled={busy || working || preparing}
              onClick={() => void action("browser")}
            >
              准备浏览器组件
            </Button>
          )}
          {(status?.running || preparing) && (
            <Button disabled={working} onClick={() => void action("stop")}>
              {preparing ? "取消准备" : "停止服务"}
            </Button>
          )}
        </div>
      </div>
      {preparing && (
        <Progress percent={status?.progress ?? 0} status="active" />
      )}
      {(error || status?.state === "error") && (
        <Alert type="error" showIcon message={error || status?.message} />
      )}
      {configs.length > 0 && (
        <details key={channel?.id} className={styles.localConfig}>
          <summary>
            当前频道账号配置{basicGithub ? "（基础趋势无需 Token）" : ""}
          </summary>
          <p>配置保存在本机并加密，已保存的值不会回显。留空表示保留原值。</p>
          {configs.map((config) => (
            <label key={config.name}>
              {config.name}（{config.optional || basicGithub ? "可选" : "必填"}
              ）
              <div className={styles.configValue}>
                <Input.Password
                  autoComplete="new-password"
                  disabled={working || busy}
                  value={values[config.name] || ""}
                  onChange={(event) =>
                    setValues({ ...values, [config.name]: event.target.value })
                  }
                  placeholder={
                    status?.configured_keys?.includes(config.name)
                      ? "已配置，留空保留"
                      : "尚未配置"
                  }
                  maxLength={20000}
                />
                {status?.configured_keys?.includes(config.name) && (
                  <Button
                    size="small"
                    disabled={working || busy}
                    onClick={() => void save({ [config.name]: "" })}
                  >
                    清除配置
                  </Button>
                )}
              </div>
            </label>
          ))}
          <Button
            disabled={working || busy || !Object.values(values).some(Boolean)}
            onClick={() =>
              void save(
                Object.fromEntries(
                  Object.entries(values).filter(([, value]) => value !== ""),
                ),
              )
            }
          >
            保存频道配置
          </Button>
          {configMessage && <p role="status">{configMessage}</p>}
        </details>
      )}
    </section>
  );
}

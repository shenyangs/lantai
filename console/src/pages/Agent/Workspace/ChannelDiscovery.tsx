import { useEffect, useRef, useState } from "react";
import { Alert, Checkbox, Empty, Input, Select, Spin, Radio } from "antd";
import { Button } from "@/vendor/lantaiDesign";
import {
  readingApi,
  type ReadingChannel,
  type ChannelCatalog,
  type RSSHubSettings,
  type ReadingLocalStatus,
} from "@/api/modules/reading";
import styles from "./channels.module.less";
import LocalReadingService from "./LocalReadingService";

const categories: Record<string, string> = {
  custom: "我的频道",
  anime: "动漫",
  bbs: "论坛",
  blog: "博客",
  design: "设计",
  finance: "财经",
  forecast: "预报",
  game: "游戏",
  government: "政务",
  journal: "期刊",
  live: "直播",
  multimedia: "多媒体",
  "new-media": "新媒体",
  other: "其他",
  picture: "图片",
  "program-update": "程序更新",
  programming: "编程",
  reading: "阅读",
  shopping: "购物",
  "social-media": "社交媒体",
  sport: "体育",
  study: "学习",
  "traditional-media": "传统媒体",
  travel: "旅行",
  university: "高校",
};

export default function ChannelDiscovery({
  onSubscribed,
}: {
  onSubscribed: () => void;
}) {
  const [catalog, setCatalog] = useState<ChannelCatalog | null>(null);
  const [query, setQuery] = useState("");
  const [search, setSearch] = useState("");
  const [category, setCategory] = useState("");
  const [offset, setOffset] = useState(0);
  const [revision, setRevision] = useState(0);
  const [selected, setSelected] = useState<ReadingChannel | null>(null);
  const [parameters, setParameters] = useState<Record<string, string>>({});
  const [folder, setFolder] = useState("");
  const [override, setOverride] = useState("");
  const [preview, setPreview] = useState("");
  const [settings, setSettings] = useState<RSSHubSettings | null>(null);
  const [localStatus, setLocalStatus] = useState<ReadingLocalStatus | null>(
    null,
  );
  const [serviceMode, setServiceMode] = useState<"local" | "external">("local");
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [instance, setInstance] = useState("");
  const [allowPrivate, setAllowPrivate] = useState(false);
  const [customOpen, setCustomOpen] = useState(false);
  const [customId, setCustomId] = useState<string | undefined>();
  const [customName, setCustomName] = useState("");
  const [customPath, setCustomPath] = useState("");
  const [loading, setLoading] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [status, setStatus] = useState("");
  const catalogRef = useRef<HTMLDivElement>(null);
  const detailRef = useRef<HTMLDivElement>(null);
  const mounted = useRef(true);
  const pending = useRef(false);
  const version = useRef(0);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
      version.current++;
    };
  }, []);
  useEffect(() => {
    const current = ++version.current;
    setLoading(true);
    setError("");
    void readingApi
      .channels({ q: search, category, offset: String(offset) })
      .then((result) => {
        if (mounted.current && current === version.current) setCatalog(result);
      })
      .catch((reason: Error) => {
        if (mounted.current && current === version.current)
          setError(reason.message);
      })
      .finally(() => {
        if (mounted.current && current === version.current) setLoading(false);
      });
  }, [search, category, offset, revision]);
  useEffect(() => {
    void readingApi
      .rsshubSettings()
      .then((result) => {
        if (!mounted.current) return;
        setSettings(result);
        setServiceMode(result.mode || "external");
        setInstance(result.instance_url);
        setAllowPrivate(result.allow_private);
      })
      .catch((reason: Error) => {
        if (mounted.current) setError(reason.message);
      });
  }, []);
  const run = async (operation: () => Promise<void>) => {
    if (pending.current) return;
    pending.current = true;
    setBusy(true);
    setError("");
    setStatus("");
    try {
      await operation();
    } catch (reason) {
      if (mounted.current)
        setError(reason instanceof Error ? reason.message : "操作失败，请重试");
    } finally {
      pending.current = false;
      if (mounted.current) setBusy(false);
    }
  };
  const choose = (channel: ReadingChannel) => {
    setSelected(channel);
    setOverride("");
    setPreview("");
    setStatus("");
    setParameters(
      Object.fromEntries(
        channel.fields.map((field) => [field.name, field.default]),
      ),
    );
  };
  useEffect(() => {
    if (detailRef.current) detailRef.current.scrollTop = 0;
    if (selected && window.matchMedia?.("(max-width: 800px)")?.matches)
      detailRef.current?.scrollIntoView?.({ block: "start" });
  }, [selected]);
  const selection = { parameters, folder, path_override: override };
  return (
    <div className={styles.discovery}>
      <div className={styles.heading}>
        <div>
          <h2>频道目录</h2>
          <p>
            预置 RSSHub
            社区频道，支持离线查找。填写参数后，由选定的服务实际读取并验证。
          </p>
        </div>
        <div className={styles.actions}>
          <Button
            onClick={() => {
              setCustomId(undefined);
              setCustomName("");
              setCustomPath("");
              setCustomOpen(true);
            }}
          >
            自定义频道
          </Button>
          <Button
            onClick={() => {
              setSettingsOpen(!settingsOpen);
              if (settings) {
                setServiceMode(settings.mode || "external");
                setInstance(settings.instance_url);
                setAllowPrivate(settings.allow_private);
              }
            }}
          >
            服务设置
          </Button>
        </div>
      </div>
      {error && <Alert showIcon type="error" message={error} />}
      {status && <Alert showIcon type="success" message={status} />}
      {settingsOpen && (
        <form
          className={styles.form}
          onSubmit={(event) => {
            event.preventDefault();
            void run(async () => {
              const result = await readingApi.saveRSSHubSettings({
                mode: serviceMode,
                instance_url: instance,
                allow_private: allowPrivate,
              });
              if (!mounted.current) return;
              setSettings(result);
              setInstance(result.instance_url);
              setSettingsOpen(false);
              setPreview("");
              setStatus("实例设置已保存，将用于之后添加的订阅。");
            });
          }}
        >
          <Radio.Group
            aria-label="频道服务位置"
            value={serviceMode}
            onChange={(event) => setServiceMode(event.target.value)}
            options={[
              { value: "local", label: "澜台管理本机服务" },
              { value: "external", label: "连接自建实例" },
            ]}
          />
          {serviceMode === "external" && (
            <>
              <label>
                RSSHub 实例地址
                <Input
                  value={instance}
                  onChange={(event) => setInstance(event.target.value)}
                  placeholder="https://rsshub.app"
                  required
                  maxLength={2048}
                />
              </label>
              <Checkbox
                checked={allowPrivate}
                onChange={(event) => setAllowPrivate(event.target.checked)}
              >
                允许访问此实例所在的本机或局域网
              </Checkbox>
              <p>
                自建实例可填写
                http://localhost:1200。此许可仅用于该实例的订阅抓取，普通网页读取不受影响；旧订阅保留原实例和网络许可，可在订阅源中暂停或取消。
              </p>
            </>
          )}
          <div className={styles.actions}>
            <Button htmlType="submit" type="primary" loading={busy}>
              保存实例
            </Button>
            <Button disabled={busy} onClick={() => setSettingsOpen(false)}>
              取消
            </Button>
          </div>
        </form>
      )}
      {settings?.mode === "local" && (
        <LocalReadingService
          channel={selected}
          onStatus={setLocalStatus}
          busy={busy}
        />
      )}
      {customOpen && (
        <form
          className={styles.form}
          onSubmit={(event) => {
            event.preventDefault();
            void run(async () => {
              const channel = await readingApi.saveChannel({
                id: customId,
                name: customName,
                path: customPath,
              });
              if (!mounted.current) return;
              choose(channel);
              setCustomOpen(false);
              setRevision((value) => value + 1);
              setStatus("自定义频道已保存，可填写参数后订阅。");
            });
          }}
        >
          <label>
            频道名称
            <Input
              value={customName}
              onChange={(event) => setCustomName(event.target.value)}
              required
              maxLength={150}
            />
          </label>
          <label>
            路由或路由模板
            <Input
              value={customPath}
              onChange={(event) => setCustomPath(event.target.value)}
              placeholder="/github/issue/:user/:repo"
              required
              maxLength={2048}
            />
          </label>
          <p>
            用 :名称 表示必填参数，:名称? 表示可选参数；也可以直接保存完整路由。
          </p>
          <div className={styles.actions}>
            <Button htmlType="submit" type="primary" loading={busy}>
              保存频道
            </Button>
            <Button disabled={busy} onClick={() => setCustomOpen(false)}>
              取消
            </Button>
          </div>
        </form>
      )}
      <div className={styles.browser}>
        <div className={styles.catalog} ref={catalogRef}>
          <Input.Search
            aria-label="搜索社区频道"
            placeholder="搜索平台、频道或路由"
            value={query}
            onChange={(event) => {
              setQuery(event.target.value);
              if (!event.target.value) {
                setSearch("");
                setOffset(0);
              }
            }}
            onSearch={(value) => {
              setSearch(value);
              setOffset(0);
            }}
            allowClear
            maxLength={200}
          />
          <Select
            aria-label="频道分类"
            value={category}
            onChange={(value) => {
              setCategory(value);
              setOffset(0);
            }}
            options={[
              { value: "", label: "全部分类" },
              ...(catalog?.categories ?? []).map((value) => ({
                value,
                label: categories[value] || value,
              })),
            ]}
          />
          <p className={styles.meta}>
            {catalog
              ? `找到 ${catalog.total} 条 · 预置 ${catalog.snapshot.route_count} 条社区路由`
              : "正在读取目录…"}
          </p>
          {loading ? (
            <Spin />
          ) : !catalog?.items.length ? (
            <Empty description="没有找到频道，可换个关键词或添加自定义频道" />
          ) : (
            <div className={styles.routeList}>
              {catalog.items.map((channel) => (
                <button
                  disabled={busy}
                  key={channel.id}
                  aria-current={
                    selected?.id === channel.id ? "true" : undefined
                  }
                  onClick={() => choose(channel)}
                >
                  <strong>
                    {channel.site} · {channel.name}
                  </strong>
                  <code>{channel.path}</code>
                  <span>
                    {channel.categories
                      .map((value) => categories[value] || value)
                      .join(" · ")}
                    {channel.features.requireConfig ? " · 含实例配置项" : ""}
                  </span>
                </button>
              ))}
            </div>
          )}
          <div className={styles.pagination}>
            <Button
              size="small"
              disabled={loading || !offset}
              onClick={() => setOffset(offset - 50)}
            >
              上一页
            </Button>
            <span>
              {Math.floor(offset / 50) + 1} /{" "}
              {Math.max(1, Math.ceil((catalog?.total ?? 0) / 50))}
            </span>
            <Button
              size="small"
              disabled={loading || offset + 50 >= (catalog?.total ?? 0)}
              onClick={() => setOffset(offset + 50)}
            >
              下一页
            </Button>
          </div>
          {catalog && (
            <small>
              目录快照：{catalog.snapshot.generated_at.slice(0, 10)} ·{" "}
              {catalog.snapshot.source_revision.slice(0, 8)}
            </small>
          )}
        </div>
        <div className={styles.detail} ref={detailRef}>
          {selected ? (
            <>
              <Button
                className={styles.backToCatalog}
                type="text"
                size="small"
                onClick={() => {
                  setSelected(null);
                  window.setTimeout(
                    () =>
                      catalogRef.current?.scrollIntoView?.({ block: "start" }),
                    0,
                  );
                }}
              >
                返回频道目录
              </Button>
              <h3>
                {selected.site} · {selected.name}
              </h3>
              <code>{selected.path}</code>
              {selected.docs && (
                <a
                  href={selected.docs}
                  target="_blank"
                  rel="noopener noreferrer"
                >
                  查看官方路由说明
                </a>
              )}
              {selected.description && (
                <details>
                  <summary>频道说明</summary>
                  <p className={styles.description}>{selected.description}</p>
                </details>
              )}
              {(selected.features.requireConfig ||
                selected.features.requirePuppeteer ||
                selected.features.antiCrawler) && (
                <Alert
                  type="info"
                  showIcon
                  message="此路由有运行条件"
                  description={[
                    selected.features.requireConfig
                      ? settings?.mode === "local"
                        ? selected.path.startsWith("/github/trending/")
                          ? "基础趋势无需 Token；账号配置可用于增强读取"
                          : Array.isArray(selected.features.requireConfig) &&
                              selected.features.requireConfig.every(
                                (config) => config.optional,
                              )
                            ? "此频道的账号配置为可选"
                            : "请在上方展开“当前频道账号配置”填写必需信息"
                        : "可能需要在 RSSHub 实例配置账号或密钥"
                      : "",
                    selected.features.requirePuppeteer
                      ? settings?.mode === "local"
                        ? localStatus?.browser_available
                          ? "浏览器组件已就绪"
                          : "请先准备浏览器组件"
                        : "实例需要浏览器环境"
                      : "",
                    selected.features.antiCrawler ? "上游有反爬限制" : "",
                  ]
                    .filter(Boolean)
                    .join("；")}
                />
              )}
              <form
                className={styles.parameterForm}
                onSubmit={(event) => {
                  event.preventDefault();
                  void run(async () => {
                    const subscribed = await readingApi.subscribeChannel(
                      selected.id,
                      selection,
                    );
                    if (!mounted.current) return;
                    setStatus(
                      `已订阅「${subscribed.title}」${typeof subscribed.entry_count === "number" ? `，本次读取 ${subscribed.entry_count} 条` : ""}。文章已保存在本机。`,
                    );
                    onSubscribed();
                  });
                }}
              >
                {selected.fields.map((field) => (
                  <label key={field.name}>
                    {field.name}（{field.optional ? "可选" : "必填"}）
                    {field.options?.length ? (
                      <Select
                        aria-label={field.name}
                        allowClear
                        value={parameters[field.name] || undefined}
                        options={field.options.map((option) => ({
                          label: option.label,
                          value: String(option.value),
                        }))}
                        onChange={(value) => {
                          setParameters({
                            ...parameters,
                            [field.name]: value || "",
                          });
                          setPreview("");
                        }}
                      />
                    ) : (
                      <Input
                        value={parameters[field.name] || ""}
                        required={!override && !field.optional}
                        maxLength={1000}
                        onChange={(event) => {
                          setParameters({
                            ...parameters,
                            [field.name]: event.target.value,
                          });
                          setPreview("");
                        }}
                      />
                    )}
                    {field.description && <small>{field.description}</small>}
                  </label>
                ))}
                <label>
                  保存到订阅分组
                  <Input
                    value={folder}
                    onChange={(event) => setFolder(event.target.value)}
                    placeholder="例如：科技"
                    maxLength={80}
                  />
                </label>
                <details>
                  <summary>编辑完整路由 / 使用示例</summary>
                  <label>
                    完整路由（填写后优先使用）
                    <Input
                      value={override}
                      onChange={(event) => {
                        setOverride(event.target.value);
                        setPreview("");
                      }}
                      maxLength={2048}
                      placeholder="/weibo/search/hot"
                    />
                  </label>
                  {(Array.isArray(selected.example)
                    ? selected.example
                    : [selected.example]
                  )
                    .filter(Boolean)
                    .map((example) => (
                      <Button
                        key={example}
                        size="small"
                        onClick={() => {
                          setOverride(example);
                          setPreview("");
                        }}
                      >
                        {example}
                      </Button>
                    ))}
                </details>
                <p className={styles.meta}>
                  当前服务：
                  {settings?.mode === "local"
                    ? "澜台管理的本机 RSSHub"
                    : settings?.instance_url || "尚未读取设置"}
                </p>
                {preview && (
                  <output className={styles.preview}>{preview}</output>
                )}
                <div className={styles.actions}>
                  <Button
                    disabled={busy || !settings}
                    onClick={() =>
                      void run(async () => {
                        const result = await readingApi.previewChannel(
                          selected.id,
                          selection,
                        );
                        if (mounted.current) setPreview(result.url);
                      })
                    }
                  >
                    预览订阅地址
                  </Button>
                  <Button
                    type="primary"
                    htmlType="submit"
                    aria-label="验证并订阅"
                    aria-busy={busy}
                    loading={busy}
                    disabled={
                      !settings ||
                      (settings.mode === "local" &&
                        (!localStatus?.installed ||
                          localStatus.state === "preparing"))
                    }
                  >
                    验证并订阅
                  </Button>
                  <Button
                    disabled={busy}
                    onClick={() => {
                      setCustomId(
                        selected.id.startsWith("custom-")
                          ? selected.id
                          : undefined,
                      );
                      setCustomName(selected.name);
                      setCustomPath(override || selected.path);
                      setCustomOpen(true);
                    }}
                  >
                    {selected.id.startsWith("custom-")
                      ? "编辑频道"
                      : "另存为自定义"}
                  </Button>
                </div>
              </form>
            </>
          ) : (
            <Empty description="选择一个频道，查看参数和订阅条件" />
          )}
        </div>
      </div>
    </div>
  );
}

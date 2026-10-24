import {
  useCallback,
  useEffect,
  useState,
  type MouseEvent as ReactMouseEvent,
} from "react";
import { GithubOutlined } from "@ant-design/icons";
import { ExternalLink } from "lucide-react";
import { PageHeader } from "../../../components/PageHeader";
import { rootApi } from "../../../api/modules/root";
import styles from "./index.module.less";

const DEVELOPER_GITHUB_URL = "https://github.com/shenyangs";

interface ReleaseBuildIdentity {
  version: string;
  gitSha: string;
  shortGitSha: string;
  sourceBranch: string;
  buildTime: string;
  canonicalMain: boolean;
}

export default function AboutPage() {
  const [currentVersion, setCurrentVersion] = useState("");
  const [buildIdentity, setBuildIdentity] =
    useState<ReleaseBuildIdentity | null>(null);
  const [externalLinkError, setExternalLinkError] = useState("");

  useEffect(() => {
    let active = true;
    void (async () => {
      try {
        const desktopVersion = await window.electronAPI?.getAppVersion?.();
        if (desktopVersion?.ok && desktopVersion.version) {
          if (active) setCurrentVersion(desktopVersion.version);
          return;
        }
      } catch {
        // Browser previews and older desktop shells use the API fallback.
      }

      try {
        const result = await rootApi.getVersion();
        if (active && result.version) setCurrentVersion(result.version);
      } catch {
        // Version is supporting information; the page remains usable without it.
      }
    })();
    return () => {
      active = false;
    };
  }, []);

  useEffect(() => {
    let active = true;
    void fetch("./release-provenance.json", { cache: "no-store" })
      .then((response) => {
        if (!response.ok) throw new Error("build identity unavailable");
        return response.json() as Promise<ReleaseBuildIdentity>;
      })
      .then((identity) => {
        if (!active) return;
        setBuildIdentity(identity);
        setCurrentVersion((version) => version || identity.version || "");
      })
      .catch(() => {
        // Development previews do not have packaged release provenance.
      });
    return () => {
      active = false;
    };
  }, []);

  const handleGithubClick = useCallback(
    async (event: ReactMouseEvent<HTMLAnchorElement>) => {
      setExternalLinkError("");
      if (window.electronAPI?.openExternal) {
        event.preventDefault();
        const result =
          await window.electronAPI.openExternal(DEVELOPER_GITHUB_URL);
        if (!result.success) {
          setExternalLinkError(result.error || "无法打开 GitHub");
        }
        return;
      }
      if (window.pywebview?.api?.open_external_link) {
        event.preventDefault();
        try {
          await window.pywebview.api.open_external_link(DEVELOPER_GITHUB_URL);
        } catch (error) {
          setExternalLinkError(
            error instanceof Error ? error.message : "无法打开 GitHub",
          );
        }
      }
    },
    [],
  );

  return (
    <main className={styles.aboutPage}>
      <PageHeader parent="设置" current="关于澜台" />

      <div className={styles.aboutContent}>
        <header className={styles.brandIdentity}>
          <img className={styles.brandMark} src="./lantai-mark.png" alt="" />
          <div className={styles.brandCopy}>
            <h2>澜台</h2>
            <p>洞察世界，澜见人心。</p>
            <span className={styles.versionText}>
              {currentVersion ? `版本 ${currentVersion}` : "正在读取版本"}
            </span>
          </div>
        </header>

        {buildIdentity ? (
          <section
            className={styles.buildIdentity}
            aria-labelledby="build-identity-title"
          >
            <div>
              <h2 id="build-identity-title">构建身份</h2>
              <p>用于确认当前运行的 App 是否来自最新主线。</p>
            </div>
            <dl>
              <div>
                <dt>Git SHA</dt>
                <dd title={buildIdentity.gitSha}>
                  {buildIdentity.shortGitSha}
                </dd>
              </div>
              <div>
                <dt>来源分支</dt>
                <dd>{buildIdentity.sourceBranch}</dd>
              </div>
              <div>
                <dt>构建时间</dt>
                <dd>
                  {new Date(buildIdentity.buildTime).toLocaleString("zh-CN", {
                    hour12: false,
                  })}
                </dd>
              </div>
              <div>
                <dt>发布主线</dt>
                <dd>
                  {buildIdentity.canonicalMain ? "已验证 main" : "隔离候选"}
                </dd>
              </div>
            </dl>
          </section>
        ) : null}

        <section
          className={styles.developerSection}
          aria-labelledby="developer-title"
        >
          <div className={styles.developerProfile}>
            <img
              className={styles.developerAvatar}
              src="./developer-yanggeer.jpg"
              alt="旸哥儿的头像"
            />
            <div className={styles.developerDetails}>
              <h2 id="developer-title">旸哥儿</h2>
              <p>在设计、传播与 AI 之间持续造东西。</p>
              <a
                href={DEVELOPER_GITHUB_URL}
                target="_blank"
                rel="noreferrer"
                onClick={handleGithubClick}
              >
                <GithubOutlined aria-hidden="true" />
                <span>GitHub</span>
                <ExternalLink size={13} aria-hidden="true" />
              </a>
              {externalLinkError ? (
                <span className={styles.externalLinkError} role="alert">
                  {externalLinkError}
                </span>
              ) : null}
            </div>
          </div>

          <figure className={styles.wechatContact}>
            <img
              className={styles.wechatQr}
              src="./wechat-yanggeer-qr.png"
              alt="旸哥儿的微信二维码"
            />
            <figcaption>
              <strong>微信联系</strong>
              <span>扫码添加，请注明来意</span>
            </figcaption>
          </figure>
        </section>
      </div>
    </main>
  );
}

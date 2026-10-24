import { useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import {
  CheckCircleFilled,
  CloseCircleFilled,
  FileExcelOutlined,
  FileMarkdownOutlined,
  FilePdfOutlined,
  MoreOutlined,
  PlusCircleOutlined,
  ReloadOutlined,
  SearchOutlined,
  SettingOutlined,
} from "@ant-design/icons";
import { SparkToolLine } from "@/vendor/lantaiIcons";
import { providerApi } from "../api/modules/provider";
import { useAgentStore } from "../stores/agentStore";
import { stripProductHiddenActiveModel } from "../utils/providerVisibility";
import styles from "./index.module.less";

interface WorkspaceContextPanelProps {
  selectedKey: string;
}

interface ActiveModelState {
  providerId?: string;
  model?: string;
  loading: boolean;
}

const memoryItems = [
  { text: "用户偏好使用精简直接的回答风格", time: "昨天 18:30" },
  { text: "用户关注品牌传播与内容策略", time: "05/22" },
  { text: "用户位于北京，工作日 9:00-18:00 活跃", time: "05/20" },
];

const fileItems = [
  {
    name: "传播策略框架.md",
    meta: "1.2 MB · 05/23",
    icon: <FileMarkdownOutlined />,
    tone: "gold",
  },
  {
    name: "项目进度表.xlsx",
    meta: "45 KB · 05/22",
    icon: <FileExcelOutlined />,
    tone: "green",
  },
  {
    name: "热点案例库.pdf",
    meta: "2.4 MB · 05/20",
    icon: <FilePdfOutlined />,
    tone: "red",
  },
];

const toolCalls = [
  { name: "execute_shell_command", status: "成功", time: "10:21 · 1.2s" },
  { name: "execute_shell_command", status: "失败", time: "10:22 · 2.8s" },
];

export default function WorkspaceContextPanel({
  selectedKey,
}: WorkspaceContextPanelProps) {
  const navigate = useNavigate();
  const { selectedAgent, agents } = useAgentStore();
  const [activeModel, setActiveModel] = useState<ActiveModelState>({
    loading: true,
  });

  const activeAgent = useMemo(
    () => agents.find((agent) => agent.id === selectedAgent),
    [agents, selectedAgent],
  );

  const modelLabel = activeModel.model
    ? activeModel.model.replace(/^moonshot[-/]/i, "")
    : activeModel.loading
      ? "读取中"
      : "未配置模型";

  useEffect(() => {
    let cancelled = false;
    setActiveModel({ loading: true });

    providerApi
      .getActiveModels({ scope: "effective", agent_id: selectedAgent })
      .then((models) => {
        if (cancelled) return;
        const visibleModels = stripProductHiddenActiveModel(models);
        setActiveModel({
          providerId: visibleModels?.active_llm?.provider_id,
          model: visibleModels?.active_llm?.model,
          loading: false,
        });
      })
      .catch(() => {
        if (!cancelled) setActiveModel({ loading: false });
      });

    return () => {
      cancelled = true;
    };
  }, [selectedAgent]);

  return (
    <aside className={styles.contextPanel} aria-label="上下文">
      <div className={styles.contextTabs}>
        <button type="button" className={styles.contextTabActive}>
          上下文
        </button>
        <button type="button">任务</button>
      </div>

      <section className={styles.contextSection}>
        <div className={styles.contextSectionLabel}>智能体</div>
        <div className={styles.agentContextCard}>
          <img src="./lantai.png" alt="" className={styles.agentContextAvatar} />
          <div className={styles.agentContextCopy}>
            <div>
              <strong>{activeAgent?.name || "沈行知"}</strong>
              <button type="button">编辑</button>
            </div>
            <p>你的长期执行搭档与策略伙伴，擅长复杂问题拆解、信息核实与方案推进。</p>
          </div>
        </div>
      </section>

      <section className={styles.contextSection}>
        <div className={styles.contextSectionLabel}>模型</div>
        <button
          type="button"
          className={styles.modelContextCard}
          onClick={() => navigate("/models")}
        >
          <span className={styles.kimiMark}>M</span>
          <strong>{modelLabel || "未配置模型"}</strong>
          <em>{activeModel.providerId ? "MoE | 128K" : "MoE | 128K"}</em>
          <SettingOutlined className={styles.contextCardIconRight} />
        </button>
      </section>

      <section className={styles.contextSection}>
        <div className={styles.contextSectionLabel}>记忆</div>
        <div className={styles.memoryCard}>
          <div className={styles.memorySearch}>
            <SearchOutlined />
            <span>搜索记忆...</span>
          </div>
          {memoryItems.map((item) => (
            <div key={item.text} className={styles.memoryRow}>
              <PlusCircleOutlined />
              <span>{item.text}</span>
              <time>{item.time}</time>
            </div>
          ))}
          <button type="button" className={styles.contextMoreButton}>
            查看全部 (12)
          </button>
        </div>
      </section>

      <section className={styles.contextSection}>
        <div className={styles.contextSectionLabel}>文件</div>
        <div className={styles.fileCard}>
          {fileItems.map((item) => (
            <div key={item.name} className={styles.fileRow}>
              <span className={`${styles.fileIcon} ${styles[item.tone]}`}>
                {item.icon}
              </span>
              <span>{item.name}</span>
              <time>{item.meta}</time>
            </div>
          ))}
          <button type="button" className={styles.contextMoreButton}>
            查看全部 (8)
          </button>
        </div>
      </section>

      <section className={styles.contextSection}>
        <div className={styles.contextSectionLabel}>工具调用</div>
        <div className={styles.toolCard}>
          {toolCalls.map((call) => {
            const success = call.status === "成功";
            return (
              <div key={`${call.name}-${call.status}`} className={styles.toolRow}>
                <SparkToolLine size={15} />
                <strong>{call.name}</strong>
                {success ? (
                  <CheckCircleFilled className={styles.toolSuccess} />
                ) : (
                  <CloseCircleFilled className={styles.toolFailed} />
                )}
                <span className={success ? styles.toolSuccess : styles.toolFailed}>
                  {call.status}
                </span>
                <time>{call.time}</time>
              </div>
            );
          })}
          <button type="button" className={styles.contextMoreButton}>
            查看全部 (14)
          </button>
        </div>
      </section>

      <section className={styles.contextSection}>
        <div className={styles.contextSectionLabel}>任务状态</div>
        <div className={styles.taskCard}>
          <div className={styles.taskTitleRow}>
            <CloseCircleFilled />
            <strong>{selectedKey === "chat" ? "安装 uv 工具链" : "当前页面巡检"}</strong>
            <button type="button">
              <ReloadOutlined />
              重试
            </button>
            <MoreOutlined />
          </div>
          <p>创建于 10:22 · 耗时 2.8s</p>
        </div>
      </section>
    </aside>
  );
}

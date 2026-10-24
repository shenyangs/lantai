import { useNavigate } from "react-router-dom";
import { useAgentStore } from "../../../../stores/agentStore";
import styles from "./SkillLibraryNav.module.less";

export function SkillLibraryNav({ active }: { active: "agent" | "library" }) {
  const navigate = useNavigate();
  const { selectedAgent, agents = [] } = useAgentStore();
  const agentName =
    agents.find((agent) => agent.id === selectedAgent)?.name || selectedAgent;
  return (
    <nav className={styles.nav} aria-label="技能库范围">
      <button
        type="button"
        aria-current={active === "agent" ? "page" : undefined}
        data-active={active === "agent"}
        onClick={() => navigate("/skills")}
      >
        当前智能体 · {agentName}
        <small>这里只改变该智能体以后可用的技能</small>
      </button>
      <button
        type="button"
        aria-current={active === "library" ? "page" : undefined}
        data-active={active === "library"}
        onClick={() => navigate("/skill-pool")}
      >
        全部技能库
        <small>本机安装、更新与分发；不会自动启用</small>
      </button>
    </nav>
  );
}

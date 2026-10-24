import { useEffect } from "react";
import { useAgentStore } from "../../stores/agentStore";
import { agentsApi } from "../../api/modules/agents";
import { useTranslation } from "react-i18next";
import { useAppMessage } from "../../hooks/useAppMessage";

interface AgentSelectorProps {
  collapsed?: boolean;
}

export default function AgentSelector(_props: AgentSelectorProps) {
  const { t } = useTranslation();
  const { message } = useAppMessage();
  const { selectedAgent, agents, setSelectedAgent, setAgents } =
    useAgentStore();

  useEffect(() => {
    loadAgents();
  }, []);

  const loadAgents = async () => {
    try {
      const data = await agentsApi.listAgents();
      const sortedAgents = [...data.agents].sort((a, b) => {
        if (a.enabled === b.enabled) return 0;
        return a.enabled ? -1 : 1;
      });
      setAgents(sortedAgents);
    } catch (error) {
      console.error("Failed to load agents:", error);
      message.error(t("agent.loadFailed"));
    }
  };

  // Auto-switch to default if the selected agent was deleted or disabled
  useEffect(() => {
    if (!agents?.length || selectedAgent === "default") return;

    const currentAgent = agents.find((a) => a.id === selectedAgent);

    if (!currentAgent) {
      setSelectedAgent("default");
      message.warning(t("agent.currentAgentDeleted"));
    } else if (!currentAgent.enabled) {
      setSelectedAgent("default");
      message.warning(t("agent.currentAgentDisabled"));
    }
  }, [agents, selectedAgent, setSelectedAgent, t]);

  // Hidden: user only interacts with the default agent (小澜).
  // Sub-agents are invoked internally by the default agent.
  return null;
}

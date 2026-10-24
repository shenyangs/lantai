export * from "./types";

export { request } from "./request";

export { getApiUrl, getApiToken } from "./config";

import { rootApi } from "./modules/root";
import { channelApi } from "./modules/channel";
import { heartbeatApi } from "./modules/heartbeat";
import { cronJobApi } from "./modules/cronjob";
import { chatApi, sessionApi } from "./modules/chat";
import { envApi } from "./modules/env";
import { providerApi } from "./modules/provider";
import { skillApi } from "./modules/skill";
import { agentApi } from "./modules/agent";
import { agentsApi } from "./modules/agents";
import { workspaceApi } from "./modules/workspace";
import { localModelApi } from "./modules/localModel";
import { mcpApi } from "./modules/mcp";
import { tokenUsageApi } from "./modules/tokenUsage";
import { agentStatsApi } from "./modules/agentStats";
import { toolsApi } from "./modules/tools";
import { securityApi } from "./modules/security";
import { userTimezoneApi } from "./modules/userTimezone";
import { languageApi } from "./modules/language";
import { backupApi } from "./modules/backup";
import { computerPermissionsApi } from "./modules/computerPermissions";
import { browserPairingApi } from "./modules/browserPairing";
import { browserWorkbenchApi } from "./modules/browserSession";
import { settingsApi } from "./modules/settings";
import { capabilityApi } from "./modules/capability";
import { runtimeApi } from "./modules/runtime";
import { reminderApi } from "./modules/reminder";
import { dailyBriefApi } from "./modules/dailyBrief";
import { officeDocumentApi } from "./modules/officeDocument";
import { codingApi } from "./modules/coding";
import { utilityIntelligenceApi } from "./modules/utilityIntelligence";
import { imageGenerationApi } from "./modules/imageGeneration";
import { videoGenerationApi } from "./modules/videoGeneration";
import { webSearchApi } from "./modules/webSearch";
import { timeApi } from "./modules/time";
import { workMethodApi } from "./modules/workMethod";
import { customizationApi } from "./modules/customization";
import { presentationApi } from "./modules/presentation";
import { kdocsApi } from "./modules/kdocs";

export const api = {
  // Root
  ...rootApi,

  // Channels
  ...channelApi,

  // Heartbeat
  ...heartbeatApi,

  // Cron Jobs
  ...cronJobApi,

  // Chats
  ...chatApi,

  // Sessions（Legacy aliases）
  ...sessionApi,

  // Environment Variables
  ...envApi,

  // Providers
  ...providerApi,

  // Agent
  ...agentApi,

  // Skills
  ...skillApi,

  // Workspace
  ...workspaceApi,

  // Local Models
  ...localModelApi,

  // MCP Clients
  ...mcpApi,

  // Token Usage
  ...tokenUsageApi,
  // Agent Statistics
  ...agentStatsApi,
  // Tools
  ...toolsApi,

  // Security
  ...securityApi,

  // User Timezone
  ...userTimezoneApi,

  // Language
  ...languageApi,

  // Backups
  ...backupApi,

  // Computer permissions
  ...computerPermissionsApi,

  // Browser pairing
  ...browserPairingApi,

  // Browser Workbench
  ...browserWorkbenchApi,

  // UI settings
  ...settingsApi,

  // Capabilities
  ...capabilityApi,

  // Runtime
  ...runtimeApi,

  // Reminders
  ...reminderApi,

  // Source-backed daily brief read model
  ...dailyBriefApi,

  // Office documents
  ...officeDocumentApi,

  // Coding workbench
  ...codingApi,

  // Isolated background intelligence
  ...utilityIntelligenceApi,

  // Image generation inside chat
  ...imageGenerationApi,

  // Video generation inside chat
  ...videoGenerationApi,

  // Unified Web search settings with an independent DeepSeek credential
  ...webSearchApi,

  // Today timeline
  ...timeApi,

  // Agent-native work methods
  ...workMethodApi,

  // Versioned and recoverable customization
  ...customizationApi,

  // Independent presentation generation workbench
  ...presentationApi,
  ...kdocsApi,
};

export default api;

// Export individual APIs for direct access
export { agentsApi };
export { chatApi };
export { capabilityApi };
export { runtimeApi };
export { reminderApi };
export { dailyBriefApi };
export { officeDocumentApi };
export { codingApi };
export { browserWorkbenchApi };
export { utilityIntelligenceApi };
export { imageGenerationApi };
export { videoGenerationApi };
export { webSearchApi };
export { timeApi };
export { workMethodApi };
export { customizationApi };
export { presentationApi };
export { kdocsApi };

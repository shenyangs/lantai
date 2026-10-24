const { contextBridge } = require("electron");

const apiBaseUrl = process.env.LANTAI_TASK_CENTER_SMOKE_API_BASE_URL || "";

contextBridge.exposeInMainWorld("electronAPI", {
  isDesktop: true,
  apiBaseUrl,
});
